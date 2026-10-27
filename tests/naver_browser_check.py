"""Headless Chrome QA with isolated mock services; never loads .env or the live DB.

Requires the existing QA interpreter's websocket-client; no app dependency added.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen
import websocket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from food_trends.api import make_server
from food_trends.config import NaverConfig, InstagramConfig
from food_trends.naver_service import NaverService
from food_trends.naver_datalab import NaverClient
from food_trends.instagram_service import InstagramService
from food_trends.storage import Storage
from food_trends.service import CollectionService
from instagram_browser_check import Browser
from test_mvp import fixture
from test_naver import Transport, http_error


def main():
    chrome = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'
    if not chrome.exists():
        raise SystemExit('Chrome is required for this optional QA script')
    with tempfile.TemporaryDirectory(prefix='naver-qa-') as temporary:
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
                                    '--no-default-browser-check', '--remote-debugging-port=9235',
                                    '--remote-allow-origins=http://localhost:9235',
                                    '--user-data-dir='+str(Path(temporary)/'chrome'), 'about:blank'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        servers = []
        browser = None
        report = {'data': 'Mock only; isolated temporary SQLite; no live NAVER requests'}
        try:
            for _ in range(20):
                try:
                    pages = json.load(urlopen('http://127.0.0.1:9235/json', timeout=1))
                    page = next(p for p in pages if p['type'] == 'page')
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise AssertionError('Chrome CDP unavailable')
            browser = Browser.__new__(Browser)
            browser.ws = websocket.create_connection(page['webSocketDebuggerUrl'], origin='http://localhost:9235', timeout=10)
            browser.sequence, browser.errors = 0, []
            browser.call('Runtime.enable', {})
            storage = Storage.create(Path(temporary)/'browser.sqlite3')
            rss = CollectionService(storage, fetcher=lambda _: (fixture('두바이 쫀득 쿠키'), 200)); rss.collect()
            config = NaverConfig(client_id='synthetic-id', client_secret='synthetic-secret')
            sender = Transport()
            naver = NaverService(storage, config, NaverClient(config, sender, lambda _: None))
            instagram = InstagramService(storage, InstagramConfig())
            server = make_server(rss, port=0, instagram=instagram, naver=naver)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}'
            browser.navigate(base)
            browser.wait("document.querySelector('#naver-overview select')?.options.length===2")
            assert not sender.calls  # No query triggered by polling or opening the screen.
            for days in (7, 30, 90):
                browser.js(f"document.querySelector('[aria-label=\"NAVER 조회 기간\"]').value='{days}';document.querySelector('[aria-label=\"NAVER 조회 기간\"]').dispatchEvent(new Event('change'));document.querySelector('#naver-overview button').click()")
                browser.wait("document.querySelector('#naver-overview svg')!==null && !document.querySelector('#naver-overview button').disabled")
                assert browser.js("document.getElementById('naver-overview').textContent.includes('73.125')")
                assert browser.js("document.querySelector('#naver-overview svg').getAttribute('aria-label').includes('상대 검색 지수')")
            report['periods_and_chart'] = '7/30/90 days passed; relative index; accessible value table'
            browser.js("document.querySelector('#naver-overview button').click()")
            browser.wait("document.querySelector('#naver-overview [role=status]').textContent.includes('같은 요청')")
            assert browser.js("document.querySelector('#naver-overview svg')===null")
            report['duplicate'] = 'passed; stale chart cleared'
            browser.js("document.querySelector('.keyword').click()")
            browser.wait("document.querySelector('#naver-detail select')?.options.length===1")
            assert browser.js("document.getElementById('detail-body').textContent.includes('<item')")
            assert browser.js("document.getElementById('instagram-detail')!==null")
            report['rss_and_instagram_detail'] = 'passed'
            browser.call('Emulation.setDeviceMetricsOverride', {'width':390, 'height':844, 'deviceScaleFactor':1, 'mobile':True})
            assert browser.js("document.documentElement.scrollWidth<=window.innerWidth")
            report['mobile_overflow'] = 'passed at 390px'
            browser.js("document.getElementById('close').click()")
            # Test failures on fresh fingerprints; no error fixtures leave the temp DB.
            for code, expected in ((401, '인증 실패'), (403, '인증 실패'), (429, '호출량 제한'), (503, '서버 오류')):
                with storage.connect() as db:
                    db.execute('DELETE FROM naver_guards')
                sender.errors = [http_error(code)] * (3 if code in (429, 503) else 1)
                browser.js("document.querySelector('#naver-overview button').click()")
                browser.wait(f"document.querySelector('#naver-overview [role=status]').textContent.includes('{expected}') && !document.querySelector('#naver-overview button').disabled")
            report['authentication_and_limits'] = '401/403/429/503 passed'
            with storage.connect() as db:
                db.execute('DELETE FROM naver_guards')
            sender.empty = True
            browser.js("document.querySelector('#naver-overview button').click()")
            browser.wait("document.querySelector('#naver-overview [role=status]').textContent.includes('데이터가 없습니다')")
            report['empty'] = 'passed'
            browser.call('Network.enable', {})
            browser.call('Network.emulateNetworkConditions', {'offline':True, 'latency':0, 'downloadThroughput':0, 'uploadThroughput':0})
            browser.js("document.querySelector('#naver-overview button').click()")
            browser.wait("document.querySelector('#naver-overview [role=status]').textContent.includes('네트워크 오류')")
            browser.call('Network.emulateNetworkConditions', {'offline':False, 'latency':0, 'downloadThroughput':-1, 'uploadThroughput':-1})
            report['network_error'] = 'passed; browser offline; no external request'
            unconfigured = NaverService(storage, NaverConfig())
            other = make_server(rss, port=0, instagram=instagram, naver=unconfigured)
            servers.append(other)
            threading.Thread(target=other.serve_forever, daemon=True).start()
            browser.navigate(f'http://127.0.0.1:{other.server_port}')
            browser.wait("document.querySelector('#naver-overview [role=status]').textContent.includes('API 키 미설정')")
            assert browser.js("document.querySelector('#naver-overview button').disabled")
            report['unconfigured'] = 'passed; RSS still renders'
            assert not browser.errors, browser.errors
            report['javascript_errors'] = browser.errors
            Path('data').mkdir(exist_ok=True)
            Path('data/naver-browser-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            if browser:
                try:
                    browser.call('Browser.close', {})
                except Exception:
                    pass
                browser.ws.close()
            for server in servers:
                server.shutdown(); server.server_close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(timeout=10)
            # Chrome can release the profile asynchronously after its root process exits.
            for _ in range(30):
                try:
                    import shutil
                    shutil.rmtree(Path(temporary)/'chrome')
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    time.sleep(.1)


if __name__ == '__main__':
    main()
