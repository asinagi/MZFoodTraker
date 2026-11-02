"""Project-anchored paths. The default database never depends on the caller's working directory."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / 'data' / 'trends.sqlite3'
ENV_FILE = PROJECT_ROOT / '.env'


def database_path(environ=None):
    """DATABASE_PATH if set (relative values resolve against the project root, as run.ps1 always did), else the default."""
    raw = (os.environ if environ is None else environ).get('DATABASE_PATH', '').strip()
    if not raw:
        return DEFAULT_DATABASE
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
