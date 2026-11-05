# ORACLE-RUNTIME-2 통합 체크리스트 (feat/b8-app-api → main)

> **완료 (2026-11-05, B8-INTEGRATION-1).** 아래 절차(병합, 검증, main Fast-forward)와 §5-7의 Oracle 연결·동등성 확인을 수행했다. 결과와 발견 사항은 [B8-INTEGRATION-1 보고서](../development-b8-integration-2026-11-05/REPORT.md). 워크트리 정리(§5-6)는 사용자 결정 전까지 보류.

PARALLEL-B8-API는 ORACLE-RUNTIME-2와 병렬로 별도 워크트리(`D:\MZFoodTraker-B8-API`)·브랜치(`feat/b8-app-api`)에서 개발했다. **ORACLE-RUNTIME-2가 끝나기 전에는 main에 병합하지 않는다.** 이 문서는 그 이후의 통합 작업 목록이다.

## 1. 기준점

| 항목 | 값 |
| --- | --- |
| 분기점 | `origin/main` 3ac56d9 (docs: record Oracle runtime parity validation) |
| B8 커밋 | 9b2156c (P1 계약), 6e9d274 (P2 분석), 7d3da22 (P3 API·Mock), d869c75 (P4 테스트·인수 문서), 이후 앱 개발자 안내 문서 보강 커밋(코드·계약 변경 없음) |
| 점검 시점 `origin/main` | f3a77d1 — ORACLE-RUNTIME-2 진행 커밋 91ab0dd, 203e67b, f3a77d1 (병합하지 않음) |

## 2. 변경 파일 겹침

- **B8은 기존 파일을 하나도 수정하지 않았다.** 추가 파일: `food_trends/{app_contract,analytics_reader,analytics_service,observation_writer,app_api,app_mock}.py`, `tests/{test_food_analytics,test_app_api,test_app_contract,app_api_support}.py`, `docs/app-api/**`, `reports/development-b8-app-api-2026-11-04/**`.
- f3a77d1까지 ORACLE-RUNTIME-2가 바꾼 파일: `oracle_execution.py`, `oracle_ingestion.py`, `oracle_observations.py`, `oracle_planner.py`(신규), `query_planner.py`, `runtime_ownership.py`(신규), `runtime_storage.py`, `shadow.py`, `target_migration.py`, `target_schema.py`(대상 스키마 v2→v5), `tests/test_oracle_*`, `tests/test_runtime_ownership.py`.
- 같은 경로가 없으므로 텍스트 충돌은 예상되지 않는다. 남은 위험은 아래 의미상 의존이다.

## 3. B8이 의존하는 기존 계약

| 의존 | 사용처 | 통합 시 확인 |
| --- | --- | --- |
| SQLite v11의 B4/B5 테이블·컬럼 (`ingest_runs`, `source_items`, `run_items`, `quality_results`, `food_mentions`, `metric_observations`, `food_entities`, `food_aliases`, `food_relations`, `food_events`, `entity_change_effects`) | `analytics_reader` SQL | 컬럼 이름·상태값·`instagram.observed_posts` 의미(실패 회차는 `NOT_OBSERVED`) 유지 |
| B6 대상 스키마의 같은 테이블, `TARGET_SCHEMA_VERSION` | `TargetAnalyticsStore` | v5에서 해당 테이블 변경 없음(사전 점검 확인). 이후 버전에서도 확인 |
| `FoodObservationService.link_run/_run/_insert_metric/purge`, `IngestionService.ingest/rss_run/purge` | `observation_writer` (Mock·테스트 전용) | 내부 메서드 이름·시그니처 변경 시 기록기 수정 |
| `target_migration.migrate`, `TargetRepository.migrate` | 대상 스키마 동등성 테스트 | v5 이관에서 B4/B5 행 제외 0 유지 |
| `api.LOOPBACK`, `api.TOKEN_HEADER`, `contracts.SOURCES` 정책, `food_observations.mention_policy`, `ingestion.METRIC_STORAGE` | `app_api`, `analytics_service` | 정책이 바뀌면 `availability`가 자동으로 따라감. YouTube·NAVER가 INCLUDED가 되면 계약 변경으로 별도 검토 |

## 4. 사전 통합 점검 (dry run, 병합 아님)

점검 시점 `origin/main` f3a77d1의 `git archive` 스냅샷(스크래치 폴더, `.env`·`data/` 없음)에 B8 파일만 복사해 실행했다. 브랜치·main·다른 세션 작업 트리는 바꾸지 않았다. 결과는 [REPORT.md](REPORT.md) §6.

- 첫 실행에서 결함 발견: 예시 `error-admin-token.json`이 `.gitignore`의 `*token*.json`에 걸려 P3 커밋에서 빠졌다(로컬 워크트리에서는 파일이 있어 테스트가 통과). P4에서 `error-admin-header-rejected.json`으로 이름을 바꾸고, 예시 이름이 무시 규칙에 걸리지 않는지 검사하는 테스트를 추가했다.

## 5. 통합 절차 (ORACLE-RUNTIME-2 완료 후)

1. ORACLE-RUNTIME-2 완료와 main Push를 사용자에게 확인한다. 공유 Oracle 테스트 스키마(MZ_TEST) 사용 일정을 조율한다.
2. B8 워크트리에서 `git fetch origin` 후 **`git merge origin/main`**(브랜치에 병합 커밋). 이미 Push한 커밋을 다시 쓰는 rebase·`--force` Push는 하지 않는다.
3. 충돌을 검토한다(예상 없음). `README.md`는 이 브랜치에서 수정하지 않았으므로, 병합 후 최신 README에 다음을 추가한다: 구현 현황 표의 "앱 데이터 API (B8)" 행, 테스트 수, `docs/app-api/README.md` 링크. 또한 `docs/app-api/README.md`의 "빠른 시작"이 `feat/b8-app-api` 브랜치를 받도록 안내하므로, main 기준(`git clone https://github.com/asinagi/MZFoodTraker.git`)으로 바꾸고 앱 개발자에게 알린다.
4. 검증
   - 기본 SQLite 전체: `py -3.12 -B tests/run_offline.py --output data/<폴더>` (B8 38개 포함, 실패 0)
   - 계약 산출물: `py -3.12 -B -m food_trends.app_contract --docs docs/app-api --check`, `py -3.12 -B -m food_trends.app_mock --write-examples docs/app-api/examples` 후 `git diff --exit-code docs/app-api`(코드가 바뀌면 Mock 데이터셋을 다시 만들지만 응답은 같아야 함)
   - 실제 Oracle 전체(MZ_TEST, `ORACLE_TEST_*` 설정 시): 기존 Oracle 회귀 + 아래 7번
5. 통과하면 `git push origin feat/b8-app-api`, 이어서 `git push origin feat/b8-app-api:main`(main이 브랜치의 조상이므로 Fast-forward). 원격 `main`이 브랜치 HEAD와 같은지 `git ls-remote`로 확인한다.
6. 정리: 병합 후 `git worktree remove D:\MZFoodTraker-B8-API`, 브랜치 보존·삭제 여부를 사용자에게 보고한다.
7. **앱 API의 Oracle 연결(후속)**: `app_api` CLI에 `--store oracle-test`(→ `runtime_storage.select('oracle-test')` + `TargetAnalyticsStore`)를 추가하고 MZ_TEST에서 확인한다.
   - `SET TRANSACTION READ ONLY`가 첫 문장으로 동작하는지, 읽기 트랜잭션이 `runtime_ownership`(v5)의 어떤 모드에서도 쓰기 소유권을 요구하지 않는지
   - TSTZ 반환값(naive/aware) → UTC 변환, `food_events` JSON CLOB 읽기, `count(DISTINCT …)` 숫자형, 대문자 컬럼명 → 소문자 키
   - IN 목록 바인드 개수(병합 음식 수, 1000 제한 미만)
   - 같은 입력에서 SQLite와 Oracle 응답이 같은지(`test_target_schema_store_matches_sqlite_store`를 Oracle 대상에 확장)

## 6. 이번 범위에서 하지 않은 것 (통합 후 과제)

- 저장 집계 테이블·마이그레이션 ([MATERIALIZED_AGGREGATE.md](MATERIALIZED_AGGREGATE.md))
- 외부 공개 배포·인증 ([ACCESS.md](../../docs/app-api/ACCESS.md) §6)
- 종합 유행 점수 ([COMPOSITE_SCORE_PROPOSAL.md](COMPOSITE_SCORE_PROPOSAL.md), 비활성)
- 운영 DB 마이그레이션(현재 v0) — 앱 API 실제 서버는 최신 스키마 DB만 연다
