"""Cooknote V4.2: self-hostable HTTPS web application + limited media extraction API.

Use a trusted HTTPS reverse proxy. The API intentionally has no permissive CORS:
the web client is served from the same origin. Optional HTTP Basic login protects
both the UI and the computationally expensive extraction endpoints.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from extractor import ExtractionError, extract_url, is_social, validate_public_url, package
from multimodal import binaries, extract_video_url, ocr_image, process_media_file, visual_to_recipe, MAX_MEDIA

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
API_VERSION = '4.2'
LIMITS = {'uploadBytes': MAX_MEDIA, 'imageBytes': 8_000_000, 'maxMinutes': 12}
app = FastAPI(title='Cooknote V4.2', version=API_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
# Process-local controls. For hosted deployments run exactly one worker/instance.
_gate = threading.BoundedSemaphore(value=1)
_rate_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)
_RATE_WINDOW = 3600
_RATE_LIMIT = 12


def _require_secure_config():
    if os.getenv('COOKNOTE_ENV') == 'production':
        password = os.getenv('COOKNOTE_SITE_PASSWORD', '')
        if len(password) < 12:
            raise RuntimeError('COOKNOTE_SITE_PASSWORD doit contenir au moins 12 caractères pour un déploiement public.')


@app.on_event('startup')
async def startup():
    _require_secure_config()


def _same_origin(request: Request):
    # Browsers set Origin on cross-site fetch POST. Reject cross-site calls.
    origin = request.headers.get('origin', '')
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
        host = request.headers.get('host', '').lower()
        return parsed.scheme in ('https', 'http') and parsed.netloc.lower() == host
    except ValueError:
        return False


def _basic_authorized(request: Request) -> bool:
    password = os.getenv('COOKNOTE_SITE_PASSWORD', '')
    if not password:
        return True
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        import base64
        decoded = base64.b64decode(auth[6:], validate=True).decode('utf-8')
        name, submitted = decoded.split(':', 1)
        return secrets.compare_digest(name, 'cooknote') and secrets.compare_digest(submitted, password)
    except (UnicodeError, ValueError):
        return False


@app.middleware('http')
async def security(request: Request, call_next):
    if request.url.path == '/ready':
        return Response('ready', status_code=200, media_type='text/plain')
    if not _basic_authorized(request):
        return Response('Accès privé Cooknote : identifiant cooknote et mot de passe du serveur.', status_code=401,
                        headers={'WWW-Authenticate': 'Basic realm="Cooknote", charset="UTF-8"',
                                 'Cache-Control': 'no-store'})
    if request.url.path.startswith('/api/'):
        if request.method != 'GET' and not _same_origin(request):
            return JSONResponse({'detail':'Origine non autorisée.'}, status_code=403)
        if request.method == 'POST':
            # On single-process hosting this throttles costly jobs by connecting IP.
            # Prefer a trusted edge rate limiter as well for public deployments.
            ip = (request.client.host if request.client else 'unknown')
            now = time.monotonic()
            with _rate_lock:
                q = _hits[ip]
                while q and now - q[0] > _RATE_WINDOW:
                    q.popleft()
                if len(q) >= _RATE_LIMIT:
                    return JSONResponse({'detail':'Limite de 12 analyses par heure atteinte.'}, status_code=429,
                                        headers={'Retry-After':'3600'})
                q.append(now)
    resp = await call_next(request)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['X-Frame-Options'] = 'DENY'
    if request.url.path in ('/', '/index.html') or request.url.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@contextmanager
def processing_slot():
    if not _gate.acquire(blocking=False):
        raise HTTPException(503, detail='Une extraction est déjà en cours. Réessayez dans quelques instants.',
                            headers={'Retry-After':'30'})
    try:
        yield
    finally:
        _gate.release()


class ExtractRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


def supported_video_url(url: str):
    """Limit yt-dlp usage to simple public video URLs, not redirects/playlists."""
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower().removeprefix('www.').removeprefix('m.')
    path = parsed.path
    if parsed.scheme not in ('https','http') or parsed.username or parsed.password or parsed.port not in (None,80,443):
        return False
    if host == 'instagram.com':
        return bool(re.fullmatch(r'/(?:reel|p|tv)/[A-Za-z0-9_-]{5,40}/?', path))
    if host == 'tiktok.com':
        return bool(re.fullmatch(r'/@[^/]{1,70}/video/\d{8,25}/?', path))
    if host == 'vm.tiktok.com' or host == 'vt.tiktok.com':
        return bool(re.fullmatch(r'/[A-Za-z0-9]+/?', path))
    if host == 'youtu.be':
        return bool(re.fullmatch(r'/[A-Za-z0-9_-]{11}/?', path))
    if host in ('youtube.com','music.youtube.com'):
        return bool((path == '/watch' and re.fullmatch(r'[\w-]{11}', parse_qs(parsed.query).get('v',[''])[0]))
                    or re.fullmatch(r'/(?:shorts|live)/[A-Za-z0-9_-]{11}/?', path))
    return False


def extract_checked(url: str):
    # Validate URL and ensure public resolution before any external requests.
    validate_public_url(url)
    if is_social(url):
        if not supported_video_url(url):
            raise ExtractionError('Lien vidéo non pris en charge : utilisez un Reel, un TikTok ou une vidéo YouTube.', 'url_invalide')
        return extract_video_url(url)
    return extract_url(url)


@app.get('/ready')
def ready():
    return {'ok': True}


@app.get('/api/health')
def health():
    caps = binaries()
    return {'ok': True, 'version': API_VERSION, 'capabilities': caps,
            'social': caps['ytdlp'], 'audio': caps['whisper'], 'ocr': caps['tesseract'],
            'video': caps['pyav'] or (caps['ffmpeg'] and caps['ffprobe']), 'limits': LIMITS}


@app.post('/api/extract')
def extract(req: ExtractRequest):
    with processing_slot():
        try:
            return extract_checked(req.url)
        except ExtractionError as exc:
            raise HTTPException(422, detail={'code': exc.code, 'message': str(exc)}) from exc
        except Exception as exc:
            # Never expose provider details, cookies, environment or internal paths.
            raise HTTPException(502, detail='Extraction indisponible sur ce média. Essayez le fichier vidéo.') from exc


async def _limited_upload(file: UploadFile, max_bytes: int, target: Path):
    size = 0
    with target.open('wb') as out:
        while data := await file.read(1024 * 1024):
            size += len(data)
            if size > max_bytes:
                raise HTTPException(413, detail=f'Fichier supérieur à {max_bytes // 1_000_000} Mo.')
            out.write(data)
    if not size:
        raise HTTPException(422, detail='Fichier vide.')


@app.post('/api/media')
async def media_upload(media: UploadFile = File(...)):
    import tempfile
    allowed_ext = {'.mp4','.m4v','.mov','.webm','.mkv','.mp3','.m4a','.wav','.ogg','.flac'}
    ext = Path(media.filename or '').suffix.lower()
    if ext not in allowed_ext:
        raise HTTPException(415, detail='Format incompatible. Importez MP4, MOV, WebM, MP3, M4A ou WAV.')
    with processing_slot():
        with tempfile.TemporaryDirectory(prefix='cooknote-media-') as folder:
            path = Path(folder) / ('upload' + ext)
            await _limited_upload(media, MAX_MEDIA, path)
            try:
                return await run_in_threadpool(process_media_file, path, title=Path(media.filename or 'Recette').stem[:120])
            except ExtractionError as exc:
                raise HTTPException(422, detail={'code':exc.code,'message':str(exc)}) from exc
            except Exception as exc:
                raise HTTPException(422, detail='Impossible de décoder ce média.') from exc


@app.post('/api/ocr')
async def image_upload(image: UploadFile = File(...)):
    from PIL import Image, ImageOps, UnidentifiedImageError
    import tempfile
    ext = Path(image.filename or '').suffix.lower()
    if ext not in {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'}:
        raise HTTPException(415, detail='Choisissez une photo JPG, PNG ou WebP.')
    with processing_slot():
        with tempfile.TemporaryDirectory(prefix='cooknote-image-') as folder:
            path = Path(folder) / ('image' + ext)
            await _limited_upload(image, LIMITS['imageBytes'], path)
            try:
                with Image.open(path) as probe:
                    probe.verify()
                with Image.open(path) as original:
                    if original.width * original.height > 25_000_000:
                        raise HTTPException(413, detail='Dimensions image trop importantes.')
                    rgb = ImageOps.exif_transpose(original).convert('RGB')
                    rgb.thumbnail((2500, 2500))
                raw = await run_in_threadpool(ocr_image, rgb)
                recipe = visual_to_recipe(raw, title=Path(image.filename or 'Recette').stem, platform='Photo')
                result = package(recipe, 'OCR photo', note='Vérifiez les textes lus sur la photo.')
                result['rawText'] = raw[:12000]
                return result
            except HTTPException:
                raise
            except (OSError, ValueError, UnidentifiedImageError, ExtractionError) as exc:
                raise HTTPException(422, detail='Photo illisible ou reconnaissance indisponible.') from exc


@app.get('/')
def index():
    return FileResponse(WEB / 'index.html', media_type='text/html')


app.mount('/', StaticFiles(directory=WEB, html=True), name='web')

if __name__ == '__main__':
    import uvicorn
    _require_secure_config()
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT','8000')), workers=1)