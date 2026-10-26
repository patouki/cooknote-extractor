"""Regression: zero-percent queues, deduplication, cancellation."""
import threading
import time
from unittest.mock import patch
from fastapi.testclient import TestClient
import server

ONE='https://www.instagram.com/reel/DeBvYW5u80z/'
TWO='https://www.instagram.com/reel/AbCdEFgh123/'

def test_no_indefinite_queue():
    with server._jobs_lock:
        server._jobs.clear()
        server._job_futures.clear()
    entered=threading.Event()
    release=threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'status':'complete','recipe':{'title':'Pâtes','ingredients':['100 g pâtes','sel'],'steps':['Cuire les pâtes.']}}
    c=TestClient(server.app)
    try:
        with patch.object(server,'validate_public_url',return_value=ONE), patch.object(server,'extract_checked',side_effect=slow):
            first=c.post('/api/jobs',json={'url':ONE})
            assert first.status_code==202,first.text
            jid=first.json()['jobId']
            assert entered.wait(2),'worker did not start'
            again=c.post('/api/jobs',json={'url':ONE})
            assert again.status_code==202,again.text
            assert again.json()['jobId']==jid
            assert again.json()['reused'] is True
            busy=c.post('/api/jobs',json={'url':TWO})
            assert busy.status_code==503,busy.text
            assert 'déjà occupé' in busy.json()['detail']
            stop=c.post('/api/jobs/'+jid+'/cancel')
            assert stop.status_code==200,stop.text
            assert stop.json()['state']=='cancelled'
            assert c.get('/api/jobs/'+jid).json()['state']=='cancelled'
            blocked=c.post('/api/jobs',json={'url':TWO})
            assert blocked.status_code==503,'running extraction must still block new work'
            release.set()
            future=server._job_futures[jid]
            future.result(timeout=5)
            assert c.get('/api/jobs/'+jid).json()['state']=='cancelled'
            nextjob=c.post('/api/jobs',json={'url':TWO})
            assert nextjob.status_code==202,nextjob.text
            server._job_futures[nextjob.json()['jobId']].result(timeout=5)
    finally:
        release.set()

def test_invalid_job():
    c=TestClient(server.app)
    assert c.post('/api/jobs/wrong/cancel').status_code==404
    assert c.get('/api/jobs/wrong').status_code==404