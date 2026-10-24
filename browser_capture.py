"""Optional, sandboxed-ish screenshot/audio capture of *publicly playable* social videos.

A browser is not a universal social-media downloader: accounts, DRM, CAPTCHAs,
geo restrictions and consent gates are not bypassed. The routine verifies that a
real HTMLVideoElement advances before claiming to have watched the clip.

Activation requires a Chromium/Playwright worker and optionally an isolated
PulseAudio monitor sink for audio capture. Only known social video URLs passed
through server.supported_video_url() may reach this module.
"""
from __future__ import annotations

import io
import ipaddress
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from extractor import ExtractionError, image_url
from multimodal import MAX_DURATION, audio_transcript, merge_evidence, ocr_image

MAX_WATCH_SECONDS = min(MAX_DURATION, int(os.getenv('COOKNOTE_BROWSER_MAX_SECONDS', '360')))
MAX_FRAMES = 54
FRAME_INTERVAL = 2.5


def browser_available() -> bool:
    if os.getenv('COOKNOTE_BROWSER_CAPTURE', '').lower() not in ('1', 'true', 'yes'):
        return False
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _network_allowed(request_url: str) -> bool:
    """Reject unsafe schemes, internal hosts and literal private addresses."""
    try:
        u = urlparse(request_url)
        if u.scheme not in ('http', 'https') or u.username or u.password:
            return False
        host = (u.hostname or '').lower().rstrip('.')
        if not host or host in {'localhost', 'metadata.google.internal'} or host.endswith(('.localhost', '.local', '.internal')):
            return False
        try:
            ip = ipaddress.ip_address(host.strip('[]'))
            return ip.is_global
        except ValueError:
            return True
    except ValueError:
        return False


def _video_state(page):
    return page.evaluate('''() => {
      const videos=[...document.querySelectorAll('video')];
      const v=videos.find(x=>x.clientWidth>60&&x.clientHeight>60) || videos[0];
      if(!v) return {present:false};
      return {present:true, currentTime:Number(v.currentTime||0),
        duration:Number.isFinite(v.duration)?v.duration:0, ended:!!v.ended,
        paused:!!v.paused, readyState:v.readyState, error:!!v.error};
    }''')


def _play_video(page):
    # Only user-visible video elements are targeted; no credential/captcha actions.
    page.evaluate('''() => {
      const v=[...document.querySelectorAll('video')].find(x=>x.clientWidth>60&&x.clientHeight>60) || document.querySelector('video');
      if(v) {v.muted=false; v.volume=1; v.play().catch(()=>{});}
    }''')
    for selector in ['[aria-label="Play"]', '[aria-label="Lire"]', 'button[title="Play"]']:
        try:
            loc=page.locator(selector).first
            if loc.count() and loc.is_visible():
                state=_video_state(page)
                if state.get('present') and not state.get('paused'):
                    return
                loc.click(timeout=1600, force=True)
                return
        except Exception:
            continue


def _launch_audio_capture(path:Path):
    # In the browser-enabled Docker image an isolated PulseAudio null sink is
    # configured. No attempt is made to capture system-wide audio on a host.
    sink = os.getenv('COOKNOTE_PULSE_MONITOR', '').strip()
    if not sink:
        return None
    try:
        return subprocess.Popen([
            'ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
            '-f','pulse','-i',sink,'-ac','1','-ar','16000','-c:a','pcm_s16le',str(path)
        ],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _stop_audio(proc):
    if proc is None:return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
    except OSError:pass


def capture_public_video(url:str, progress=None):
    """Watch and sample a public video in an isolated Chromium session.

    Returns a recipe result containing evidence labels or raises ExtractionError
    if playback cannot be observed. Never invents audio/visual evidence.
    """
    if not browser_available():
        raise ExtractionError('Capture navigateur non activée sur ce serveur.', 'navigateur_indisponible')

    from playwright.sync_api import sync_playwright
    from PIL import Image

    def stage(phase, progress_pct, description):
        if progress:progress(phase, progress_pct, description)

    with tempfile.TemporaryDirectory(prefix='cooknote-browser-') as tmp:
        workspace=Path(tmp)
        frames=[]
        audio_path=workspace/'browser-audio.wav'
        stage('opening', 15, 'Ouverture du navigateur virtuel…')
        with sync_playwright() as play:
            executable=os.getenv('COOKNOTE_CHROMIUM_PATH', '').strip()
            options={
                'headless':True,
                'args':['--no-sandbox','--disable-dev-shm-usage',
                        '--autoplay-policy=no-user-gesture-required',
                        '--no-first-run','--disable-background-timer-throttling',
                        '--disable-features=OptimizationHints']
            }
            if executable:options['executable_path']=executable
            browser=play.chromium.launch(**options)
            try:
                context=browser.new_context(
                    record_video_dir=str(workspace),
                    record_video_size={'width':430,'height':850},
                    viewport={'width':430,'height':850},
                    is_mobile=True,has_touch=True,device_scale_factor=1,
                    locale='fr-FR',
                    user_agent='Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36',
                    ignore_https_errors=False,
                    accept_downloads=False,
                    service_workers='block'
                )
                context.route('**/*',lambda route: route.continue_() if _network_allowed(route.request.url) else route.abort())
                page=context.new_page()
                try:
                    page.goto(url,wait_until='domcontentloaded',timeout=45000)
                    page.wait_for_timeout(2000)
                    _play_video(page)
                    started=time.monotonic()
                    last=-1.0
                    advanced=False
                    last_shot=-1000.0
                    frozen=0
                    audio_proc=None
                    caption=''
                    try:
                        caption=page.locator('meta[property="og:description"]').first.get_attribute('content',timeout=1300) or ''
                    except Exception:pass
                    title=page.title()[:130] or 'Recette vidéo'
                    stage('watching', 23, 'Lecture de la vidéo et capture des images…')
                    while time.monotonic()-started < MAX_WATCH_SECONDS:
                        try:
                            state=_video_state(page)
                        except Exception:
                            break
                        t=float(state.get('currentTime') or 0)
                        duration=float(state.get('duration') or 0)
                        if not state.get('present'):
                            page.wait_for_timeout(1300)
                            if time.monotonic()-started>25:break
                            _play_video(page)
                            continue
                        if t>last+0.15 and t>0.25:
                            advanced=True
                            frozen=0
                            if audio_proc is None: audio_proc=_launch_audio_capture(audio_path)
                        else:frozen+=1
                        if advanced and (t-last_shot>=FRAME_INTERVAL) and len(frames)<MAX_FRAMES:
                            try:
                                raw=page.screenshot(type='jpeg',quality=62,timeout=8500)
                                dest=workspace/f'frame-{len(frames):03d}.jpg'
                                dest.write_bytes(raw)
                                frames.append(dest)
                                last_shot=t
                            except Exception:pass
                        if duration>0 and duration<=MAX_WATCH_SECONDS:
                            stage('watching',min(74,25+int(49*t/duration)),f'Lecture vidéo : {int(t)} s / {int(duration)} s')
                        elif int(time.monotonic()-started)%7==0:
                            stage('watching',45,f'Lecture vidéo : {int(t)} secondes')
                        if state.get('ended') or (duration>0 and t>=duration-0.3 and advanced):
                            break
                        if advanced and frozen>12:break
                        if not advanced and time.monotonic()-started>30:break
                        if state.get('paused') and not state.get('ended'):
                            _play_video(page)
                        last=t
                        page.wait_for_timeout(1100)
                finally:
                    _stop_audio(locals().get('audio_proc'))
                    context.close()
            finally:browser.close()

        if not advanced:
            raise ExtractionError('La vidéo ne se lance pas dans le navigateur virtuel (connexion, restriction ou lecteur inaccessible).', 'lecture_bloquee')
        stage('audio',76,'Transcription de la piste audio…')
        evidence=[];warnings=[]
        if caption:
            evidence.append(('légende affichée',caption[:24000]))
        if audio_path.exists() and audio_path.stat().st_size>16000:
            try:
                transcript=audio_transcript(audio_path)
                if transcript:evidence.append(('audio du navigateur',transcript))
                else:warnings.append('L’audio de la vidéo n’a livré aucun texte intelligible.')
            except ExtractionError as exc:warnings.append(str(exc))
        else:warnings.append('Audio non capturé : la lecture visuelle reste exploitable uniquement par OCR.')
        stage('ocr',88,f'Lecture des textes affichés ({len(frames)} captures)…')
        lines=[];seen=set()
        for f in frames:
            try:
                with Image.open(f) as img:
                    img.thumbnail((960,960))
                    text=ocr_image(img)
                for l in text.splitlines():
                    l=re.sub(r'\s+',' ',l).strip()
                    key=l.casefold()
                    if len(l)>=4 and key not in seen:
                        seen.add(key);lines.append(l)
            except (OSError,ExtractionError):continue
        if lines:evidence.append(('captures écran OCR','\n'.join(lines)[:35000]))
        result=merge_evidence(title,urlparse(url).hostname or 'Vidéo',evidence,url=url)
        result['evidence']=[label for label,_ in evidence]
        result['frameCount']=len(frames)
        result['warnings']=warnings
        result['browserWatched']=True
        result['message']=('Vidéo lue dans un navigateur virtuel. '+result['message']+' '+ ' '.join(warnings)).strip()
        stage('finished',100,'Analyse de la vidéo terminée.')
        return result