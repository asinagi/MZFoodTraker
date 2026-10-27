"""Browser QA for hashtag dashboards. Only the optional live phase calls Meta.

The real QA server must use INSTAGRAM_MAX_PAGES=1. Mock fixtures stay in a
temporary database. Requires websocket-client in the QA interpreter and CDP 9224.
"""
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.request import urlopen
from instagram_browser_check import Browser
from test_instagram import Transport,MEDIA,FAKE_TOKEN
from test_mvp import fixture
from food_trends.storage import Storage
from food_trends.config import InstagramConfig
from food_trends.service import CollectionService
from food_trends.instagram_service import InstagramService
from food_trends.instagram_graph import GraphClient
from food_trends.api import make_server


def displayed(browser):
    return browser.js("Array.from(document.querySelectorAll('#instagram-overview .ig-co-row')).map(n=>({tag:n.dataset.tag,count:Number(n.dataset.count),rate:n.children[3].textContent}))")


def assert_dashboard(browser,base,manual=False):
    read=lambda days=7:json.load(urlopen(base+f'/api/instagram?days={days}',timeout=10))
    initial=read();h=initial['hashtags'][0];before_run=h['latest_run']['id']
    browser.navigate(base)
    browser.wait("document.querySelector('#instagram-overview .ig-co-row')!==null")
    for days in [7,14,30]:
        browser.js(f"document.querySelector('#instagram-overview .ig-days').value='{days}';document.querySelector('#instagram-overview .ig-days').dispatchEvent(new Event('change'))")
        browser.wait(f"document.querySelector('#instagram-overview .ig-coverage')?.textContent.includes('선택 {days}일')")
        expected=read(days)['hashtags'][0]['analysis']['ranking']
        actual=displayed(browser)
        assert [r['tag'] for r in actual]==[r['normalized_name'] for r in expected]
        assert [r['count'] for r in actual]==[r['post_count'] for r in expected]
        assert [r['rate'] for r in actual]==[str(r['rate_pct']).removesuffix('.0')+'%' for r in expected]
    browser.js("document.querySelector('#instagram-overview .ig-grain').value='hour';document.querySelector('#instagram-overview .ig-grain').dispatchEvent(new Event('change'))")
    browser.wait("document.querySelector('#instagram-overview .ig-grain')?.value==='hour' && document.getElementById('instagram-overview').textContent.includes('X축: 시간')")
    expected=read(30)['hashtags'][0]['hourly']
    assert browser.js("Array.from(document.querySelectorAll('#instagram-overview .ig-value')).map(n=>n.textContent)")==[str(r['new_count']) for r in expected]
    assert 'media' not in read()['hashtags'][0]
    assert not browser.js("document.querySelector('#instagram-overview .ig-hashtag>ul')!==null")
    # Missing body text and credentials must stay outside the UI, including debug.
    assert not browser.js("document.body.textContent.includes('caption-body-must-not-render')")
    assert not browser.js("document.body.textContent.includes('synthetic-credential-do-not-log')")
    if manual:
        browser.wait("!document.querySelector('#instagram-overview [data-ig-collect]').disabled")
        browser.js("document.querySelector('#instagram-overview [data-ig-collect]').click()")
        browser.wait("document.getElementById('instagram-overview').textContent.includes('요청을 완료했습니다.')",timeout=60)
    after=read(30)['hashtags'][0]
    if manual:
        assert after['latest_run']['id']>before_run
        assert after['latest_run']['status'] in ('limited','success')
        assert after['total_observed']==h['total_observed']+after['latest_run']['new_count']
    return after


def main():
    browser=Browser();report={}
    with tempfile.TemporaryDirectory() as temp:
        storage=Storage.create(Path(temp)/'qa.sqlite3')
        rss=CollectionService(storage,fetcher=lambda _: (fixture('두쫀쿠'),200));rss.collect()
        transport=Transport()
        transport.media_pages=[{'data':[dict(MEDIA,id='1',caption='caption-body-must-not-render #두쫀쿠 #CAFE #cafe #디저트'),dict(MEDIA,id='2',caption='#두쫀쿠 #cafe'),dict(MEDIA,id='3',caption=None)]}]
        config=InstagramConfig(token=FAKE_TOKEN)
        ig=InstagramService(storage,config,GraphClient(config,transport,lambda _:None))
        ig.start_tracking('두쫀쿠')
        server=make_server(rss,port=0,instagram=ig);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            h=assert_dashboard(browser,f'http://127.0.0.1:{server.server_port}',manual=True)
            assert h['total_observed']==3
            assert h['analysis']['ranking'][0]['post_count']==2
            assert h['analysis']['ranking'][0]['rate_pct']==66.67
            assert len(ig.store.tags())==1
            assert [c[0] for c in transport.calls].count('ig_hashtag_search')==1
            # A background status update must not unlock a pending local action.
            result=browser.js("(async()=>{const p=document.getElementById('instagram-overview'),b=p.querySelector('[data-ig-collect]');p.dataset.busy='true';b.disabled=true;syncInstagramActions(await api('/api/instagram'));const protected=b.disabled;p.dataset.busy='false';syncInstagramActions(await api('/api/instagram'));return protected&&!b.disabled;})()")
            assert result
            # Local reprocessing consumes no Meta calls or new observations.
            calls=len(transport.calls);before=h['analysis']['period_unique_posts']
            ig.store.analysis.reprocess()
            assert len(transport.calls)==calls and ig.status()['hashtags'][0]['analysis']['period_unique_posts']==before
            report['mock']={'status':'passed','duplicates':'passed','rates':'2/3=66.67%','privacy':'passed','no_new_search':'passed','busy_state':'passed'}
        finally:
            server.shutdown();server.server_close();thread.join()
    base=os.getenv('BROWSER_APP_URL','http://127.0.0.1:8001')
    live=assert_dashboard(browser,base,manual=True)
    with sqlite3.connect('file:data/trends.sqlite3?mode=ro',uri=True) as db:
        run=live['latest_run']
        observations=db.execute('SELECT count(*) FROM instagram_analysis_observations WHERE run_id=?',(run['id'],)).fetchone()[0]
        updated_links=db.execute('SELECT count(*) FROM instagram_hashtag_media WHERE hashtag_id=? AND last_seen=?',
                                 (live['id'],run['finished_at'])).fetchone()[0]
        assert observations==updated_links and 0<observations<=run['returned_count']
        name=live['analysis']['ranking'][0]['normalized_name']
        count=db.execute('''SELECT count(DISTINCT o.media_id) FROM instagram_analysis_observations o
            JOIN instagram_observation_tags t ON t.observation_id=o.id AND t.active=1
            WHERE o.hashtag_id=? AND t.normalized_name=? AND o.observed_at>=?''',(live['id'],name,live['analysis']['period_start'])).fetchone()[0]
        assert count==live['analysis']['ranking'][0]['post_count']
        original=db.execute('''SELECT count(*) FROM instagram_hashtag_media WHERE first_seen<=
            (SELECT finished_at FROM instagram_runs WHERE id=1)''').fetchone()[0]
        assert original==67
    browser.js("document.getElementById('instagram-overview').scrollIntoView()")
    browser.screenshot('data/instagram-hashtags-desktop.png',1440,1500,'#instagram-overview')
    browser.screenshot('data/instagram-hashtags-mobile.png',390,844,'#instagram-overview')
    browser.js("document.querySelector('#instagram-overview .ig-cooccurrence').scrollIntoView()")
    browser.screenshot('data/instagram-hashtags-ranking-mobile.png',390,844,'#instagram-overview .ig-cooccurrence')
    assert not browser.errors,browser.errors
    a=live['analysis']
    report['live']={'status':'passed','source':'real_instagram_graph_api','observed':live['total_observed'],
        'original_67_preserved':original,'period_unique_posts':a['period_unique_posts'],
        'caption_available':a['caption_available_posts'],'caption_missing':a['caption_missing_posts'],
        'hashtag_extractable':a['hashtag_extractable_posts'],'related_tag_count':a['unique_related_tags'],
        'top':[{k:r[k] for k in ('normalized_name','post_count','rate_pct')} for r in a['ranking'][:10]],
        'manual_run':run,'automatic_analysis_observations':observations,
        'periods_7_14_30':'passed','daily_and_hourly':'passed','db_and_ui_match':'passed',
        'javascript_errors':browser.errors}
    Path('data/instagram-hashtags-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    browser.ws.close()


if __name__=='__main__':
    main()
