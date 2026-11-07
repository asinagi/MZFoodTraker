import argparse
import logging
import os
from .storage import Storage
from .storage import utcnow as utc_stamp
from .service import CollectionService
from .api import make_server
from .config import load_env, InstagramConfig, NaverConfig, YouTubeConfig
from .youtube_service import YouTubeService
from .naver_service import NaverService
from .naver_datalab import NaverError
from .instagram_service import InstagramService
from .instagram_graph import GraphError


def instagram_command(args, instagram):
    import json
    from pathlib import Path
    try:
        if getattr(args, 'instagram_verify', False):
            from .instagram_verify import verify
            destination = Path(args.verification_output)
            if destination.suffix.lower() != '.json':
                raise ValueError('검증 결과는 .json 파일에만 저장할 수 있습니다')
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = verify(instagram)
            destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['status'] == 'passed' else 2
        elif args.instagram_status:
            result = instagram.status()
        elif args.instagram_check:
            result = instagram.check_connection()
        elif args.instagram_token_check:
            result = instagram.check_token()
        elif getattr(args, 'instagram_reanalyze', False):
            result = dict(instagram.store.analysis.reprocess(), success=True, api_requests=0)
        elif args.instagram_start:
            result = instagram.start_tracking(args.instagram_start, args.candidate_id)
        elif args.instagram_collect:
            result = instagram.collect(args.hashtag_id)
        else:
            from .instagram_tokens import exchange
            result = exchange(instagram, args.token_output)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('success', True) else 1
    except GraphError as exc:
        print(json.dumps({'success': False, 'error_type': exc.kind, 'error': str(exc)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({'success': False, 'error': '관리 명령 실패. 설정 또는 출력 파일의 존재 여부를 확인하세요.'}, ensure_ascii=False))
    return 1

def target_repository(dialect, sqlite_path, database_path, environ=None):
    """B6 target selection. The SQLite stand-in may never be the application's own database."""
    from pathlib import Path
    from .target_db import OracleSettings, OracleTarget, SQLiteTarget
    from .target_repository import TargetRepository
    if dialect == 'oracle':
        repository = TargetRepository(OracleTarget(OracleSettings.from_env(environ)))
        if repository.db.settings.environment == 'production':
            from .production_access import verify
            verify(repository.db, environ)  # the declared production schema and its approval record, before any DDL or load
            repository.db._production_verified = True
        return repository
    if not sqlite_path or Path(sqlite_path).resolve() == Path(database_path).resolve():
        raise ValueError('--target-sqlite에 운영 DB와 다른 경로가 필요합니다.')
    return TargetRepository(SQLiteTarget(sqlite_path))


def target_load(repository, database_path):
    """B7 load of the configured SQLite file (opened read-only) into a migrated target, legacy usage import, reconciliation.
    The import also covers the configured Instagram account: its usage rows exist only inside the 7-day window, so an account
    whose last legacy search is older would otherwise stay unknown and its writer could never be transferred."""
    from .oracle_live import account_scope
    from .quota_ledger import import_legacy_usage
    from .storage import Storage
    from .target_migration import migrate, reconcile
    from .target_schema import TARGET_SCHEMA_VERSION
    if repository.schema_version() != TARGET_SCHEMA_VERSION:
        raise ValueError('대상 스키마가 준비되지 않았습니다. --target-migrate를 먼저 실행하세요.')
    try:
        scopes = [('instagram_graph', account_scope('instagram_graph'))]
    except (ValueError, OverflowError):  # a malformed Instagram setting: the live readiness check refuses that account anyway
        scopes = []
    load = migrate(database_path, repository)
    usage = import_legacy_usage(database_path, repository, scopes=scopes)
    check = reconcile(database_path, repository, source_storage=Storage(database_path))
    return {'batch_key': load['batch_key'], 'loaded_rows': sum(t['loaded_rows'] for t in load['tables'].values()),
            'excluded_rows': sum(t['excluded_rows'] for t in load['tables'].values()),
            'rejected_rows': sum(t['rejected_rows'] for t in load['tables'].values()),
            'tombstones': sum(t['tombstones'] for t in load['tables'].values()),
            'dispositions': {t: v['dispositions'] for t, v in load['tables'].items() if v['dispositions']},
            'legacy_usage_rows': usage['rows'], 'legacy_usage_accounts': usage['scopes'], 'consistent': check['consistent'], 'summary': check['summary'],
            'checks': {k: v for k, v in check['checks'].items() if k != 'food_view_mismatches'}, 'http_requests': 0}


def recovery_command(repository, database_path, args):
    from .reverse_recovery import Recovery
    recovery = Recovery(database_path, repository, allow_operational=args.allow_operational_database or args.recovery == 'plan')
    if args.recovery == 'plan':  # writes nothing anywhere
        return dict(recovery.plan(), preconditions=recovery.preconditions(), baseline=recovery.baseline())
    result = recovery.run(apply_updates=args.apply_updates)
    return {k: result[k] for k in ('preconditions', 'baseline', 'updates_requested', 'updates_applied', 'writes', 'converged')} | \
        {'remaining': result['plan_after']['summary'], 'losses': result['plan_after']['losses'], 'conflicts': result['plan_after']['conflicts']}


def oracle_admin_server(repo, port, admin_token=None, rss_fetcher=None, clock=None, transport=None, backend=None):
    """The admin server on the Oracle runtime alone: RSS discovery, food review, tracking, planning and execution over one
    target, with the mock backend (Mock responses only) unless a live backend (oracle_live) is given. Opens no SQLite
    database, runs no DDL and starts no scheduler; the legacy direct collectors (YouTube/NAVER/Instagram services) are
    not attached, so their routes answer 503."""
    from .runtime_storage import oracle_backend
    from .tracking_access import mock_accounts
    backend = backend or oracle_backend(repo, mock_accounts(), clock, transport, rss_fetcher=rss_fetcher)
    server = make_server(backend.rss, port=port, foods=backend.foods, tracking=backend.tracking, planner=backend.planner,
                         execution=backend.controller, admin_token=admin_token)
    return server, backend


def oracle_command(args, parser):
    """--backend oracle-test: the admin server (mock, or live from an approved registry), the Oracle maintenance, the live
    readiness check, the writer transfer, its release and the writer status, all on the Oracle runtime alone (no SQLite database
    is opened)."""
    import json
    from .food_service import FoodError
    from .oracle_live import oracle_live_backend, oracle_live_readiness, release_writer, transfer_writer, writer_status
    from .runtime_storage import oracle_backend, oracle_maintenance, production_backend, select
    from .tracking_access import mock_accounts
    allowed = ('backend', 'rss_mock_file', 'no_schedulers', 'maintenance', 'execution_mode', 'execution_registry', 'execution_automatic',
               'execution_readiness', 'writer_transfer', 'writer_release', 'release_wait', 'writer_status')
    conflicts = sorted('--' + k.replace('_', '-') for k, v in vars(args).items() if k not in allowed and v != parser.get_default(k))
    if conflicts:
        parser.error('--backend %s는 관리 서버·--maintenance·Live 점검·Writer 이전/철회/상태에만 사용합니다: %s' % (args.backend, ', '.join(conflicts)))
    commands = [k for k in ('maintenance', 'execution_readiness', 'writer_transfer', 'writer_release', 'writer_status') if getattr(args, k)]
    if len(commands) > 1:
        parser.error('한 번에 하나만 실행합니다: ' + ', '.join('--' + k.replace('_', '-') for k in commands))
    live = args.execution_mode == 'live'
    if (live or args.writer_transfer or args.writer_release) and not args.execution_registry:
        parser.error('Live 실행과 Writer 이전·철회에는 --execution-registry가 필요합니다')
    if args.release_wait and not args.writer_release or not 0 <= args.release_wait <= 600:
        parser.error('--release-wait(0~600초)는 --writer-release와 함께만 사용합니다')
    if args.execution_automatic and not live:
        parser.error('--execution-automatic은 --execution-mode live와 함께만 사용합니다')
    if args.rss_mock_file and live:
        parser.error('Live 모드에서는 --rss-mock-file을 쓰지 않습니다')
    production = args.backend == 'oracle-production'
    if args.rss_mock_file and production:
        parser.error('운영 Oracle에는 Mock RSS 응답을 쓰지 않습니다(--rss-mock-file 거부)')
    port, repo, backend, server, result = int(os.getenv('PORT', '8000')), None, None, None, None
    try:
        repo = select(args.backend)  # oracle-production is refused before any connection
        if args.execution_readiness:
            result = oracle_live_readiness(repo, args.execution_readiness)
        elif args.writer_transfer:
            result = transfer_writer(repo, args.writer_transfer, args.execution_registry)
        elif args.writer_release:
            result = release_writer(repo, args.writer_release, args.execution_registry, wait_seconds=args.release_wait)
        elif args.writer_status:
            result = writer_status(repo)
        elif args.maintenance:
            backend = production_backend(repo) if production else oracle_backend(repo, mock_accounts())
            result = oracle_maintenance(backend)
        else:
            if live:
                backend = oracle_live_backend(repo, args.execution_registry)
            elif production:
                backend = production_backend(repo)  # management and observations on the real accounts, no collector
            server, backend = oracle_admin_server(repo, port, os.getenv('MZ_ADMIN_TOKEN') or None,
                                                  mock_file_fetcher(args.rss_mock_file) if args.rss_mock_file else None, backend=backend)
            if live and not args.no_schedulers and backend.rss.origin == 'live_api':
                backend.rss.start()  # the RSS schedule of an approved live source only
            if args.execution_automatic:
                backend.controller.start()  # refused unless B3_AUTOMATIC_ENABLED=true on top of the live approval
    except FoodError as exc:
        error = str(exc)
    except OSError as exc:
        error = f'로컬 서버 시작 실패: 포트 {port} 또는 --rss-mock-file을 확인하세요. {exc}'
    except Exception as exc:  # error codes only; DSN and credentials never printed
        error = 'oracle error: %s' % getattr(exc, 'code', type(exc).__name__)
    else:
        error = None
    if error is not None or server is None:
        _close(server, backend, repo)
        if error is not None:
            parser.exit(2, error + '\n')
        print(json.dumps(result, ensure_ascii=False, default=str))
        unfinished = args.execution_readiness and not result['ready'] or args.writer_release and result['status'] != 'RELEASED'
        return 2 if unfinished else 0  # a release still DRAINING is not done: run it again
    mode = 'Live 승인 수집원' if live else '수집 없음(관리·관측)' if production else 'Mock 수집만'
    print(f'Local ({args.backend}, {mode}): http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _close(server, backend, repo)
    return 0


def _close(server, backend, repo):
    if backend is not None:
        backend.rss.stop_event.set()
        backend.controller.stop_event.set()
        backend.execution.release()
    if server is not None:
        server.server_close()
    if repo is not None:
        repo.db.close()


def mock_file_fetcher(path):
    """A local RSS XML file served as the Mock response of every collection (no HTTP)."""
    from pathlib import Path
    data = Path(path).read_bytes()
    return lambda timeout: (data, 200)


def execution_for_mode(storage, args, environ=None):
    """Explicit B3 mode. Returns (controller or None, error). Live is fail-closed; nothing starts here."""
    from .execution_runtime import live_controller
    from .food_service import FoodError
    if args.execution_mode == 'preview':
        if args.execution_registry or args.execution_automatic:
            return None, 'preview 모드에서는 Live registry·자동 실행을 사용할 수 없습니다.'
        return None, None
    if not args.execution_registry:
        return None, 'Live 모드에는 --execution-registry가 필요합니다.'
    try:
        controller = live_controller(storage, args.execution_registry, environ)
    except FoodError as exc:
        return None, str(exc)
    if args.execution_automatic and not controller.execution.settings.automatic:
        return None, '자동 실행에는 B3_AUTOMATIC_ENABLED=true 설정도 필요합니다.'
    return controller, None


def main():
    load_env()
    parser = argparse.ArgumentParser(description='Google Trends 음식 트렌드 탐지')
    parser.add_argument('--collect-once', action='store_true')
    maintenance_group = parser.add_mutually_exclusive_group()
    maintenance_group.add_argument('--migrate', action='store_true', help='Explicit local schema migration; no API calls')
    maintenance_group.add_argument('--migration-status', action='store_true', help='Read-only schema version check')
    parser.add_argument('--create-database', action='store_true',
                        help='--migrate와 함께: 지정 경로에 DB 파일이 없을 때만 새로 생성(명시적 초기화)')
    maintenance_group.add_argument('--maintenance', action='store_true', help='Local expiry cleanup; no API calls or reprocessing')
    maintenance_group.add_argument('--ingest-pending', action='store_true', help='완료 회차의 공통 스테이징 처리; API 호출 없음')
    maintenance_group.add_argument('--ingest-legacy', choices=('youtube', 'instagram_graph'),
                                   help='출처 불명 legacy 저장분을 표시된 논리 회차로 스테이징; API 호출 없음')
    maintenance_group.add_argument('--isolate-database', metavar='PATH',
                                   help='DATABASE_PATH(읽기 전용)를 SQLite 백업 API로 새 파일 PATH(.sqlite3)에 복사해 격리 표시하고, 사본만 v11 '
                                        '마이그레이션·스테이징·보존 정리; 운영 DB 경로·기존 파일 거부, API 호출 없음')
    parser.add_argument('--reanalyze', action='store_true')
    parser.add_argument('--no-schedulers', action='store_true', help='자동 외부 수집 없이 로컬 저장 데이터 UI 실행')
    parser.add_argument('--export-json', metavar='PATH', help='RSS 원본/분류/관측/이력을 JSON으로 내보내기')
    naver_group = parser.add_mutually_exclusive_group()
    naver_group.add_argument('--naver-status', action='store_true')
    naver_group.add_argument('--naver-collect', action='store_true')
    naver_group.add_argument('--naver-query', type=int, metavar='CANDIDATE_ID')
    naver_group.add_argument('--naver-keyword', metavar='KEYWORD', help='RSS 후보를 만들지 않고 사용자 지정 음식 검색어 조회')
    parser.add_argument('--naver-days', type=int, choices=(7, 30, 90), default=30)
    youtube_group = parser.add_mutually_exclusive_group()
    youtube_group.add_argument('--youtube-status', action='store_true')
    youtube_group.add_argument('--youtube-collect', action='store_true')
    youtube_group.add_argument('--youtube-verify', action='store_true', help='소량 라이브 검증: 발굴/추적 각 1개 검색어')
    youtube_group.add_argument('--youtube-refresh', action='store_true')
    youtube_group.add_argument('--youtube-delete', action='store_true', help='로컬 YouTube API 저장 데이터 삭제')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--instagram-status', action='store_true')
    group.add_argument('--instagram-check', action='store_true')
    group.add_argument('--instagram-token-check', action='store_true')
    group.add_argument('--instagram-start', metavar='HASHTAG')
    group.add_argument('--instagram-collect', action='store_true')
    group.add_argument('--instagram-exchange-token', action='store_true')
    group.add_argument('--instagram-verify', action='store_true', help='두쫀쿠 한 태그의 실제 연결·수집·DB 집계 검증')
    group.add_argument('--instagram-reanalyze', action='store_true', help='저장된 캡션의 해시태그 재처리 (외부 호출 없음)')
    parser.add_argument('--target-migrate', choices=('oracle', 'sqlite'),
                        help='B6 대상 스키마 명시 생성(SQLite --migrate와 별개). oracle은 ORACLE_DSN/USER/PASSWORD 필요')
    parser.add_argument('--target-status', choices=('oracle', 'sqlite'), help='B6 대상 스키마 버전 조회')
    parser.add_argument('--target-load', choices=('oracle', 'sqlite'),
                        help='B7 적재: DATABASE_PATH 또는 --isolated-database(읽기 전용)를 대상에 옮기고 레거시 사용량 원장을 가져온 뒤 대조; API 호출 없음')
    parser.add_argument('--recovery', choices=('plan', 'apply'),
                        help='Oracle→SQLite 역방향 복구(대상 → DATABASE_PATH 또는 --isolated-database). plan은 쓰기 없음, apply는 운영 DB면 '
                             '--allow-operational-database 필요')
    parser.add_argument('--recovery-target', choices=('oracle', 'sqlite'), help='역방향 복구의 원천 대상')
    parser.add_argument('--apply-updates', action='store_true', help='apply: 적재 당시 파일이 그대로일 때만 mutable 열 갱신을 허용')
    parser.add_argument('--allow-operational-database', action='store_true', help='apply: 설정된 운영 DB로의 복구 허용(G-OPS 승인 후)')
    parser.add_argument('--isolated-database', metavar='PATH',
                        help='--target-load·--recovery·--writer-handover·--legacy-resume-check가 DATABASE_PATH 대신 쓸 격리 사본'
                             '(--isolate-database로 만든 파일만, 운영 DB는 거부; --allow-operational-database 불필요)')
    parser.add_argument('--target-sqlite', metavar='PATH', help='SQLite 대역 대상 DB 경로(운영 DB 경로 사용 불가)')
    parser.add_argument('--shadow', choices=('status', 'enable', 'disable', 'deliver', 'compare', 'repair'),
                        help='B7 Shadow: SQLite 확정 변경을 대상에 멱등 반영; API 호출 없음. enable은 운영 승인 후 사용')
    parser.add_argument('--shadow-target', choices=('oracle', 'sqlite'), help='Shadow 대상(deliver/compare/repair에 필요)')
    parser.add_argument('--backend', choices=('sqlite', 'oracle-test', 'oracle-production'), default='sqlite',
                        help='관리 서버 저장소. sqlite(기본) | oracle-test(ORACLE_TEST_* 격리 스키마만 사용, Mock 수집만) | '
                             'oracle-production(승인된 운영 스키마만: ORACLE_*·ORACLE_PRODUCTION_SCHEMA·ORACLE_PRODUCTION_APPROVAL, Mock 수집 없음)')
    parser.add_argument('--rss-mock-file', metavar='PATH', help='--backend oracle-test: RSS 수집에 실제 HTTP 대신 쓸 로컬 RSS XML(Mock 응답)')
    parser.add_argument('--writer-transfer', choices=('instagram_graph', 'youtube', 'naver_datalab', 'google_trends_rss'),
                        help='--backend oracle-test: 기존 SQLite 수집기를 멈춘 수집원을 Oracle 런타임 단독 Writer로 기록(G-OPS 승인 후, --execution-registry 필요)')
    parser.add_argument('--writer-release', choices=('instagram_graph', 'youtube', 'naver_datalab', 'google_trends_rss'),
                        help='--backend oracle-test: Oracle 단독 Writer 철회. 새 요청은 즉시 중단, 진행 중 요청이 끝나면 남은 작업을 '
                             'WRITER_RELEASED로 닫고 IMPORTED로 되돌림(진행 중이면 DRAINING, 다시 실행; --execution-registry 필요)')
    parser.add_argument('--release-wait', type=int, default=0, metavar='SECONDS',
                        help='--writer-release: 진행 중 요청이 끝나기를 기다릴 최대 초(0~600, 기본 0)')
    parser.add_argument('--writer-status', action='store_true',
                        help='--backend oracle-test: 수집원별 Writer 상태·이전/철회 이력·열린 작업·인계할 사용량(읽기 전용)')
    parser.add_argument('--writer-handover', choices=('instagram_graph', 'youtube', 'naver_datalab', 'google_trends_rss'),
                        help='철회한 수집원의 Oracle 기간 API 사용량을 설정된 SQLite DB(또는 --isolated-database)의 레거시 호출 기록으로 인계'
                             '(--recovery-target 필요, 운영 DB는 --allow-operational-database)')
    parser.add_argument('--legacy-resume-check', choices=('instagram_graph', 'youtube', 'naver_datalab', 'google_trends_rss'),
                        help='기존 SQLite 수집기를 다시 켜도 되는지 판정(읽기 전용, --recovery-target 필요, 격리 사본은 --isolated-database)')
    parser.add_argument('--runtime-status', choices=('sqlite', 'oracle-test', 'oracle-production'),
                        help='실행 저장소 선택 점검(읽기 전용). oracle-test는 ORACLE_TEST_* 필요, oracle-production은 승인 전 비활성')
    parser.add_argument('--execution-readiness', metavar='REGISTRY',
                        help='읽기 전용 B3 Live 준비 점검; API 호출·DB 쓰기 없음')
    parser.add_argument('--execution-mode', choices=('preview', 'live'), default='preview',
                        help='서버의 B3 실행 모드. 기본 preview는 실행 어댑터 없음. Mock은 격리 food_demo 전용')
    parser.add_argument('--execution-registry', metavar='REGISTRY', help='Live 모드의 검토된 출처 승인 registry')
    parser.add_argument('--execution-automatic', action='store_true',
                        help='Live 자동 실행; B3_AUTOMATIC_ENABLED=true도 필요')
    parser.add_argument('--candidate-id', type=int)
    parser.add_argument('--hashtag-id', type=int)
    parser.add_argument('--token-output', default='data/instagram-long-lived.secret')
    parser.add_argument('--verification-output', default='data/instagram-live-verification.json')
    args = parser.parse_args()
    if args.create_database and not args.migrate:
        parser.error('--create-database는 --migrate와 함께만 사용할 수 있습니다')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    interval = float(os.getenv('COLLECT_INTERVAL_SECONDS', '1800'))
    timeout = float(os.getenv('RSS_TIMEOUT_SECONDS', '20'))
    if interval <= 0 or timeout <= 0:
        parser.error('수집 간격과 타임아웃은 양수여야 합니다')
    if args.backend != 'sqlite':
        raise SystemExit(oracle_command(args, parser))  # only the Oracle runtime; every SQLite command keeps its own path
    if args.rss_mock_file or args.writer_transfer or args.writer_release or args.release_wait or args.writer_status:
        parser.error('--rss-mock-file, --writer-transfer, --writer-release, --writer-status는 --backend oracle-test와 함께만 사용합니다')
    from .paths import database_path as resolve_database
    database_path = str(resolve_database())
    if args.isolated_database and not (args.target_load or args.recovery or args.writer_handover or args.legacy_resume_check):
        parser.error('--isolated-database는 --target-load·--recovery·--writer-handover·--legacy-resume-check와 함께만 사용합니다')
    if args.isolated_database and args.allow_operational_database:
        parser.error('--isolated-database(격리 사본)에는 --allow-operational-database를 쓰지 않습니다')
    if args.isolate_database:
        import json
        from .cutover import isolate
        try:
            result = isolate(database_path, args.isolate_database)
        except FileNotFoundError:
            parser.exit(2, 'isolation error: source_database_missing\n')
        except Exception as exc:  # error codes only
            parser.exit(2, 'isolation error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result, ensure_ascii=False, default=str))
        return
    if args.runtime_status:
        import json
        from .runtime_storage import status as runtime_status
        try:
            result = runtime_status(args.runtime_status, database_path)
        except Exception as exc:  # error codes only; DSN and credentials never printed
            parser.exit(2, 'runtime storage error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.target_load or args.recovery or args.writer_handover or args.legacy_resume_check:
        import json
        from .usage_handover import handover, resume_readiness
        commands = [k for k in ('target_load', 'recovery', 'writer_handover', 'legacy_resume_check') if getattr(args, k)]
        if len(commands) > 1:
            parser.error('한 번에 하나만 실행합니다: ' + ', '.join('--' + k.replace('_', '-') for k in commands))
        try:
            if not args.target_load and not args.recovery_target:
                raise ValueError('--recovery-target이 필요합니다.')
            if args.isolated_database:
                from .cutover import isolated
                database_path = str(isolated(args.isolated_database))  # a marked copy, never the operational file
            repository = target_repository(args.target_load or args.recovery_target, args.target_sqlite, database_path)
            if args.target_load:
                result = target_load(repository, database_path)
            elif args.recovery:
                result = recovery_command(repository, database_path, args)
            elif args.writer_handover:
                result = handover(repository, args.writer_handover, database_path, allow_operational=args.allow_operational_database)
            else:
                result = resume_readiness(repository, args.legacy_resume_check, database_path)
        except ValueError as exc:
            parser.exit(2, str(exc) + '\n')
        except Exception as exc:  # error codes only; DSN and credentials never printed
            parser.exit(2, 'target error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result, ensure_ascii=False, default=str))
        if args.legacy_resume_check and not result['ready']:
            raise SystemExit(2)  # not yet: the legacy collector stays off
        return
    if args.target_migrate or args.target_status:
        import json
        try:
            repository = target_repository(args.target_migrate or args.target_status, args.target_sqlite, database_path)
            result = repository.migrate() if args.target_migrate else {'version': repository.schema_version()}
        except ValueError as exc:
            parser.exit(2, str(exc) + '\n')
        except Exception as exc:  # error codes only; DSN and credentials never printed
            parser.exit(2, 'target error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result))
        return
    from .migrations import migrate, schema_version, LATEST_VERSION, MigrationRequired
    if args.migrate or args.migration_status:
        import json
        before = {'path': database_path, 'exists': os.path.isfile(database_path), 'version': schema_version(database_path),
                  'required_version': LATEST_VERSION}
        if not args.migrate:
            print(json.dumps(before, ensure_ascii=False))
            return
        try:
            result = migrate(database_path, create=args.create_database)
        except MigrationRequired as exc:
            parser.exit(2, str(exc) + '\n')
        print(json.dumps(dict(result, path=database_path, from_version=before['version']), ensure_ascii=False))
        return
    try:
        storage = Storage(database_path)
    except MigrationRequired as exc:
        parser.exit(2, str(exc) + '\n')
    if args.maintenance:
        import json
        from .youtube_storage import YouTubeStore
        from .instagram_analysis import HashtagAnalysis
        settings = InstagramConfig.from_env()
        analysis = HashtagAnalysis(storage, settings.user_id, settings.caption_retention_days)
        with storage.connect() as db:
            captions = analysis.purge_expired(db)
        videos = YouTubeStore(storage).purge()
        from .execution_maintenance import purge as purge_execution
        from .ingestion import IngestionService
        from .food_observations import FoodObservationService
        execution_cleanup = purge_execution(storage)
        staging = IngestionService(storage).purge()
        observations = FoodObservationService(storage)
        links = observations.purge()
        reprocessed = observations.apply_entity_changes()
        print(json.dumps({'success': True, 'purged_captions': captions, 'purged_videos': videos, 'api_requests': 0,
                          'execution': execution_cleanup, 'staging': staging, 'links': links,
                          'entity_changes_reprocessed': len(reprocessed)}))
        return
    if args.ingest_pending or args.ingest_legacy:
        import json
        from .ingestion import IngestionService
        ingestion = IngestionService(storage)
        from .food_observations import FoodObservationService
        result = ingestion.ingest_pending() if args.ingest_pending else ingestion.legacy_backfill(args.ingest_legacy)
        observations = FoodObservationService(storage)
        result['linking'] = (observations.link_run(result['ingest_run_id']) if args.ingest_legacy
                             else observations.link_pending())
        print(json.dumps({k: v for k, v in result.items() if k not in ('storable_metrics', 'transient_metrics')}, ensure_ascii=False))
        return
    if args.shadow:
        import json
        from .shadow import ShadowService
        try:
            needs_target = args.shadow in ('deliver', 'compare', 'repair')
            if needs_target and not args.shadow_target:
                raise ValueError('--shadow-target이 필요합니다.')
            repository = target_repository(args.shadow_target, args.target_sqlite, database_path) if needs_target else None
            shadow = ShadowService(storage, repository)
            if args.shadow == 'status':
                result = shadow.status()
            elif args.shadow in ('enable', 'disable'):
                result = shadow.enable(args.shadow == 'enable')
            else:
                result = getattr(shadow, args.shadow)()
        except ValueError as exc:
            parser.exit(2, str(exc) + '\n')
        except Exception as exc:  # error codes only; DSN and credentials never printed
            parser.exit(2, 'shadow error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result, ensure_ascii=False, default=str))
        return
    if args.execution_readiness:
        import json
        from .execution_runtime import live_readiness
        result = live_readiness(storage, args.execution_readiness)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result['ready'] else 2)
    execution, error = execution_for_mode(storage, args)
    if error:
        parser.exit(2, error + '\n')
    if args.export_json:
        import json
        from pathlib import Path
        destination = Path(args.export_json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('x', encoding='utf-8') as output:
            json.dump(storage.export(), output, ensure_ascii=False, indent=2)
        print('RSS 감사 JSON 내보내기 완료')
        return
    if args.reanalyze:
        storage.reanalyze()
        print('원본 항목 재분류 완료')
        return
    service = CollectionService(storage, interval, timeout)
    if args.collect_once:
        import json
        print(json.dumps(service.collect(), ensure_ascii=False))
        return
    try:
        youtube = YouTubeService(storage, YouTubeConfig.from_env())
    except (ValueError, OverflowError):
        logging.warning('YouTube 설정 형식 오류 · 자동 수집 비활성화')
        youtube = YouTubeService(storage, YouTubeConfig(enabled=False))
    if any((args.youtube_status, args.youtube_collect, args.youtube_verify, args.youtube_refresh, args.youtube_delete)):
        import json
        if args.youtube_status:
            result = youtube.status()
        elif args.youtube_delete:
            result = youtube.delete_data()
        else:
            if args.youtube_verify:
                from .youtube_verify import verify, save_report
                result = verify(youtube)
                save_report(result)
            else:
                result = youtube.collect(refresh_only=args.youtube_refresh)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if (result.get('verification_passed', False) if args.youtube_verify else result.get('success', True)) else 1)
    try:
        naver = NaverService(storage, NaverConfig.from_env())
    except (ValueError, OverflowError):
        logging.warning('NAVER 설정 형식 오류 · NAVER 수집 비활성화')
        naver = NaverService(storage, NaverConfig(enabled=False))
    if args.naver_status or args.naver_collect or args.naver_query is not None or args.naver_keyword:
        import json
        try:
            result = (naver.status() if args.naver_status else naver.query_keywords([args.naver_keyword], args.naver_days)
                      if args.naver_keyword else naver.query([args.naver_query], args.naver_days)
                      if args.naver_query is not None else naver.collect())
        except NaverError as exc:
            result = {'success': False, 'error_type': exc.kind, 'error': str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result.get('success', True) else 1)
    try:
        instagram = InstagramService(storage, InstagramConfig.from_env())
    except (ValueError, OverflowError):
        logging.warning('Instagram 환경변수 형식을 확인하세요. Instagram 수집을 비활성화합니다.')
        instagram = InstagramService(storage, InstagramConfig(enabled=False))
    if any((args.instagram_status, args.instagram_check, args.instagram_token_check,
            args.instagram_start, args.instagram_collect, args.instagram_exchange_token, args.instagram_verify, args.instagram_reanalyze)):
        raise SystemExit(instagram_command(args, instagram))
    port = int(os.getenv('PORT', '8000'))
    try:
        server = make_server(service, port=port, instagram=instagram, naver=naver, youtube=youtube, execution=execution,
                             admin_token=os.getenv('MZ_ADMIN_TOKEN') or None)
    except OSError as exc:
        parser.exit(2, f'로컬 서버 시작 실패: 포트 {port} 사용 여부를 확인하세요. {exc}\n')
    if execution is not None and args.execution_automatic:
        execution.start()
    if not args.no_schedulers:
        service.start()
        naver.start()
    instagram.start(automatic=not args.no_schedulers)
    youtube.start(automatic=not args.no_schedulers)
    print(f'Local: http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_event.set()
        instagram.stop_event.set()
        naver.stop_event.set()
        youtube.stop_event.set()
        server.server_close()

if __name__ == '__main__':
    main()
