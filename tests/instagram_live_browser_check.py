"""Explicit live QA: existing real records and one manual collection via the UI.

Run the QA app with INSTAGRAM_MAX_PAGES=1 to minimize upstream requests.
No mock data is inserted. Requires a Chrome CDP session on port 9224.
"""
import json
import os
import sqlite3
from pathlib import Path
from urllib.request import urlopen
from instagram_browser_check import Browser


def main():
    base = os.getenv('BROWSER_APP_URL', 'http://127.0.0.1:8001')
    browser = Browser()
    def status(days=7):
        return json.load(urlopen(base+f'/api/instagram?days={days}',timeout=10))
    def tag(data):
        return next(h for h in data['hashtags'] if h['name']=='두쫀쿠')
    before = status()
    initial = tag(before)
    assert before['configured'] and before['connection']['status']=='connected'
    assert initial['total_observed']>0
    browser.navigate(base)
    browser.wait("document.querySelector('#instagram-overview button[data-hashtag-id]')!==null")
    browser.js(f"document.querySelector('#instagram-overview button[data-hashtag-id=\"{initial['id']}\"]').click()")
    browser.wait("document.querySelector('#instagram-detail .ig-chart')!==null")
    assert browser.js("document.getElementById('instagram-detail').textContent.includes("+json.dumps(f"누적 고유 API 관측 {initial['total_observed']}개")+")")
    assert browser.js("document.getElementById('instagram-detail').textContent.includes('수집 시작:')")
    for days in (7,14,30):
        browser.js(f"document.querySelector('#instagram-detail select').value='{days}';document.querySelector('#instagram-detail select').dispatchEvent(new Event('change'))")
        browser.wait(f"document.querySelector('#instagram-detail select')?.value==='{days}' && document.querySelector('#instagram-detail .ig-chart')!==null")
        expected=tag(status(days))
        bars=browser.js("Array.from(document.querySelectorAll('#instagram-detail .ig-value')).map(n=>n.textContent)")
        assert bars==[str(r['new_count']) for r in expected['daily']]
    browser.js("document.querySelectorAll('#instagram-detail select')[1].value='hour';document.querySelectorAll('#instagram-detail select')[1].dispatchEvent(new Event('change'))")
    browser.wait("document.querySelector('#instagram-detail .ig-chart')!==null && document.getElementById('instagram-detail').textContent.includes('X축: 시간')")
    hourly=tag(status(30))['hourly']
    assert browser.js("Array.from(document.querySelectorAll('#instagram-detail .ig-value')).map(n=>n.textContent)")==[str(r['new_count']) for r in hourly]
    links=browser.js("Array.from(document.querySelectorAll('#instagram-detail a')).map(n=>({host:new URL(n.href).hostname,target:n.target,rel:n.rel}))")
    assert links and all(link['host'] in ('www.instagram.com','instagram.com') and link['target']=='_blank' and 'noopener' in link['rel'] for link in links)
    before_run=initial['latest_run']['id']
    browser.wait("Array.from(document.querySelectorAll('#instagram-detail button')).some(b=>b.textContent==='지금 관측 수집'&&!b.disabled)",timeout=15)
    browser.js("Array.from(document.querySelectorAll('#instagram-detail button')).find(b=>b.textContent==='지금 관측 수집').click()")
    browser.wait("document.getElementById('instagram-detail').textContent.includes('요청을 완료했습니다.')",timeout=60)
    after=tag(status(30))
    run=after['latest_run']
    assert run['id']>before_run and run['status'] in ('success','limited')
    assert run['duplicate_count']>0
    assert after['total_observed']==initial['total_observed']+run['new_count']
    with sqlite3.connect('file:data/trends.sqlite3?mode=ro',uri=True) as db:
        unique=db.execute('SELECT count(*),count(DISTINCT media_id) FROM instagram_hashtag_media WHERE hashtag_id=?',(initial['id'],)).fetchone()
        assert unique[0]==unique[1]==after['total_observed']
        daily=db.execute("SELECT sum(new_count) FROM instagram_metrics WHERE hashtag_id=? AND grain='day'",(initial['id'],)).fetchone()[0]
        hourly_total=db.execute("SELECT sum(new_count) FROM instagram_metrics WHERE hashtag_id=? AND grain='hour'",(initial['id'],)).fetchone()[0]
        assert daily==hourly_total==after['total_observed']
    browser.js("document.getElementById('instagram-detail').scrollIntoView()")
    browser.screenshot('data/instagram-live-desktop.png')
    browser.screenshot('data/instagram-live-mobile.png',390,844)
    assert not browser.errors,browser.errors
    report={'source':'real_instagram_graph_api','status':'passed','target':'두쫀쿠',
            'displayed_total':after['total_observed'],'new_24h':after['new_24h'],
            'daily_sum':daily,'hourly_sum':hourly_total,'periods_7_14_30':'passed',
            'daily_and_hourly_graph':'passed','original_links':'passed','first_observation_label':'passed',
            'manual_collection':'passed','dedup':'passed','manual_run':run,
            'scheduler_interval_minutes':before['interval_minutes'],'javascript_errors':browser.errors,
            'screenshots':['data/instagram-live-desktop.png','data/instagram-live-mobile.png']}
    Path('data/instagram-live-browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    browser.ws.close()


if __name__=='__main__':
    main()
