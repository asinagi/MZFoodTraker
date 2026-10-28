"""Local identity review demo with its own database and no API clients or schedulers."""
import argparse
from pathlib import Path
from .api import make_server
from .service import CollectionService
from .storage import Storage


def main():
    parser = argparse.ArgumentParser(description='격리 음식 등록 UI; 외부 API·환경 파일을 사용하지 않습니다.')
    parser.add_argument('--port', type=int, default=8010)
    parser.add_argument('--mock-tracking', action='store_true', help='Mock 계정의 추적·요청 계획만 허용; 외부 실행 없음')
    parser.add_argument('--mock-execution', action='store_true', help='격리 Mock Transport 실행까지 허용; 실제 외부 호출 없음')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Fixed disposable-demo destination: never the configured DATABASE_PATH.
    path = root / 'data' / 'food-identity-demo.sqlite3'
    production = root / 'data' / 'trends.sqlite3'
    if path.is_symlink() or path.resolve() == production.resolve() or (path.exists() and production.exists() and path.samefile(production)):
        parser.error('운영 DB를 데모 DB로 사용할 수 없습니다.')
    storage = Storage.create(path)

    def disabled(_):
        raise RuntimeError('격리 데모에서는 외부 수집이 비활성화되어 있습니다.')

    service = CollectionService(storage, fetcher=disabled)
    from .tracking_access import mock_accounts
    from .tracking_service import TrackingService
    tracking = TrackingService(storage, mock_accounts()) if args.mock_tracking or args.mock_execution else None
    from .query_planner import QueryPlanner
    from .execution_mock import mock_controller
    planner = QueryPlanner(storage,tracking) if tracking else None
    execution = mock_controller(storage,planner) if args.mock_execution else None
    server = make_server(service, port=args.port, tracking=tracking,planner=planner,execution=execution)
    base_handler = server.RequestHandlerClass

    class DemoHandler(base_handler):
        def do_POST(self):
            allowed=('/api/foods/', '/api/tracking/', '/api/planner/') + (('/api/execution/',) if args.mock_execution else ())
            if not self.path.startswith(allowed):
                return self.send(403, {'error': '격리 데모에서는 음식 등록·검토만 사용할 수 있습니다.'})
            return super().do_POST()

    server.RequestHandlerClass = DemoHandler
    print(f'Food identity demo: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Demo DB: {path}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
