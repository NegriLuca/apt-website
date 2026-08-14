import re
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract


@dataclass
class MrzResult:
    ok: bool
    error: str | None = None
    raw_lines: list[str] | None = None
    document_type: str | None = None
    document_number: str | None = None
    surname: str | None = None
    given_names: str | None = None
    nationality: str | None = None
    birth_date: str | None = None
    sex: str | None = None
    expiry_date: str | None = None
    country: str | None = None


_MRZ_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<'

_OCR_LANG: str | None = None


def _pick_ocr_lang() -> str:
    """Prefer the OCR-B traineddata (built for MRZ); fall back to English."""
    global _OCR_LANG
    if _OCR_LANG is not None:
        return _OCR_LANG
    try:
        available = set(pytesseract.get_languages(config=''))
        _OCR_LANG = 'ocrb_int' if 'ocrb_int' in available else 'eng'
    except Exception:
        _OCR_LANG = 'eng'
    return _OCR_LANG


def _to_grayscale(image: np.ndarray) -> np.ndarray:
    if len(image.shape) == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def _crop_mrz_region(image: np.ndarray) -> np.ndarray | None:
    """Find the machine-readable zone (bottom rows of dense dark text)."""
    gray = _to_grayscale(image)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    row_density = thresh.sum(axis=1) / 255.0

    height = thresh.shape[0]
    nonzero = [i for i in range(height) if row_density[i] > 0]
    if not nonzero:
        return gray

    # Group consecutive dark rows; the MRZ is the block of groups near the
    # bottom of the document. Merge groups whose gaps are small relative to
    # the dominant line height so both MRZ lines are captured.
    groups: list[tuple[int, int]] = []
    start: int | None = None
    for i in range(height):
        if row_density[i] > 0 and start is None:
            start = i
        elif row_density[i] == 0 and start is not None:
            groups.append((start, i - 1))
            start = None
    if start is not None:
        groups.append((start, height - 1))

    if not groups:
        return gray

    bottom_group = groups[-1]
    line_heights = [g[1] - g[0] + 1 for g in groups]
    median_height = float(sorted(line_heights)[len(line_heights) // 2])
    max_gap = max(2, int(median_height * 0.8))

    top = bottom_group[0]
    for idx in range(len(groups) - 2, -1, -1):
        gap = groups[idx + 1][0] - groups[idx][1] - 1
        if gap <= max_gap:
            top = groups[idx][0]
        else:
            break

    bottom = bottom_group[1]
    mrz = gray[max(0, top - 5):min(height, bottom + 5), :]
    return mrz if mrz.size > 0 else None


def _ocr_lines(image: np.ndarray, psm: int) -> list[str]:
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    height, width = thresh.shape
    if width >= 600 and height < 200:
        scale = 2.0
        thresh = cv2.resize(
            thresh,
            (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_CUBIC,
        )
    text = pytesseract.image_to_string(
        thresh,
        lang=_pick_ocr_lang(),
        config=f'--oem 1 --psm {psm} -c tessedit_char_whitelist={_MRZ_CHARS}',
    )
    return [re.sub(r'[^A-Z0-9<]', '', line) for line in text.splitlines() if line.strip()]


def _normalize_line(line: str) -> str:
    return re.sub(r'[^A-Z0-9<]', '', line).upper()


def _digits(s: str) -> str:
    return s.replace('O', '0').replace('I', '1').replace('B', '8').replace('S', '5')


def _letters(s: str) -> str:
    s = s.replace('1', 'I').replace('0', 'O').replace('8', 'B').replace('5', 'S')
    return s.rstrip('O')


def _name_field(part: str) -> str:
    """Clean a surname/given-name field; drop trailing filler artifacts."""
    return _letters(part).rstrip('O')


def extract_mrz(data: bytes) -> MrzResult:
    """Extract structured data from the MRZ zone of a passport/ID image or PDF."""
    image = _decode_input(data)
    if isinstance(image, MrzResult):
        return image

    try:
        region = _crop_mrz_region(image)
        lines = _ocr_lines(region, psm=6)
        if len(lines) < 2:
            lines = _ocr_lines(region, psm=11)
    except Exception as exc:
        return MrzResult(ok=False, error=f'OCR failed: {exc}')

    cleaned = [_normalize_line(line) for line in lines if line.strip()]
    if not cleaned:
        return MrzResult(ok=False, error='No text recognised in the image')

    parsed = _parse_mrz_scan(cleaned)
    if parsed is None:
        return MrzResult(ok=False, error='Could not parse a valid MRZ from the text', raw_lines=cleaned)

    return parsed


def _parse_mrz_scan(lines: list[str]) -> MrzResult | None:
    """Search all recognised lines for a valid MRZ, bottom-up: the MRZ sits at
    the very bottom of the document, so the last candidate wins."""
    for i in range(len(lines) - 1, -1, -1):
        parsed = _parse_mrz(lines[i:i + 3])
        if parsed is not None:
            return parsed
    return None


def _decode_input(data: bytes) -> np.ndarray | MrzResult:
    """Decode raw upload bytes into an image. Handles JPEG/PNG and PDF."""
    if data[:5] == b'%PDF-':
        image = _render_pdf(data)
        if image is None:
            return MrzResult(ok=False, error='Could not render the PDF to an image')
        return image

    try:
        arr = np.frombuffer(data, np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            return MrzResult(ok=False, error='Could not decode image (only images and PDFs are supported)')
        return image
    except Exception as exc:
        return MrzResult(ok=False, error=f'Could not decode image: {exc}')


def _render_pdf(data: bytes) -> np.ndarray | None:
    """Render the first page of a PDF to a high-res BGR image via PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None

    try:
        with fitz.open(stream=data, filetype='pdf') as doc:
            page = doc[0]
            # 200 DPI keeps MRZ text legible for OCR without huge images
            zoom = 200 / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            return cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if pix.n == 3 else img
    except Exception:
        return None


def _parse_mrz(lines: list[str]) -> MrzResult | None:
    # TD3: passport — 2 lines of 44 chars. Line 1 must contain the '<<'
    # surname/given-name separator, which plain OCR text never produces.
    if len(lines) >= 2 and len(lines[0]) >= 18 and len(lines[1]) >= 30:
        line1, line2 = lines[0], lines[1]
        if line1[0] in 'PVAIC' and '<<' in line1[5:]:
            # Split on '<<'; trailing fillers may be OCR'd as garbage, so only
            # trust the surname and given-name fields at the start of line 1.
            parts = re.split(r'<<+', line1[5:])
            surname = _name_field(parts[0] if parts else '')
            given = _name_field(parts[1] if len(parts) > 1 else '')
            if (
                _valid_name(surname)
                and _valid_country(line1[2:5])
                and _valid_country(line2[10:13])
                and _looks_like_date(line2[13:19])
                and _looks_like_date(line2[21:27])
            ):
                return MrzResult(
                    ok=True,
                    raw_lines=[line1[:44], line2[:44]],
                    document_type=line1[0],
                    country=_name_field(line1[2:5]),
                    surname=surname,
                    given_names=given,
                    document_number=_digits(line2[0:9]).replace('<', ''),
                    nationality=_name_field(line2[10:13]),
                    birth_date=_digits(line2[13:19]),
                    sex=line2[20].replace('O', '0'),
                    expiry_date=_digits(line2[21:27]),
                )

    # TD1: ID card — 3 lines of 30 chars (trailing fillers often dropped by OCR)
    if len(lines) >= 3 and len(lines[0]) >= 24 and len(lines[1]) >= 19 and len(lines[2]) >= 10:
        line1, line2, line3 = lines[0], lines[1], lines[2]
        if line1[0] in 'IACV' and '<<' in line3[0:29]:
            parts = re.split(r'<<+', line3[0:29])
            surname = _name_field(parts[0] if parts else '')
            given = _name_field(parts[1] if len(parts) > 1 else '')
            if (
                _valid_name(surname)
                and _valid_country(line1[2:5])
                and _valid_country(line2[15:18])
                and _looks_like_date(line2[0:6])
                and _looks_like_date(line2[8:14])
            ):
                return MrzResult(
                    ok=True,
                    raw_lines=[line1[:30], line2[:30], line3[:30]],
                    document_type=line1[0],
                    country=_name_field(line1[2:5]),
                    document_number=_digits(line1[5:14]).replace('<', ''),
                    surname=surname,
                    given_names=given,
                    nationality=_name_field(line2[15:18]),
                    birth_date=_digits(line2[0:6]),
                    sex=line2[7].replace('O', '0'),
                    expiry_date=_digits(line2[8:14]),
                )

    return None


def _valid_name(value: str) -> bool:
    return bool(value) and len(value) >= 2 and value.isalpha()


def _valid_country(value: str) -> bool:
    return bool(value) and len(value) >= 2 and value.isalpha()


def _looks_like_date(value: str) -> bool:
    """YYMMDD / YYMMDD expiry: after OCR digit-correction, mostly digits."""
    if len(value) < 4:
        return False
    digits = sum(1 for ch in _digits(value) if ch.isdigit())
    return digits >= len(value) - 2


def image_to_data_uri(image_bytes: bytes, mime: str = 'image/jpeg') -> str:
    import base64

    return f'data:{mime};base64,{base64.b64encode(image_bytes).decode()}'


def preview_data_uri(data: bytes) -> str | None:
    """Return a data URI to preview an uploaded image/PDF (None if unsupported)."""
    import base64

    if data[:5] == b'%PDF-':
        image = _render_pdf(data)
        if image is None:
            return None
        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return None
        return f'data:image/jpeg;base64,{base64.b64encode(encoded.tobytes()).decode()}'

    if data[:4] == b'\x89PNG':
        mime = 'image/png'
    elif data[:3] in (b'\xff\xd8\xff',):
        mime = 'image/jpeg'
    else:
        return None
    return f'data:{mime};base64,{base64.b64encode(data).decode()}'
