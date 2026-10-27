import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen
from food_trends.classifier import classify, VERSION
from food_trends.storage import Storage
from food_trends.service import CollectionService
from food_trends.api import make_server
from food_trends.audit import audit, evaluate
from test_mvp import fixture


class TargetEvidenceTests(unittest.TestCase):
    def test_regressions_and_novel_names(self):
        cases = [
            ('광주송정역',['광주 송정역 남도 먹거리 축제','광주송정역 남도 먹거리와 공연'],'NON_FOOD'),
            ('새솔역',['새솔역 디저트 판매 축제','새솔역 남도 먹거리'],'NON_FOOD'),
            ('농민',['농민 협회 디지털 모델 출시'],'NON_FOOD'),
            ('김민석',['대법원 와인 구입','김민석 헌법훼손 검토'],'NON_FOOD'),
            ('최지우',['최지우 붕어빵 딸 공개'],'NON_FOOD'),
            ('식품의약품안전처장',['식품의약품안전처장 국감'],'NON_FOOD'),
            ('여행',['프리미엄 여행 상품 출시'],'NON_FOOD'),
            ('청바지',['아이스크림 시식','청바지 패션'],'NON_FOOD'),
            ('애플',['애플 스마트홈 출시'],'NON_FOOD'),
            ('렉서스',['렉서스 전기차 출시'],'NON_FOOD'),
            ('레임덕',['대통령 소통은 어떡하나'],'NON_FOOD'),
            ('아리엘 후라도',['감독 떡잎 알아봤다'],'NON_FOOD'),
            ('아무이름',['신제품 출시 품절','디저트 판매'],'NON_FOOD'),
            ('쿠키런',['쿠키런 게임 출시'],'NON_FOOD'),
            ('배우 나누리',['배우 나누리, 쿠키 시식'],'NON_FOOD'),
            ('나누리',['배우 나누리 디저트 판매'],'NON_FOOD'),
            ('두바이 쫀득 쿠키',['신상 디저트 판매 품절'],'FOOD'),
            ('두쫀쿠',["신상 디저트 '두쫀쿠' 판매 열풍"],'FOOD'),
            ('탕후루',["탕후루는 디저트, 구매 인기"],'FOOD'),
            ('미등록파란볼',["미등록파란볼은 신종 디저트, 판매 시작"],'FOOD'),
            ('무작위자주볼',["신상 디저트 ‘무작위자주볼’ 출시"],'FOOD'),
            ('두쫀쿠',['두쫀쿠의 식감 인기'],'REVIEW'),
            ('처음본음식',['처음본음식 레시피 공개'],'REVIEW'),
            ('오로라브랜드',['오로라브랜드, 쿠키 신제품 출시'],'FOOD'),
            ('오로라브랜드',['오로라브랜드 주가 급등','신메뉴 쿠키 판매'],'NON_FOOD'),
            ('호박',["호박 한 개 무게 1.4t",'최대 호박 선발대회'],'REVIEW'),
            ('호박',["채소 호박 레시피"],'FOOD'),
            ('완전미지수',[],'NON_FOOD'),
            ('미등록파란볼',["신상 디저트 '다른파란볼' 판매"],'NON_FOOD'),
            ('새브랜드',['새브랜드리뉴얼 디저트 판매'],'NON_FOOD'),
        ]
        for keyword, titles, expected in cases:
            with self.subTest(keyword=keyword, titles=titles):
                result = classify({'keyword':keyword,'news':[{'title':t} for t in titles]})
                self.assertEqual(result['classification'],expected)
                self.assertTrue(0 <= result['rule_score'] <= 100)
                self.assertIn('확률',result['score_basis'])
                self.assertEqual(result['classifier_version'], VERSION)


class AuditStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.db'

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_migration_preserves_raw_and_old_classification(self):
        # Build actual v1 schema, not a copy of v2 migration implementation.
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE candidates(item_id INTEGER PRIMARY KEY,normalized_name TEXT,
                classification TEXT,reasons TEXT,classifier_version TEXT);
                INSERT INTO candidates VALUES (1,'광주송정역','FOOD','["먹거리"]','context-rules-v1');''')
        db.close()
        store = Storage.create(self.path)
        service = CollectionService(store,fetcher=lambda _: (fixture('광주송정역'),200))
        service.collect()
        record = store.trends(item_id=1)[0]
        self.assertEqual(record['classification'],'NON_FOOD')
        self.assertEqual(record['entity_type'],'LOCATION')
        self.assertIsNotNone(record['classified_at'])
        with store.connect() as db:
            previous = json.loads(db.execute('SELECT result_json FROM classification_history').fetchone()[0])
            self.assertEqual(previous['classifier_version'],'context-rules-v1')
            self.assertIsNone(previous['classified_at'])
            raw_before = db.execute('SELECT raw_response FROM runs').fetchone()[0]
        store.reanalyze(); store.reanalyze()
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT raw_response FROM runs').fetchone()[0],raw_before)
            self.assertEqual(db.execute('SELECT count(*) FROM classification_history').fetchone()[0],1)

    def test_recent_stats_dedup_failure_and_export(self):
        store = Storage.create(self.path)
        service = CollectionService(store,fetcher=lambda _: (fixture('두바이 쿠키'),200))
        service.collect()
        status = store.status()
        self.assertEqual(status['latest_run']['new_food_count'],1)
        self.assertEqual(status['latest_run']['current_food_count'],1)
        self.assertEqual(status['latest_run']['discovery_state'],'NEW_ITEMS')
        service.collect()
        self.assertEqual(store.status()['latest_run']['new_food_count'],0)
        self.assertEqual(store.status()['latest_run']['discovery_state'],'NO_NEW_ITEMS')
        before = store.export()
        self.assertEqual(len(before['items']),1)
        self.assertEqual(len(before['observations']),2)
        self.assertIn('classification_json',before['observations'][0])
        self.assertIsNotNone(before['runs'][0]['raw_response_base64'])
        self.assertNotIn('instagram_state',before)
        def fail(_):
            raise TimeoutError('offline')
        service.fetcher=fail
        service.collect()
        status=store.status()
        self.assertEqual(status['food_count'],1)
        self.assertIsNone(status['latest_run']['current_food_count'])
        self.assertIsNone(status['latest_run']['food_count'])
        self.assertEqual(status['latest_run']['discovery_state'],'COLLECTION_FAILED')

    def test_export_api_and_audit_integrity(self):
        store=Storage.create(self.path)
        service=CollectionService(store,fetcher=lambda _: (fixture(),200))
        service.collect()
        report=audit(self.path,Path(self.temp.name)/'audit')
        self.assertEqual(report['integrity_errors'],[])
        self.assertEqual(report['parsed_success_raw_runs'],1)
        self.assertEqual(report['sqlite_integrity_check'],['ok'])
        self.assertEqual(report['foreign_key_errors'],[])
        self.assertIsNone(report['evaluation']['precision'])
        server=make_server(service,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/export') as response:
                self.assertIn('attachment',response.headers['Content-Disposition'])
                exported=json.load(response)
                self.assertEqual(len(exported['items']),1)
                self.assertEqual(exported['candidates'][0]['entity_type'],'UNKNOWN')
        finally:
            server.shutdown();server.server_close();thread.join()

    @unittest.skipUnless(os.name=='nt','Windows port reuse regression')
    def test_second_server_cannot_bind_same_port(self):
        store=Storage.create(self.path)
        service=CollectionService(store)
        first=make_server(service,port=0)
        try:
            with self.assertRaises(OSError):
                make_server(service,port=first.server_port)
        finally:
            first.server_close()

    def test_agent_labels_cannot_be_used_as_ground_truth(self):
        rows=[{'id':1,'classification':'FOOD'}]
        report=evaluate(rows,[{'item_id':1,'actual_food':True,'review_status':'agent_reviewed'}])
        self.assertEqual(report['human_verified_count'],0)
        self.assertIsNone(report['precision'])
        verified=evaluate(rows,[{'item_id':1,'actual_food':False,'review_status':'human_verified'}])
        self.assertEqual(verified['counts']['fp'],1)
        self.assertEqual(verified['precision'],0)
