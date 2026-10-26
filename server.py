"""Cooknote V4.2: self-hostable HTTPS web application + limited media extraction API.

Use a trusted HTTPS reverse proxy. The API intentionally has no permissive CORS:
the web client is served from the same origin. Optional HTTP Basic login protects
both the UI and the computationally expensive extraction endpoints.
"""
from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
API_VERSION = '4.3'
LIMITS = {'uploadBytes': MAX_MEDIA, 'imageBytes': 8_000_000, 'maxMinutes': 12}
app = FastAPI(title='Cooknote V4.2', version=API_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
# Process-local controls. For hosted deployments run exactly one worker/instance.
_gate = threading.BoundedSemaphore(value=1)
_rate_lock = threading.Lock()
_jobs_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_job_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cooknote-job')
_MAX_QUEUED = 5
_JOB_TTL_SECONDS = 2 * 3600
_hits: dict[str, deque] = defaultdict(deque)
_RATE_WINDOW = 3600
_RATE_LIMIT = 24


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
                    return JSONResponse({'detail':'Limite de 24 analyses par heure atteinte.'}, status_code=429,
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
    if not _gate.acquire(timeout=60):
        raise HTTPException(503, detail='Moteur occupé : attente prolongée, réessayez bientôt.',
                            headers={'Retry-After':'20'})
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


def extract_checked(url: str, progress=None):
    # Validate URL and ensure public resolution before any external requests.
    validate_public_url(url)
    if is_social(url):
        if not supported_video_url(url):
            raise ExtractionError('Lien vidéo non pris en charge : utilisez un Reel, un TikTok ou une vidéo YouTube.', 'url_invalide')
        return extract_video_url(url, progress=progress) if progress else extract_video_url(url)
    return extract_url(url)


@app.get('/ready')
def ready():
    return {'ok': True}


@app.get('/api/health')
def health():
    caps = binaries()
    from browser_capture import browser_available
    caps['browserCapture'] = browser_available()
    return {'ok': True, 'version': API_VERSION, 'capabilities': caps,
            'social': caps['ytdlp'], 'audio': caps['whisper'], 'ocr': caps['tesseract'],
            'video': caps['pyav'] or (caps['ffmpeg'] and caps['ffprobe']), 'limits': LIMITS}


# Jobs avoid 90-second browser/proxy timeouts and allow the phone to reload.
# These are intentionally private (the existing HTTP Basic middleware applies).
# A production multi-instance deployment should use a persistent queue/Redis.
def _job_public(job: dict):
    return {k: v for k, v in job.items() if k in
            ('jobId', 'state', 'phase', 'percent', 'message', 'createdAt', 'updatedAt', 'result', 'error')}


def _job_update(job_id: str, state=None, phase=None, percent=None, message=None, **extra):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        if state is not None: job['state'] = state
        if phase is not None: job['phase'] = phase
        if percent is not None: job['percent'] = min(100, max(0, int(percent)))
        if message is not None: job['message'] = str(message)[:300]
        job['updatedAt'] = datetime.now(timezone.utc).isoformat()
        job.update(extra)


def _job_work(job_id: str, url: str):
    _job_update(job_id, 'running', 'metadata', 5, 'Lecture des métadonnées et de la légende…')
    try:
        # Serialise the expensive media processing even if legacy /api/extract is used.
        with processing_slot():
            import browser_capture
            is_video = is_social(url)
            output = None
            first_error = None
            try:
                output = extract_checked(url, progress=lambda phase, pct, msg: _job_update(job_id,'running',phase,pct,msg))
            except ExtractionError as exc:
                first_error = exc
            except Exception:
                first_error = ExtractionError('Source indisponible pour le téléchargeur.', 'acces_refuse')
            if output and output.get('status') == 'complete':
                result = output
            elif is_video and browser_capture.browser_available():
                def progress(phase, pct, text):
                    _job_update(job_id, 'running', phase, pct, text)
                try:
                    result = browser_capture.capture_public_video(url, progress=progress)
                    if output and output.get('status') != 'none' and result.get('status') == 'none':
                        result = output
                        result.setdefault('warnings', []).append('Lecture navigateur : aucun ingrédient supplémentaire détecté.')
                except ExtractionError as exc:
                    if output:
                        result = output
                        result.setdefault('warnings', []).append(str(exc))
                    else:
                        raise
            elif output:
                result = output
                if is_video:
                    result.setdefault('warnings', []).append(
                        'Capture navigateur non activée sur cette instance. Le moteur ne peut lire que les médias accessibles à yt-dlp.')
            elif first_error:
                raise first_error
            else:
                raise ExtractionError('Aucune recette détectée.', 'source_indisponible')
        _job_update(job_id, 'completed', 'finished', 100, 'Analyse terminée.', result=result)
    except ExtractionError as exc:
        _job_update(job_id, 'failed', 'failed', 100, str(exc), error={'code': exc.code, 'message': str(exc)})
    except HTTPException as exc:
        _job_update(job_id, 'failed', 'failed', 100, str(exc.detail), error={'code': str(exc.status_code), 'message': str(exc.detail)})
    except Exception:
        _job_update(job_id, 'failed', 'failed', 100, 'Le moteur a échoué. Réessayez ou importez la vidéo.',
                    error={'code': 'internal', 'message': 'Le moteur vidéo ne peut pas traiter ce média.'})


@app.post('/api/jobs', status_code=202)
def start_job(req: ExtractRequest):
    try:
        validate_public_url(req.url)
    except ExtractionError as exc:
        raise HTTPException(422, detail={'code':exc.code,'message':str(exc)}) from exc
    if is_social(req.url) and not supported_video_url(req.url):
        raise HTTPException(422, detail='Lien social non pris en charge : utilisez un Reel ou une vidéo publique.')
    now = datetime.now(timezone.utc).isoformat()
    with _jobs_lock:
        # Jobs expire after two hours, but never prune a running job.
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_JOB_TTL_SECONDS)
        for key, job in list(_jobs.items()):
            if job['state'] in ('completed', 'failed'):
                try: expired = datetime.fromisoformat(job['updatedAt']) < cutoff
                except ValueError: expired = True
                if expired: del _jobs[key]
        active = sum(j['state'] in ('queued', 'running') for j in _jobs.values())
        if active >= _MAX_QUEUED:
            raise HTTPException(503, detail='La file de capture est pleine : réessayez plus tard.',
                                headers={'Retry-After': '30'})
        job_id = uuid.uuid4().hex
        job = {'jobId': job_id, 'state': 'queued', 'phase': 'queued', 'percent': 0,
               'message': 'Extraction en cours : en attente du navigateur virtuel…',
               'createdAt': now, 'updatedAt': now, 'result': None, 'error': None}
        _jobs[job_id] = job
        _job_executor.submit(_job_work, job_id, req.url)
        return {**_job_public(job), 'pollAfterMs': 2500}


@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise HTTPException(404, detail='Analyse introuvable.')
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, detail='Analyse expirée ou serveur redémarré. Relancez le lien.')
        return {**_job_public(job), 'pollAfterMs': 3000}


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