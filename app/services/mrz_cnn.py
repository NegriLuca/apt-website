"""MRZ OCR using a purpose-trained CNN (mrz-scanner's mrz-cnn.onnx).

The model classifies 20x20 character crops into 37 MRZ symbols (0-9, A-Z, <).
Unlike tesseract, it needs no system binary and no traineddata: it runs on
onnxruntime (pure pip) and is far smaller than any general OCR engine.

Model + symbol table live in app/services/models/. See
https://github.com/alsenet-labs/mrz-scanner (packages/mrz-ocr).
"""

import json
import os
import threading

import cv2
import numpy as np

_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
_MODEL_PATH = os.path.join(_MODELS_DIR, 'mrz-cnn.onnx')
_JSON_PATH = os.path.join(_MODELS_DIR, 'mrz-cnn.json')

_MSYMBOLS: list[str] | None = None
_INPUT_SIZE = 20

_session = None
_session_lock = threading.Lock()


def _load_symbols() -> list[str]:
    global _MSYMBOLS
    if _MSYMBOLS is None:
        with open(_JSON_PATH) as fh:
            _MSYMBOLS = json.load(fh)['symbols']
    return _MSYMBOLS


def available() -> bool:
    """True when the model files exist and onnxruntime is importable."""
    if not os.path.exists(_MODEL_PATH) or not os.path.exists(_JSON_PATH):
        return False
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _get_session():
    """Lazily load the ONNX inference session (thread-safe singleton)."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort

            _session = ort.InferenceSession(
                _MODEL_PATH, providers=['CPUExecutionProvider']
            )
    return _session


def _segment_characters(image: np.ndarray) -> list[list[dict]]:
    """Split a tight MRZ crop into per-line character boxes.

    Mirrors mrz-ocr.ts: Otsu threshold, connected components, aspect-ratio
    filter (0.3..3.0), Y-clustering into lines, keep the last 3 lines that
    hold at least 5 characters.
    """
    grey = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # dark text on light background -> chars are the black components
    mask = (thresh == 0).astype(np.uint8) * 255
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    rois = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 5:
            continue
        ratio = w / h
        if ratio < 0.3 or ratio > 3.0:
            continue
        rois.append({'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h),
                     'cy': y + h / 2.0})

    if not rois:
        return []

    rois.sort(key=lambda r: r['cy'])
    line_threshold = grey.shape[0] / 6.0
    lines: list[list[dict]] = []
    current: list[dict] = []
    for r in rois:
        if not current or abs(r['cy'] - current[0]['cy']) < line_threshold:
            current.append(r)
        else:
            lines.append(current)
            current = [r]
    if current:
        lines.append(current)

    return [ln for ln in lines if len(ln) >= 5][-3:]


def _recognize(region: np.ndarray, symbols: list[str]) -> list[str]:
    """Segment and classify the characters of a tight MRZ crop."""
    lines = _segment_characters(region)
    if not lines:
        return []

    sess = _get_session()
    grey = region if len(region.shape) == 2 else cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)

    crops: list[np.ndarray] = []
    line_indexes: list[tuple[int, int]] = []
    for line in lines:
        line.sort(key=lambda r: r['x'])
        start = len(crops)
        for r in line:
            crop = grey[r['y']:r['y'] + r['h'], r['x']:r['x'] + r['w']]
            if crop.size == 0:
                continue
            resized = cv2.resize(
                crop, (_INPUT_SIZE, _INPUT_SIZE), interpolation=cv2.INTER_LINEAR
            )
            crops.append(resized.astype(np.float32) / 255.0)
        line_indexes.append((start, len(crops)))

    if not crops:
        return []

    batch = np.stack(crops)[:, None, :, :]
    out = sess.run(['output'], {'input': batch})[0]
    preds = np.argmax(out, axis=1)

    result = []
    for start, end in line_indexes:
        result.append(''.join(symbols[int(p)] for p in preds[start:end]))
    return result


def cnn_ocr_lines(region: np.ndarray) -> list[str]:
    """Recognize MRZ lines from a tight MRZ crop via the CNN."""
    if not available():
        return []
    try:
        return _recognize(region, _load_symbols())
    except Exception:
        return []