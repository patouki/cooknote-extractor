"""FIFO, progress positions, capacity, cancellation and queue wait regression tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
import server

URLS = [
    'https://www.marmiton.org/recettes/recette_quiche-lorraine_30258.aspx?test='+str(i)
    for i in range(30)
]

@pytest.fixture
def pending(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    monkeypatch.setattr(server, 'validate_public_url', lambda _: True)
    # Fake executor captures the FIFO submission instead of running expensive media.
    submitted=[]
    monkeypatch.setattr(server._job_executor, 'submit',
                        lambda callback, jid, url: submitted.append((callback,jid,url)))
    with server._jobs_lock:
        server._jobs.clear()
    with server._rate_lock:
        server._hits.clear()
    yield TestClient(server.app),submitted
    with server._jobs_lock:
        server._jobs.clear()

def test_capacity_expanded_and_one_worker(pending):
    client, submitted=pending
    assert server._MAX_QUEUED >= 10
    assert server._MAX_QUEUE_WAIT >= 1800
    for i in range(5):
        x=client.post('/api/jobs',json={'url':URLS[i]})
        assert x.status_code == 202
        assert x.json()['queuePosition'] == i+1
        assert x.json()['queueAhead'] == i
        assert x.json()['parallelJobs'] == 1
        assert x.json()['queueLength'] == i+1
    assert [u for _,_,u in submitted] == URLS[:5]

def test_position_accounts_for_current_running_job(pending):
    client,submitted=pending
    ids=[client.post('/api/jobs',json={'url':url}).json()['jobId'] for url in URLS[:4]]
    server._job_update(ids[0],'running','audio',58,'Transcription Whisper')
    first=client.get('/api/jobs/'+ids[1]).json()
    last=client.get('/api/jobs/'+ids[3]).json()
    assert first['queuePosition']==1
    assert first['queueAhead']==1
    assert last['queuePosition']==3
    assert last['queueAhead']==3
    assert first['queueBusy']
    assert first['queueLength']==3

def test_cancelled_queue_item_releases_place(pending):
    client,submitted=pending
    ids=[client.post('/api/jobs',json={'url':url}).json()['jobId'] for url in URLS[:3]]
    canceled=client.post('/api/jobs/'+ids[0]+'/cancel')
    assert canceled.status_code==200
    assert canceled.json()['state']=='cancelled'
    second=client.get('/api/jobs/'+ids[1]).json()
    third=client.get('/api/jobs/'+ids[2]).json()
    assert second['queuePosition']==1
    assert third['queuePosition']==2
    assert second['queueAhead']==0

def test_waiting_longer_than_old_105s_not_expired(pending):
    client,submitted=pending
    job=client.post('/api/jobs',json={'url':URLS[0]}).json()
    with server._jobs_lock:
        server._jobs[job['jobId']]['createdAt']=(datetime.now(timezone.utc)-timedelta(seconds=125)).isoformat()
    status=client.get('/api/jobs/'+job['jobId']).json()
    assert status['state']=='queued'
    assert status['queuePosition']==1

def test_repeat_same_link_not_duplicated(pending):
    client,submitted=pending
    first=client.post('/api/jobs',json={'url':URLS[0]}).json()
    second=client.post('/api/jobs',json={'url':URLS[0]}).json()
    assert first['jobId']==second['jobId']
    assert second['reused']
    assert len(submitted)==1

def test_over_capacity_response_is_explicit(pending,monkeypatch):
    client,submitted=pending
    monkeypatch.setattr(server,'_MAX_QUEUED',3)
    for url in URLS[:3]:
        assert client.post('/api/jobs',json={'url':url}).status_code==202
    extra=client.post('/api/jobs',json={'url':URLS[3]})
    assert extra.status_code==503
    assert 'pleine' in extra.json()['detail']