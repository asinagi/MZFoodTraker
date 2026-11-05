"""ORACLE-CUTOVER-PREP-1 phase 2: the cut-over rehearsal on a synthetic v0 database (no operational data).

A v0-shaped file (legacy tables only, no migration history) stands for the operational database: inspection and
the backup copy leave it byte-identical, v0 -> v11 keeps every original row and column, the documented local
follow-ups stage what has a staging path and report what has none, the B7 load reconciles, the app API answers
alike from both stores, a re-run writes nothing, and a later load drops expired content. The stand-in runs by
default; the Oracle subclass repeats every case on the disposable MZ_TEST schema.
"""
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from food_trends import cutover
from food_trends.migrations import _legacy_schema
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_repository import TargetRepository
import rehearse_cutover
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema, settle_after_ddl

NOW = datetime(2026, 11, 5, 3, tzinfo=timezone.utc)
SEEN = NOW - timedelta(days=9)
ROOT = Path(__file__).resolve().parents[1]


def build_v0(path):
    """Legacy RSS, Instagram and YouTube rows as the v0 collectors left them (one live_api-provenance video)."""
    s = SEEN.isoformat()
    with closing(sqlite3.connect(path)) as db:
        _legacy_schema(db)
        db.execute("INSERT INTO runs(id,started_at,finished_at,success,item_count,new_count,warnings) VALUES (1,?,?,1,2,2,'[]')", (s, s))
        db.execute("INSERT INTO runs(id,started_at,finished_at,success,error,item_count,new_count,warnings) VALUES (2,?,?,0,'outage',0,0,'[]')", (s, s))
        for n, keyword in enumerate(('두쫀쿠', '마라탕'), 1):
            source_id = hashlib.sha256(keyword.encode()).hexdigest()
            payload = json.dumps({'source_id': source_id, 'keyword': keyword, 'published_at': s, 'traffic': '1,000+',
                                  'news': [{'title': keyword + ' 열풍'}]}, ensure_ascii=False)
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?)', (n, source_id, keyword, s, s, s, payload))
            db.execute('INSERT INTO observations(run_id,item_id,payload) VALUES (1,?,?)', (n, payload))
            db.execute("INSERT INTO candidates(item_id,normalized_name,classification,reasons,classifier_version) VALUES (?,?,?,'[]','rules-v1')",
                       (n, keyword, 'REVIEW' if n == 1 else 'NON_FOOD'))
        db.execute("INSERT INTO classification_history(item_id,recorded_at,result_json) VALUES (1,?,'{}')", (s,))
        db.execute("INSERT INTO instagram_hashtags(id,user_id,name,created_at,active) VALUES (1,'fixture-user','두쫀쿠',?,1)", (s,))
        for media in ('17900000001', '17900000002'):
            db.execute('''INSERT INTO instagram_media(id,published_at,first_seen,last_seen,media_type,caption,caption_captured_at)
                VALUES (?,?,?,?,'IMAGE','#두쫀쿠 맛집',?)''', (media, s, s, s, s))
            db.execute("INSERT INTO instagram_caption_tags VALUES (?,'두쫀쿠','[\"두쫀쿠\"]',?,?,1)", (media, s, s))
            db.execute('INSERT INTO instagram_hashtag_media VALUES (1,?,?,?)', (media, s, s))
        db.execute("INSERT INTO youtube_queries(id,query,mode) VALUES (1,'두쫀쿠','tracking')")
        for video, origin in (('vidLiveApi1', 'live_api'), ('vidLegacy01', 'legacy_unknown')):
            payload = json.dumps({'video_id': video, 'title': '두쫀쿠 먹방', 'description': '', 'published_at': s, 'channel_id': 'channel',
                                  'channel_title': '채널', 'view_count': '10', 'like_count': '1', 'comment_count': '0'}, ensure_ascii=False)
            db.execute('INSERT INTO youtube_videos VALUES (?,?,?,?,?)', (video, payload, s, s, (SEEN + timedelta(days=29)).isoformat()))
            db.execute('INSERT INTO youtube_video_provenance VALUES (?,?,?,NULL)', (video, origin, s))
            db.execute('INSERT INTO youtube_matches VALUES (1,?,?)', (video, s))
        # A call of the still open Los Angeles day: the only legacy usage the ledger keeps.
        db.execute("INSERT INTO youtube_calls(id,day,endpoint,query_id,started_at,finished_at,status) VALUES (1,'2026-11-04','search',1,?,?,'succeeded')",
                   (NOW.isoformat(), NOW.isoformat()))
        db.commit()


class CutoverRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'operational-v0.sqlite3'
        build_v0(self.source)
        self.copy = Path(self.temp.name) / 'copy.sqlite3'

    def make_target(self, name='target'):
        return SQLiteTarget(Path(self.temp.name) / (name + '.sqlite3'))

    def repo(self, at, name='target'):
        db = self.make_target(name)
        if hasattr(db, 'close'):
            self.addCleanup(db.close)
        return TargetRepository(db, clock=lambda: at)

    def prepared(self):
        cutover.backup(self.source, self.copy)
        return cutover.prepare(self.copy, NOW)

    def test_inspection_and_copy_leave_the_source_untouched(self):
        before, raw = cutover.inspect(self.source), self.source.read_bytes()
        self.assertEqual((before['schema_version'], before['integrity'], before['tables']['youtube_videos']), (0, 'ok', 2))
        copied = cutover.backup(self.source, self.copy)
        self.assertEqual(copied['tables'], before['tables'])
        with self.assertRaises(FileExistsError):
            cutover.backup(self.source, self.copy)
        self.assertEqual(cutover.inspect(self.source), before)
        self.assertEqual(self.source.read_bytes(), raw)

    def test_prepare_keeps_originals_and_reports_what_has_no_staging_path(self):
        prepared = self.prepared()
        self.assertEqual((prepared['migration']['applied'], prepared['original_tables']), (list(range(1, 12)), 30))
        self.assertTrue(prepared['original_rows_and_columns_preserved'])
        self.assertEqual((prepared['staged_runs'], prepared['legacy_backfill']['instagram_graph']['staged_items'],
                          prepared['legacy_backfill']['youtube']['staged_items'], prepared['foods_approved']), (2, 2, 1, 0))
        lineage = cutover.lineage(self.copy)
        self.assertEqual({k: lineage[k]['mismatches'] for k in ('rss_runs', 'rss_items', 'youtube_videos', 'instagram_media')}, dict.fromkeys(
            ('rss_runs', 'rss_items', 'youtube_videos', 'instagram_media'), {}))
        self.assertEqual(lineage['youtube_videos']['unstaged'], {'no_staging_path_for_origin_live_api': 1})
        self.assertEqual(lineage['rss_observations'], {'expected': 2, 'run_items': 2, 'traffic_metrics': 2})
        self.assertEqual(lineage['search_evidence']['item_query_matches_by_ref'], {'instagram_hashtag': 2, 'youtube_query': 1})
        dispositions = cutover.table_dispositions(self.copy)
        self.assertFalse([t for t, v in dispositions.items() if v['disposition'] == 'UNCLASSIFIED'])
        self.assertEqual((dispositions['youtube_videos']['disposition'], dispositions['youtube_runs']['disposition'],
                          dispositions['youtube_calls']['disposition']), ('MIGRATED', 'EXCLUDED', 'LEDGER'))
        self.assertEqual(cutover.source_categories(self.copy)['food_identity'], dict.fromkeys(('food_entities', 'food_aliases', 'food_relations'), 0))

    def test_load_reconciles_answers_alike_and_a_rerun_writes_nothing(self):
        self.prepared()
        storage, repo = Storage(self.copy), self.repo(NOW)
        loaded = rehearse_cutover.load_and_compare(self.copy, repo, NOW, storage)
        self.assertTrue(loaded['reconciliation']['consistent'], loaded['reconciliation']['summary'])
        self.assertEqual(loaded['reconciliation']['hash_equal_tables'], loaded['reconciliation']['compared_tables'])
        self.assertTrue(loaded['keys']['clean'])
        self.assertEqual((loaded['legacy_usage']['rows'], loaded['legacy_usage']['by_evidence']['LEGACY_UNKNOWN_ORIGIN']), (1, 1))
        categories = loaded['target_categories']
        self.assertEqual(categories['source_contents_by_source_origin'], {'google_trends_rss|legacy_unknown': 2, 'instagram_graph|legacy_unknown': 2,
                                                                          'youtube|live_api': 1, 'youtube|legacy_unknown': 1})
        self.assertEqual(categories['candidates_and_decisions']['rss_classifications'], {'NON_FOOD': 1, 'REVIEW': 1})
        self.assertEqual(categories['captions_held'], 2)
        settle_after_ddl(repo.db)  # the app API's read-only snapshot cannot read tables defined a moment ago on Oracle
        parity = cutover.api_parity(storage, repo, lambda: NOW)
        self.assertEqual({k: v['mismatches'] for k, v in parity.items()}, {'live_api': [], 'mock': []})
        self.assertGreater(parity['live_api']['requests'], 10)
        again = rehearse_cutover.rerun(self.copy, repo, NOW, storage)
        self.assertEqual((again['completed_batch_writes'], again['full_pass_writes'], again['row_counts_unchanged'], again['consistent_after']),
                         (0, 0, True, True))

    def test_a_later_load_drops_expired_content_and_closed_captions(self):
        self.prepared()
        later = NOW + timedelta(days=30)  # past the videos' 29-day expiry and the 30-day caption window
        loaded = rehearse_cutover.load_and_compare(self.copy, self.repo(later, 'later'), later, Storage(self.copy))
        self.assertTrue(loaded['reconciliation']['consistent'], loaded['reconciliation']['summary'])
        self.assertEqual(loaded['tables']['source_contents']['dispositions'], {'EXCLUDED_EXPIRED:source_retention_elapsed': 2})
        self.assertEqual(loaded['tables']['source_items']['dispositions'], {'EXCLUDED_EXPIRED:source_retention_elapsed': 1})
        self.assertIn('EXCLUDED_EXPIRED:parent_excluded:run_items', loaded['tables']['item_query_matches']['dispositions'])
        self.assertEqual(loaded['target_categories']['captions_held'], 0)
        self.assertEqual(loaded['reconciliation']['checks']['expired_rows_in_target'], 0)

    def test_the_guard_refuses_writes_to_the_operational_file_and_the_network(self):
        uri = self.source.resolve().as_uri() + '?mode=ro'
        with rehearse_cutover.guarded(self.source.resolve()) as attempts:
            with self.assertRaises(RuntimeError):
                sqlite3.connect(self.source)
            with self.assertRaises(RuntimeError):
                sqlite3.connect(self.source.resolve().as_uri() + '?mode=rw', uri=True)
            sqlite3.connect(uri, uri=True).close()
            with self.assertRaises(RuntimeError), closing(socket.socket()) as sock:
                sock.connect(('203.0.113.7', 443))
        self.assertEqual(attempts, ['operational_database_write_connection', 'operational_database_write_connection', 'external_socket'])
        sqlite3.connect(self.copy).close()  # restored on exit


class CutoverCommandTests(unittest.TestCase):
    """The runbook's commands on a prepared copy configured as DATABASE_PATH, with the SQLite stand-in as the target."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        source, self.copy = Path(self.temp.name) / 'operational-v0.sqlite3', Path(self.temp.name) / 'copy.sqlite3'
        build_v0(source)
        cutover.backup(source, self.copy)
        cutover.prepare(self.copy, NOW)
        self.target = str(Path(self.temp.name) / 'target.sqlite3')

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != 'DATABASE_PATH'}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8', DATABASE_PATH=str(self.copy))
        return subprocess.run([sys.executable, '-B', '-m', 'food_trends', *args, '--target-sqlite', self.target], cwd=self.temp.name, env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=300)

    def test_load_reconcile_and_recovery_commands(self):
        unready = self.cli('--target-load', 'sqlite')
        self.assertEqual(unready.returncode, 2)  # the target schema is migrated explicitly first
        self.assertEqual(self.cli('--target-migrate', 'sqlite').returncode, 0)
        load = self.cli('--target-load', 'sqlite')
        self.assertEqual(load.returncode, 0, load.stderr)
        loaded = json.loads(load.stdout)
        self.assertEqual((loaded['consistent'], loaded['rejected_rows']), (True, 0))  # usage windows follow the wall clock here
        self.assertGreater(loaded['loaded_rows'], 0)
        planned = json.loads(self.cli('--recovery', 'plan', '--recovery-target', 'sqlite').stdout)
        self.assertEqual((planned['summary']['target_only'], planned['summary']['conflicts'], planned['baseline']['verified']), (0, 0, True))
        refused = self.cli('--recovery', 'apply', '--recovery-target', 'sqlite')
        self.assertEqual((refused.returncode, refused.stderr.strip()), (2, 'target error: operational_database_requires_approval'))
        applied = json.loads(self.cli('--recovery', 'apply', '--recovery-target', 'sqlite', '--allow-operational-database').stdout)
        self.assertEqual((applied['converged'], applied['writes']), (True, {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0}))


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleCutoverRehearsalTests(CutoverRehearsalTests):
    """The same rehearsal cases loading into the real disposable Oracle test schema."""

    def make_target(self, name='target'):
        db = oracle_target()
        reset_target_schema(db)
        return db


if __name__ == '__main__':
    unittest.main()
