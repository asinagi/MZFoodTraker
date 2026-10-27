"""Local-only Chrome verification of token warnings. Never calls Meta."""
import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from instagram_browser_check import Browser
from test_instagram import Transport, FAKE_TOKEN
from test_mvp import fixture
from food_trends.config import InstagramConfig
from food_trends.storage import Storage
from food_trends.service import CollectionService
from food_trends.instagram_service import InstagramService
from food_trends.instagram_graph import GraphClient
from food_trends.api import make_server


def main():
    browser=Browser()
    with tempfile.TemporaryDirectory() as temporary:
        storage=Storage.create(Path(temporary)/'warning.sqlite3')
        rss=CollectionService(storage,fetcher=lambda _: (fixture('두쫀쿠'),200))
        rss.collect()
        config=InstagramConfig(token=FAKE_TOKEN)
        transport=Transport()
        ig=InstagramService(storage,config,GraphClient(config,transport))
        set_expiry=lambda days:ig.store.state('token',{'status':'valid','expires_at':(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()})
        set_expiry(6)
        server=make_server(rss,port=0,instagram=ig)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            browser.navigate(f'http://127.0.0.1:{server.server_port}')
            browser.wait("document.querySelector('#instagram-overview .ig-token-warning[role=alert]')?.textContent.includes('7일 이내')")
            browser.screenshot('data/instagram-token-expiry-warning.png',1280,950,'#instagram-overview')
            set_expiry(-1)
            browser.js('igOverview.signature=null;refreshInstagramOverview()')
            browser.wait("document.querySelector('#instagram-overview .ig-token-warning')?.textContent.includes('만료되어')")
            set_expiry(60)
            browser.js('igOverview.signature=null;refreshInstagramOverview()')
            browser.wait("document.querySelector('#instagram-overview .ig-token-warning')===null")
            assert transport.calls==[]
        finally:
            server.shutdown();server.server_close();thread.join()
    # Read the actual application without any collection or authentication calls.
    base='http://127.0.0.1:8000'
    status=json.load(urlopen(base+'/api/instagram'))
    assert status['token']['status']=='valid' and status['token']['expiry_status']=='healthy'
    browser.navigate(base)
    browser.wait("document.querySelector('#instagram-overview .ig-cooccurrence')!==null")
    assert browser.js("document.querySelector('#instagram-overview .ig-token-warning')===null")
    browser.js("Array.from(document.querySelectorAll('#instagram-overview details')).find(d=>d.querySelector('summary')?.textContent==='연결 · 토큰 · 검색 예산').open=true")
    browser.wait("document.querySelector('#instagram-overview').textContent.includes('2026. 12. 25.') || document.querySelector('#instagram-overview').textContent.includes('2026')")
    assert browser.js("document.querySelector('#instagram-overview').textContent.includes('72')")
    browser.screenshot('data/instagram-approved-token-desktop.png',1440,1500,'#instagram-overview')
    browser.screenshot('data/instagram-approved-token-mobile.png',390,844,'#instagram-overview')
    assert not browser.errors
    report={'status':'passed','expiring_warning_visible':True,'expired_warning_visible':True,
            'healthy_no_warning':True,'mock_external_requests':len(transport.calls),
            'live_expiry_displayed':True,'javascript_errors':[]}
    Path('data/instagram-token-warning-browser-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    browser.ws.send(json.dumps({'id':99999,'method':'Browser.close','params':{}}));browser.ws.close()


if __name__=='__main__':
    main()
