"""Regression tests for duplicate imports, wait expiry and cancellation."""
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from fastapi.testclient import TestClient
import server


def setup_function():
    with server._jobs_lock:
        server._jobs.clear()
        server._job_processes.clear()


def client():
    return TestClient(server.app)


def test_duplicate_same_url_does_not_queue_twice(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    url = 'https://www.instagram.com/reel/DeBvYW5u80z/'
    with patch.object(server, 'validate_public_url', return_value=url), \
         patch.object(server._job_executor, 'submit', return_value=None):
        c = client()
        first = c.post('/api/jobs', json={'url':url})
        second = c.post('/api/jobs', json={'url':url})
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()['jobId'] == second.json()['jobId']
    assert second.json()['reused'] is True
    assert len(server._jobs) == 1


def test_queue_position_and_expiry(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    with patch.object(server, 'validate_public_url', return_value=True), \
         patch.object(server._job_executor, 'submit', return_value=None):
        c = client()
        first = c.post('/api/jobs', json={'url':'https://marmiton.org/a'}).json()
        second = c.post('/api/jobs', json={'url':'https://marmiton.org/b'}).json()
        assert first['queuePosition'] == 1
        assert second['queuePosition'] == 2
        with server._jobs_lock:
            server._jobs[second['jobId']]['createdAt'] = (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        expired = c.get('/api/jobs/'+second['jobId']).json()
        assert expired['state'] == 'failed'
        assert expired['error']['code'] == 'queue_timeout'


def test_cancel_queued_then_start_new_job(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    url = 'https://www.instagram.com/reel/DeBvYW5u80z/'
    with patch.object(server, 'validate_public_url', return_value=url), \
         patch.object(server._job_executor, 'submit', return_value=None):
        c = client()
        first = c.post('/api/jobs', json={'url':url}).json()
        cancelled = c.post('/api/jobs/'+first['jobId']+'/cancel')
        assert cancelled.status_code == 200
        assert cancelled.json()['state'] == 'cancelled'
        again = c.post('/api/jobs', json={'url':url}).json()
        assert again['jobId'] != first['jobId']


def test_isolated_success_propagates_to_api(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    jid='a'*32
    recipe={'status':'complete','recipe':{'title':'Pâtes au pesto','ingredients':['Pâtes'], 'steps':['Cuire.']}}
    line=server._JOB_PREFIX+json.dumps({'type':'result','result':recipe})+'\n'
    class FakePopen:
        def __init__(self,*args,**kwargs):
            self.pid=123456
            self.stdout=io.StringIO(line)
        def poll(self):return 0
        def wait(self,timeout=None):return 0
    with server._jobs_lock:
        server._jobs[jid]={'jobId':jid,'state':'queued','phase':'queued','percent':0,
         'message':'', 'createdAt':datetime.now(timezone.utc).isoformat(),
         'updatedAt':datetime.now(timezone.utc).isoformat(),'result':None,'error':None}
    with patch.object(server.subprocess,'Popen',FakePopen),patch.object(server,'_terminate_worker',return_value=None):
        server._isolated_job_work(jid,'https://marmiton.org/a')
    assert server._jobs[jid]['state']=='completed'
    assert server._jobs[jid]['result']['recipe']['title']=='Pâtes au pesto'


def test_running_job_timeout_releases_worker(monkeypatch):
    monkeypatch.delenv('COOKNOTE_SITE_PASSWORD', raising=False)
    jid='b'*32
    class StuckPopen:
        def __init__(self,*args,**kwargs):
            self.pid=987654
            self.stdout=io.StringIO('')
            self.stopped=False
        def poll(self):return 0 if self.stopped else None
        def wait(self,timeout=None):return 0
    proc=[]
    def build(*args,**kwargs):
        x=StuckPopen();proc.append(x);return x
    def kill(x):x.stopped=True
    with server._jobs_lock:
        server._jobs[jid]={'jobId':jid,'state':'queued','phase':'queued','percent':0,
         'message':'','createdAt':datetime.now(timezone.utc).isoformat(),
         'updatedAt':datetime.now(timezone.utc).isoformat(),'result':None,'error':None}
    with patch.object(server.subprocess,'Popen',side_effect=build), \
         patch.object(server,'_terminate_worker',side_effect=kill), \
         patch.object(server,'_MAX_JOB_RUNTIME',0.03):
        server._isolated_job_work(jid,'https://marmiton.org/a')
    assert proc and proc[0].stopped
    assert server._jobs[jid]['state']=='failed'
    assert server._jobs[jid]['error']['code']=='job_timeout'