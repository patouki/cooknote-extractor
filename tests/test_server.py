from __future__ import annotations
import base64
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from server import app, supported_video_url
from extractor import ExtractionError
from multimodal import merge_evidence

@pytest.fixture(autouse=True)
def standard_env(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    monkeypatch.delenv('COOKNOTE_ENV', raising=False)

@pytest.fixture
def client():
    return TestClient(app)


def test_supported_urls():
    assert supported_video_url('https://www.instagram.com/reel/DeBvYW5u80z/?xtok=123')
    assert supported_video_url('https://www.youtube.com/watch?v=J---aiyznGQ')
    assert supported_video_url('https://youtu.be/J---aiyznGQ')
    assert supported_video_url('https://www.youtube.com/shorts/J---aiyznGQ')
    assert supported_video_url('https://www.tiktok.com/@demo/video/7200000000000000000')
    assert not supported_video_url('https://www.instagram.com/accounts/login/')
    assert not supported_video_url('https://youtube.com/redirect?q=http://127.0.0.1')
    assert not supported_video_url('file:///etc/passwd')
    assert not supported_video_url('http://localhost/watch?v=J---aiyznGQ')


def test_landing_health(client):
    home = client.get('/')
    assert home.status_code == 200
    assert 'Cooknote V4.2' in home.text
    assert '/server-bridge.js' in home.text
    assert client.get('/server-bridge.js').status_code == 200
    assert client.get('/ready').status_code == 200
    assert client.get('/api/health').json()['version'] == '4.3'
    # Ensure backend source code is not served to the browser.
    assert client.get('/extractor.py').status_code == 404
    assert client.get('/.env').status_code == 404


def test_social_extract_mock(client):
    example='https://www.instagram.com/reel/DeBvYW5u80z/'
    expected=merge_evidence('Tarte aux pommes','Instagram',[
        ('audio Whisper','200 g de farine, 2 oeufs. Mélanger la pâte. Cuire au four.')],url=example)
    with patch('server.validate_public_url', return_value=example), patch('server.extract_video_url',return_value=expected) as extractor:
        response=client.post('/api/extract',json={'url':example})
    assert response.status_code == 200, response.text
    assert response.json()['recipe']['ingredients']
    extractor.assert_called_once_with(example)


def test_reject_unsafe_video_url(client):
    with patch('server.validate_public_url', return_value='ok'):
        bad=client.post('/api/extract',json={'url':'https://www.instagram.com/accounts/login/'})
    assert bad.status_code == 422
    assert bad.json()['detail']['code'] == 'url_invalide'


def test_private_password_auth(client, monkeypatch):
    monkeypatch.setenv('COOKNOTE_SITE_PASSWORD','not-a-real-password-123')
    assert client.get('/').status_code == 401
    assert client.get('/api/health').status_code == 401
    assert client.get('/ready').status_code == 200
    value = base64.b64encode(b'cooknote:not-a-real-password-123').decode()
    assert client.get('/', headers={'Authorization':f'Basic {value}'}).status_code == 200
    assert client.get('/api/health',headers={'Authorization':f'Basic {value}'}).status_code == 200


def test_reject_other_origins(client):
    response=client.post('/api/extract',json={'url':'https://www.instagram.com/reel/DeBvYW5u80z/'},headers={'Origin':'https://attacker.example'})
    assert response.status_code == 403


def test_media_upload_mock(client):
    expected=merge_evidence('Crêpes','Vidéo', [('images OCR','200 g de farine\n2 oeufs\nMélanger les ingrédients.')])
    with patch('server.process_media_file',return_value=expected):
        resp=client.post('/api/media',files={'media':('crepes.mp4',b'fake video contents','video/mp4')})
    assert resp.status_code == 200, resp.text
    assert resp.json()['status']=='complete'
    assert client.post('/api/media',files={'media':('code.sh',b'x','text/plain')}).status_code==415
    assert client.post('/api/media',files={'media':('a.mp4',b'','video/mp4')}).status_code==422


def test_busy_gate(client):
    from server import _gate
    assert _gate.acquire(False)
    try:
        res=client.post('/api/extract',json={'url':'https://www.instagram.com/reel/DeBvYW5u80z/'})
        assert res.status_code==503
    finally:
        _gate.release()


def test_web_only_server_production_password_required(monkeypatch):
    import server
    monkeypatch.setenv('COOKNOTE_ENV','production')
    monkeypatch.setenv('COOKNOTE_SITE_PASSWORD','short')
    with pytest.raises(RuntimeError):server._require_secure_config()


def test_real_video_frame_ocr_without_whisper(client):
    """End-to-end local sample: real FFmpeg/Tesseract, not mocked OCR."""
    import shutil
    if not all(shutil.which(name) for name in ('ffmpeg','ffprobe','tesseract')):
        pytest.skip('FFmpeg ou Tesseract non installés dans cet environnement')
    sample=Path(__file__).resolve().parents[1]/'examples/demo-video.mp4'
    with patch('multimodal.audio_transcript',side_effect=ExtractionError('Whisper non chargé','dependance')):
        response=client.post('/api/media',files={'media':('video-recette.mp4',sample.read_bytes(),'video/mp4')})
    assert response.status_code==200, response.text
    recipe=response.json()['recipe']
    assert len(recipe['ingredients'])==4, recipe
    assert len(recipe['steps'])==3, recipe
    assert any('pâtes' in i for i in recipe['ingredients'])