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
        return TargetRepository(OracleTarget(OracleSettings.from_env(environ)))
    if not sqlite_path or Path(sqlite_path).resolve() == Path(database_path).resolve():
        raise ValueError('--target-sqlite에 운영 DB와 다른 경로가 필요합니다.')
    return TargetRepository(SQLiteTarget(sqlite_path))


def target_load(repository, database_path):
    """B7 load of the configured SQLite file (opened read-only) into a migrated target, legacy usage import, reconciliation."""
    from .quota_ledger import import_legacy_usage
    from .storage import Storage
    from .target_migration import migrate, reconcile
    from .target_schema import TARGET_SCHEMA_VERSION
    if repository.schema_version() != TARGET_SCHEMA_VERSION:
        raise ValueError('대상 스키마가 준비되지 않았습니다. --target-migrate를 먼저 실행하세요.')
    load = migrate(database_path, repository)
    usage = import_legacy_usage(database_path, repository)
    check = reconcile(database_path, repository, source_storage=Storage(database_path))
    return {'batch_key': load['batch_key'], 'loaded_rows': sum(t['loaded_rows'] for t in load['tables'].values()),
            'excluded_rows': sum(t['excluded_rows'] for t in load['tables'].values()),
            'rejected_rows': sum(t['rejected_rows'] for t in load['tables'].values()),
            'tombstones': sum(t['tombstones'] for t in load['tables'].values()),
            'dispositions': {t: v['dispositions'] for t, v in load['tables'].items() if v['dispositions']},
            'legacy_usage_rows': usage['rows'], 'consistent': check['consistent'], 'summary': check['summary'],
            'checks': {k: v for k, v in check['checks'].items() if k != 'food_view_mismatches'}, 'http_requests': 0}


def recovery_command(repository, database_path, args):
    from .reverse_recovery import Recovery
    recovery = Recovery(database_path, repository, allow_operational=args.allow_operational_database or args.recovery == 'plan')
    if args.recovery == 'plan':  # writes nothing anywhere
        return dict(recovery.plan(), preconditions=recovery.preconditions(), baseline=recovery.baseline())
    result = recovery.run(apply_updates=args.apply_updates)
    return {k: result[k] for k in ('preconditions', 'baseline', 'updates_requested', 'updates_applied', 'writes', 'converged')} | \
        {'remaining': result['plan_after']['summary'], 'losses': result['plan_after']['losses'], 'conflicts': result['plan_after']['conflicts']}


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
                        help='B7 적재: DATABASE_PATH(읽기 전용)를 대상에 옮기고 레거시 사용량 원장을 가져온 뒤 대조; API 호출 없음')
    parser.add_argument('--recovery', choices=('plan', 'apply'),
                        help='Oracle→SQLite 역방향 복구(대상 → DATABASE_PATH). plan은 쓰기 없음, apply는 운영 DB면 --allow-operational-database 필요')
    parser.add_argument('--recovery-target', choices=('oracle', 'sqlite'), help='역방향 복구의 원천 대상')
    parser.add_argument('--apply-updates', action='store_true', help='apply: 적재 당시 파일이 그대로일 때만 mutable 열 갱신을 허용')
    parser.add_argument('--allow-operational-database', action='store_true', help='apply: 설정된 운영 DB로의 복구 허용(G-OPS 승인 후)')
    parser.add_argument('--target-sqlite', metavar='PATH', help='SQLite 대역 대상 DB 경로(운영 DB 경로 사용 불가)')
    parser.add_argument('--shadow', choices=('status', 'enable', 'disable', 'deliver', 'compare', 'repair'),
                        help='B7 Shadow: SQLite 확정 변경을 대상에 멱등 반영; API 호출 없음. enable은 운영 승인 후 사용')
    parser.add_argument('--shadow-target', choices=('oracle', 'sqlite'), help='Shadow 대상(deliver/compare/repair에 필요)')
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
    from .paths import database_path as resolve_database
    database_path = str(resolve_database())
    if args.runtime_status:
        import json
        from .runtime_storage import status as runtime_status
        try:
            result = runtime_status(args.runtime_status, database_path)
        except Exception as exc:  # error codes only; DSN and credentials never printed
            parser.exit(2, 'runtime storage error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.target_load or args.recovery:
        import json
        try:
            if args.recovery and not args.recovery_target:
                raise ValueError('--recovery-target이 필요합니다.')
            repository = target_repository(args.target_load or args.recovery_target, args.target_sqlite, database_path)
            result = target_load(repository, database_path) if args.target_load else recovery_command(repository, database_path, args)
        except ValueError as exc:
            parser.exit(2, str(exc) + '\n')
        except Exception as exc:  # error codes only; DSN and credentials never printed
            parser.exit(2, 'target error: %s\n' % getattr(exc, 'code', type(exc).__name__))
        print(json.dumps(result, ensure_ascii=False, default=str))
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
