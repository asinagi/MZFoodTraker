# 운영 승인 게이트 (G-OPS-1 ~ 5)

2026-10-28 기준. **이 문서의 어떤 단계도 실행하지 않았다.** 각 단계는 실행 직전 사용자의 별도 명시 승인이 필요하다. 이번 요청서는 운영 변경에 대한 포괄 승인이 아니다.

## 현재 운영 상태 (읽기 전용 확인)

| 항목 | 값 |
| --- | --- |
| 운영 DB | `data/trends.sqlite3`, WAL 모드, 스키마 v0(30개 테이블, `schema_migrations` 없음), integrity ok |
| Writer 1 | PID 50584 `python -m food_trends` (2026-10-26 15:34 시작). RSS·설정상 활성 출처 스케줄러 포함 |
| Writer 2 | PID 51280 `python -m food_trends --no-schedulers` (15:37 시작). UI 서버지만 UI POST로 수집·쓰기 가능 |
| 실행 중 코드 | 두 프로세스 모두 v0 시절 코드가 메모리에 적재됨. 현재 코드(v10)로 재시작하면 `MigrationRequired`로 기동 거부 |
| 관측 | 세션 중 RSS runs 87→91 등 계속 증가. 기존 Writer가 정상 수집 중 |

## G-OPS-1 운영 DB 백업

| 승인 요청 항목 | 내용 |
| --- | --- |
| 실행 작업 | SQLite online backup API로 `data/trends.sqlite3` 사본 생성(WAL 포함 일관 사본), `PRAGMA integrity_check`, SHA-256 기록 |
| 변경 대상 | 새 파일 `data/ops-backup-<UTC>/trends.sqlite3`만 생성. 원본은 읽기만 |
| 영향 | 백업 중 짧은 읽기 잠금. 수집 중단 없음 |
| 복구 | 백업은 원본을 덮어쓰는 데 쓰지 않는다. 복원은 별도 경로 DB로 전환하는 방식 |
| 선행 조건 | 디스크 여유(현재 약 8.8 MB의 수 배), `data/`가 Git 제외인지 재확인 |
| 승인 필요 사항 | 백업 생성 시점과 보관 경로, 보관 기간(출처 TTL이 지난 원문은 사본에서도 정리) |

## G-OPS-2 유지보수 창과 Writer 정지

| 승인 요청 항목 | 내용 |
| --- | --- |
| 실행 작업 | PID 51280(UI) → PID 50584(스케줄러) 순서로 Ctrl+C 상당의 정상 종료. 진행 중 수집이 끝날 때까지 대기 |
| 변경 대상 | 두 로컬 서버 프로세스 |
| 영향 | 유지보수 창 동안 RSS·Instagram·NAVER·YouTube 정기 수집과 로컬 UI 중단 |
| 완료·중단 기준 | 종료 후 WAL checkpoint가 끝나고 `-wal` 크기 0, 다른 연결 없음 확인 |
| 재시작 순서 | G-OPS-3 완료 후 `python -m food_trends`(preview 모드), 확인 후 UI 서버 |
| 복구 | 실패 시 G-OPS-1 백업을 **새 경로**에 복원하고 이전 코드 커밋(`59376f2` 이전 실행 코드)으로 별도 기동 |
| 승인 필요 사항 | 유지보수 시각과 허용 중단 시간 |

## G-OPS-3 운영 DB 마이그레이션 (v0 → v10)

| 승인 요청 항목 | 내용 |
| --- | --- |
| 실행 작업 | `python -m food_trends --migrate` 한 번. 1~10이 단일 트랜잭션으로 적용되고 실패 시 전체 rollback |
| 변경 대상 | 운영 DB 스키마. 기존 30개 테이블의 열·행은 변경하지 않음(보존 스냅샷 복제본에서 0→10 대조 통과) |
| 영향 | 이전 코드는 새 스키마에서도 기존 테이블로 동작하나, 현재 코드만 v10을 요구. 버전 불일치 시 기동 차단 |
| 사전 검증 | 직전 G-OPS-1 백업의 복제본에서 `tests/verify_preserved_copy.py`, `tests/verify_b45_copy.py` 재실행 |
| 실패 시 | 트랜잭션 rollback으로 v0 유지. 파일 손상 시 백업을 새 경로로 복원 |
| 선택 후속 | `--ingest-pending`(RSS 회차 스테이징), `--ingest-legacy youtube|instagram_graph`. 모두 API 호출 없음, 별도 승인 권장 |
| 승인 필요 사항 | 마이그레이션 실행과 적용 후 스테이징 백필 여부 |

## G-OPS-4 수집 실행 소유권 이전

| 승인 요청 항목 | 내용 |
| --- | --- |
| 역할 비교 | 기존 수집기: 출처별 자체 루프·자체 예산. B3: 계획→Outbox→Lease→예약→HTTP→페이지 커밋, B4/B5 자동 연결 |
| 단일 책임 | 출처·계정마다 `execution_source_ownership.owner`가 LEGACY 또는 B3 하나. B3면 기존 수집기는 `busy`/`duplicate`로 거부 |
| 실행 작업 | 출처 하나에 대해: 해당 `*_COLLECTION_ENABLED=false`, `execution_ownership.transfer(..., legacy_stopped=True)`, `B3_EXCLUSIVE_SOURCES=<출처>` |
| 시작 조건 | `--execution-readiness <registry>`가 ready=true(스키마, B3_LIVE_ENABLED, registry 승인, 자격 증명, legacy 스케줄러 off, 소유권, 차단 없음) |
| 복원 | owner를 LEGACY로 되돌리고 플래그 복구. B3 작업은 남아 있어도 Live 게이트가 막음 |
| 정합성 확인 | 전환 전후 `youtube_calls`/`naver_calls`·`execution_calls` 일별 합계, 중복 HTTP 0 |
| 승인 필요 사항 | 대상 출처와 순서(권장: NAVER → YouTube → Instagram 중 한 개씩) |

## G-OPS-5 Live API 검증 (출처별 사전 보고)

모든 수치는 현재 계획기 설정 기준의 상한이다. 실제 계정 한도는 공급자 콘솔이 최종 기준이며 **이번에 확인하지 않았다(미확인)**.

| 출처 | 대상·목적 | HTTP 최대 | 예산 영향 | 저장·파생 | 충돌·차단 조건 |
| --- | --- | --- | --- | --- | --- |
| NAVER | 승인 FOOD 1개, 별칭 그룹 1개, 30일 일별 지수 | 1 (재시도는 별도 job attempt, 최대 3) | KST 일 100 중 최대 3 | 응답·ratio 저장 0, 메모리 DQ만 | 기존 NAVER 스케줄러 off 필수. 401/403 → 계정 차단 |
| YouTube | 승인 별칭 검색어 1개, 최근 7일 | 검색 1·상세 1 per page, max_pages 1 → attempt당 2, 최대 6 | search·videos 일 한도(PT 기준) | 영상 29일 TTL, 음식 언급·파생 지표 0 | quota 오류 → PT 자정까지 차단 |
| Instagram | 승인 정확 태그 1개 | 사용 확인 ≤3 + lookup 1 + media ≤20 페이지 → attempt당 ≤24 | 7일 고유 태그 1개 소모 | 캡션 30일, 캡션 해시태그 기반 언급만 | 고유 태그 30/soft28 초과 시 7일 차단, 토큰 만료 시 계정 차단 |

공통 실패 처리: Retry-After 존중(단축 없음), 전송 후 불확실은 UNKNOWN·환불 없음, 3회 실패 시 FAILED와 결과 범위 기록.

승인 필요 사항: 출처, 검색어·FOOD_ID, 실행 시각, 최대 호출 수 동의.
