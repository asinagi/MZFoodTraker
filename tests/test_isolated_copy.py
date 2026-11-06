"""ORACLE-LIVE-VERIFICATION-READY-1 Phase A: the isolated v11 copy of the operational database and its path guard.

A v0-shaped file (test_cutover.build_v0, no operational data) configured as DATABASE_PATH stands for the operational
database. isolate() copies it with the backup API from a read-only connection, marks the copy (application_id) and
prepares only the copy to v11 as G-OPS-3 would; the source stays byte-identical and a second preparation stages nothing
twice. Every write path refuses the operational file: an operational (by name, path, DATABASE_PATH or a hard link),
existing or Git-tracked destination is refused before anything is copied, and --isolated-database accepts only a marked
copy that is not an operational database, so the B7 load, usage handover, reverse recovery and the resume check run on
the copy without the operational switch, and never on the operational file.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from food_trends import cutover
from food_trends.__main__ import main
from food_trends.reverse_recovery import RecoveryRefused
from food_trends.storage import Storage
from test_cutover import NOW, build_v0

FACTS = ('sha256', 'bytes', 'modified_at', 'wal_bytes', 'schema_version', 'integrity', 'tables')


class IsolatedCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.operational = self.dir / 'operational-v0.sqlite3'
        build_v0(self.operational)
        env = patch.dict(os.environ, {'DATABASE_PATH': str(self.operational)})  # the application's configured database
        env.start()
        self.addCleanup(env.stop)
        self.before = cutover.inspect(self.operational)

    def assertOperationalUnchanged(self):
        after = cutover.inspect(self.operational)
        self.assertEqual({k: after[k] for k in FACTS}, {k: self.before[k] for k in FACTS})

    def cli(self, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with patch('sys.argv', ['food_trends', *argv]), patch('food_trends.__main__.load_env'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_isolate_reads_the_source_only_and_prepares_a_marked_v11_copy(self):
        copy = self.dir / 'isolated' / 'rehearsal.sqlite3'
        report = cutover.isolate(self.operational, copy, NOW)
        self.assertOperationalUnchanged()
        self.assertTrue(all(report['source_unchanged'].values()), report['source_unchanged'])
        self.assertTrue(report['isolated'] and report['copy_equal_source_before_migration'])
        self.assertTrue(report['prepare']['original_rows_and_columns_preserved'])
        self.assertEqual({k: report['copy'][k] for k in ('schema_version', 'integrity', 'foreign_key_errors', 'food_entities')},
                         dict(schema_version=11, integrity='ok', foreign_key_errors=0, food_entities=0))
        self.assertEqual((report['prepare']['foods_approved'], report['http_requests']), (0, 0))
        self.assertGreater(report['prepare']['staged_runs'], 0)
        self.assertEqual(cutover.isolated(copy), copy.resolve())
        self.assertEqual(Storage(copy).path, str(copy.resolve()))  # a current store the rehearsal commands open
        # Preparing the copy again stages no new run and changes no row (the logical legacy runs and the links are re-applied
        # idempotently): nothing is loaded twice.
        prepared, _ = cutover.digests(copy)
        again = cutover.prepare(copy, NOW)
        self.assertEqual((again['staged_runs'], again['migration']['applied']), (0, []))
        self.assertEqual(cutover.digests(copy)[0], prepared)
        # A second copy of the same source at the same instant holds the same data (the migration log keeps its own clock and
        # DQ results have random ids).
        second = self.dir / 'second.sqlite3'
        cutover.isolate(self.operational, second, NOW)
        _, columns = cutover.digests(copy)
        columns = {t: [c for c in names if (t, c) != ('quality_results', 'result_id')] for t, names in columns.items() if t != 'schema_migrations'}
        self.assertEqual(cutover.digests(copy, columns)[0], cutover.digests(second, columns)[0])
        self.assertOperationalUnchanged()

    def test_no_write_path_reaches_the_operational_file(self):
        linked = self.dir / 'linked.sqlite3'
        os.link(self.operational, linked)  # the same file under another name
        named = self.dir / 'elsewhere' / 'trends.sqlite3'  # the operational file's name in any folder
        existing = Path(Storage.create(self.dir / 'existing.sqlite3').path)
        stale = self.dir / 'stale.sqlite3'
        Path(str(stale) + '-wal').write_bytes(b'')  # a leftover journal is never adopted
        tracked = self.dir / 'copy.json'  # a name the repository would not keep out of Git
        refused = {}
        for name, destination in (('configured', self.operational), ('linked', linked), ('named', named), ('existing', existing),
                                  ('stale', stale), ('tracked', tracked)):
            with self.assertRaises(RecoveryRefused) as stop:
                cutover.isolate(self.operational, destination, NOW)
            refused[name] = stop.exception.code
        self.assertEqual(refused, dict(configured='isolated_copy_is_operational', linked='isolated_copy_is_operational',
                                       named='isolated_copy_is_operational', existing='isolated_copy_exists', stale='isolated_copy_exists',
                                       tracked='isolated_copy_name_not_ignored'))
        self.assertEqual([p.exists() for p in (named.parent, stale, tracked)], [False, False, False])
        copy = self.dir / 'copy.sqlite3'
        cutover.isolate(self.operational, copy, NOW)
        unmarked = Path(Storage.create(self.dir / 'unmarked.sqlite3').path)
        refused = {}
        for name, path in (('configured', self.operational), ('linked', linked), ('unmarked', unmarked), ('missing', self.dir / 'missing.sqlite3')):
            with self.assertRaises(RecoveryRefused) as stop:
                cutover.isolated(path)
            refused[name] = stop.exception.code
        with patch.dict(os.environ, {'DATABASE_PATH': str(copy)}), self.assertRaises(RecoveryRefused) as stop:
            cutover.isolated(copy)  # a copy the application is configured to use is its operational database
        refused['configured_copy'] = stop.exception.code
        self.assertEqual(refused, dict(configured='isolated_database_is_operational', linked='isolated_database_is_operational',
                                       unmarked='isolated_database_not_marked', missing='isolated_database_missing',
                                       configured_copy='isolated_database_is_operational'))
        self.assertOperationalUnchanged()

    def test_the_rehearsal_commands_write_the_marked_copy_and_never_the_operational_file(self):
        copy, target = self.dir / 'copy.sqlite3', self.dir / 'target.sqlite3'
        code, out, err = self.cli('--isolate-database', str(copy))
        self.assertEqual(code, 0, err)
        created = json.loads(out)
        self.assertEqual((created['isolated'], created['copy']['schema_version'], all(created['source_unchanged'].values())), (True, 11, True))
        self.assertEqual(self.cli('--isolate-database', str(copy))[::2], (2, 'isolation error: isolated_copy_exists\n'))
        stand_in = ('--target-sqlite', str(target))
        isolated = ('--isolated-database', str(copy))
        self.assertEqual(self.cli('--target-migrate', 'sqlite', *stand_in)[0], 0)
        code, out, err = self.cli('--target-load', 'sqlite', *stand_in, *isolated)
        self.assertEqual((code, json.loads(out)['consistent']), (0, True), err)
        code, out, err = self.cli('--recovery', 'plan', '--recovery-target', 'sqlite', *stand_in, *isolated)
        self.assertEqual((code, json.loads(out)['baseline']['verified']), (0, True), err)
        code, out, err = self.cli('--recovery', 'apply', '--recovery-target', 'sqlite', '--apply-updates', *stand_in, *isolated)
        applied = json.loads(out)
        self.assertEqual((code, applied['converged'], applied['updates_applied']), (0, True, True), err)  # no operational switch needed
        code, out, err = self.cli('--writer-handover', 'youtube', '--recovery-target', 'sqlite', *stand_in, *isolated)
        self.assertEqual((code, json.loads(out)['handed_over'], json.loads(out)['destination']), (0, 0, copy.name), err)
        code, out, err = self.cli('--legacy-resume-check', 'youtube', '--recovery-target', 'sqlite', *stand_in, *isolated)
        checked = json.loads(out)
        self.assertEqual((code, checked['ready'], checked['database_writes']), (0, True, 0), err)
        # Without --isolated-database the configured (operational) file is refused; nothing points the copy flag at it.
        for argv, message in (
                (('--recovery', 'apply', '--recovery-target', 'sqlite', *stand_in), 'target error: operational_database_requires_approval'),
                (('--writer-handover', 'youtube', '--recovery-target', 'sqlite', *stand_in), 'target error: operational_database_requires_approval'),
                (('--writer-handover', 'youtube', '--recovery-target', 'sqlite', *stand_in, '--isolated-database', str(self.operational)),
                 'target error: isolated_database_is_operational'),
                (('--recovery', 'apply', '--recovery-target', 'sqlite', *stand_in, '--isolated-database', str(target)),
                 'target error: isolated_database_not_marked'),
                (('--recovery', 'apply', '--recovery-target', 'sqlite', *stand_in, *isolated, '--allow-operational-database'),
                 '--allow-operational-database를 쓰지 않습니다'),
                (isolated, '--isolated-database는'),
                (('--isolate-database', str(self.dir / 'other.sqlite3'), '--migrate'), 'not allowed with argument'),
                (('--isolate-database', str(self.operational)), 'isolation error: isolated_copy_is_operational')):
            code, _, err = self.cli(*argv)
            self.assertEqual((code, message in err), (2, True), (argv, err))
        self.assertFalse((self.dir / 'other.sqlite3').exists())
        self.assertOperationalUnchanged()


if __name__ == '__main__':
    unittest.main()
