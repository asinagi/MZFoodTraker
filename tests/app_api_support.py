"""Shared fixtures for the app API tests: a small live_api-origin database on a fixed clock (no network)."""
import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from food_trends.analytics_reader import SQLiteAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi, make_app_server
from food_trends.observation_writer import ObservationWriter, ScratchStorage

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)  # 2026-11-03 12:00 KST
END = date(2026, 11, 2)
HOST = {'Host': '127.0.0.1:8020'}


def kst(day, hour=12, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def build_live_fixture(directory):
    """Production-shaped rows (origin live_api): RSS 20 days with one outage, Instagram 14 days, a merge, a variant,
    one mock-origin RSS run a live server must ignore, and a legacy_unknown backfill."""
    storage = ScratchStorage.create(Path(directory) / 'app-api.sqlite3')
    w = ObservationWriter(storage, at=NOW - timedelta(days=60))
    w.food('FOOD_A', '두쫀쿠', ['두바이쫀득쿠키', '두바이 쫀득 쿠키'], 'PRODUCT')
    w.food('FOOD_B', '마라탕', [], 'DISH')
    w.food('FOOD_C', '탕후루', [], 'DISH')
    w.food('FOOD_D', '약과', [], 'PRODUCT')
    w.food('FOOD_OLD', '두바이 쿠키', ['두바이쿠키'], 'PRODUCT')
    w.food('FOOD_V', '짭쫀쿠', [], 'PRODUCT')
    w.relate('FOOD_V', 'FOOD_A')
    w.query('Q_A', '두쫀쿠')
    w.query('Q_B', '마라탕')
    for n in range(19, -1, -1):
        day = END - timedelta(days=n)
        entries = [{'keyword': '두쫀쿠 %d' % k} for k in range(1 + n % 3)] + [{'keyword': '마라탕', 'traffic': '1,000+'}]
        if n % 4 == 0:
            entries.append({'keyword': '두바이쿠키', 'traffic': '2,000+'})
        w.rss_run(kst(day, 9), entries, success=n != 12, origin='live_api')
        if n < 14:
            w.instagram_run(kst(day, 13), 'Q_A', ['FOOD_A'], [{'id': '7%02d%02d' % (n, k), 'tags': ['두쫀쿠']} for k in range(14 - n)],
                            coverage='NONE' if n == 9 else 'COMPLETE', origin='live_api')
            w.instagram_run(kst(day, 14), 'Q_B', ['FOOD_B'], [{'id': '8%02d00' % n, 'tags': ['마라탕']}], origin='live_api')
    w.merge('FOOD_OLD', 'FOOD_A', at=NOW - timedelta(days=3))
    w.rss_run(kst(END, 10), [{'keyword': '탕후루'}, {'keyword': '약과'}], origin='mock')  # must not count on a live server
    w.legacy_instagram(NOW - timedelta(days=1), [{'id': '9001', 'tags': ['마라탕'], 'seen_at': kst(END - timedelta(days=2), 18)}])
    w.purge(NOW)
    return storage


def api_for(storage, origin='live_api', data_mode='database', clock=None):
    return AppApi(FoodAnalytics(SQLiteAnalyticsStore(storage), clock=clock or (lambda: NOW), origin=origin), data_mode=data_mode)


def get(api, target, headers=None, method='GET'):
    status, body, extra = api.handle(method, target, dict(HOST, **(headers or {})))
    return status, body, extra


class LiveServer:
    """make_app_server on an ephemeral loopback port, served from a background thread."""

    def __init__(self, api, **options):
        self.server = make_app_server(api, port=0, quiet=True, **options)
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, method='GET', headers=None, data=None):
        """(status, headers, parsed JSON body or None)."""
        try:
            with urlopen(Request(self.base + path, method=method, headers=headers or {}, data=data), timeout=10) as response:
                raw, status, head = response.read(), response.status, dict(response.headers.items())
        except HTTPError as error:
            raw, status, head = error.read(), error.code, dict(error.headers.items())
        return status, head, json.loads(raw) if raw else None
