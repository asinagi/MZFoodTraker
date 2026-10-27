"""Disposable Chrome/DB QA for B1 tracking controls; imports only stdlib CDP helper."""
import base64
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.request import urlopen
from food_browser_check import CDP, ROOT
from food_trends.api import make_server
from food_trends.food_service import FoodService
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.tracking_access import mock_accounts
from food_trends.tracking_service import TrackingService
from food_trends.query_planner import QueryPlanner


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--planner',action='store_true');args=parser.parse_args()
    prefix='b2' if args.planner else 'b1'
    output=ROOT/'reports/development-b1-b2-2026-10-27';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        directory=Path(directory);storage=Storage.create(directory/'browser.sqlite3');foods=FoodService(storage)
        c=foods.command('submit',dict(request_key='demo-input',name='두쫀쿠',aliases=['두바이 쫀득 쿠키','두바이쫀득쿠키']))
        food=foods.command('review',dict(request_key='demo-approval',candidate_id=c['candidate_id'],expected_revision=c['revision'],
            action='create',canonical_name='두바이 쫀득 쿠키',approved=True,actor_ref='reviewer',reason='정확 명칭 검토'))['resolution']
        tracking=TrackingService(storage,mock_accounts())
        planner=QueryPlanner(storage,tracking)
        rss=CollectionService(storage,fetcher=lambda _:(_ for _ in ()).throw(AssertionError('No collection')))
        server=make_server(rss,port=0,tracking=tracking,planner=planner);base_handler=server.RequestHandlerClass;forbidden=[]
        class Handler(base_handler):
            def do_POST(self):
                if not self.path.startswith(('/api/foods/','/api/tracking/','/api/planner/')):
                    forbidden.append(self.path);return self.send(403,{'error':'Collection forbidden in browser QA'})
                return super().do_POST()
            def log_message(self,*args):pass
        server.RequestHandlerClass=Handler;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        profile=directory/'chrome';chrome=Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Google/Chrome/Application/chrome.exe'
        process=subprocess.Popen([str(chrome),'--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
            '--disable-component-update','--disable-default-apps','--disable-sync','--metrics-recording-only',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+str(profile),'about:blank'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        browser=None
        try:
            deadline=time.monotonic()+15
            while not (profile/'DevToolsActivePort').exists():
                if time.monotonic()>deadline:raise RuntimeError('Chrome failed to launch')
                time.sleep(.1)
            port=(profile/'DevToolsActivePort').read_text().splitlines()[0]
            pages=json.load(urlopen('http://127.0.0.1:'+port+'/json'))
            browser=CDP(next(p['webSocketDebuggerUrl'] for p in pages if p['type']=='page'))
            browser.call('Runtime.enable');browser.call('Fetch.enable',{'patterns':[{'urlPattern':'*'}]})
            browser.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}'})
            browser.wait("document.querySelector('#food-entity-list button')!==null")
            browser.js("document.querySelector('#food-entity-list button').click();true")
            browser.wait("document.querySelectorAll('.tracking-panel .food-card').length===3")
            browser.js("""(()=>{const inputs=document.querySelectorAll('.tracking-panel .food-form input');
                inputs[0].value='browser-reviewer';inputs[1].value='추적 계획 상태 검증';inputs[2].checked=true;return true;})()""")
            def click(source,label,state):
                expression="""(()=>{const card=Array.from(document.querySelectorAll('.tracking-panel .food-card')).find(n=>n.querySelector('strong').textContent.startsWith(SOURCE));
                    const b=Array.from(card.querySelectorAll('button')).find(n=>n.textContent===LABEL);if(!b)throw Error('Missing button');b.click();return true;})()"""
                browser.js(expression.replace('SOURCE',json.dumps(source)).replace('LABEL',json.dumps(label)))
                browser.wait("Array.from(document.querySelectorAll('.tracking-panel .food-card')).find(n=>n.querySelector('strong').textContent.startsWith("+json.dumps(source)+"))?.textContent.includes("+json.dumps(state)+")")
            for source in ('instagram_graph','youtube','naver_datalab'):
                click(source,'추적 대상 등록','PENDING');click(source,'계획 활성화 승인','ACTIVE')
            assert len(tracking.due())==3
            if args.planner:
                with storage.reader() as db:before=list(db.iterdump())
                for source in ('instagram_graph','youtube','naver_datalab'):
                    browser.js("Array.from(document.querySelectorAll('.tracking-panel .food-card')).find(n=>n.querySelector('strong').textContent.startsWith("+json.dumps(source)+")).querySelector('.query-preview').open=true;true")
                    browser.wait("Array.from(document.querySelectorAll('.tracking-panel .food-card')).find(n=>n.querySelector('strong').textContent.startsWith("+json.dumps(source)+"))?.querySelector('.query-preview pre')?.textContent.includes('QUERY_')")
                with storage.reader() as db:assert list(db.iterdump())==before
                for source in ('instagram_graph','youtube','naver_datalab'):
                    expression="""(()=>{const card=Array.from(document.querySelectorAll('.tracking-panel .food-card')).find(n=>n.querySelector('strong').textContent.startsWith(SOURCE));
                        Array.from(card.querySelectorAll('button')).find(n=>n.textContent==='이 계획 명시적으로 기록').click();return true;})()"""
                    browser.js(expression.replace('SOURCE',json.dumps(source)))
                    browser.wait("Array.from(document.querySelectorAll('.tracking-panel .food-card')).find(n=>n.querySelector('strong').textContent.startsWith("+json.dumps(source)+"))?.textContent.includes('실제 수집 없음')")
                assert len(planner.history())==6
                with storage.reader() as db:assert db.execute('SELECT count(*) FROM quota_reservations').fetchone()[0]==0
                old_youtube=next(p for p in planner.history() if p['source_id']=='youtube')
                browser.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                browser.js("document.querySelector('.query-preview').scrollIntoView();true")
                shot=browser.call('Page.captureScreenshot',{'format':'png'})
                (output/'b2-query-preview-mobile.png').write_bytes(base64.b64decode(shot['data']))
            click('youtube','일시 중단','PAUSED');assert len(tracking.due())==2
            if args.planner:assert not planner.plan(old_youtube['plan_id'])['current_validation']['valid']
            click('youtube','재개','ACTIVE')
            click('youtube','추적 종료','STOPPED');click('youtube','명시적 재등록','PENDING')
            assert all(t['last_success_at'] is None for t in tracking.list(food['food_id']))
            browser.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            browser.js("document.querySelector('.tracking-panel').scrollIntoView();true")
            shot=browser.call('Page.captureScreenshot',{'format':'png'});(output/(prefix+'-tracking-mobile.png')).write_bytes(base64.b64decode(shot['data']))
            assert not browser.errors,browser.errors
            assert not browser.external,browser.external
            assert not forbidden,forbidden
            assert storage.status()['total_keywords']==0
            result={'successful':True,'mock_accounts':3,'zero_rss_food':True,'register_activate_pause_resume_stop_reregister':True,
                'javascript_errors':len(browser.errors),'external_page_requests':len(browser.external),'provider_posts':len(forbidden),
                'production_database_used':False,'planner_preview_persist_history':args.planner}
            (output/(prefix+'-browser-summary.json')).write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
        finally:
            if browser:
                try:browser.call('Browser.close')
                except (OSError,EOFError):pass
                browser.socket.close()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
