"""S1: database and .env paths are anchored to the project, and a new database is never created implicitly."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from food_trends import paths
from food_trends.config import load_env
from food_trends.migrations import LATEST_VERSION, MigrationRequired, migrate, schema_version
from food_trends.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)

    def cli(self, *args, database=None):
        env = {k: v for k, v in os.environ.items() if k != 'DATABASE_PATH'}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8', DATABASE_PATH=database)
        return subprocess.run([sys.executable, '-B', '-m', 'food_trends', *args], cwd=self.temp.name, env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)

    def test_default_database_is_the_same_from_any_working_directory(self):
        expected = ROOT / 'data' / 'trends.sqlite3'
        self.assertEqual(paths.database_path({}), expected)
        os.chdir(self.temp.name)
        self.assertEqual(paths.database_path({}), expected)
        self.assertEqual(paths.database_path({'DATABASE_PATH': '  '}), expected)

    def test_explicit_absolute_and_relative_paths(self):
        absolute = Path(self.temp.name) / 'explicit.sqlite3'
        self.assertEqual(paths.database_path({'DATABASE_PATH': str(absolute)}), absolute)
        os.chdir(self.temp.name)
        self.assertEqual(paths.database_path({'DATABASE_PATH': 'data/other.sqlite3'}), (ROOT / 'data' / 'other.sqlite3').resolve())
        name = 'path-test-%s.sqlite3' % uuid.uuid4().hex
        status = self.cli('--migration-status', database='data/' + name)  # read-only, from a foreign directory
        self.assertEqual(status.returncode, 0, status.stderr)
        result = json.loads(status.stdout)
        self.assertEqual((Path(result['path']), result['exists'], result['version']), ((ROOT / 'data' / name).resolve(), False, 0))
        self.assertFalse((ROOT / 'data' / name).exists())
        self.assertFalse((Path(self.temp.name) / 'data').exists())

    def test_cli_refuses_to_create_a_missing_database_implicitly(self):
        target = Path(self.temp.name) / 'missing' / 'trends.sqlite3'
        refused = self.cli('--migrate', database=str(target))
        self.assertEqual(refused.returncode, 2)
        self.assertIn('refusing to create', refused.stderr)
        self.assertFalse(target.exists() or target.parent.exists())
        alone = self.cli('--create-database', database=str(target))
        self.assertEqual(alone.returncode, 2)

    def test_cli_creates_a_new_database_only_when_asked(self):
        target = Path(self.temp.name) / 'fresh' / 'trends.sqlite3'
        created = self.cli('--migrate', '--create-database', database=str(target))
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertEqual((result['version'], result['from_version'], Path(result['path'])), (LATEST_VERSION, 0, target))
        again = json.loads(self.cli('--migrate', database=str(target)).stdout)
        self.assertEqual(again['applied'], [])

    def test_library_creation_is_explicit_too(self):
        missing = Path(self.temp.name) / 'library.sqlite3'
        with self.assertRaises(MigrationRequired):
            migrate(missing)
        self.assertFalse(missing.exists())
        Storage.create(missing)  # the explicit test/initialisation path
        self.assertEqual(schema_version(missing), LATEST_VERSION)

    def test_env_file_is_read_from_the_project_not_the_working_directory(self):
        env_file = Path(self.temp.name) / 'project.env'
        key = 'MZ_PATH_TEST_%s' % uuid.uuid4().hex[:8].upper()
        env_file.write_text('%s=anchored\n' % key, encoding='utf-8')
        (Path(self.temp.name) / '.env').write_text('%s=wrong\n' % key, encoding='utf-8')
        os.chdir(self.temp.name)
        try:
            with patch.object(paths, 'ENV_FILE', env_file):
                load_env()
            self.assertEqual(os.environ[key], 'anchored')
        finally:
            os.environ.pop(key, None)
