import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from food_trends.collector import parse
from food_trends.classifier import classify, normalize
from food_trends.storage import Storage
from food_trends.service import CollectionService
from food_trends.api import make_server

# Synthetic fixtures are used only in temporary test databases, never the UI database.
def fixture(title='새로운 구름볼', date='Tue, 13 Oct 2026 10:00:00 +0900'):
    return f'''<rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item><title>{title}</title><pubDate>{date}</pubDate><ht:approx_traffic>500+</ht:approx_traffic><ht:news_item><ht:news_item_title>{title}의 식감 알아보기</ht:news_item_title><ht:news_item_url>https://example.com/news</ht:news_item_url><ht:news_item_source>테스트</ht:news_item_source></ht:news_item></item></channel></rss>'''.encode()

class MVPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Storage.create(Path(self.temp.name) / 'test.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_fields_and_missing(self):
        items, warnings = parse(fixture())
        self.assertEqual(items[0]['traffic'], '500+')
        self.assertEqual(items[0]['published_at'], '2026-10-13T01:00:00+00:00')
        self.assertEqual(items[0]['news'][0]['source'], '테스트')
        self.assertIsNone(items[0]['related_queries'])
        items, _ = parse(b'<rss><channel><item><title>unknown</title></item></channel></rss>')
        self.assertIsNone(items[0]['traffic'])
        self.assertIsNone(items[0]['published_at'])
        with self.assertRaises(ValueError):
            parse(b'<html>failure</html>')

    def test_classifier_new_names_and_mixed_context(self):
        self.assertEqual(normalize('  구름볼\n 특별판 '), '구름볼 특별판')
        self.assertEqual(classify({'keyword':'구름볼','news':[{'title':'새 디저트 구름볼 레시피'}]})['classification'], 'FOOD')
        self.assertEqual(classify({'keyword':'구름볼','news':[{'title':'구름볼의 식감'}]})['classification'], 'REVIEW')
        self.assertEqual(classify({'keyword':'구름볼','news':[{'title':'디저트 시식'}]})['classification'], 'NON_FOOD')
        self.assertEqual(classify({'keyword':'대통령 선거','news':[]})['classification'], 'NON_FOOD')
        self.assertEqual(classify({'keyword':'음료 회사','news':[{'title':'음료 회사 주가 급등'}]})['classification'], 'NON_FOOD')

    def test_dedup_and_reappearance_and_reanalysis(self):
        service = CollectionService(self.store, fetcher=lambda _: (fixture(), 200))
        service.collect(); service.collect()
        self.assertEqual(self.store.status()['total_keywords'], 1)
        self.assertEqual(self.store.status()['observation_count'], 2)
        service.fetcher = lambda _: (fixture(date='Tue, 13 Oct 2026 11:00:00 +0900'), 200)
        service.collect()
        self.assertEqual(self.store.status()['total_keywords'], 2)
        self.assertEqual(self.store.trends()[0]['occurrence_count'], 2)
        self.store.reanalyze()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM runs WHERE raw_response IS NOT NULL').fetchone()[0], 3)

    def test_failures_keep_existing_data_and_raw(self):
        service = CollectionService(self.store, fetcher=lambda _: (fixture(), 200))
        service.collect()
        for error in [TimeoutError('timeout'), HTTPError('rss',503,'unavailable',{},None)]:
            def fail(_, error=error):
                raise error
            service.fetcher = fail
            self.assertFalse(service.collect()['success'])
        service.fetcher = lambda _: (b'<html>broken</html>', 200)
        self.assertFalse(service.collect()['success'])
        self.assertEqual(len(self.store.trends()), 1)
        self.assertEqual(self.store.status()['latest_run']['success'], 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT raw_response FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0], b'<html>broken</html>')

    def test_concurrent_collection(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(_):
            entered.set(); release.wait(3)
            return fixture(), 200
        service = CollectionService(self.store, fetcher=fetch)
        worker = threading.Thread(target=service.collect)
        worker.start(); self.assertTrue(entered.wait(2))
        self.assertFalse(service.collect()['accepted'])
        release.set(); worker.join()

    def test_api_and_manual_collection(self):
        service = CollectionService(self.store, fetcher=lambda _: (fixture(), 200))
        server = make_server(service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(Request(base+'/api/collect',method='POST')) as r:
                self.assertTrue(json.load(r)['success'])
            with urlopen(base+'/api/trends?classification=REVIEW') as r:
                items = json.load(r)['items']
                self.assertEqual(len(items),1)
            with urlopen(base+f'/api/trends/{items[0]["id"]}') as r:
                self.assertIn('raw_xml',json.load(r))
            with urlopen(base+'/api/status') as r:
                self.assertEqual(json.load(r)['total_keywords'],1)
            with urlopen(base+'/') as r:
                self.assertIn('음식 트렌드 탐지',r.read().decode())
        finally:
            server.shutdown(); server.server_close(); thread.join()

if __name__ == '__main__':
    unittest.main()
