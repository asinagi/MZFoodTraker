"""Standalone headless Chrome QA of the admin page served by the Oracle runtime (`--backend oracle-test` assembly).

Run: py -3.12 -B tests/oracle_admin_browser_check.py [--target oracle]
The default target is a disposable SQLite stand-in of the target schema; --target oracle uses the disposable MZ_TEST
schema (ORACLE_TEST_*, reset first). RSS answers come from a Mock feed; the browser may reach loopback only. No
existing browser, server, credential or production database is used.
"""
import argparse
import base64
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen
from food_browser_check import CDP, ROOT
from food_trends.__main__ import oracle_admin_server
from food_trends.target_db import SQLiteTarget
from food_trends.target_repository import TargetRepository
from test_oracle_backend import Feed
from test_oracle_rss import BAGEL, BRAND, COOKIE, PERSON, feed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='reports/development-oracle-backend-completion-2026-11-05')
    parser.add_argument('--target', choices=('standin', 'oracle'), default='standin')
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    chrome = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        directory = Path(directory)
        if args.target == 'oracle':
            from oracle_support import oracle_target, reset_target_schema
            db = oracle_target()
            reset_target_schema(db)
        else:
            db = SQLiteTarget(directory / 'target.sqlite3')
        repo = TargetRepository(db)
        repo.migrate()
        server, backend = oracle_admin_server(repo, 0, rss_fetcher=lambda timeout: (feed(COOKIE, BAGEL, BRAND, PERSON), 200), transport=Feed())
        handler = server.RequestHandlerClass

        class Quiet(handler):
            def log_message(self, *args):
                pass

        server.RequestHandlerClass = Quiet
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = directory / 'chrome'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
            '--no-default-browser-check', '--disable-background-networking', '--disable-component-update',
            '--disable-default-apps', '--disable-sync', '--metrics-recording-only',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
            '--user-data-dir=' + str(profile), 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        browser = None
        try:
            deadline = time.monotonic() + 15
            while not (profile / 'DevToolsActivePort').exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('Disposable Chrome failed to launch')
                time.sleep(.1)
            port = (profile / 'DevToolsActivePort').read_text().splitlines()[0]
            pages = json.load(urlopen('http://127.0.0.1:' + port + '/json'))
            browser = CDP(next(p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page'))
            browser.call('Runtime.enable')
            browser.call('Fetch.enable', {'patterns': [{'urlPattern': '*'}]})
            browser.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 2400, 'deviceScaleFactor': 1, 'mobile': False})
            browser.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}'})
            browser.wait("document.getElementById('status')?.textContent.includes('수집을 기다리는 중')")
            # RSS collection from the page button (admin.js adds the token): status, list and candidates come from the target.
            browser.js("document.getElementById('collect').click();true")
            browser.wait("document.getElementById('status').textContent.includes('RSS 수집 성공')")
            browser.wait("document.querySelectorAll('#rows tr').length === 2")  # the FOOD filter: the food and the brand
            browser.js("document.getElementById('food-reload').click();true")
            browser.wait("document.getElementById('food-candidate-list').textContent.includes('%s')" % COOKIE[0])
            assert len(backend.foods.candidates()) == 3 and not backend.foods.foods()
            # Approve the food candidate through the page's fetch (token added by admin.js), then reload the lists.
            candidate = next(c for c in backend.foods.candidates() if c['surface_form'] == COOKIE[0])
            approved = browser.js("""fetch('/api/foods/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(%s)})
                .then(r=>r.status)""" % json.dumps({'request_key': 'browser-review-1', 'candidate_id': candidate['candidate_id'],
                                                   'expected_revision': candidate['revision'], 'action': 'create', 'canonical_name': '두쫀쿠',
                                                   'approved': True, 'actor_ref': 'browser-qa', 'reason': '브라우저 검토'}, ensure_ascii=False))
            assert approved == 200, approved
            browser.js("document.getElementById('food-reload').click();true")
            browser.wait("document.getElementById('food-entity-list').textContent.includes('두쫀쿠')")
            # A request that does not carry the page token (XMLHttpRequest is not patched by admin.js) is refused.
            unauthenticated = browser.js("""new Promise(ok=>{const x=new XMLHttpRequest();x.open('POST','/api/foods/submit');
                x.setRequestHeader('Content-Type','application/json');x.onload=()=>ok(x.status);
                x.send(JSON.stringify({request_key:'browser-no-token',name:'마라탕',aliases:[]}));})""")
            assert unauthenticated == 401, unauthenticated
            # The execution panel shows one line per source, read from the target.
            browser.js("document.getElementById('execution-refresh').click();true")
            browser.wait("document.getElementById('execution-overview').textContent.includes('google_trends_rss')")
            sources = browser.js("document.getElementById('execution-overview').textContent")
            shot = browser.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True})
            (output / ('oracle-admin-ui-%s.png' % args.target)).write_bytes(base64.b64decode(shot['data']))
            legacy = browser.js("['instagram-overview','naver-overview','youtube-overview'].map(id=>document.getElementById(id).textContent.slice(0,80))")
            assert not browser.errors, browser.errors
            assert not browser.external, browser.external
            result = {'successful': True, 'target': args.target, 'storage': backend.execution.storage_label(),
                      'rss_collected_from_page': True, 'list_rows_food_filter': 2, 'candidates': 3, 'food_approved_from_page': True,
                      'request_without_token': unauthenticated, 'execution_sources_shown': all(s in sources for s in
                      ('instagram_graph', 'youtube', 'naver_datalab', 'google_trends_rss')),
                      'legacy_panels': legacy, 'javascript_errors': len(browser.errors), 'external_page_requests': len(browser.external),
                      'sqlite_application_database_used': False}
            (output / ('oracle-admin-browser-check-%s.json' % args.target)).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
        finally:
            if browser:
                browser.socket.close()
            process.terminate()
            process.wait(timeout=10)
            server.shutdown()
            server.server_close()
            backend.execution.release()
            if args.target == 'oracle':
                db.close()


if __name__ == '__main__':
    main()
