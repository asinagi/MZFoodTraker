# B6 Oracle Repository · B7 단계 이관과 Shadow 검증

2026-11-02. Oracle을 목표로 하는 대상 스키마·Repository·이관·대조·Shadow를 구현했다. **이 환경에는 Oracle 인스턴스와 드라이버가 없어 Oracle에서는 아무것도 실행하지 않았다.** 검증은 같은 명세로 만든 SQLite 대역, 정적 DDL 검사, 보존 스냅샷의 임시 복제본에서 했다. 따라서 B6 완료 조건(실제 Oracle에서 CRUD·트랜잭션·제약·동시성 통과)은 **미충족**이다. B7은 대역 기준으로 검증했으며 Oracle 대상 실행은 미검증이다. 실제 외부 API 호출 0, 운영 DB·서버 변경 0.

상태: IMPLEMENTED, TESTED_SQLITE(SQLite 대역 실행), TESTED_ORACLE(실제 Oracle), TESTED_SHADOW(Shadow 경로), TESTED_LIVE, POLICY_BLOCKED, APPROVAL_REQUIRED, NOT_VERIFIED.

## 1. 시작 환경

| 항목 | 값 |
| --- | --- |
| Git | `main` = `origin/main` = `9f8f703`, 미커밋 없음, worktree 1개 |
| 테스트 | 280개 재현 통과([baseline](baseline-tests/test-summary.json)) |
| 운영 SQLite | v0(30개 테이블), integrity ok, 읽기 전용으로만 확인 |
| 서버 | 8000/8002 리스너·Python 프로세스 없음(시작 전부터 정지, 마지막 DB 쓰기 10-28 14:20). 중지·재시작하지 않음 |
| Oracle | 설치·인스턴스(1521)·`sqlplus`·`python-oracledb`·Docker/Podman/WSL 모두 없음 |
| SQLite 의존 | 15개 모듈에 `BEGIN IMMEDIATE`·`INSERT OR IGNORE`·`ON CONFLICT`·`rowid`·`json_valid` 등, `execute` 약 480곳 |

## 2. B6 구현과 검증

| 구성 | 내용 | 상태 |
| --- | --- | --- |
| 대상 스키마 명세 | 29개 데이터 테이블과 관리 테이블. 같은 명세에서 Oracle/SQLite DDL 생성([ORACLE_SCHEMA](ORACLE_SCHEMA.md), [DDL](oracle-target-ddl-v1.sql)) | IMPLEMENTED, 정적 검사 통과, TESTED_SQLITE |
| 값 변환 | UTC 순간값, 정규 JSON, 선언된 열만 ''↔NULL, 19자리 정수, 길이 제한. DBMS 무관하게 같은 거부 규칙 | TESTED_SQLITE |
| Oracle 연결 | `python-oracledb` Thin, 커넥션 풀, `call_timeout`, commit/rollback, ORA/DPY 오류 매핑, 지연 import. 운영 접속은 별도 승인 플래그 없이는 거부. 자격 증명 비출력 | IMPLEMENTED, **NOT_VERIFIED** |
| Repository | 이식 SQL 업서트(조회→INSERT, UNIQUE 충돌 시 재조회→UPDATE), 불변 열·origin 승격 거부, B4/B5 정책 게이트, FOOD_ID 조회·별칭 해석 | TESTED_SQLITE |
| 대상 마이그레이션 | `--target-migrate oracle|sqlite`, `--target-status`. 체크섬 기록, 재실행 무변화, 객체 존재 확인 후 재개(Oracle DDL 암묵 커밋 대비). 서비스 생성자는 DDL을 실행하지 않음 | TESTED_SQLITE, Oracle NOT_VERIFIED |
| 계약 테스트 15종 | 연결·트랜잭션, 반복 마이그레이션, PK/FK/UNIQUE/CHECK, 한글·이모지·조합형, UTC/KST/DST 왕복, ''와 NULL, JSON, 큰 정수, 동시 업서트, UNIQUE 경쟁 재조회, Outbox 재전달, origin 거부, NAVER·YouTube 차단, 만료·불변 | SQLite 15/15 통과, **Oracle 15개 skip** |

## 3. Oracle 버전과 드라이버

정하지 못했다. 설치된 Oracle이 없다. 설계 기준은 기존 ORACLE_MODEL의 19c 호환 기능(CLOB IS JSON, TSTZ, 함수 기반 인덱스, 지연 제약, 식별자 30자)이며 Oracle AI Database Free를 개발 후보로 둔다. 드라이버는 `python-oracledb` Thin 모드로 코드를 작성했고 버전은 설치 시점에 고정한다(G-OPS-0).

## 4~5. 스키마와 Repository 차이

[ORACLE_SCHEMA.md](ORACLE_SCHEMA.md) 참조. 요점: SQLite 서비스는 기록 시스템으로 그대로 두고, 대상 Repository는 이관·Shadow·읽기 모델을 맡는다. B3 Lease 선점과 B4/B5 처리의 Oracle 직접 실행은 미구현이다(G-OPS-6 전제).

## 6~7. B7 이관 대상·제외와 출처별 결과

파이프라인: 읽기 전용 Export → 정책·TTL 필터(만료 제외, 제외된 부모의 자식 연쇄 제외, 캡션 창 경과 시 캡션 필드만 제거) → 매핑(LEGACY_KEY_MAP) → Oracle 기준 엄격 변환·검증 → 청크별 커밋(마지막 키 기록, 재개) → 대조.

보존 스냅샷(SHA-256 `09e323f9…`, 전후 동일)의 임시 복제본, 2026-11-02 기준([b7-copy-migration.json](b7-copy-migration.json)):
- 준비: v0→v11 → B4/B5 보강 → 복제본 안에서만 테스트용 음식 '두쫀쿠' 승인(실제 언급 생성 목적, 라벨 표기) → 원본 정책 만료 정리.
- 적재 16,576행, 제외 0, 거부 0. 이 스냅샷의 YouTube(29일)·Instagram 캡션(30일)이 아직 기한 안이라 제외가 없다. 만료·연쇄 제외·캡션 제거는 테스트에서 별도 검증했다.

| 출처 | 고유 콘텐츠 | 원본(source_contents) | 비고 |
| --- | --- | --- | --- |
| Google RSS | 334 | 334 | 분류 334, 분류 이력 245, LEGACY_KEY_MAP |
| Instagram | 129 | 129 | 캡션은 30일 창 안에서만 이관 |
| YouTube | 248(스테이징) | 253 | 원래 만료 시각 그대로 |
| NAVER | 0 | 0 | 콘텐츠·지수 저장 없음(정책) |

검색 근거 400, 음식 언급 99, 지표 820, 품질 결과 10,978. 실데이터에는 B3 실행 작업이 없어 추적·실행 테이블은 0건이며, 이 영역의 이관은 테스트 픽스처에서만 검증됐다.

## 8. 건수·해시·관계 대조

29개 테이블 모두 누락 0, 초과 0, 변경 0, 정규화 후 해시 일치, NULL 수 일치. FK 오류 0, 원본 참조 미해결 0, 정책 위반 행 0, 대상 내 만료 행 0, FOOD_ID 조회 일치. 재이관은 쓰기 0, 건수 불변. 테스트에서는 값 변조·행 삭제를 대조가 탐지함을 확인했다.

## 9. Legacy와 TTL

출처 불명 콘텐츠 711건이 모두 `first_origin=legacy_unknown`, 논리 회차는 `coverage=UNKNOWN`으로 유지됐다. origin 승격은 Repository가 거부한다. 만료 시각은 복사만 하고 재계산하지 않는다. 테스트에서 YouTube 만료 시각이 원본과 같은 순간값임을 확인했다. 만료 행은 이관하지 않고 자식도 함께 제외하며, 만료 캡션 기반 언급은 재생성하지 않는다. 이관 임시 파일은 임시 디렉터리에서만 쓰고 삭제했다.

## 10. Shadow

SQLite v11 트리거가 같은 트랜잭션에서 (테이블, 키)만 기록하고, `enabled=1`일 때만 동작한다(기본 비활성). 전달기는 현재 확정 행을 B7과 같은 규칙으로 다시 읽어 대상에 멱등 반영하며, 수집기를 다시 실행하거나 API를 호출하지 않는다.
- 실데이터 복제본: 초기 이관 후 별칭 승인과 재처리로 이벤트 7건이 생겼고, 7건 모두 전달됐다. 실패 0, 전달 후 정합성 일치. **TESTED_SHADOW (SQLite 대역)**.
- 테스트: 기본 비활성, 신규 활동 수렴(HTTP 증가 0), 반복 전달 멱등, 대상 장애 시 대기·백오프·복구, 커밋 후 응답 유실, 부모보다 먼저 온 자식의 재시도, 드리프트 탐지와 복구, 삭제·만료 전파.

## 11. 장애·재시도·복구

중단 후 재개(커밋된 청크만 남고 재실행 시 완료), 커밋 후 응답 유실(재실행 쓰기 0), 중복 입력·UNIQUE 경쟁, 부모·자식 순서(지연 FK와 재시도), 참조 누락(`missing_parent` 거부), Outbox 반복 전달, 원본 만료, 정책 위반(`EXCLUDED_POLICY`), SQLite와 대상 쓰기 시점 차이(Shadow 대기열)를 테스트했다. 복구 리허설: `repair()`가 대상을 원본과 같게 되돌린다. Oracle 전환 **후** 역방향 복구는 미구현이다.

## 12. 성능 (개발 PC, SQLite 대역 — 운영·Oracle 수치 아님)

| 측정 | 값 |
| --- | --- |
| 초기 이관 | 16,576행 9.2초(약 1,800행/초) |
| 대조 | 4.7초 |
| 단건 업서트 트랜잭션 | 약 13ms |
| FOOD_ID 조회 / 별칭 조회 | 약 5ms / 1.8ms |
| Shadow 반영 | 이벤트당 평균 약 38ms |
| Python 최대 메모리 | 약 26MB |
| 연결 수 | 트랜잭션당 1개, 종료 후 0 |
| 용량 | 대상 11.4MB(원본 복제본 17.6MB) |

같은 회차의 첫 실행은 약 885행/초였다. 측정 편차가 크므로 참고치로만 본다.

## 13. 테스트

| 단계 | 결과 |
| --- | --- |
| 기준 | 280 통과 |
| Commit 1 | 290 통과 |
| Commit 2 | 306 실행 통과 + Oracle 15 skip |
| Commit 3 | 315 실행 통과 + 15 skip |
| Commit 4·최종 | 323 실행 통과 + Oracle 15 skip, 실패·오류 0, 외부 소켓 0. [final](final-tests/test-summary.json) |

신규: `test_target_schema.py`(10), `test_target_compat.py`(SQLite 16 + Oracle 15 skip), `test_target_migration.py`(9), `test_shadow.py`(8). 복제본 마이그레이션 0→11에서 원래 30개 테이블 보존([copy-migration.json](copy-migration.json)).

## 14~16. 실제 Oracle·API·운영 영향

- 실제 Oracle 테스트: 0건(15개 계약 테스트 skip).
- 실제 외부 API 호출: 0건.
- 운영 DB·서버: 변경 0([production-final.json](production-final.json)).

## 17. Git

| 커밋 | 내용 |
| --- | --- |
| `5303b61` | feat: implement Oracle repository and database abstractions |
| `e55f81c` | test: validate Oracle repository compatibility |
| `81fd1cd` | feat: implement staged SQLite to Oracle migration |
| `18035ba` | feat: add Oracle shadow verification and recovery |
| 이 보고서 커밋 | docs: document Oracle migration validation and rollout gates |

모두 `main`에 fast-forward Push했다.

## 18. 승인 게이트

[DEPLOYMENT_GATES.md](DEPLOYMENT_GATES.md). G-OPS-0(개발 Oracle·드라이버 준비)을 새로 추가했으며 B6 완료에 필요하다. G-OPS-1~6은 모두 APPROVAL_REQUIRED이고 실행하지 않았다.

## 19. 미구현·미검증·차단

| 항목 | 상태 |
| --- | --- |
| 실제 Oracle에서의 DDL·계약·이관·Shadow | NOT_VERIFIED (G-OPS-0 승인 필요) |
| Oracle TSTZ 읽기 값의 시간대 처리(naive 반환 시 UTC 가정) | NOT_VERIFIED |
| 서비스 계층(B3 Lease 선점, B4/B5 처리)의 Oracle 직접 실행 | 미구현 |
| Oracle 전환 후 역방향 복구 | 미구현 |
| 제외한 legacy 테이블의 행별 이관 | 미구현(ORACLE_SCHEMA 범위 참조) |
| 실데이터의 실행 이력(B3 작업) 이관 | 데이터 없음, 픽스처에서만 검증 |
| YouTube 음식 언급·지표, NAVER 지수 저장 | POLICY_BLOCKED |
| Live API, 운영 마이그레이션, Oracle 운영 생성·전환 | APPROVAL_REQUIRED |

## 20. B8 인수 계약

[B8_HANDOFF.md](B8_HANDOFF.md).

## 재실행

```powershell
py -3.12 -B tests/run_offline.py --output reports/development-b6-b7-2026-11-02/final-tests
py -3.12 -B tests/verify_b7_copy.py data/b3-preservation-20261027T082023Z/snapshot.sqlite3 --output <out>.json
# 실제 Oracle(격리 스키마, 승인 후): ORACLE_TEST_DSN/USER/PASSWORD, ORACLE_TEST_ALLOW_RESET=true
py -3.12 -B -m unittest discover -s tests -p test_target_compat.py
```
