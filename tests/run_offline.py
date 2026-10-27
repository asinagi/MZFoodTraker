"""Run fixture tests with external sockets and the production DB blocked."""
import argparse
import contextlib
import json
import socket
import sqlite3
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='reports/development-a1-a3-2026-10-27')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    production = (root / 'data/trends.sqlite3').resolve()
    attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo
    original_db = sqlite3.connect

    def allow_host(host):
        if host not in ('127.0.0.1', '::1', 'localhost', b'127.0.0.1', None):
            attempts.append('blocked_external_socket')
            raise RuntimeError('External network disabled for fixture tests')

    def connect(sock, address):
        if isinstance(address, tuple):
            allow_host(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple):
            allow_host(address[0])
        return original_connect_ex(sock, address)

    def resolve(host, *args, **kwargs):
        allow_host(host)
        return original_resolve(host, *args, **kwargs)

    def database(path, *args, **kwargs):
        text = str(path)
        if text != ':memory:':
            if text.startswith('file:'):
                text = unquote(urlsplit(text).path)
                if len(text) > 2 and text[0] == '/' and text[2] == ':':
                    text = text[1:]
            if Path(text).resolve() == production:
                raise RuntimeError('Production database disabled for fixture tests')
        return original_db(path, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, resolve
    sqlite3.connect = database
    output = root / args.output
    output.mkdir(parents=True, exist_ok=True)
    try:
        with (output / 'tests.log').open('w', encoding='utf-8') as stream:
            with contextlib.redirect_stderr(stream):
                suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_*.py')
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = original_connect, original_connect_ex, original_resolve
        sqlite3.connect = original_db
    summary = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'blocked_external_attempts': len(attempts),
               'production_database': 'blocked', 'successful': result.wasSuccessful() and not attempts}
    (output / 'test-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
