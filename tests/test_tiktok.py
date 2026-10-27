"""TikTok short-link + public caption fallback without Chromium or guessed steps."""
import io
import json
from unittest.mock import patch

import pytest
import extractor
import multimodal
import job_worker

SHORT='https://vm.tiktok.com/ZGdCK1JFV/'
DIRECT='https://www.tiktok.com/@benjaminfurno/video/7634970970989595926'

CAPTION=("Poulet Marry me 👰🏻‍♀️🍗 - 6 hauts de cuisse de poulet - 60g de farine "
         "- Huile + beurre - 3 échalotes - 200ml de bouillon de poulet "
         "- 300ml de crème liquide 30% - 3 cas de paprika doux "
         "- 1 cas de piment doux - 1 cas d’herbes de Provence - Poivre "
         "- 3 tomates séchées - 3 gousses d’ail - 50g de parmesan - Persil")

class FakeResponse:
    def __init__(self,status=200,redirect='',content=b''):
        self.status_code=status
        self.headers={'Content-Type':'text/html', 'Location':redirect}
        self.content=content
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def iter_content(self,chunk_size=32768):
        yield self.content

class FakeSession:
    def __init__(self,responses):
        self.responses=iter(responses)
        self.urls=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def get(self,url,**kwargs):
        assert kwargs.get('allow_redirects') is False, 'no unchecked redirects'
        assert kwargs.get('stream') is True
        self.urls.append(url)
        return next(self.responses)

def fake_html(desc=CAPTION,identifier='7634970970989595926',status=0):
    data={'__DEFAULT_SCOPE__':{'webapp.video-detail':{
        'statusCode':status,
        'itemInfo':{'itemStruct':{'id':identifier,'desc':desc,'video':{'duration':62}}},
    }}}
    return ('<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'+json.dumps(data,ensure_ascii=False)+'</script></html>').encode()

def test_short_link_uses_canonical_tiktok_video(monkeypatch):
    fake=FakeSession([FakeResponse(301,DIRECT+'?_r=1&_t=xx')])
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
    assert extractor.resolve_tiktok_video_url(SHORT)==DIRECT
    assert fake.urls==[SHORT]

@pytest.mark.parametrize('target',[
  'https://www.evil.example/private',
  'http://127.0.0.1:8123/internal',
  'file:///etc/passwd',
  'https://www.tiktok.com/login',
])
def test_short_link_rejects_external_or_nonvideo_redirect(monkeypatch,target):
    fake=FakeSession([FakeResponse(301,target),FakeResponse(200)])
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
    assert extractor.resolve_tiktok_video_url(SHORT)==SHORT

def test_public_tiktok_caption_reads_item_without_media(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    fake=FakeSession([FakeResponse(content=fake_html())])
    monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
    info=extractor.tiktok_public_caption(DIRECT)
    assert '6 hauts de cuisse' in info['caption']
    assert '\n- 60g de farine' in info['caption']
    assert info['duration']==62
    assert info['id']=='7634970970989595926'

def test_private_or_mismatched_tiktok_data_ignored(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    for body in (fake_html(identifier='1234567890'),fake_html(status=10204)):
        fake=FakeSession([FakeResponse(content=body)])
        monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
        assert extractor.tiktok_public_caption(DIRECT)=={}

def test_invalid_tiktok_source_never_requested(monkeypatch):
    monkeypatch.setattr(extractor.requests,'Session',
       lambda:(_ for _ in ()).throw(AssertionError('No external URL allowed')))
    assert extractor.tiktok_public_caption('https://www.evil.example/abc')=={}

def test_metadata_failure_returns_real_caption_recipe(monkeypatch):
    monkeypatch.setattr(multimodal,'validate_public_url',lambda url:url)
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(extractor,'resolve_tiktok_video_url',lambda url:DIRECT)
    monkeypatch.setattr(extractor,'tiktok_public_caption',lambda url:{
        'caption':CAPTION.replace(' - ','\n- '),'cover':'','duration':62})
    class YDL:
        def __init__(self,opts):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):raise RuntimeError('blocked media')
    import yt_dlp
    monkeypatch.setattr(yt_dlp,'YoutubeDL',YDL)
    checkpoints=[]
    result=multimodal.extract_video_url(SHORT,checkpoint=checkpoints.append)
    assert result['status']=='partial'
    assert len(result['recipe']['ingredients'])>=10
    assert 'paprika' in ' '.join(result['recipe']['ingredients']).lower()
    assert result['recipe']['steps']==[]
    assert result['sourceUrl']==SHORT
    assert checkpoints

def test_tiktok_free_instance_skips_chromium(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(job_worker,'low_memory_instance',lambda:True)
    monkeypatch.setattr(job_worker,'send',lambda *args,**kwargs:None)
    import browser_capture
    monkeypatch.setattr(browser_capture,'browser_available',lambda:True)
    monkeypatch.setattr(browser_capture,'capture_public_video',lambda *args,**kwargs:
        (_ for _ in ()).throw(AssertionError('Chromium should not run on 512 MB TikTok fallback')))
    data={'status':'partial','evidence':['légende'],'recipe':{
      'title':'Poulet Marry me','ingredients':['6 hauts de cuisse de poulet'],'steps':[]},'warnings':[]}
    monkeypatch.setattr(multimodal,'extract_video_url',lambda url,progress=None,checkpoint=None:data)
    assert job_worker.extract_video(SHORT) is data

def test_tiktok_cloud_denial_is_explained(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(job_worker,'low_memory_instance',lambda:True)
    monkeypatch.setattr(job_worker,'send',lambda *args,**kwargs:None)
    import browser_capture
    monkeypatch.setattr(browser_capture,'browser_available',lambda:True)
    monkeypatch.setattr(multimodal,'extract_video_url',lambda *args,**kwargs:
        (_ for _ in ()).throw(extractor.ExtractionError('TikTok cloud blocked')))
    with pytest.raises(extractor.ExtractionError,match='TikTok limite'):
        job_worker.extract_video(SHORT)

def test_worker_sigkill_recovers_checkpoint_if_ingredients_exist(monkeypatch):
    import server
    import datetime
    monkeypatch.setattr(server, '_terminate_worker', lambda proc: None)
    recipe={'status':'partial','recipe':{'title':'Poulet Marry Me',
         'ingredients':['6 hauts de cuisse de poulet','60g de farine'],'steps':[]},
         'evidence':['légende'],'warnings':[]}
    event=server._JOB_PREFIX+json.dumps({'type':'checkpoint','result':recipe})+'\n'
    class KilledProc:
        pid=12345
        returncode=-9
        stdout=io.StringIO(event)
        def poll(self):return -9
    monkeypatch.setattr(server.subprocess,'Popen',lambda *args,**kwargs:KilledProc())
    jid='a'*32
    with server._jobs_lock:
        server._jobs[jid]={'jobId':jid,'state':'queued','phase':'queued',
                           'percent':0,'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    server._isolated_job_work(jid,SHORT)
    actual=server._jobs[jid]
    assert actual['state']=='completed'
    assert actual['result']['status']=='partial'
    assert len(actual['result']['recipe']['ingredients'])==2
    assert 'interrompue' in ' '.join(actual['result']['warnings'])


def test_worker_sigkill_without_recipe_has_specific_memory_error(monkeypatch):
    import server
    import datetime
    monkeypatch.setattr(server,'_terminate_worker',lambda proc:None)
    class KilledProc:
        pid=12345
        returncode=-9
        stdout=io.StringIO('')
        def poll(self):return -9
    monkeypatch.setattr(server.subprocess,'Popen',lambda *args,**kwargs:KilledProc())
    jid='b'*32
    with server._jobs_lock:
        server._jobs[jid]={'jobId':jid,'state':'queued','phase':'queued',
                           'percent':0,'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    server._isolated_job_work(jid,SHORT)
    actual=server._jobs[jid]
    assert actual['state']=='failed'
    assert actual['error']['code']=='worker_memory'
    assert 'mémoire' in actual['error']['message']
