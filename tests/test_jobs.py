"""Async jobs, concurrency, expiry and response schema."""
from __future__ import annotations
import time
from unittest.mock import patch
from fastapi.testclient import TestClient
import server


def setup_function():
    with server._jobs_lock:
        server._jobs.clear()


def _wait(client, jid):
    for i in range(45):
        result=client.get('/api/jobs/'+jid)
        assert result.status_code==200
        job=result.json()
        if job['state'] in ('completed','failed'):
            return job
        time.sleep(.05)
    assert False,'Job did not finish'


def test_async_success(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    recipe={'status':'complete','recipe':{'title':'Brioche','ingredients':['2 oeufs','200 g farine'],'steps':['Mélanger.']}}
    with patch.object(server,'validate_public_url',return_value=True), patch.object(server,'extract_checked',return_value=recipe):
        c=TestClient(server.app)
        response=c.post('/api/jobs',json={'url':'https://www.marmiton.org/recettes/recette_quiche-lorraine_30258.aspx'})
        assert response.status_code==202
        jid=response.json()['jobId']
        assert len(jid)==32
        final=_wait(c,jid)
        assert final['state']=='completed'
        assert final['result']['recipe']['title']=='Brioche'
        assert final['percent']==100


def test_async_failure(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    with patch.object(server,'validate_public_url',return_value=True), patch.object(server,'extract_checked',side_effect=server.ExtractionError('Erreur de source','acces_refuse')):
        c=TestClient(server.app)
        r=c.post('/api/jobs',json={'url':'https://www.marmiton.org/recettes/recette_quiche-lorraine_30258.aspx'})
        assert r.status_code==202
        final=_wait(c,r.json()['jobId'])
        assert final['state']=='failed'
        assert final['error']['code']=='acces_refuse'


def test_bad_job_id(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    c=TestClient(server.app)
    assert c.get('/api/jobs/../../sensitive').status_code in (404, 307)
    assert c.get('/api/jobs/not-an-id').status_code==404


def test_missing_password(monkeypatch):
    monkeypatch.setenv('COOKNOTE_SITE_PASSWORD','this-is-a-test-only-password')
    c=TestClient(server.app)
    assert c.post('/api/jobs',json={'url':'https://www.instagram.com/reel/DeBvYW5u80z/'}).status_code==401
    assert c.get('/api/jobs/abcdefabcdefabcdefabcdefabcdefab').status_code==401


def test_browser_url_safety():
    from browser_capture import _network_allowed
    assert not _network_allowed('http://127.0.0.1')
    assert not _network_allowed('http://169.254.169.254')
    assert not _network_allowed('file:///etc/passwd')
    assert not _network_allowed('http://localhost:3000')
    assert _network_allowed('https://www.instagram.com/reel/DeBvYW5u80z/')