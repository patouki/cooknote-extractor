"""Isolated, time-bounded worker for Cooknote imports.

The parent captures JSON-prefixed stdout events and terminates this process group
on cancellation or timeout, so a hung Whisper/Chromium worker cannot block the queue.
"""
from __future__ import annotations

import json
import sys

PREFIX = 'COOKNOTE_JOB_JSON:'


def low_memory_instance() -> bool:
    """Use a lighter TikTok path on 512 MB Render instances.

    Starting Chromium after a rejected media download can push the process
    above its cgroup limit and kill the entire job before it can report errors.
    """
    from pathlib import Path
    try:
        raw = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if raw.isdecimal():
            return int(raw) <= 768 * 1024 * 1024
    except (OSError, ValueError):
        pass
    return False


def send(kind: str, **fields):
    print(PREFIX + json.dumps({'type': kind, **fields}, ensure_ascii=False, default=str), flush=True)


def extract_video(url: str):
    from extractor import (ExtractionError, extract_url, is_social, validate_public_url,
                           url_content_kind, facebook_video_link, resolve_facebook_video_url,
                           tiktok_video_link, resolve_tiktok_video_url)
    from multimodal import extract_video_url
    import browser_capture

    validate_public_url(url)

    def progress(phase, percent, message):
        send('progress', phase=phase, percent=percent, message=message)

    output = None
    first_error = None
    content_kind = url_content_kind(url)
    progress('metadata',10, 'Analyse de la page web et de ses données de recette…' if content_kind=='webpage' else
             'Lecture de votre document texte…' if content_kind in ('text','json') else
             'Analyse audio et visuelle du média…')
    video = content_kind in ('social-video','direct-media')
    try:
        output = (extract_video_url(url, progress=progress,
                  checkpoint=lambda item: send('checkpoint', result=item))
                  if video else extract_url(url))
    except ExtractionError as exc:
        first_error = exc
    except Exception:
        first_error = ExtractionError('La plateforme ne fournit pas de flux vidéo accessible.', 'acces_refuse')

    if output and output.get('status') == 'complete':
        return output
    browser_enabled = browser_capture.browser_available()
    media_evidence = bool(output and ('durationSeconds' in output or set(output.get('evidence') or ()).intersection({'audio Whisper', 'images OCR'})))
    # Do not open Chromium after a full local download has already been
    # transcribed or OCR'd. On a 512 MB server, Whisper/RapidOCR + Chromium
    # in one process can trigger the OOM killer and discard the usable result.
    # On low-RAM instances, Chromium + video decoders can be OOM-killed when
    # TikTok denies its public MP4. Prefer the existing caption/subtitle output
    # and a clear import error over a killed worker and lost progress.
    heavy_tiktok_fallback = tiktok_video_link(url) and low_memory_instance()
    if content_kind == 'social-video' and browser_enabled and not media_evidence and not heavy_tiktok_fallback:
        try:
            capture_url = (resolve_facebook_video_url(url) if facebook_video_link(url)
                           else resolve_tiktok_video_url(url) if tiktok_video_link(url) else url)
            captured = browser_capture.capture_public_video(capture_url, progress=progress)
            if facebook_video_link(url) and isinstance(captured, dict):
                captured['sourceUrl'] = url
            if output and output.get('status') != 'none' and captured.get('status') == 'none':
                output.setdefault('warnings', []).append('Navigateur : aucun nouvel ingrédient fiable détecté.')
                return output
            return captured
        except ExtractionError as exc:
            if output:
                output.setdefault('warnings', []).append(str(exc))
                return output
            if facebook_video_link(url):
                raise ExtractionError('Facebook empêche la lecture de ce Reel depuis le navigateur cloud. Essayez un lien direct /reel/<identifiant> ou importez le fichier vidéo.', 'facebook_acces_refuse') from exc
            raise
    if output:
        if heavy_tiktok_fallback and output.get('status') == 'none':
            output.setdefault('warnings', []).append('La lecture de TikTok est limitée sur ce serveur. Réessayez avec la vidéo enregistrée.')
        if video and not browser_enabled:
            output.setdefault('warnings', []).append('Capture navigateur non disponible.')
        elif video and media_evidence and output.get('status') != 'complete':
            output.setdefault('warnings', []).append('Audio et images analysés. Complétez les éléments manquants avant la sauvegarde.')
        return output
    if first_error:
        if heavy_tiktok_fallback:
            raise ExtractionError('TikTok limite l’accès à cette vidéo depuis le serveur. Importez le fichier vidéo pour analyser son audio et ses images.', 'tiktok_acces_refuse') from first_error
        raise first_error
    raise ExtractionError('Aucune recette détectée.', 'source_indisponible')


def main():
    if len(sys.argv) != 2:
        send('error', code='bad_arguments', message='Lien manquant.')
        return 2
    try:
        output = extract_video(sys.argv[1])
        send('result', result=output)
        return 0
    except Exception as exc:
        from extractor import ExtractionError
        if isinstance(exc, ExtractionError):
            send('error', code=exc.code, message=str(exc))
        else:
            send('error', code='internal', message='La vidéo ne peut pas être traitée. Réessayez avec un fichier local.')
        return 1


if __name__ == '__main__':
    sys.exit(main())