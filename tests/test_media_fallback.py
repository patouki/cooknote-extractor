"""Preserve already transcribed Facebook media results without spawning Chromium."""
import pytest
import extractor
import job_worker
import multimodal
import browser_capture

@pytest.mark.parametrize("status,evidence", [
    ('partial', ['légende', 'audio Whisper']),
    ('none', ['images OCR']),
    ('partial', ['images OCR','audio Whisper']),
])
def test_downloaded_media_result_does_not_spawn_chromium(monkeypatch, status, evidence):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(browser_capture,'browser_available',lambda:True)
    called={'browser':0}
    def browser(url,progress=None):
        called['browser']+=1
        raise AssertionError('Would needlessly launch chromium after audio/OCR')
    monkeypatch.setattr(browser_capture,'capture_public_video',browser)
    result={'status':status,'evidence':evidence,'recipe':{'title':'Recette à nommer','ingredients':['Sel'],'steps':[]},'warnings':[]}
    monkeypatch.setattr(multimodal,'extract_video_url',lambda url,progress=None:result)
    monkeypatch.setattr(job_worker,'send',lambda *args,**kwargs:None)
    produced=job_worker.extract_video('https://www.facebook.com/share/r/1LuJhpW783/')
    assert produced is result
    assert called['browser']==0
    assert any('Complétez' in warning for warning in produced['warnings'])

def test_downloaded_but_no_recognizable_audio_or_ocr_skips_browser(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(browser_capture,'browser_available',lambda:True)
    monkeypatch.setattr(browser_capture,'capture_public_video',lambda url,progress=None:(_ for _ in ()).throw(AssertionError('Do not spawn Chromium after full media processing')))
    media={'status':'none','evidence':[],'durationSeconds':42,'recipe':{'title':'Recette à nommer','ingredients':[],'steps':[]},'warnings':[]}
    monkeypatch.setattr(multimodal,'extract_video_url',lambda url,progress=None:media)
    assert job_worker.extract_video('https://www.facebook.com/share/r/1LuJhpW783/') is media


def test_browser_fallback_still_attempted_when_metadata_only(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda url:url)
    monkeypatch.setattr(browser_capture,'browser_available',lambda:True)
    monkeypatch.setattr(extractor,'resolve_facebook_video_url',lambda url:url)
    monkeypatch.setattr(multimodal,'extract_video_url',lambda url,progress=None:{'status':'none','evidence':[],'recipe':{'ingredients':[],'steps':[]}})
    result={'status':'complete','recipe':{'title':'Poulet rôti','ingredients':['1 poulet','sel'],'steps':['Cuire le poulet']}}
    calls=[]
    monkeypatch.setattr(browser_capture,'capture_public_video',lambda url,progress=None:(calls.append(url) or result))
    monkeypatch.setattr(job_worker,'send',lambda *args,**kwargs:None)
    assert job_worker.extract_video('https://www.facebook.com/share/r/1LuJhpW783/') is result
    assert len(calls)==1