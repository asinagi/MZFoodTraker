import logging
import threading
from . import collector

class CollectionService:
    def __init__(self, storage, interval=1800, timeout=20, fetcher=None):
        self.storage = storage
        self.interval = interval
        self.timeout = timeout
        self.fetcher = fetcher or collector.fetch
        self.origin = 'live_api' if fetcher is None else 'mock'
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def collect(self):
        if not self.lock.acquire(blocking=False):
            return {'accepted': False, 'message': '이미 수집 중입니다'}
        run_id = None
        raw = None
        status = None
        try:
            run_id = self.storage.start(origin=self.origin)
            raw, status = self.fetcher(self.timeout)
            items, warnings = collector.parse(raw)
            summary = self.storage.finish(run_id, items, raw, status, warnings=warnings)
            return {'accepted': True, 'success': True, 'run_id': run_id, 'item_count': len(items), **summary}
        except Exception as exc:
            logging.exception('Collection failed')
            error = f'{type(exc).__name__}: {exc}'
            if run_id is not None:
                try:
                    self.storage.finish(run_id, raw=raw, http_status=getattr(exc, 'code', status), error=error)
                except Exception:
                    logging.exception('Could not persist collection error')
            return {'accepted': True, 'success': False, 'error': error, 'run_id': run_id}
        finally:
            self.lock.release()

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                self.collect()
                if self.stop_event.wait(self.interval):
                    break
        self.thread = threading.Thread(target=loop, name='rss-scheduler', daemon=True)
        self.thread.start()

    def status(self):
        return dict(self.storage.status(), collecting=self.lock.locked(), interval_seconds=self.interval,
                    source_url=collector.SOURCE)
