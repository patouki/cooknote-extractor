"""Optional browser smoke test (requires Playwright + Chromium)."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from contextlib import contextmanager
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PORT='18762'


@contextmanager
def local_server():
    proc=subprocess.Popen([sys.executable,'-m','uvicorn','server:app','--host','127.0.0.1','--port',PORT],
                          cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, env={**os.environ,'COOKNOTE_ENV':'development'})
    try: yield proc
    finally:
        proc.terminate()
        try:proc.wait(timeout=4)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)

def test_automatic_import_from_reel():
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':385,'height':840}, device_scale_factor=1)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        output={'status':'complete','method':'audio Whisper + images OCR','sourceUrl':'https://www.instagram.com/reel/DeBvYW5u80z/',
                'recipe':{'title':'Crêpes moelleuses','servings':4,'minutes':25,
                          'ingredients':['200 g de farine','2 oeufs','150 ml de lait'],
                          'steps':['Mélanger les ingrédients.','Cuire les crêpes à la poêle.'],
                          'description':''},'message':'Transcription réussie et texte visuel lu.'}
        calls=[]
        def mock_route(route):
            calls.append(route.request.post_data_json)
            route.fulfill(status=200,content_type='application/json',body=json.dumps(output))
        page.goto('about:blank')
        markup=(ROOT/'web/index.html').read_text().replace('<script src="/server-bridge.js" defer></script>','')
        page.evaluate('(text)=>{document.open();document.write(text);document.close()}',markup)
        page.evaluate('payload => { window.__v42Requests=[]; window.fetch=(url,options) => {window.__v42Requests.push({url,body:JSON.parse(options.body)});return Promise.resolve(new Response(JSON.stringify(payload),{status:200,headers:{"Content-Type":"application/json"}}));};}',output)
        page.add_script_tag(path=str(ROOT/'web/server-bridge.js'))
        assert 'Cooknote V4.2' in page.title()
        page.locator('[data-action="open-import"]').last.click()
        page.locator('#import-url').fill('https://www.instagram.com/reel/DeBvYW5u80z/?xtok=secret')
        page.locator('#add-url-form button[type="submit"]').click()
        page.get_by_text('Vérifier la recette', exact=True).wait_for(timeout=12000)
        calls=page.evaluate('window.__v42Requests')
        assert len(calls)==1, calls
        assert calls[0]['body']['url'].startswith('https://www.instagram.com/reel/')
        assert page.locator('#bv4-review-form [name="title"]').input_value()=='Crêpes moelleuses'
        assert '200 g de farine' in page.locator('#bv4-review-form [name="ingredients"]').input_value()
        page.locator('#bv4-review-form button[type="submit"]').click()
        page.get_by_text('Crêpes moelleuses').first.wait_for(timeout=7000)
        assert not errors, errors
        browser.close()

if __name__=='__main__':
    test_automatic_import_from_reel()
    print('Test navigateur V4.2 : OK')