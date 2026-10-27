"""Verify the real running app with at most three NAVER requests. No mock responses.

Response bodies exist in memory only for comparison and are never written to disk.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen, Request
import websocket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from instagram_browser_check import Browser


class LiveBrowser(Browser):
    def call(self, method, params):
        self.sequence += 1
        self.ws.send(json.dumps({'id': self.sequence, 'method': method, 'params': params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get('method') == 'Runtime.exceptionThrown':
                self.errors.append(message['params']['exceptionDetails'].get('text', 'JavaScript error'))
            if message.get('method') == 'Network.responseReceived':
                response = message['params']['response']
                if response['url'].endswith('/api/naver/query'):
                    self.query_responses.append({'request_id': message['params']['requestId'],
                                                 'status': response['status'], 'headers': response['headers']})
            if message.get('id') == self.sequence:
                if 'error' in message:
                    raise RuntimeError(message['error'])
                return message.get('result', {})


def main():
    base = os.getenv('BROWSER_APP_URL', 'http://127.0.0.1:8000')
    before = json.load(urlopen(base+'/api/naver', timeout=5))
    if not before['configured'] or before['daily_requests'] > 90:
        raise SystemExit('Missing credentials or insufficient remaining daily budget')
    report = {'mode': 'live_api_only', 'periods': {}, 'series_persisted': False}
    with tempfile.TemporaryDirectory(prefix='naver-live-browser-') as temporary:
        chrome = Path(os.environ['PROGRAMFILES']) / 'Google/Chrome/Application/chrome.exe'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
                                    '--no-default-browser-check', '--remote-debugging-port=9236',
                                    '--remote-allow-origins=http://localhost:9236',
                                    '--user-data-dir='+str(Path(temporary)/'profile'), 'about:blank'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        browser = None
        try:
            for _ in range(20):
                try:
                    pages = json.load(urlopen('http://127.0.0.1:9236/json', timeout=1))
                    page = next(p for p in pages if p['type'] == 'page')
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise AssertionError('Chrome CDP unavailable')
            browser = LiveBrowser.__new__(LiveBrowser)
            browser.ws = websocket.create_connection(page['webSocketDebuggerUrl'], origin='http://localhost:9236', timeout=30)
            browser.sequence, browser.errors, browser.query_responses = 0, [], []
            browser.call('Runtime.enable', {})
            browser.call('Network.enable', {})
            browser.navigate(base)
            browser.wait("Array.from(document.querySelector('#naver-overview select')?.options||[]).some(o=>o.value==='manual')")
            browser.js("document.querySelector('#naver-overview select').value='manual';document.querySelector('#naver-overview select').dispatchEvent(new Event('change'))")
            if os.getenv('NAVER_QA_NO_QUERY') == '1':
                assert browser.js("document.querySelector('#naver-overview svg')===null")
                assert browser.js("!document.querySelector('#naver-overview button').disabled")
                after = json.load(urlopen(base+'/api/naver'))
                assert before['total_requests'] == after['total_requests']
                report = {'mode':'live_after_restart_no_query', 'configured':after['configured'],
                          'scheduler_running':after['scheduler_running'], 'interval_seconds':after['interval_seconds'],
                          'daily_limit':after['daily_limit'], 'retained_request_attempts':after['total_requests'],
                          'retained_counts':after['counts'], 'old_series_displayed':False,
                          'query_enabled':True, 'new_api_attempts':0, 'javascript_errors':browser.errors}
                Path('data/naver-live-restart-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(report, ensure_ascii=False), flush=True)
                return
            for days in (7, 30, 90):
                browser.js(f"document.querySelector('#naver-overview [aria-label=\"NAVER 조회 기간\"]').value='{days}';document.querySelector('#naver-overview [aria-label=\"NAVER 조회 기간\"]').dispatchEvent(new Event('change'));document.querySelector('#naver-overview button').click()")
                browser.wait("document.querySelector('#naver-overview svg')!==null && !document.querySelector('#naver-overview button').disabled", timeout=65)
                response = browser.query_responses[-1]
                assert response['status'] == 200, response['status']
                raw = browser.call('Network.getResponseBody', {'requestId': response['request_id']})
                payload = json.loads(raw['body'])
                assert payload['origin'] == 'user_keyword'
                assert len(payload['results']) == 1
                result = payload['results'][0]
                assert result['title'] == '두쫀쿠'
                assert result['keywords'] == ['두쫀쿠', '두바이 쫀득 쿠키', '두바이쫀득쿠키']
                values = browser.js("Array.from(document.querySelectorAll('#naver-overview .naver-chart table tr')).slice(1).map(r=>[r.cells[0].textContent,Number(r.cells[1].textContent)])")
                assert values == [[p['period'], p['ratio']] for p in result['data']]
                circles = browser.js("Array.from(document.querySelectorAll('#naver-overview svg circle')).map(c=>({y:Number(c.getAttribute('cy')),title:c.textContent}))")
                assert len(circles) == len(values)
                for circle, point in zip(circles, result['data']):
                    assert abs(circle['y'] - (185-point['ratio']*1.55)) < .000001
                assert any(k.lower() == 'cache-control' and v == 'no-store' for k, v in response['headers'].items())
                report['periods'][str(days)] = {'http_status': 200, 'point_count': len(values), 'table_matches_response': True,
                                                'svg_matches_response': True, 'alias_group_count': 1}
                # User-requested example is printed once, never added to the saved report.
                print(json.dumps({'days': days, 'keyword': result['title'], 'latest_example': result['data'][-1]}, ensure_ascii=False), flush=True)
                del raw, payload, result, values, circles
            # The latest exact duplicate must not cause an external API request.
            calls_before_duplicate = json.load(urlopen(base+'/api/naver'))['total_requests']
            browser.js("document.querySelector('#naver-overview button').click()")
            browser.wait("document.querySelector('#naver-overview [role=status]').textContent.includes('같은 요청')")
            assert json.load(urlopen(base+'/api/naver'))['total_requests'] == calls_before_duplicate
            report['duplicate_blocked_without_api_call'] = True
            # Verify the explicit alias maps to the same duplicate guard, without calling NAVER again.
            browser.js("document.querySelector('#naver-overview [aria-label=\"NAVER 사용자 지정 음식\"]').value='두바이 쫀득 쿠키';document.querySelector('#naver-overview button').click()")
            browser.wait("!document.querySelector('#naver-overview button').disabled")
            assert json.load(urlopen(base+'/api/naver'))['total_requests'] == calls_before_duplicate
            report['alias_duplicate_blocked'] = True
            browser.call('Emulation.setDeviceMetricsOverride', {'width':390, 'height':844, 'deviceScaleFactor':1, 'mobile':True})
            assert browser.js('document.documentElement.scrollWidth<=window.innerWidth')
            report['mobile_390px'] = 'passed'
            assert not browser.errors, browser.errors
            report['javascript_errors'] = browser.errors
            after = json.load(urlopen(base+'/api/naver'))
            report['new_api_attempts'] = after['total_requests']-before['total_requests']
            report['rss_food_count'] = len(after['targets'])
            report['food_detail_live_check'] = 'unavailable: no real RSS FOOD candidates' if not after['targets'] else 'not run'
            Path('data/naver-live-browser-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False), flush=True)
            # Leave the analysis screen in the user's ordinary browser with a fresh query when opened.
        finally:
            if browser:
                try:
                    browser.call('Browser.close', {})
                except Exception:
                    pass
                browser.ws.close()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(10)
            for _ in range(30):
                try:
                    import shutil
                    shutil.rmtree(Path(temporary)/'profile')
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    time.sleep(.1)


if __name__ == '__main__':
    main()
