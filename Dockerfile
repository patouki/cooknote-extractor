FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY server.py extractor.py multimodal.py browser_capture.py /app/
COPY web/ /app/web/
RUN useradd --create-home --uid 10001 cooknote && mkdir -p /var/cache/cooknote \
    && chown -R cooknote:cooknote /var/cache/cooknote /app
USER cooknote
ENV COOKNOTE_WHISPER_MODEL=tiny COOKNOTE_MODEL_DIR=/var/cache/cooknote/models PORT=8000
EXPOSE 8000
CMD ["sh","-c","exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]