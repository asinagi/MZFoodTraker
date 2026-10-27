"""Real-browser audit QA; synthetic failure is isolated in a copied temporary DB."""
import base64
import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen
import websocket

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from food_trends.api import make_server
from food_trends.storage import Storage
from food_trends.service import CollectionService

OUTPUT=Path('data/audit-20261026')

class Browser:
    def __init__(self):
        pages=json.load(urlopen('http://127.0.0.1:9225/json'))
        page=next(p for p in pages if p['type']=='page')
        self.ws=websocket.create_connection(page['webSocketDebuggerUrl'],origin='http://localhost:9225',timeout=15)
        self.sequence=0
        self.errors=[]
        self.call('Runtime.enable',{})

    def call(self,method,params):
        self.sequence+=1
        self.ws.send(json.dumps({'id':self.sequence,'method':method,'params':params}))
        while True:
            response=json.loads(self.ws.recv())
            if response.get('method')=='Runtime.exceptionThrown':
                self.errors.append(response)
            if response.get('id')==self.sequence:
                if 'error' in response:
                    raise RuntimeError(response['error'])
                return response.get('result',{})

    def evaluate(self,expression):
        result=self.call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'])
        return result['result'].get('value')

    def wait(self,expression,timeout=45):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if self.evaluate(expression):
                return
            time.sleep(.15)
        raise AssertionError(expression)

    def visit(self,url):
        self.call('Page.navigate',{'url':url})
        self.wait("document.getElementById('recent-summary')?.children.length === 5")

    def filter(self,value):
        self.evaluate("document.getElementById('filter').value="+json.dumps(value)+"; refresh()")

    def screenshot(self,name):
        result=self.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
        (OUTPUT/name).write_bytes(base64.b64decode(result['data']))


def main():
    browser=Browser()
    base='http://127.0.0.1:8000'
    browser.visit(base)
    browser.wait("document.getElementById('collect').disabled === false")
    assert browser.evaluate("document.getElementById('filter').value")=='FOOD'
    assert browser.evaluate("document.getElementById('rows').textContent.includes('음식 후보가 발견되지 않았습니다')")
    browser.screenshot('food-empty-desktop.png')
    browser.filter('ALL')
    browser.wait("document.querySelectorAll('tr[data-id]').length > 0")
    expected=json.load(urlopen(base+'/api/trends'))['items']
    visible=browser.evaluate("Array.from(document.querySelectorAll('tr[data-id] .keyword')).map(x=>x.textContent)")
    assert visible==[r['keyword'] for r in expected]
    browser.evaluate("document.getElementById('search').value='광주송정역'; refresh()")
    browser.wait("document.querySelectorAll('tr[data-id]').length===1")
    assert browser.evaluate("document.querySelector('tr[data-id]').textContent.includes('NON_FOOD')")
    browser.evaluate("document.querySelector('.keyword').click()")
    browser.wait("document.getElementById('detail').open")
    text=browser.evaluate("document.getElementById('detail-body').textContent")
    for word in ['LOCATION','target-context-rules-v2','규칙 점수','분류 시각','광주 송정역','<item']:
        assert word in text,word
    browser.screenshot('location-detail-desktop.png')
    browser.evaluate("document.getElementById('close').click(); document.getElementById('search').value=''; document.getElementById('filter').value='REVIEW'; refresh()")
    browser.wait("document.querySelectorAll('tr[data-id]').length===1 && document.querySelector('.keyword').textContent==='호박'")
    browser.screenshot('review-desktop.png')
    browser.filter('NON_FOOD')
    browser.wait("document.querySelectorAll('tr[data-id]').length>200")
    exported=browser.evaluate("fetch('/api/export').then(r=>r.json()).then(x=>({items:x.items.length,runs:x.runs.length,raw:x.runs.some(r=>!!r.raw_response_base64),history:x.classification_history.length,instagram:!!x.instagram_state}))")
    assert exported['items']==len(expected) and exported['raw'] and exported['history']>=240 and not exported['instagram']
    before=json.load(urlopen(base+'/api/status'))
    browser.evaluate("document.getElementById('collect').click()")
    browser.wait("document.getElementById('collect').disabled===true")
    browser.wait("document.getElementById('collect').disabled===false",60)
    after=json.load(urlopen(base+'/api/status'))
    assert after['latest_run']['id']>before['latest_run']['id']
    assert after['latest_run']['success']==1
    assert after['total_keywords']-before['total_keywords']==after['latest_run']['new_count']
    # Deliberate failure uses real source records copied into a TEMPORARY database.
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'failure-preview.sqlite3'
        source=sqlite3.connect('data/trends.sqlite3'); destination=sqlite3.connect(path)
        source.backup(destination);source.close();destination.close()
        store=Storage.create(path)
        def fail(_):
            raise TimeoutError('QA ONLY: simulated offline, not a live RSS observation')
        service=CollectionService(store,fetcher=fail)
        service.collect()
        server=make_server(service,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            browser.visit(f'http://127.0.0.1:{server.server_port}')
            assert browser.evaluate("document.getElementById('status').classList.contains('error')")
            assert browser.evaluate("document.getElementById('status').textContent.includes('수집 실패')")
            assert browser.evaluate("document.getElementById('recent-summary').textContent.includes('확인 불가')")
            browser.filter('ALL')
            browser.wait("document.querySelectorAll('tr[data-id]').length>200")
            browser.screenshot('isolated-offline-qa.png')
        finally:
            server.shutdown();server.server_close();thread.join()
    browser.visit(base)
    browser.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
    browser.screenshot('food-empty-mobile.png')
    browser.call('Emulation.clearDeviceMetricsOverride',{})
    browser.screenshot('final-desktop.png')
    assert not browser.errors,browser.errors
    report={'real_ui':'passed','default_empty_state':'passed','non_food_filter':'passed','review_filter':'passed',
            'location_detail':'passed','export':exported,'manual_collection':after['latest_run'],
            'simulated_offline_isolated_db':'passed','browser_exceptions':browser.errors}
    (OUTPUT/'browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    browser.ws.close()

if __name__=='__main__':
    main()
