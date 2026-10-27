"""Optional Chrome QA: mock by default; --base validates the running live UI without API writes.

Uses an existing websocket-client installation only in the QA interpreter.
Never loads .env. Live mode does not click collection/refresh/delete or save API content.
"""
import argparse
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from instagram_browser_check import Browser
from food_trends.api import make_server
from food_trends.config import YouTubeConfig, InstagramConfig, NaverConfig
from food_trends.storage import Storage
from food_trends.service import CollectionService
from food_trends.youtube import YouTubeClient
from food_trends.youtube_service import YouTubeService
from food_trends.instagram_service import InstagramService
from food_trends.naver_service import NaverService
from test_mvp import fixture
from test_youtube import Transport


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='youtube-ui-') as temporary:
        chrome = Path(os.getenv('PROGRAMFILES', 'C:/Program Files'))/'Google/Chrome/Application/chrome.exe'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
                                    '--no-default-browser-check', '--remote-debugging-port=9240',
                                    '--remote-allow-origins=http://localhost:9240',
                                    '--user-data-dir='+str(Path(temporary)/'profile'), 'about:blank'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        server, browser = None, None
        report = {'mode': 'live_ui_read_only' if args.base else 'mock_ui', 'screenshots_saved': False}
        try:
            for _ in range(50):
                try:
                    pages = json.load(urlopen('http://127.0.0.1:9240/json', timeout=1))
                    page = next(p for p in pages if p['type'] == 'page')
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise AssertionError('Chrome CDP unavailable')
            browser = Browser.__new__(Browser)
            browser.ws = websocket.create_connection(page['webSocketDebuggerUrl'], origin='http://localhost:9240', timeout=20)
            browser.sequence, browser.errors = 0, []
            browser.call('Runtime.enable', {})
            if args.base:
                base = args.base
            else:
                storage = Storage.create(Path(temporary)/'ui.sqlite3')
                rss = CollectionService(storage, fetcher=lambda _: (fixture('두바이 쫀득 쿠키'), 200)); rss.collect()
                config = YouTubeConfig(api_key='synthetic-secret', discovery_queries=('신상 디저트',), queries_per_run=3)
                sender = Transport()
                youtube = YouTubeService(storage, config, YouTubeClient(config, sender, lambda _: None))
                server = make_server(rss, port=0, youtube=youtube,
                                     instagram=InstagramService(storage, InstagramConfig(enabled=False)),
                                     naver=NaverService(storage, NaverConfig(enabled=False)))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f'http://127.0.0.1:{server.server_port}'
            before = json.load(urlopen(base+'/api/youtube'))
            browser.navigate(base)
            browser.wait("document.querySelectorAll('[data-youtube-action]').length===3")
            if not args.base:
                assert not sender.calls
                browser.js("document.querySelector('[data-youtube-action=collect]').click()")
                browser.wait("document.querySelectorAll('.youtube-video').length===1 && !document.querySelector('[data-youtube-action=collect]').disabled")
                text = browser.js("document.querySelector('#youtube-overview').textContent")
                assert '좋아요 미확인' in text and 'FOOD #' in text and '내용 미검증' in text
                assert '9007199254740993' in text
                assert 'Mock 테스트 결과' in text
                assert '검색 결과 6행 / 고유 영상 1개 / 중복 제거 5행' in text
                assert youtube.status()['connection_status'] == 'mock_only'
                assert 'synthetic-secret' not in browser.js('document.body.textContent')
                sender.title = '<script>throw new Error("injection")</script>'
                browser.js("document.querySelector('[data-youtube-action=refresh]').click()")
                browser.wait("document.querySelector('.youtube-video a')?.textContent.includes('<script>')")
                assert not browser.js("document.querySelector('.youtube-video script')!==null")
                report['collect_refresh_nulls_provenance_xss'] = 'passed'
            else:
                browser.wait("!document.querySelector('#youtube-overview').textContent.includes('연결 상태를 확인하고 있습니다')")
                report['rendered_videos'] = browser.js("document.querySelectorAll('.youtube-video').length")
                report['configured'] = before['configured']
                assert before['status_version'] == 2
                assert before['connection_status'] == 'live_verified'
                text = browser.js("document.querySelector('#youtube-overview').textContent")
                assert '실제 API HTTP 200 확인' in text
                assert f"저장 영상 {before['stored_videos']}개" in text
                latest = before['latest_run']
                assert f"검색 결과 {latest['search_result_rows']}행" in text
                assert f"저장 {latest['saved_videos']}개" in text
                assert f"FOOD 검색 연결 {latest['food_connections']}개" in text
                visible_live = [v for v in before['videos'] if v['provenance']['origin'] == 'live_api']
                assert visible_live
                rendered = browser.js("Array.from(document.querySelectorAll('.youtube-video')).map(n=>({url:n.querySelector('a').href,text:n.textContent}))")
                for video in visible_live:
                    row = next(r for r in rendered if r['url'] == video['video_url'])
                    assert '실제 Google API 응답' in row['text']
                    for metric in ('view_count','like_count','comment_count'):
                        assert str(video[metric]) in row['text'] if video[metric] is not None else '미확인' in row['text']
                report['live_videos_displayed'] = len(visible_live)
                report['ui_matches_db'] = True
            for width in (1280, 390):
                browser.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 600})
                assert browser.js('document.documentElement.scrollWidth<=window.innerWidth+1'), width
            report['desktop_mobile_overflow'] = 'passed'
            if not args.base:
                browser.js("document.querySelector('[data-youtube-action=delete]').click()")
                browser.wait("document.querySelectorAll('.youtube-video').length===0 && !document.querySelector('[data-youtube-action=delete]').disabled")
                assert json.load(urlopen(base+'/api/youtube'))['stored_videos'] == 0
                report['local_delete'] = 'passed'
            else:
                after = json.load(urlopen(base+'/api/youtube'))
                report['external_requests_during_ui_check'] = after['search_requests']+after['video_requests']-before['search_requests']-before['video_requests']
                assert report['external_requests_during_ui_check'] == 0
            assert not browser.errors, browser.errors
            report['javascript_errors'] = browser.errors
            report['status'] = 'passed'
            destination = Path('data/youtube-live-browser-verification.json' if args.base else 'data/youtube-browser-verification.json')
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            if browser:
                try:
                    browser.call('Browser.close', {})
                except Exception:
                    pass
                browser.ws.close()
            if server:
                server.shutdown(); server.server_close()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(10)
            # Chrome can hold profile files briefly after root-process exit.
            import shutil
            profile = (Path(temporary)/'profile').resolve()
            assert profile.is_relative_to(Path(temporary).resolve())
            for _ in range(30):
                try:
                    shutil.rmtree(profile)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    time.sleep(.1)


if __name__ == '__main__':
    main()
