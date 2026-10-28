"""Disposable Chrome/DB QA for the B3 execution panel; Mock transport only, loopback only.

Run: py -3.12 -B tests/execution_browser_check.py
No provider socket, credential, existing server or production database is used.
"""
import base64
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
from food_trends.execution_mock import MockTransport, mock_controller
from food_trends.food_service import FoodService
from food_trends.query_planner import QueryPlanner
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.tracking_access import mock_accounts
from food_trends.tracking_service import TrackingService

SOURCES = ('instagram_graph', 'youtube', 'naver_datalab')


def main():
    output = ROOT / 'reports/development-b3-2026-10-27'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        directory = Path(directory)
        storage = Storage.create(directory / 'browser.sqlite3')
        foods = FoodService(storage)
        c = foods.command('submit', dict(request_key='b3-input', name='두쫀쿠', aliases=['두바이 쫀득 쿠키', '두바이쫀득쿠키']))
        food = foods.command('review', dict(request_key='b3-approval', candidate_id=c['candidate_id'], expected_revision=c['revision'],
            action='create', canonical_name='두바이 쫀득 쿠키', approved=True, actor_ref='reviewer', reason='B3 브라우저 검증'))['resolution']
        tracking = TrackingService(storage, mock_accounts())
        planner = QueryPlanner(storage, tracking)
        for source in SOURCES:
            target = tracking.command('register', dict(request_key=source + '-register', food_id=food['food_id'], source_id=source,
                actor_ref='reviewer', reason='격리 계획 등록'))
            tracking.command('activate', dict(request_key=source + '-activate', target_id=target['target_id'],
                expected_revision=target['revision'], approved=True, actor_ref='reviewer', reason='격리 계획 승인'))
        transport = MockTransport()
        controller = mock_controller(storage, planner, transport)
        rss = CollectionService(storage, fetcher=lambda _: (_ for _ in ()).throw(AssertionError('No RSS collection allowed')))
        server = make_server(rss, port=0, tracking=tracking, planner=planner, execution=controller)
        base_handler = server.RequestHandlerClass
        forbidden, posts = [], []

        class Handler(base_handler):
            def do_POST(self):
                if not self.path.startswith('/api/execution/'):
                    forbidden.append(self.path)
                    return self.send(403, {'error': 'Only the Mock execution API is allowed in browser QA'})
                posts.append(self.path)
                return super().do_POST()

            def log_message(self, *args):
                pass

        server.RequestHandlerClass = Handler
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = directory / 'chrome'
        chrome = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
            '--disable-component-update', '--disable-default-apps', '--disable-sync', '--metrics-recording-only',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--user-data-dir=' + str(profile), 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        browser = None
        try:
            deadline = time.monotonic() + 15
            while not (profile / 'DevToolsActivePort').exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('Chrome failed to launch')
                time.sleep(.1)
            port = (profile / 'DevToolsActivePort').read_text().splitlines()[0]
            pages = json.load(urlopen('http://127.0.0.1:' + port + '/json'))
            browser = CDP(next(p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page'))
            browser.call('Runtime.enable')
            browser.call('Fetch.enable', {'patterns': [{'urlPattern': '*'}]})
            browser.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}'})

            # 1. Empty state; Live is closed and automatic execution is off.
            browser.wait("document.querySelector('#execution-overview')?.textContent.includes('등록된 실행 작업이 없습니다')")
            summary = browser.js("document.querySelector('#execution-overview').textContent")
            assert '자동 실행 꺼짐' in summary and 'live' not in summary.split('모드')[1].split('·')[0], summary
            assert browser.js("document.querySelector('#execution-schedule').disabled") is False

            # 2. Scheduling writes outbox jobs but sends no provider HTTP.
            browser.js("document.querySelector('#execution-schedule').click();true")
            browser.wait("document.querySelectorAll('.execution-job').length===6")
            assert transport.calls == [], transport.calls
            assert browser.js("document.querySelectorAll('.execution-run').length") == 6
            browser.wait("document.querySelector('#execution-overview').textContent.includes('아직 API를 실행하지 않았습니다')")

            # 3. Run each job once through the UI; completed jobs have no run button and no repeated HTTP.
            for done in range(1, 7):
                browser.js("document.querySelector('.execution-run').click();true")
                browser.wait(f"document.querySelectorAll('.execution-run').length==={6 - done}")
            browser.wait("document.querySelectorAll('.execution-job').length===6 && "
                         "Array.from(document.querySelectorAll('.execution-job strong')).every(n=>n.textContent.endsWith('SUCCEEDED'))")
            calls_after = len(transport.calls)
            assert calls_after == 11, calls_after
            browser.js("document.querySelector('#execution-refresh').click();true")
            time.sleep(.5)
            assert len(transport.calls) == calls_after
            assert browser.js("document.querySelectorAll('.execution-run').length") == 0
            assert browser.js("document.querySelector('#execution-schedule').click(), true")
            browser.wait("document.querySelector('#execution-overview').textContent.includes('작업 0개 확인')")
            assert len(transport.calls) == calls_after

            # 4. Evidence the UI exposes: attempt, uncertainty flag and no response body.
            text = browser.js("document.querySelector('#execution-overview').textContent")
            assert '원격 중복 가능성 기록 없음' in text and '수집 범위 complete_request' in text, text[:400]
            detail = browser.js("Array.from(document.querySelectorAll('.execution-job pre')).map(n=>n.textContent).join('\\n')")
            assert 'synthetic-mock' not in detail and 'Mock public video' not in detail

            browser.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
            browser.js("document.querySelector('#execution-overview').scrollIntoView();true")
            shot = browser.call('Page.captureScreenshot', {'format': 'png'})
            (output / 'execution-mobile.png').write_bytes(base64.b64decode(shot['data']))

            with storage.reader() as db:
                counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                          for t in ('execution_jobs', 'execution_calls', 'execution_results', 'quota_reservations', 'items')}
            assert not browser.errors, browser.errors
            assert not browser.external, browser.external
            assert not forbidden, forbidden
            assert storage.status()['total_keywords'] == 0
            result = {'successful': True, 'jobs_rendered': 6, 'mock_http_attempts': calls_after, 'real_http_attempts': 0,
                'http_after_schedule_click': 0, 'extra_http_after_completion_and_refresh': 0, 'run_buttons_after_completion': 0,
                'live_mode_available': False, 'automatic_enabled': False, 'javascript_errors': len(browser.errors),
                'external_page_requests': len(browser.external), 'forbidden_posts': len(forbidden),
                'execution_posts': sorted(set(posts)), 'database_counts': counts, 'production_database_used': False}
            (output / 'browser-summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result))
        finally:
            if browser:
                try:
                    browser.call('Browser.close')
                except (OSError, EOFError):
                    pass
                browser.socket.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    main()
