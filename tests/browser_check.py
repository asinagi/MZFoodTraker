"""Optional local QA. Requires websocket-client in the QA interpreter only.

Run against an already running app and headless browser CDP port 9224.
Never inserts synthetic records. Not part of unittest discovery.
"""
import json
import time
from urllib.request import urlopen
from pathlib import Path
import websocket

base = 'http://127.0.0.1:8000'
pages = json.load(urlopen('http://127.0.0.1:9224/json'))
page = next(p for p in pages if p['type'] == 'page')
ws = websocket.create_connection(page['webSocketDebuggerUrl'], origin='http://localhost:9224', timeout=10)
sequence = 0

def call(method, params):
    global sequence
    sequence += 1
    ws.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
    while True:
        message = json.loads(ws.recv())
        if message.get('id') == sequence:
            if 'error' in message:
                raise RuntimeError(message['error'])
            return message.get('result', {})

def evaluate(expression):
    r = call('Runtime.evaluate', {'expression':expression, 'returnByValue':True, 'awaitPromise':True})
    if 'exceptionDetails' in r:
        raise RuntimeError(r['exceptionDetails'])
    return r['result'].get('value')

def wait_for(expression, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if evaluate(expression):
            return
        time.sleep(.2)
    raise AssertionError(expression)

call('Page.navigate', {'url':base})
wait_for("document.getElementById('summary')?.children.length === 5")
evaluate("document.getElementById('filter').value='ALL'; document.getElementById('filter').dispatchEvent(new Event('change'))")
wait_for("document.querySelectorAll('tr[data-id]').length > 0")
api = json.load(urlopen(base+'/api/trends'))['items']
visible = evaluate("Array.from(document.querySelectorAll('tr[data-id] .keyword')).map(x=>x.textContent)")
assert visible == [r['keyword'] for r in api]
evaluate("document.querySelector('.keyword').click()")
wait_for("document.getElementById('detail').open")
assert evaluate("document.getElementById('detail-body').textContent.includes('<item')")
evaluate("document.getElementById('close').click()")
keyword = api[0]['keyword']
evaluate("document.getElementById('search').value="+json.dumps(keyword)+"; document.getElementById('search').dispatchEvent(new Event('input'))")
wait_for("document.querySelectorAll('tr[data-id]').length === 1")
evaluate("document.getElementById('search').value=''; document.getElementById('search').dispatchEvent(new Event('input'))")
wait_for("document.querySelectorAll('tr[data-id]').length === "+str(len(api)))
before = json.load(urlopen(base+'/api/status'))
evaluate("document.getElementById('collect').click()")
wait_for("document.getElementById('collect').disabled === true")
wait_for("document.getElementById('collect').disabled === false",60)
after = json.load(urlopen(base+'/api/status'))
assert after['latest_run']['id'] > before['latest_run']['id']
assert after['latest_run']['success'] == 1
assert after['latest_run']['new_count'] == 0
assert after['total_keywords'] == before['total_keywords']
report = {'status':after, 'visible_keywords':visible, 'manual_button':'passed', 'detail':'passed', 'search':'passed', 'real_database_render':'passed'}
Path('verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
ws.close()
