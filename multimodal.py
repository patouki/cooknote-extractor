"""Cooknote V4: public video captions, subtitles, speech and video-frame OCR.

All outputs are heuristic and must be user-reviewed. No login/cookies or private
media bypass. No external AI service required. Whisper weights download once.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import shutil
import sys
import subprocess
from functools import lru_cache
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from extractor import (ExtractionError, completion, image_url, package,
                       text_recipe, validate_public_url, STEP_VERBS, INGREDIENT_PATTERN)
from recipe_polish import recipe_title, explicit_seasonings

MAX_MEDIA = 100 * 1024 * 1024
MAX_DURATION = 12 * 60
MAX_FRAME_SAMPLES = 9
MAX_TRANSCRIPT = 35_000
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.mov', '.m4v'}
AUDIO_EXTENSIONS = {'.m4a', '.mp3', '.wav', '.aac', '.ogg', '.flac'}


def binaries():
    return {'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
            'tesseract': bool(shutil.which('tesseract') or _module_installed('rapidocr')),
            'pyav': _module_installed('av'),
            'whisper': _module_installed('faster_whisper'), 'ytdlp': _module_installed('yt_dlp')}


def _module_installed(module):
    import importlib.util
    return importlib.util.find_spec(module) is not None


def _safe_run(argv, *, timeout=100):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError(f'Impossible de traiter le média avec FFmpeg ({type(exc).__name__}).', 'media_invalide') from exc


def media_duration(path: Path):
    if _module_installed('av'):
        import av
        try:
            with av.open(str(path)) as container:
                duration = (container.duration / av.time_base) if container.duration else 0
                if not duration:
                    streams = list(container.streams.video) + list(container.streams.audio)
                    duration = max((float(stream.duration * stream.time_base)
                                    for stream in streams if stream.duration and stream.time_base), default=0)
        except (ValueError, OSError, av.error.FFmpegError) as exc:
            raise ExtractionError('Impossible de décoder ce média.', 'media_invalide') from exc
        if not 0 < duration <= MAX_DURATION:
            raise ExtractionError('Vidéo ou son limité à 12 minutes.', 'limite')
        return duration
    if not shutil.which('ffprobe'):
        raise ExtractionError('FFprobe requis pour limiter la durée des médias.', 'dependance')
    out = _safe_run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], timeout=20).stdout.strip()
    try:
        seconds = float(out)
    except ValueError:
        raise ExtractionError('Impossible de déterminer la durée de la vidéo.', 'media_invalide')
    if not 0 < seconds <= MAX_DURATION:
        raise ExtractionError('Vidéo limitée à 12 minutes.', 'limite')
    return seconds


def audio_transcript(path: Path) -> str:
    """CPU speech recognition with faster-whisper/PyAV; no external FFmpeg executable.

    Models are fetched on first use unless pre-cached in COOKNOTE_MODEL_DIR.
    """
    if not _module_installed('faster_whisper'):
        raise ExtractionError('Installez la dépendance « faster-whisper » pour la transcription.', 'dependance')
    from faster_whisper import WhisperModel
    bundled = Path(__file__).resolve().parent / 'whisper-model'
    cache = Path(os.environ.get('COOKNOTE_MODEL_DIR', Path.home() / '.cooknote' / 'models'))
    cache.mkdir(parents=True, exist_ok=True)
    model_name = os.environ.get('COOKNOTE_WHISPER_MODEL', 'base')
    if model_name not in {'tiny', 'base', 'small'}:
        model_name = 'base'
    try:
        model_source = str(bundled) if model_name == 'base' and (bundled / 'model.bin').exists() else model_name
        model = WhisperModel(model_source, device='cpu', compute_type='int8', download_root=str(cache))
        segments, _ = model.transcribe(str(path), beam_size=3, vad_filter=True)
        return ' '.join(s.text.strip() for s in segments if s.text.strip())[:MAX_TRANSCRIPT]
    except Exception as exc:
        raise ExtractionError('Transcription impossible : modèle absent, réseau indisponible ou audio non lisible.', 'transcription') from exc


@lru_cache(maxsize=1)
def rapidocr_engine():
    from rapidocr import RapidOCR
    return RapidOCR()


def ocr_image(img) -> str:
    """Tesseract (including French) or packaged RapidOCR fallback."""
    from PIL import ImageOps, ImageEnhance
    image = ImageEnhance.Contrast(ImageOps.grayscale(img)).enhance(1.4)
    if shutil.which('tesseract'):
        import pytesseract
        langs = set(pytesseract.get_languages(config=''))
        language = 'fra+eng' if {'fra','eng'} <= langs else 'fra' if 'fra' in langs else 'eng' if 'eng' in langs else None
        if language:
            return pytesseract.image_to_string(image, lang=language, config='--psm 11', timeout=18)
    if _module_installed('rapidocr'):
        # Model weights are included in RapidOCR wheels by default.
        result = rapidocr_engine()(image)
        return '\n'.join(result.txts or ())
    raise ExtractionError('OCR indisponible : installez RapidOCR ou Tesseract.', 'dependance')


def pyav_frame_text(path: Path, duration: float) -> str:
    """Sample decoded frames with PyAV: portable on Windows/macOS without ffmpeg.exe."""
    import av
    seen, lines = set(), []
    points = [(i + 0.5) * duration / MAX_FRAME_SAMPLES for i in range(MAX_FRAME_SAMPLES)]
    with av.open(str(path)) as container:
        if not container.streams.video:
            return ''
        stream = container.streams.video[0]
        for second in points:
            try:
                if not stream.time_base:
                    continue
                timestamp = int(second / float(stream.time_base))
                container.seek(max(0, timestamp), stream=stream, backward=True)
                for frame in container.decode(stream):
                    if frame.time is not None and frame.time + 0.05 < second:
                        continue
                    img = frame.to_image()
                    img.thumbnail((1000, 1000))
                    content = ocr_image(img)
                    for line in content.splitlines():
                        line = re.sub(r'\s+', ' ', line).strip()
                        if len(line) >= 5 and line.casefold() not in seen:
                            seen.add(line.casefold()); lines.append(line)
                    break
            except (ExtractionError, OSError, ValueError, RuntimeError):
                continue
    return '\n'.join(lines)[:MAX_TRANSCRIPT]


def frame_text(path: Path, duration: float) -> str:
    """Read visible overlay text in sampled video frames with Tesseract."""
    if not (shutil.which('tesseract') or _module_installed('rapidocr')):
        return ''
    if _module_installed('av'):
        return pyav_frame_text(path, duration)
    if not shutil.which('ffmpeg'):
        return ''
    import pytesseract
    from PIL import Image, ImageOps, ImageEnhance
    languages = set(pytesseract.get_languages(config=''))
    lang = 'fra+eng' if {'fra', 'eng'} <= languages else 'fra' if 'fra' in languages else 'eng' if 'eng' in languages else None
    if not lang:
        return ''
    texts = []
    seen = set()
    # Includes middle frames and avoids first/last title/end cards.
    points = [(i + 0.5) * duration / MAX_FRAME_SAMPLES for i in range(MAX_FRAME_SAMPLES)]
    with tempfile.TemporaryDirectory(prefix='cooknote-frames-') as tmp:
        for i, second in enumerate(points):
            frame = Path(tmp) / f'f{i}.png'
            try:
                _safe_run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', str(round(second, 2)),
                           '-i', str(path), '-frames:v', '1', '-vf', 'scale=960:-2', str(frame)], timeout=20)
                with Image.open(frame) as im:
                    image = ImageEnhance.Contrast(ImageOps.grayscale(im)).enhance(1.5)
                    content = pytesseract.image_to_string(image, lang=lang, config='--psm 11', timeout=12)
                lines = [re.sub(r'\s+', ' ', line).strip() for line in content.splitlines()]
                for line in lines:
                    key = line.casefold()
                    if len(line) >= 5 and key not in seen:
                        seen.add(key)
                        texts.append(line)
            except (ExtractionError, OSError, RuntimeError, ValueError, pytesseract.TesseractError):
                continue
    return '\n'.join(texts)[:MAX_TRANSCRIPT]


def subtitle_text(raw: str, ext: str = 'vtt') -> str:
    """Clean common VTT/SRT/YouTube json3 subtitles."""
    if ext == 'json3':
        try:
            entries = json.loads(raw).get('events', [])
            values = [''.join(x.get('utf8', '') for x in e.get('segs', [])) for e in entries]
        except (ValueError, AttributeError):
            return ''
    else:
        values = []
        for line in raw.splitlines():
            line = line.strip()
            if (not line or re.match(r'^(?:WEBVTT|NOTE|Kind:|Language:|\d+$)', line, re.I)
                    or '-->' in line or re.match(r'^\d{1,2}:\d{2}[:.]', line)):
                continue
            values.append(line)
    seen = set()
    unique = []
    for chunk in values:
        chunk = html.unescape(re.sub(r'<[^>]*>', '', chunk)).strip()
        chunk = re.sub(r'\s+', ' ', chunk)
        if chunk and chunk.casefold() not in seen:
            seen.add(chunk.casefold()); unique.append(chunk)
    return ' '.join(unique)[:MAX_TRANSCRIPT]


def remote_subtitles(info: dict) -> str:
    """Use available public caption URLs; never pass unverified URLs to requests."""
    import requests
    for key in ('subtitles', 'automatic_captions'):
        options = info.get(key) or {}
        for language in ('fr', 'fr-FR', 'en', 'en-US'):
            entries = options.get(language) or []
            for format_info in sorted(entries, key=lambda f: {'vtt': 0, 'json3': 1, 'srt': 2}.get(f.get('ext', ''), 9)):
                link = format_info.get('url', '')
                if not link or format_info.get('ext') not in ('vtt', 'json3', 'srt'):
                    continue
                try:
                    from extractor import get_public_text
                    body = get_public_text(link, max_bytes=350_000)
                    text = subtitle_text(body, format_info['ext'])
                    if text:
                        return text
                except (ExtractionError, requests.RequestException):
                    continue
    return ''


def speech_to_recipe(text: str, *, title: str, platform: str) -> dict:
    """Translate commonly-spoken quantified lists & imperative phrases into lines.

    Does not infer quantities or ingredients from visual appearance.
    """
    base = text_recipe(text, fallback_title=title, platform=platform)
    if completion(base) == 'complete' or not text:
        return base
    # Spoken lists often lack explicit newlines. Split at sentence and conjunctions,
    # preserving numbers already spoken (no extrapolation).
    text = re.sub(r'\s+(?:et|and)\s+(?=\d+(?:[,.]\d+)?\s)', ', ', text, flags=re.I)
    fragments = re.split(r'(?<=[.!?;])\s+|\s*,\s*|\s+(?:et ensuite|puis|ensuite|and then)\s+', text, flags=re.I)
    ingredient = re.compile(r'(\d+(?:[,.]\d+)?\s*(?:(?:k|m)?g|grammes?|ml|cl|litres?|oeufs?|œufs?|oignons?|tomates?|gousses?|cuill[eè]res?|tasses?|tranches?|pinc[eé]es?)\b[^.!?;,]{0,72})', re.I)
    ingredients = list(base['ingredients'])
    steps = list(base['steps'])
    for piece in fragments:
        piece = piece.strip(' •-.')
        for match in ingredient.finditer(piece):
            candidate = match.group(1).strip(' ,.')
            if candidate and not any(candidate.casefold() == old.casefold() for old in ingredients):
                ingredients.append(candidate)
        # Detect actual action phrases. Avoid treating promotional sentences as steps.
        clause = re.sub(r'^(?:alors|donc|on va|vous allez|il faut|pour commencer|maintenant)\s+', '', piece, flags=re.I)
        narrated_action = re.match(r"^(?:j[’']|je\s+|on\s+|nous\s+|vous\s+)(?:(?:vais|va|allons|allez)\s+)?(?:ajout\w*|mett\w*|m[eé]lang\w*|vers\w*|coup\w*|cuis\w*|assaisonn\w*|d[eé]pos\w*|incorpor\w*|saupoudr\w*)\b", clause, re.I)
        if (STEP_VERBS.match(clause) or narrated_action or re.match(r'^(?:mélangez|ajoutez|coupez|versez|faites|préchauffez|cuisez|laissez|servez|incorporez|mettez|fouettez|battez|mixez|retirez|égouttez|émincez|épluchez|réservez|on\s+(?:mélange|ajoute|coupe|fait|met|verse))\b', clause, re.I)) and len(clause) >= 12 and clause.casefold() not in {s.casefold() for s in steps}:
            steps.append(clause[:420])
    base['ingredients'] = ingredients[:80]
    base['steps'] = steps[:80]
    return base


def visual_to_recipe(content: str, *, title: str, platform: str) -> dict:
    """Classify OCR text line-by-line, since two-column cards interleave sections."""
    base = text_recipe(content, fallback_title=title, platform=platform)
    ingredients, steps = [], []
    for line in content.splitlines():
        cleaned = line.strip(' •\t')
        normalized = re.sub(r'^\d+[.)]\s*', '', cleaned).strip()
        if INGREDIENT_PATTERN.match(cleaned) and not STEP_VERBS.match(normalized):
            ingredients.append(cleaned)
        elif STEP_VERBS.match(normalized) or re.match(r'^(?:mélangez|ajoutez|cuisez|coupez|versez|servez)\b', normalized, re.I):
            if len(normalized) > 7:
                steps.append(normalized)
    if ingredients:
        base['ingredients'] = list(dict.fromkeys(ingredients))[:80]
    if steps:
        base['steps'] = list(dict.fromkeys(steps))[:80]
    return base


def merge_evidence(title: str, platform: str, fragments: list[tuple[str, str]], image: str = '', url: str = '') -> dict:
    ingredients, steps, methods, seen_i, seen_s = [], [], [], set(), set()
    recipe = None
    for method, content in fragments:
        if not content.strip():
            continue
        parsed = (speech_to_recipe(content, title=title, platform=platform) if method == 'audio Whisper'
                  else visual_to_recipe(content, title=title, platform=platform) if method == 'images OCR'
                  else text_recipe(content, fallback_title=title, platform=platform))
        methods.append(method)
        if recipe is None:
            recipe = parsed
        for dest, seen, key in ((ingredients, seen_i, 'ingredients'), (steps, seen_s, 'steps')):
            for item in parsed[key]:
                normalized = re.sub(r'\s+', ' ', item).strip().casefold()
                if normalized not in seen:
                    seen.add(normalized); dest.append(item)
    if recipe is None:
        recipe = text_recipe('', fallback_title=title, platform=platform)
    # Social-platform video titles are frequently account names. Determine the
    # dish from caption, transcription and OCR, or request the user's own title.
    recipe['title'] = recipe_title(title, *(content for _, content in fragments))
    recipe['ingredients'] = explicit_seasonings(
        ingredients[:80], [*(content for _, content in fragments), *steps]
    )
    recipe['steps'] = steps[:80]
    recipe['image'] = image
    method_label = ' + '.join(methods) if methods else 'Aucun média accessible'
    result = package(recipe, method_label, url, note=('Sources analysées : ' + ', '.join(methods) + '. Vérifiez les mots reconnus et les quantités.' if methods else 'Aucune donnée exploitable depuis ce média.'))
    result['evidence'] = methods
    return result


def process_media_file(path: Path, *, title='Recette vidéo', platform='Vidéo locale', url='',
                       known_fragments=None, image='', progress=None) -> dict:
    if path.stat().st_size > MAX_MEDIA:
        raise ExtractionError('Fichier vidéo limité à 100 Mo.', 'limite')
    duration = media_duration(path)
    sources = list(known_fragments or [])
    errors = []
    if not (shutil.which('tesseract') or _module_installed('rapidocr')):
        errors.append('OCR non disponible (RapidOCR ou Tesseract requis).')
    if progress: progress('audio', 58, 'Transcription audio Whisper en cours (durée variable)…')
    try:
        transcript = audio_transcript(path)
        if transcript:
            sources.append(('audio Whisper', transcript))
    except ExtractionError as exc:
        errors.append(str(exc))
    if progress: progress('ocr', 81, 'Analyse OCR des images de la vidéo…')
    try:
        overlay = frame_text(path, duration)
        if overlay:
            sources.append(('images OCR', overlay))
    except (OSError, ValueError):
        errors.append('Lecture des images indisponible.')
    if progress: progress('ocr', 95, 'Fusion des ingrédients et des étapes…')
    result = merge_evidence(title, platform, sources, image=image, url=url)
    result['durationSeconds'] = round(duration)
    result['warnings'] = errors
    if errors:
        result['message'] += ' ' + ' '.join(errors)
    return result


def _download_video(url: str, destination: Path, on_progress=None):
    import yt_dlp
    class TooLarge(Exception):
        pass
    last_report=[0.0]
    def progress(data):
        n=data.get('downloaded_bytes') or 0
        if n > MAX_MEDIA:
            raise TooLarge()
        if not on_progress or data.get('status') not in ('downloading','finished'):
            return
        now=__import__('time').monotonic()
        if now-last_report[0]<2 and data.get('status')!='finished':
            return
        last_report[0]=now
        total=data.get('total_bytes') or data.get('total_bytes_estimate') or MAX_MEDIA
        pct=30+int(22*min(1,n/max(1,total)))
        on_progress('downloading',pct,'Téléchargement de la vidéo : '+str(round(n/1048576,1))+' Mo')
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'skip_download': False,
            'format': 'best[height<=720][ext=mp4]/best[height<=720]/best',
            'outtmpl': str(destination / 'source.%(ext)s'),
            'max_filesize': MAX_MEDIA, 'progress_hooks': [progress],
            'socket_timeout': 18, 'retries': 1, 'extractor_retries': 1,
            'cachedir': False, 'ignoreerrors': False}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except TooLarge as exc:
        raise ExtractionError('Média distant supérieur à 100 Mo.', 'limite') from exc
    except Exception as exc:
        raise ExtractionError(f'Téléchargement vidéo impossible ({type(exc).__name__}) : source bloquée ou indisponible.', 'acces_refuse') from exc
    candidates = [p for p in destination.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS]
    if not candidates:
        raise ExtractionError('Aucune vidéo publique récupérable à cette adresse.', 'acces_refuse')
    file = max(candidates, key=lambda p: p.stat().st_size)
    if file.stat().st_size > MAX_MEDIA:
        raise ExtractionError('Média distant supérieur à 100 Mo.', 'limite')
    return file


def extract_video_url(url: str, progress=None) -> dict:
    """Only called after public URL validation + known video platform classification."""
    import yt_dlp
    from extractor import facebook_video_link, resolve_facebook_video_url
    validate_public_url(url)
    media_url = resolve_facebook_video_url(url) if facebook_video_link(url) else url
    if progress: progress('metadata', 9, 'Récupération des informations publiques de la vidéo…')
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
            'socket_timeout': 14, 'retries': 1, 'extractor_retries': 0,
            'ignoreerrors': False, 'cachedir': False, 'playlistend': 1}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(media_url, download=False)
    except Exception as exc:
        if facebook_video_link(url):
            raise ExtractionError('Facebook ne laisse pas ce Reel se lire depuis le serveur. Ouvrez-le dans Facebook pour copier son lien direct /reel/... ou importez le fichier vidéo.', 'facebook_acces_refuse') from exc
        raise ExtractionError('Lien vidéo bloqué, privé ou non lisible publiquement. Essayez le fichier vidéo local.', 'acces_refuse') from exc
    if not isinstance(info, dict):
        raise ExtractionError('Métadonnées vidéo absentes.', 'source_indisponible')
    if info.get('entries'):
        info = next((it for it in info['entries'] if isinstance(it, dict)), {})
    if progress: progress('metadata', 18, 'Lecture de la légende et des sous-titres…')
    title = str(info.get('title') or 'Recette vidéo')[:120]
    platform = urlparse(url).hostname or 'Vidéo'
    caption = str(info.get('description') or info.get('caption') or '')[:MAX_TRANSCRIPT]
    subtitles = remote_subtitles(info)
    # The metadata title can be the publisher. Do not inject it into the
    # ingredient/step classifier as if it were a cooking instruction.
    fragments = [('légende', caption)] if caption else []
    if subtitles:
        fragments.append(('sous-titres', subtitles))
    first = merge_evidence(title, platform, fragments, image=image_url(info.get('thumbnail'), url), url=url)
    if first['status'] == 'complete':
        return first
    # If ingredients/steps are missing, retrieve public media for transcription + OCR.
    duration = float(info.get('duration') or 0)
    if duration > MAX_DURATION:
        first['warnings'] = ['Vidéo de plus de 12 minutes : audio et images non analysés.']
        first['message'] += ' ' + first['warnings'][0]
        return first
    if not _module_installed('av') and (not shutil.which('ffmpeg') or not shutil.which('ffprobe')):
        first['warnings'] = ['FFmpeg et FFprobe manquants.']
        first['message'] += ' Installez FFmpeg pour analyser audio et images.'
        return first
    if progress: progress('downloading', 28, 'Préparation du téléchargement de la vidéo…')
    with tempfile.TemporaryDirectory(prefix='cooknote-video-') as temp:
        try:
            path = _download_video(media_url, Path(temp), on_progress=progress)
        except ExtractionError as exc:
            first['warnings'] = [str(exc)]
            first['message'] += ' ' + str(exc)
            return first
        return process_media_file(path, title=title, platform=platform,
                                  url=url, known_fragments=fragments, image=first['recipe']['image'], progress=progress)