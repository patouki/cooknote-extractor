"""Facebook Reel/share-link support and safe public canonical URL handling."""
from unittest.mock import patch
import io
import pytest
import extractor
import server

SHARE = "https://www.facebook.com/share/r/1LuJhpW783/"
REEL = "https://www.facebook.com/reel/123456789012345/"
WATCH = "https://www.facebook.com/watch/?v=123456789012345"
SHORT = "https://fb.watch/1LuJhpW783/"
VIDEO = "https://m.facebook.com/videos/123456789012345/"

@pytest.mark.parametrize("url", [SHARE, REEL, WATCH, SHORT, VIDEO,
    "https://www.facebook.com/share/v/1LuJhpW783/",
    "https://www.facebook.com/share/p/1LuJhpW783/",
    "https://www.facebook.com/reels/123456789012345/",
    "https://www.facebook.com/cooking/videos/123456789012345/"])
def test_facebook_urls_are_classified_as_video(url):
    assert extractor.is_social(url), url
    assert server.supported_video_url(url), url

@pytest.mark.parametrize("url", [
    "https://www.facebook.com/groups/12345/",
    "https://www.facebook.com/private",
    "https://facebook.com/share/r/../../api",
    "https://facebook.com/evil/path",
    "https://www.facebook.com:444/reel/12345678901/",
    "https://www.faceb00k.com/share/r/1LuJhpW783/",
])
def test_non_video_or_unsafe_urls_rejected(url):
    assert not server.supported_video_url(url), url

def test_original_shared_reel_is_routed_to_video_engine(monkeypatch):
    monkeypatch.setattr(server,'validate_public_url',lambda x:x)
    called=[]
    def video(url, progress=None):
        called.append(url)
        return {'status':'none','sourceUrl':url}
    monkeypatch.setattr(server,'extract_video_url',video)
    outcome=server.extract_checked(SHARE)
    assert called == [SHARE]
    assert outcome['sourceUrl'] == SHARE

class FakeResponse:
    def __init__(self,status,location='',html=''):
        self.status_code=status
        self.headers={'Content-Type':'text/html'}
        if location:
            self.headers['Location']=location
        self.html=html
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def iter_content(self,chunk_size=16384):
        yield self.html.encode('utf-8')

class FakeSession:
    def __init__(self,responses):
        self.responses=list(responses)
        self.visited=[]
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def get(self,url,**kwargs):
        self.visited.append(url)
        assert kwargs.get('allow_redirects') is False
        assert kwargs.get('stream') is True
        return self.responses.pop(0)

def test_facebook_share_redirect_resolves_to_direct_reel(monkeypatch):
    fake=FakeSession([FakeResponse(302,location="/reel/123456789012345/"),
                      FakeResponse(200,html='<meta property="og:url" content="https://www.facebook.com/reel/123456789012345/">')])
    monkeypatch.setattr(extractor,'validate_public_url',lambda x:x)
    monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
    assert extractor.resolve_facebook_video_url(SHARE) == REEL
    assert len(fake.visited)==2

def test_facebook_400_keeps_source_url(monkeypatch):
    fake=FakeSession([FakeResponse(400)])
    monkeypatch.setattr(extractor,'validate_public_url',lambda x:x)
    monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
    assert extractor.resolve_facebook_video_url(SHARE)==SHARE

def test_facebook_redirect_to_external_domain_not_followed(monkeypatch):
    fake=FakeSession([FakeResponse(302,location="http://127.0.0.1/admin")])
    monkeypatch.setattr(extractor,'validate_public_url',lambda x:x)
    monkeypatch.setattr(extractor.requests,'Session',lambda:fake)
    assert extractor.resolve_facebook_video_url(SHARE)==SHARE
    assert len(fake.visited)==1

def test_direct_reel_does_not_call_facebook_redirect_resolver_network(monkeypatch):
    def never():
        raise AssertionError("No session must be opened for canonical URLs")
    monkeypatch.setattr(extractor.requests,'Session',never)
    assert extractor.resolve_facebook_video_url(REEL)==REEL

def test_non_facebook_unchanged():
    assert extractor.resolve_facebook_video_url('https://www.youtube.com/watch?v=dQw4w9WgXcQ') == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'