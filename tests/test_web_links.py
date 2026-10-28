"""Content-aware public URL routing and recipe extraction regressions."""
import json
import pytest
from unittest.mock import patch
import extractor

DELHAIZE = 'https://www.delhaize.be/fr/recettes/recetteDetails/Moules-sauce-poulette/r/2b1964e611e2469985a90072e580219f'

@pytest.mark.parametrize('link,expected', [
    (DELHAIZE,'webpage'),
    ('https://my-recipe-site.example/recipe','webpage'),
    ('https://www.youtube.com/watch?v=dQw4w9WgXcQ','social-video'),
    ('https://vm.tiktok.com/ZGdCK1JFV/','social-video'),
    ('https://example.com/uploads/cooking.MP4?token=abc','direct-media'),
    ('https://cdn.example.com/voice.mp3','direct-media'),
    ('https://example.com/recette.txt','text'),
    ('https://example.com/ingredients.md','text'),
    ('https://example.com/recipe.jsonld','json'),
])
def test_classification(link,expected):
    assert extractor.url_content_kind(link)==expected

def test_delhaize_schema_generates_real_title_ingredients_and_steps():
    recipe={'@context':'https://schema.org','@type':'Recipe','name':'Moules sauce poulette',
            'recipeYield':'4','recipeIngredient':['de farine 25 g','citron 1','poivre','d’ail 1 petite gousse'],
            'recipeInstructions':[{'@type':'HowToStep','text':'Nettoyez les moules.'}]}
    html='<html><head><script type="application/ld+json">'+json.dumps(recipe)+'</script></head><body></body></html>'
    result=extractor.extract_html(html,DELHAIZE)
    assert result['status']=='complete'
    r=result['recipe']
    assert r['title']=='Moules sauce poulette'
    assert r['servings']==4
    assert '25 g de farine' in r['ingredients']
    assert '1 citron' in r['ingredients']
    assert 'poivre' in r['ingredients']
    assert r['steps']==['Nettoyez les moules.']

def test_delhaize_without_steps_is_partial_not_fabricated():
    recipe={'@type':'Recipe','name':'Moules sauce poulette','recipeYield':'4',
        'recipeIngredient':['de farine 25 g','de beurre 25 g'],'recipeInstructions':[]}
    result=extractor.extract_html('<script type="application/ld+json">'+json.dumps(recipe)+'</script>',DELHAIZE)
    assert result['status']=='partial'
    assert result['recipe']['steps']==[]
    assert len(result['recipe']['ingredients'])==2

def test_public_text_document_pipeline(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda u:u)
    monkeypatch.setattr(extractor,'get_public_text',lambda url,max_bytes=0:
        'Salade tomate\nIngrédients\n2 tomates\n1 c à s huile d’olive\nPréparation\nCoupez les tomates.\nMélangez.')
    result=extractor.extract_url('https://example.com/recette.txt')
    assert result['status']=='complete'
    assert result['method']=='Texte public du lien'
    assert result['recipe']['ingredients']
    assert result['recipe']['steps']

def test_json_ld_document_pipeline(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda u:u)
    monkeypatch.setattr(extractor,'get_public_text',lambda url,max_bytes=0:json.dumps({
        '@type':'Recipe','name':'Soupe','recipeIngredient':['2 pommes de terre','1 oignon'],
        'recipeInstructions':[{'text':'Cuire dans l’eau.'}]}))
    result=extractor.extract_url('https://example.com/soupe.json')
    assert result['status']=='complete'
    assert result['recipe']['title']=='Soupe'

def test_direct_media_requires_multimodal_engine(monkeypatch):
    monkeypatch.setattr(extractor,'validate_public_url',lambda u:u)
    with pytest.raises(extractor.ExtractionError,match='moteur audio/vidéo'):
        extractor.extract_url('https://example.com/media/recette.mp4')

def test_no_double_consumption_of_streamed_response(monkeypatch):
    """Reading response.apparent_encoding after streaming previously raised RuntimeError."""
    class FakeResponse:
        status_code=200
        headers={'Content-Type':'text/html; charset=utf-8'}
        encoding='utf-8'
        body=b'<html><h1>Salade</h1></html>'
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,chunk_size=65536):
            yield self.body
        @property
        def apparent_encoding(self):
            raise RuntimeError('The content for this response was already consumed')
    class FakeSession:
        def get(self,*args,**kwargs):
            assert kwargs['allow_redirects'] is False
            return FakeResponse()
    monkeypatch.setattr(extractor,'validate_public_url',lambda u:u)
    text,final=extractor.get_public_html('https://public.example.com/recipe',session=FakeSession())
    assert '<h1>Salade</h1>' in text
    assert final=='https://public.example.com/recipe'