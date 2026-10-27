"""Optional real Chrome QA; requires websocket-client only in the QA interpreter.

Run with the real app on port 8000 and headless Chrome CDP on 9224.
Mock UI tests create their own temporary DB and never alter the app's Instagram data.
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen
import websocket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from food_trends.api import make_server
from food_trends.config import InstagramConfig
from food_trends.instagram_graph import GraphClient
from food_trends.instagram_service import InstagramService
from food_trends.service import CollectionService
from food_trends.storage import Storage
from test_mvp import fixture
from test_instagram import Transport, FAKE_TOKEN


class Browser:
    def __init__(self):
        pages=json.load(urlopen('http://127.0.0.1:9224/json'))
        page=next(p for p in pages if p['type']=='page')
        self.ws=websocket.create_connection(page['webSocketDebuggerUrl'], origin='http://localhost:9224', timeout=10)
        self.sequence=0
        self.errors=[]
        self.call('Runtime.enable',{})

    def call(self,method,params):
        self.sequence+=1
        self.ws.send(json.dumps({'id':self.sequence,'method':method,'params':params}))
        while True:
            message=json.loads(self.ws.recv())
            if message.get('method')=='Runtime.exceptionThrown':
                self.errors.append(message['params']['exceptionDetails'].get('text','JavaScript error'))
            if message.get('id')==self.sequence:
                if 'error' in message:
                    raise RuntimeError(message['error'])
                return message.get('result',{})

    def js(self,expression):
        result=self.call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'])
        return result['result'].get('value')

    def wait(self,expression,timeout=15):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if self.js(expression):
                return
            time.sleep(.1)
        raise AssertionError(expression)

    def navigate(self,base):
        self.call('Page.navigate',{'url':base})
        self.wait("document.getElementById('summary')?.children.length===5")
        self.js("document.getElementById('filter').value='ALL';document.getElementById('filter').dispatchEvent(new Event('change'))")
        self.wait("document.querySelectorAll('tr[data-id]').length>0")

    def screenshot(self,path,width=1280,height=1000,selector=None):
        self.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':width<600})
        if selector:
            self.js('document.querySelector('+json.dumps(selector)+').scrollIntoView({block:"start"})')
        result=self.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
        Path(path).write_bytes(base64.b64decode(result['data']))


def main():
    Path('data').mkdir(exist_ok=True)
    browser=Browser()
    report={}
    real=os.getenv('BROWSER_APP_URL','http://127.0.0.1:8000')
    browser.navigate(real)
    browser.wait("document.getElementById('instagram-overview').textContent.includes('미연결')")
    browser.js("document.querySelector('.keyword').click()")
    browser.wait("document.querySelector('#instagram-detail .ig-approval')!==null")
    assert browser.js("document.querySelector('#instagram-detail button[type=submit]').disabled")
    assert browser.js("document.getElementById('detail-body').textContent.includes('<item')")
    browser.js("document.getElementById('instagram-detail').scrollIntoView()")
    browser.screenshot('data/instagram-unconfigured-desktop.png')
    browser.screenshot('data/instagram-unconfigured-mobile.png',390,844)
    browser.js("document.getElementById('close').click()")
    report['real_app']={'unconfigured_state':'passed','disabled_approval':'passed','rss_detail':'passed'}

    with tempfile.TemporaryDirectory() as temporary:
        storage=Storage.create(Path(temporary)/'browser.sqlite3')
        rss=CollectionService(storage,fetcher=lambda _: (fixture('두쫀쿠'),200))
        rss.collect()
        config=InstagramConfig(token=FAKE_TOKEN)
        transport=Transport()
        ig=InstagramService(storage,config,GraphClient(config,transport,lambda _: None))
        server=make_server(rss,port=0,instagram=ig)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            browser.navigate(f'http://127.0.0.1:{server.server_port}')
            browser.js("document.querySelector('.keyword').click()")
            browser.wait("document.querySelector('#instagram-detail .ig-approval')!==null")
            assert transport.calls==[]
            browser.js("document.querySelector('#instagram-detail input[type=checkbox]').click();document.querySelector('#instagram-detail button[type=submit]').click()")
            browser.wait("document.querySelector('#instagram-detail .ig-chart')!==null")
            assert browser.js("document.getElementById('instagram-detail').textContent.includes('누적 고유 API 관측 1개')")
            assert browser.js("document.querySelector('#instagram-detail a').href.startsWith('https://www.instagram.com/')")
            for days in [14,30,7]:
                browser.js(f"document.querySelector('#instagram-detail select').value='{days}';document.querySelector('#instagram-detail select').dispatchEvent(new Event('change'))")
                browser.wait(f"document.querySelector('#instagram-detail select')?.value==='{days}' && document.querySelector('#instagram-detail .ig-chart')!==null")
            browser.js("document.querySelectorAll('#instagram-detail select')[1].value='hour';document.querySelectorAll('#instagram-detail select')[1].dispatchEvent(new Event('change'))")
            browser.wait("document.querySelector('#instagram-detail .ig-chart')!==null && document.getElementById('instagram-detail').textContent.includes('X축: 시간')")
            browser.js("Array.from(document.querySelectorAll('#instagram-detail button')).find(b=>b.textContent==='지금 관측 수집').click()")
            browser.wait("document.querySelector('#instagram-detail .ig-chart')!==null && !Array.from(document.querySelectorAll('#instagram-detail button')).find(b=>b.textContent==='지금 관측 수집').disabled")
            assert ig.status()['hashtags'][0]['total_observed']==1
            browser.js("Array.from(document.querySelectorAll('#instagram-detail button')).find(b=>b.textContent==='추적 일시 정지').click()")
            browser.wait("Array.from(document.querySelectorAll('#instagram-detail button')).some(b=>b.textContent==='추적 재개')")
            assert not ig.store.tags()[0]['active']
            browser.js("Array.from(document.querySelectorAll('#instagram-detail button')).find(b=>b.textContent==='추적 재개').click()")
            browser.wait("Array.from(document.querySelectorAll('#instagram-detail button')).some(b=>b.textContent==='추적 일시 정지')")
            browser.js("document.getElementById('close').click();document.querySelector('#instagram-overview button[data-hashtag-id]').click()")
            browser.wait("document.querySelector('#instagram-detail .ig-chart')!==null")
            assert browser.js("document.querySelector('#instagram-detail .ig-approval')===null")
            assert browser.js("document.getElementById('instagram-detail').textContent.includes('수집 시작:')")
            assert browser.js("document.getElementById('instagram-detail').textContent.includes('비교 기간 부족')")
            assert browser.js("document.querySelector('#instagram-detail .ig-value').textContent==='1'")
            assert browser.js("document.getElementById('instagram-detail').textContent.includes('unnecessary private caption')")
            browser.js("const banner=Object.assign(document.createElement('p'),{textContent:'브라우저 검증용 Mock 데이터 · 실제 Instagram 관측값 아님'});banner.style='position:fixed;top:0;left:0;right:0;background:#fff1cf;color:#78551d;padding:10px;z-index:9999;margin:0;text-align:center';document.getElementById('detail').append(banner);document.getElementById('instagram-detail').scrollIntoView()")
            browser.screenshot('data/instagram-mock-preview.png')
            report['mock_ui']={'approval':'passed','dedup':'passed','daily_hourly_chart':'passed','periods_7_14_30':'passed','links':'passed','pause_resume':'passed','global_tag_view':'passed','caption':'passed','chart_matches_db':'passed','database':'temporary_only'}
        finally:
            browser.navigate(real)
            server.shutdown();server.server_close();thread.join()
    assert not browser.errors,browser.errors
    report['javascript_errors']=browser.errors
    report['rss_live_status']=json.load(urlopen(real+'/api/status'))['latest_run']
    report['instagram_live_status']=json.load(urlopen(real+'/api/instagram'))['connection']['status']
    Path('data/instagram-browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    browser.ws.close()


if __name__=='__main__':
    main()
