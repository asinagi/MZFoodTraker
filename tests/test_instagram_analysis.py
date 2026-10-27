import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from food_trends.instagram_analysis import extract_hashtags, KST
from food_trends.instagram_service import InstagramService
from food_trends.config import InstagramConfig
from food_trends.instagram_graph import GraphClient, GraphError
from food_trends.storage import Storage
from food_trends.__main__ import instagram_command
from test_instagram import Transport, MEDIA, FAKE_TOKEN


class ExtractionTests(unittest.TestCase):
    def test_unicode_duplicates_original_forms_and_separate_food_names(self):
        result=extract_hashtags('#두쫀쿠 #두바이쫀득쿠키 #CAFE #cafe #카페_2026 #123 #e\u0301 #é #두쫀쿠#디저트 ＃한글')
        tags={r['normalized_name']:r['original_names'] for r in result}
        self.assertEqual(tags['cafe'],['CAFE','cafe'])
        self.assertEqual(tags['é'],['e\u0301','é'])
        self.assertIn('두쫀쿠',tags);self.assertIn('두바이쫀득쿠키',tags)
        self.assertIn('카페_2026',tags);self.assertIn('123',tags);self.assertIn('한글',tags)
        self.assertEqual(len(tags),8)

    def test_missing_blank_punctuation_and_url_fragment(self):
        self.assertEqual(extract_hashtags(None),[])
        self.assertEqual(extract_hashtags('   # #\u0301x'),[])
        self.assertEqual([t['normalized_name'] for t in extract_hashtags('https://example.test/#anchor #진짜, #태그!')],['진짜','태그'])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.storage=Storage.create(Path(self.temp.name)/'test.sqlite3')
        self.config=InstagramConfig(token=FAKE_TOKEN)
        self.transport=Transport()
        self.client=GraphClient(self.config,self.transport,lambda _:None)
        self.ig=InstagramService(self.storage,self.config,self.client)

    def tearDown(self):
        self.temp.cleanup()

    def collect(self,items):
        self.transport.media_pages=[{'data':items}]
        if not self.ig.store.tags():
            return self.ig.start_tracking('두쫀쿠')
        return self.ig.collect()['results'][0]

    def summary(self,days=7):
        return self.ig.status(days=days)['hashtags'][0]

    def test_rates_quality_raw_privacy_dedup_and_no_tag_search(self):
        rows=[dict(MEDIA,id='1',caption='#두쫀쿠 #CAFE #cafe #디저트 #디저트'),
              dict(MEDIA,id='2',caption='#두쫀쿠 #cafe'),dict(MEDIA,id='3',caption=None),
              dict(MEDIA,id='4',caption='태그 없는 캡션'),dict(MEDIA,id='5',caption='')]
        self.collect(rows)
        h=self.summary();a=h['analysis']
        self.assertNotIn('media',h)
        self.assertNotIn('태그 없는 캡션',json.dumps(h,ensure_ascii=False))
        self.assertEqual((a['period_unique_posts'],a['caption_available_posts'],a['caption_missing_posts'],a['hashtag_extractable_posts'],a['caption_without_tags_posts']),(5,4,1,2,2))
        ranks={r['normalized_name']:r for r in a['ranking']}
        self.assertNotIn('두쫀쿠',ranks)
        self.assertEqual((ranks['cafe']['post_count'],ranks['cafe']['rate_pct']),(2,40.0))
        self.assertEqual(ranks['디저트']['rate_pct'],20.0)
        self.assertEqual(ranks['cafe']['daily'][0]['post_count'],2)
        calls=len(self.transport.calls)
        self.ig.store.analysis.reprocess();self.ig.store.analysis.reprocess()
        self.assertEqual(len(self.transport.calls),calls)
        self.collect(rows)
        after=self.summary()
        self.assertEqual(after['total_observed'],5)
        self.assertEqual(after['daily'][0]['new_count'],5)
        self.assertEqual(after['analysis']['ranking'][0]['post_count'],2)
        self.assertEqual([c[0] for c in self.transport.calls].count('ig_hashtag_search'),1)
        self.assertEqual(len(self.ig.store.tags()),1)
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_analysis_observations').fetchone()[0],10)

    def test_caption_edit_keeps_true_observation_history(self):
        self.collect([dict(MEDIA,caption='#두쫀쿠 #옛태그')])
        self.collect([dict(MEDIA,caption='#두쫀쿠 #새태그')])
        ranks={r['normalized_name']:r['post_count'] for r in self.summary()['analysis']['ranking']}
        self.assertEqual(ranks,{'옛태그':1,'새태그':1})
        self.assertEqual(self.summary()['total_observed'],1)
        with self.storage.connect() as db:
            active=[r[0] for r in db.execute('SELECT normalized_name FROM instagram_caption_tags WHERE active=1')]
        self.assertNotIn('옛태그',active)

    def test_missing_later_caption_does_not_reconfirm_old_tag(self):
        self.collect([dict(MEDIA,caption='#두쫀쿠 #디저트')])
        self.collect([dict(MEDIA,caption=None)])
        a=self.summary()['analysis']
        self.assertEqual(a['missing_caption_observations'],1)
        self.assertEqual(a['caption_available_posts'],1)
        self.assertEqual(a['ranking'][0]['post_count'],1)
        with self.storage.connect() as db:
            counts=[r[0] for r in db.execute('SELECT hashtag_count FROM instagram_analysis_observations ORDER BY id')]
        self.assertEqual(counts,[2,0])

    def test_period_denominator_is_observed_unique_posts_not_new_posts(self):
        self.collect([dict(MEDIA,caption='#두쫀쿠 #디저트')])
        with self.storage.connect() as db:
            old=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
            db.execute('UPDATE instagram_hashtag_media SET first_seen=?',(old,))
            db.execute('UPDATE instagram_analysis_observations SET observed_at=?',(old,))
        self.assertEqual(self.summary()['analysis']['period_unique_posts'],0)
        self.collect([dict(MEDIA,caption='#두쫀쿠 #디저트')])
        a=self.summary()['analysis']
        self.assertEqual(a['period_unique_posts'],1)
        self.assertEqual(a['ranking'][0]['rate_pct'],100.0)
        self.assertEqual(a['ranking'][0]['first_seen'],old)
        self.assertEqual(self.summary()['new_24h'],0)
        fortnight = self.summary(days=14)['analysis']
        self.assertEqual(fortnight['period_unique_posts'],1)
        self.assertEqual(fortnight['ranking'][0]['post_count'],1)
        daily = fortnight['ranking'][0]['daily']
        self.assertEqual(len(daily),2)
        self.assertEqual([r['post_count'] for r in daily],[1,1])
        self.assertEqual([r['observed_posts'] for r in daily],[1,1])
        self.assertEqual([r['rate_pct'] for r in daily],[100.0,100.0])

    def test_retention_preserves_tags_and_does_not_turn_purged_into_missing(self):
        self.collect([dict(MEDIA,caption='#두쫀쿠 #디저트')])
        with self.storage.connect() as db:
            old=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
            db.execute('UPDATE instagram_media SET caption_captured_at=?',(old,))
        report=self.ig.store.analysis.reprocess()
        self.assertEqual(report['raw_captions_purged'],1)
        a=self.summary()['analysis']
        self.assertEqual((a['raw_captions_retained'],a['raw_captions_purged'],a['caption_missing_posts']),(0,1,0))
        self.assertEqual(a['ranking'][0]['post_count'],1)
        self.ig.store.analysis.reprocess()
        self.assertEqual(self.summary()['analysis']['ranking'][0]['post_count'],1)

    def test_legacy_reprocess_only_seeds_last_snapshot_once(self):
        tag=self.ig.store.register('두쫀쿠');self.ig.store.approve(tag['id'],'999')
        first=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        last=datetime.now(timezone.utc).isoformat()
        with self.storage.connect() as db:
            db.execute('INSERT INTO instagram_media(id,first_seen,last_seen,caption,caption_captured_at) VALUES (?,?,?,?,?)',('legacy',first,last,'#두쫀쿠 #디저트',first))
            db.execute('INSERT INTO instagram_hashtag_media VALUES (?,?,?,?)',(tag['id'],'legacy',first,last))
        self.ig.store.analysis.reprocess();self.ig.store.analysis.reprocess()
        a=self.summary()['analysis']
        self.assertEqual(a['legacy_snapshot_count'],1)
        self.assertEqual(len(a['ranking'][0]['daily']),1)
        self.assertEqual(a['ranking'][0]['first_seen'],last)

    def test_failed_and_success_empty_runs_have_different_coverage(self):
        self.transport.media_error=GraphError('permission')
        self.collect([dict(MEDIA,caption='#디저트')])
        self.assertEqual(self.summary()['analysis']['daily_coverage'],[])
        self.transport.media_error=None
        self.collect([])
        a=self.summary()['analysis']
        self.assertEqual(len(a['daily_coverage']),1)
        self.assertEqual(a['daily_coverage'][0]['observed_posts'],0)

    def test_exchange_with_omitted_issue_date_is_checked_and_never_applied(self):
        from food_trends.instagram_tokens import exchange
        config=replace(self.config,app_id='synthetic-app',app_secret='synthetic-secret')
        self.ig.config=config;self.client.config=config
        original=self.ig.check_token
        def check(*args,**kwargs):
            result=original(*args,**kwargs)
            result['lifetime']='unknown'
            return result
        self.ig.check_token=check
        destination=Path(self.temp.name)/'replacement.secret'
        result=exchange(self.ig,destination)
        self.assertTrue(result['success'])
        self.assertFalse(result['applied'])
        self.assertEqual(destination.read_text().strip(),'synthetic-new-token')
        self.assertEqual(self.ig.config.token,FAKE_TOKEN)
        self.assertNotIn('synthetic-new-token',json.dumps(result))
        self.assertNotIn('synthetic-secret',json.dumps(result))
        self.assertNotIn('synthetic-secret',' '.join(self.transport.outbound_urls))
        self.assertTrue(all(method=='POST' for method in self.transport.outbound_methods))


if __name__=='__main__':
    unittest.main()
