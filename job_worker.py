"""Isolated, time-bounded worker for Cooknote imports.

The parent captures JSON-prefixed stdout events and terminates this process group
on cancellation or timeout, so a hung Whisper/Chromium worker cannot block the queue.
"""
from __future__ import annotations

import json
import sys

PREFIX = 'COOKNOTE_JOB_JSON:'


def send(kind: str, **fields):
    print(PREFIX + json.dumps({'type': kind, **fields}, ensure_ascii=False, default=str), flush=True)


def extract_video(url: str):
    from extractor import ExtractionError, extract_url, is_social, validate_public_url, facebook_video_link, resolve_facebook_video_url
    from multimodal import extract_video_url
    import browser_capture

    validate_public_url(url)

    def progress(phase, percent, message):
        send('progress', phase=phase, percent=percent, message=message)

    output = None
    first_error = None
    video = is_social(url)
    try:
        output = extract_video_url(url, progress=progress) if video else extract_url(url)
    except ExtractionError as exc:
        first_error = exc
    except Exception:
        first_error = ExtractionError('La plateforme ne fournit pas de flux vidéo accessible.', 'acces_refuse')

    if output and output.get('status') == 'complete':
        return output
    if video and browser_capture.browser_available():
        try:
            capture_url = resolve_facebook_video_url(url) if facebook_video_link(url) else url
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
        if video:
            output.setdefault('warnings', []).append('Capture navigateur non disponible.')
        return output
    if first_error:
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