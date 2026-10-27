"""A1-A3 regression tests: isolated databases and injected transports only."""
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from food_trends.api import make_server
from food_trends.classifier import classify
from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig
from food_trends.contracts import envelope, encode, policy_for, require_operation
from food_trends.instagram_graph import GraphClient
from food_trends.instagram_service import InstagramService
from food_trends.migrations import migrate, schema_version, MigrationRequired
from food_trends.naver_datalab import NaverClient
from food_trends.naver_service import NaverService
from food_trends.service import CollectionService
from food_trends.storage import Storage, utcnow
from food_trends.youtube import YouTubeClient, parse_video
from food_trends.youtube_service import YouTubeService
from test_instagram import Transport as InstagramTransport
from test_naver import Transport as NaverTransport
from test_youtube import Transport as YouTubeTransport, resource
from test_mvp import fixture


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return '\n'.join(db.iterdump())


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fixture.sqlite3'
        self.storage = Storage.create(self.path)
        self.rss = CollectionService(self.storage, fetcher=lambda _: (fixture('쿠키'), 200))
        self.rss.collect()
        self.ig_config = InstagramConfig(token='fixture-token')
        self.ig_sender = InstagramTransport()
        self.ig = InstagramService(self.storage, self.ig_config, GraphClient(self.ig_config, self.ig_sender))
        self.naver_config = NaverConfig(client_id='fixture-id', client_secret='fixture-secret')
        self.naver = NaverService(self.storage, self.naver_config,
                                 NaverClient(self.naver_config, NaverTransport(), lambda _: None))
        self.youtube_config = YouTubeConfig(api_key='fixture-key', discovery_queries=('fixture discovery',))
        self.youtube = YouTubeService(self.storage, self.youtube_config,
                                      YouTubeClient(self.youtube_config, YouTubeTransport(), lambda _: None))

    def tearDown(self):
        self.temp.cleanup()

    def links(self):
        with self.storage.reader() as db:
            return [tuple(r) for r in db.execute('SELECT * FROM youtube_candidate_foods ORDER BY candidate_id')]

    def test_a1_repeated_collection_and_reanalysis_preserve_links(self):
        self.youtube.store.sync_queries(self.youtube_config)
        before = self.links()
        self.assertEqual(len(before), 1)
        for _ in range(3):
            result = self.rss.collect()
            self.assertTrue(result['success'])
            self.assertEqual(result['new_count'], 0)
            self.storage.reanalyze()
            self.assertEqual(self.links(), before)
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM classification_history').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 1)

    def test_a1_version_and_classification_changes_keep_id_and_history(self):
        self.youtube.store.sync_queries(self.youtube_config)
        before = self.links()
        old = self.storage.trends()[0]
        def upgraded(item):
            return dict(classify(item), classifier_version='fixture-next-version', classification='REVIEW')
        with patch('food_trends.storage.classify', side_effect=upgraded):
            self.storage.reanalyze()
            self.storage.reanalyze()
        self.assertEqual(self.links(), before)
        current = self.storage.trends()[0]
        self.assertEqual((current['id'], current['classification']), (old['id'], 'REVIEW'))
        with self.storage.reader() as db:
            rows = db.execute('SELECT result_json FROM classification_history').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0][0])['classifier_version'], old['classifier_version'])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_a1_new_candidate_still_created(self):
        self.rss.fetcher = lambda _: (fixture('케이크'), 200)
        self.assertEqual(self.rss.collect()['new_count'], 1)
        self.assertEqual(self.storage.status()['total_keywords'], 2)

    def test_readers_reject_writes_and_constructor_does_not_create_database(self):
        with self.storage.reader() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM items')
        absent = Path(self.temp.name) / 'missing' / 'new.sqlite3'
        with self.assertRaises(MigrationRequired):
            Storage(absent)
        self.assertFalse(absent.parent.exists())

    def test_all_constructors_and_direct_status_are_pure_reads(self):
        before = snapshot(self.path)
        storage = Storage(self.path)
        ig = InstagramService(storage, self.ig_config, self.ig.client)
        naver = NaverService(storage, self.naver_config, self.naver.client)
        youtube = YouTubeService(storage, self.youtube_config, self.youtube.client)
        storage.status(); storage.trends(); storage.export()
        ig.status(); naver.status(); youtube.status()
        self.assertEqual(snapshot(self.path), before)

    def test_all_get_routes_are_readonly_even_with_pending_targets_and_expired_data(self):
        self.youtube.collect(verification=True)
        self.ig.start_tracking('두쫀쿠')
        with self.storage.connect() as db:
            db.execute("DELETE FROM naver_targets")
            db.execute("UPDATE youtube_videos SET expires_at='2000-01-01T00:00:00+00:00'")
            db.execute("UPDATE youtube_matches SET matched_at='2000-01-01T00:00:00+00:00'")
            db.execute("UPDATE youtube_queries SET page_token='expired',token_at='2000-01-01T00:00:00+00:00'")
        before = snapshot(self.path)
        server = make_server(self.rss, port=0, instagram=self.ig, naver=self.naver, youtube=self.youtube)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connect = self.storage.connect
        def readonly_only(*args, **kwargs):
            if not kwargs.get('readonly'):
                raise AssertionError('GET attempted to open a writable connection')
            return connect(*args, **kwargs)
        try:
            with patch.object(self.storage, 'connect', side_effect=readonly_only):
                for route in ('/api/status', '/api/trends', '/api/trends/1', '/api/export',
                              '/api/instagram', '/api/instagram/media?hashtag_id=1',
                              '/api/naver', '/api/youtube'):
                    with self.subTest(route=route):
                        with urlopen(f'http://127.0.0.1:{server.server_port}{route}') as response:
                            data = json.load(response)
                            if route == '/api/youtube':
                                self.assertEqual(data['stored_videos'], 0)
                                self.assertEqual(data['videos'], [])
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(snapshot(self.path), before)

    def test_changed_credentials_status_is_local_without_state_reset(self):
        self.ig.prepare_credentials()
        self.ig.store.state('token', {'status': 'invalid', 'expires_at': None})
        before = snapshot(self.path)
        changed = InstagramService(self.storage, replace(self.ig_config, token='different-fixture-token'), self.ig.client)
        self.assertEqual(changed.status()['token']['status'], 'unchecked')
        self.assertEqual(changed.status()['connection']['status'], 'unverified')
        self.assertEqual(snapshot(self.path), before)

    def test_explicit_maintenance_purges_without_network(self):
        self.youtube.collect(verification=True)
        self.ig.start_tracking('두쫀쿠')
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        with self.storage.connect() as db:
            db.execute('UPDATE youtube_videos SET expires_at=?', (old,))
            db.execute('UPDATE instagram_media SET caption_captured_at=?', (old,))
        before_calls = len(self.ig_sender.calls)
        self.assertGreater(self.ig.maintenance(), 0)
        self.assertGreater(self.youtube.store.purge(), 0)
        self.assertEqual(len(self.ig_sender.calls), before_calls)
        self.assertEqual(self.youtube.status()['stored_videos'], 0)

    def test_expired_caption_is_hidden_on_read_and_legacy_cleanup_needs_no_reprocessing(self):
        self.ig.start_tracking('두쫀쿠')
        old = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        with self.storage.connect() as db:
            db.execute('UPDATE instagram_media SET caption_captured_at=?', (old,))
            db.execute('''INSERT INTO instagram_media(id,first_seen,last_seen,caption)
                VALUES ('unprocessed-legacy',?,?,?)''', (old, old, 'expired fixture caption'))
        before = snapshot(self.path)
        view = self.ig.status(include_media=True)['hashtags'][0]
        self.assertIsNone(view['media'][0]['caption'])
        self.assertEqual(view['analysis']['raw_captions_retained'], 0)
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual(self.ig.maintenance(), 2)
        with self.storage.reader() as db:
            self.assertIsNone(db.execute("SELECT caption FROM instagram_media WHERE id='unprocessed-legacy'").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM instagram_caption_analysis WHERE media_id='unprocessed-legacy'").fetchone()[0], 0)

    def test_local_maintenance_thread_runs_with_automatic_collection_disabled(self):
        complete = threading.Event()
        maintenance = self.ig.maintenance
        def maintain_once():
            result = maintenance()
            self.ig.stop_event.set()
            complete.set()
            return result
        with patch.object(self.ig, 'maintenance', side_effect=maintain_once), \
                patch.object(self.ig, 'collect', side_effect=AssertionError('automatic collection forbidden')):
            try:
                self.ig.start(automatic=False)
                self.assertTrue(complete.wait(3))
            finally:
                self.ig.stop_event.set()
                self.ig.thread.join(3)
        self.assertFalse(self.ig.thread.is_alive())
        self.assertEqual(self.ig_sender.calls, [])

    def test_cli_migration_commands_are_explicit_and_status_does_not_write(self):
        import os
        from food_trends.__main__ import main
        path = Path(self.temp.name) / 'cli.sqlite3'
        with patch.dict(os.environ, {'DATABASE_PATH': str(path)}, clear=True), \
                patch('food_trends.__main__.load_env'):
            for command in ('--migration-status', '--migrate'):
                with patch('sys.argv', ['food_trends', command]), redirect_stdout(io.StringIO()) as stream:
                    main()
                result = json.loads(stream.getvalue())
                if command == '--migration-status':
                    self.assertEqual(result['version'], 0)
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(result['applied'], [1, 2])
            before = snapshot(path)
            with patch('sys.argv', ['food_trends', '--youtube-status']), redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as exit_code:
                main()
            self.assertEqual(exit_code.exception.code, 0)
            self.assertEqual(snapshot(path), before)

    def test_new_contracts_are_mock_and_naver_never_persists_response_values(self):
        self.naver.query_keywords(['fixture food'])
        self.youtube.collect(verification=True)
        self.ig.start_tracking('두쫀쿠')
        with self.storage.reader() as db:
            for table in ('runs', 'observations', 'naver_calls', 'instagram_runs', 'instagram_media', 'youtube_videos'):
                contract = json.loads(db.execute(f'SELECT contract_json FROM {table} WHERE contract_json IS NOT NULL LIMIT 1').fetchone()[0])
                self.assertEqual(contract['origin'], 'mock')
                self.assertIn('policy_version', contract)
                self.assertNotIn('fixture-secret', encode(contract))
            nav = db.execute('SELECT contract_json FROM naver_calls').fetchone()[0]
            self.assertNotIn('73.125', nav)
            self.assertNotIn('"ratio"', nav)
            self.assertFalse(json.loads(nav)['policy']['response_storage'])

    def test_youtube_legacy_origin_remains_unknown_on_read(self):
        self.youtube.store.save_videos(['abcdefghij1'], [parse_video(resource())])
        before = snapshot(self.path)
        data = self.youtube.status()
        self.assertEqual(data['videos'][0]['provenance']['origin'], 'legacy_unknown')
        self.assertEqual(data['videos'][0]['contract']['origin'], 'legacy_unknown')
        self.assertEqual(snapshot(self.path), before)

    def test_explicit_migration_is_idempotent_and_rolls_back_failed_step(self):
        before = snapshot(self.path)
        self.assertEqual(migrate(self.path)['applied'], [])
        self.assertEqual(snapshot(self.path), before)
        fresh = Path(self.temp.name) / 'rollback.sqlite3'
        def failed(db):
            db.execute('CREATE TABLE should_rollback (id INTEGER)')
            raise RuntimeError('synthetic migration failure')
        with patch('food_trends.migrations.MIGRATIONS', ((1, 'fixture_failure', failed),)):
            with self.assertRaises(RuntimeError):
                migrate(fresh)
        with closing(sqlite3.connect(fresh)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        self.assertEqual(schema_version(fresh), 0)


class ContractTests(unittest.TestCase):
    def test_field_absence_null_empty_and_metric_units_are_distinct(self):
        data = envelope('youtube', 'mock', utcnow(),
                        fields={'description': '', 'like_count': None, 'view_count': '0'})
        self.assertEqual(data['field_states']['description'], 'empty')
        self.assertEqual(data['field_states']['like_count'], 'null')
        self.assertEqual(data['field_states']['view_count'], 'provided')
        self.assertEqual(data['field_states']['comment_count'], 'not_provided')
        self.assertEqual(data['metric_units']['view_count'], 'video_view_count_snapshot')
        self.assertIsNone(data['published_at'])

    def test_contract_rejects_credentials_unknown_origins_and_naive_times(self):
        for request in ({'api_key': 'synthetic-secret'},
                        {'endpoint': 'https://example.com/?access_token=synthetic-secret'},
                        {'endpoint': 'https://example.com/?key=synthetic-secret'},
                        {'endpoint': {'client_id': 'synthetic-credential'}},
                        {'endpoint': {'Authorization': 'Bearer synthetic-secret'}}):
            with self.subTest(request_key=next(iter(request))):
                with self.assertRaises(ValueError):
                    envelope('youtube', 'mock', utcnow(), request=request)
        with self.assertRaises(ValueError):
            envelope('youtube', 'legacy', utcnow())
        with self.assertRaises(ValueError):
            envelope('youtube', 'mock', '2026-10-27T12:00:00')

    def test_policy_is_copied_and_unapproved_operations_are_disabled(self):
        policy = policy_for('youtube')
        policy['retention_days'] = 999
        self.assertEqual(policy_for('youtube')['retention_days'], 29)
        self.assertEqual(policy_for('naver_datalab')['retention_days'], 0)
        self.assertFalse(policy_for('instagram_graph')['expand_retention_allowed'])
        for source in ('youtube', 'naver_datalab', 'instagram_graph'):
            with self.assertRaises(ValueError):
                require_operation(source, 'food_extraction')


if __name__ == '__main__':
    unittest.main()
