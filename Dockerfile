FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# tesseract is required by app/services/mrz_extractor.py (guest ID OCR)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# OCR-B traineddata: tesseract reads the MRZ font far more accurately with it
# than with the generic English model.
RUN curl -sL -o /usr/share/tesseract-ocr/5/tessdata/ocrb_int.traineddata \
        "https://github.com/Shreeshrii/tessdata_ocrb/raw/master/ocrb_int.traineddata"

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 5001

CMD ["gunicorn", "app:create_app()", "--access-logfile=-", "--error-logfile=-", "--log-level=info"]