"""Standalone headless Chrome QA: disposable profile/DB, localhost only, no dependencies.

Run: py -3.12 -B tests/food_browser_check.py
No existing browser, server, credentials or production database is used.
"""
import base64
import argparse
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from food_trends.api import make_server
from food_trends.food_service import FoodService
from food_trends.service import CollectionService
from food_trends.storage import Storage


class CDP:
    """Minimal local WebSocket transport for a disposable Chrome debugging endpoint."""
    def __init__(self, url):
        parts = urlsplit(url)
        if parts.hostname != '127.0.0.1':
            raise ValueError('Only loopback debugging is allowed')
        self.socket = socket.create_connection((parts.hostname, parts.port), timeout=15)
        nonce = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall((f'GET {parts.path} HTTP/1.1\r\nHost: 127.0.0.1:{parts.port}\r\n'
                             f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {nonce}\r\n'
                             'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        headers = b''
        while not headers.endswith(b'\r\n\r\n'):
            headers += self.exact(1)
        accept = base64.b64encode(hashlib.sha1((nonce + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        assert b' 101 ' in headers and accept in headers
        self.seq, self.errors, self.external = 0, [], []

    def exact(self, count):
        data = b''
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise EOFError('Chrome debugging socket closed')
            data += chunk
        return data

    def send(self, payload):
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytes([0x81, 0x80 | len(data)]) if len(data) < 126 else bytes([0x81, 0xfe]) + struct.pack('!H', len(data))
        self.socket.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))

    def receive(self):
        fragments = b''
        while True:
            first, size = self.exact(2)
            count = size & 127
            if count == 126:
                count = struct.unpack('!H', self.exact(2))[0]
            elif count == 127:
                count = struct.unpack('!Q', self.exact(8))[0]
            assert not size & 128, 'Server frame unexpectedly masked'
            fragments += self.exact(count)
            if first & 128:
                return json.loads(fragments)

    def call(self, method, params=None):
        self.seq += 1
        request_id = self.seq
        self.send({'id': request_id, 'method': method, 'params': params or {}})
        while True:
            result = self.receive()
            if result.get('method') == 'Runtime.exceptionThrown':
                self.errors.append(result['params'])
            if result.get('method') == 'Fetch.requestPaused':
                event = result['params']
                url = urlsplit(event['request']['url'])
                allowed = url.scheme in ('http', 'ws') and url.hostname == '127.0.0.1'
                if not allowed:
                    self.external.append(event['request']['url'])
                self.seq += 1
                self.send({'id': self.seq, 'method': 'Fetch.continueRequest' if allowed else 'Fetch.failRequest',
                           'params': {'requestId': event['requestId'], **({} if allowed else {'errorReason': 'BlockedByClient'})}})
            if result.get('id') == request_id:
                if 'error' in result:
                    raise RuntimeError(result['error'])
                return result.get('result', {})

    def js(self, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if 'exceptionDetails' in result:
            raise AssertionError(result['exceptionDetails'])
        return result['result'].get('value')

    def wait(self, expression):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.js(expression):
                return
            time.sleep(.1)
        raise AssertionError('Browser condition failed: ' + expression)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='reports/development-a4-a5-2026-10-27')
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    chrome = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        directory = Path(directory)
        storage = Storage.create(directory / 'browser.sqlite3')
        foods = FoodService(storage)
        rss = CollectionService(storage, fetcher=lambda _: (_ for _ in ()).throw(AssertionError('No RSS collection allowed')))
        server = make_server(rss, port=0, foods=foods)
        original_handler = server.RequestHandlerClass
        forbidden = []

        class Handler(original_handler):
            def do_POST(self):
                if not self.path.startswith('/api/foods/'):
                    forbidden.append(self.path)
                    return self.send(403, {'error': 'No source collection in browser QA'})
                return super().do_POST()

            def log_message(self, *args):
                pass

        server.RequestHandlerClass = Handler
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        profile = directory / 'chrome'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
            '--no-default-browser-check', '--disable-background-networking', '--disable-component-update',
            '--disable-default-apps', '--disable-sync', '--metrics-recording-only',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
            '--user-data-dir=' + str(profile), 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
            browser.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}'})
            browser.wait("document.getElementById('food-candidate-list')?.textContent.includes('등록된 후보가 없습니다')")
            assert storage.status()['food_count'] == 0
            browser.js("""(() => { const f=document.getElementById('food-register');
                f.elements.name.value='두쫀쿠';f.elements.aliases.value='두바이 쫀득 쿠키, 두바이쫀득쿠키';
                f.requestSubmit();return true;})()""")
            browser.wait("document.querySelector('#food-dialog form [name=canonical_name]') !== null")
            assert len(foods.candidates()) == 1 and not foods.foods()
            browser.js("""(() => {const f=document.querySelector('#food-dialog form');
                f.elements.canonical_name.value='두바이 쫀득 쿠키';f.elements.reason.value='음식 및 정확 별칭을 사람이 검증';
                f.querySelector('input[type=checkbox]').checked=true;f.requestSubmit();return true;})()""")
            browser.wait("document.getElementById('food-entity-list').textContent.includes('두바이 쫀득 쿠키') && !document.getElementById('food-dialog').open")
            food = foods.foods()[0]
            assert len(food['approved_aliases']) == 3
            for name in ('두쫀쿠', '두바이 쫀득 쿠키', '두바이쫀득쿠키'):
                assert foods.resolve_name(name)['resolution']['food_id'] == food['food_id']
            # Repeated UI submission keeps the original candidate and food ID.
            browser.js("document.getElementById('food-register').requestSubmit();true")
            browser.wait("document.getElementById('food-dialog').open && document.getElementById('food-dialog-title').textContent.includes('RESOLVED')")
            assert len(foods.candidates()) == 1 and len(foods.foods()) == 1
            browser.js("document.getElementById('food-dialog-close').click();true")
            browser.js("document.querySelector('#food-entity-list button').click();true")
            browser.wait("document.getElementById('food-dialog-body').textContent.includes('추적 시스템 전달용 음식 정보')")
            browser.js("document.getElementById('food-dialog-close').click();true")
            # Mobile layout and safe rendering of user strings.
            browser.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
            browser.js("document.getElementById('food-registry').scrollIntoView();true")
            assert browser.js("document.getElementById('food-registry').getBoundingClientRect().width <= window.innerWidth")
            shot = browser.call('Page.captureScreenshot', {'format': 'png'})
            (output / 'manual-registration-mobile.png').write_bytes(base64.b64decode(shot['data']))
            assert not browser.errors, browser.errors
            assert not browser.external, browser.external
            assert not forbidden, forbidden
            assert storage.status()['total_keywords'] == 0
            result = {'successful': True, 'zero_rss_food': True, 'ui_submit_review_repeat': True,
                      'exact_aliases_same_food_id': True, 'mobile_panel_fits': True,
                      'javascript_errors': len(browser.errors), 'external_page_requests': len(browser.external),
                      'source_post_requests': len(forbidden), 'production_database_used': False}
            (output / 'browser-summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result))
        finally:
            if browser:
                try:
                    browser.call('Browser.close')
                except (EOFError, OSError):
                    pass
                browser.socket.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(timeout=10)
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    main()
