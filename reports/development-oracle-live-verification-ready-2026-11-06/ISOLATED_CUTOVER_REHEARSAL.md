# 격리 전환 리허설 — 운영 SQLite 격리 v11 사본 + MZ_TEST (Phases A·C·D-3)

실행 2026-11-07 13:28 KST, `tests/rehearse_live_cycle.py`(절전 방지 래퍼 아래, 178.6초). **통과(28개 점검 전부).** 근거: [isolated-rehearsal-oracle.json](isolated-rehearsal-oracle.json)(건수·코드·해시·시간만).

- 원본: 운영 `data/trends.sqlite3`를 읽기 전용(`mode=ro`, `query_only`)으로만 열었다. `rehearse_cutover.guarded`가 운영 파일의 쓰기 가능 연결과 루프백 밖 소켓을 모두 거부했고, 거부 시도는 0건이다.
- 명령: 운영자가 쓸 CLI를 같은 프로세스에서 그대로 실행했다(`.env`는 읽지 않고 합성 자격 증명 사용). Live 요청은 기존 클라이언트를 그대로 쓰고 네트워크 아래만 기록용 가짜 전송으로 바꿨다(밖으로 나간 요청 0).
- 격리 사본은 임시 폴더에 만들고 끝에 삭제했다(삭제 확인). MZ_TEST는 시작 전 일회용 계정 재확인 뒤 초기화, 끝에 다시 초기화(테이블 0).
- 같은 날 첫 실행은 시작 1분 46초 뒤 PC가 Modern Standby(05:42:59~13:25:35 KST)에 들어가 7시간 넘게 멈췄다가 깨어난 뒤 통과했다. 시간 간격이 없는 근거를 위해 절전 방지 래퍼로 다시 실행한 결과가 위 근거 파일이다.

## 1. 운영 SQLite 격리 사본과 v0 → v11 (Phase A)

`py -3.12 -B -m food_trends --isolate-database <사본>`

| 점검 | 결과 |
| --- | --- |
| 원본 사실(전후 동일) | SHA-256 `38b880f5…689548c0`, 9,273,344바이트, 수정 시각 2026-10-28 05:20:01Z, WAL 0, v0, integrity ok, 30개 테이블 7,701행 — 7개 항목 모두 변화 없음 |
| 사본 방식 | SQLite 백업 API(읽기 전용 원본) → 사본 헤더에 격리 표시(`application_id` `0x4D5A4953`, 운영 파일은 0) → 사본만 마이그레이션 |
| 마이그레이션 | 1~11 적용, 원래 30개 테이블의 열·행 보존(`original_rows_and_columns_preserved`, 변경 테이블 없음) |
| 사본 결과 | v11, integrity ok, FK 오류 0, 72개 테이블 21,777행 |
| B4/B5 스테이징 | RSS 회차 95개(성공 90·실패 5 포함) 스테이징, RSS 항목 370/370, 관측 900 → run_items 900·트래픽 지표 900, Instagram 출처 불명 논리 회차 185/185. YouTube `live_api` 영상 431건은 스테이징 경로 없음(원문만 이관, 기존 설계) |
| 데이터 계보 | 회차·항목·관측·영상·게시물 불일치 0(출처·origin·관측 시각·수명 범위·만료 모두 일치) |
| 보존 기간 | 지난 캡션 0·만료 영상 0(2026-11-07 기준 아직 창 안이라 정리 대상 없음, 정리 결과 0건). 만료 원문 부당 복원 0 |
| 임의 FOOD_ID | `food_entities` 0·`food_aliases` 0(음식 승인 없음) |
| 분류되지 않은 테이블 | 0(MIGRATED 31, EXCLUDED 24, RUNTIME 9, STAGED 4, LEDGER 3, EMBEDDED 2) |
| 재실행 | 같은 경로로 다시 실행 → `isolation error: isolated_copy_exists`(덮어쓰기 없음). 같은 사본의 준비 재실행은 데이터 변화 0, 같은 원본의 두 번째 사본은 마이그레이션 기록 시각·DQ 임의 ID 외에 동일(`tests/test_isolated_copy.py`) |
| 경로 보호 | 운영 파일명·경로·`DATABASE_PATH`·하드 링크·기존 파일·남은 WAL·Git이 추적할 확장자는 복사 전에 거부. `--isolated-database`는 격리 표시가 있고 운영 DB가 아닌 파일만 받는다(테스트 4개) |

## 2. Writer 전 과정 (Phase C, 수집원 4개)

| # | 단계(명령) | 결과 |
| --- | --- | --- |
| 1 | 격리 v11 준비 | 위 1절 |
| 2 | 레거시 Writer 상태 | 사본은 레거시 소유(B3 소유 없음). 운영 DB는 v0이라 SQLite 수집기 시작 불가 |
| 3 | 기존 수집기 정지 | 서버·스케줄러 없음, `*_ENABLED=false`. 스위치를 켠 채 점검하면 Instagram·YouTube가 `LEGACY_SCHEDULER_ENABLED`로 거부(확인) |
| 4 | 테스트 registry | 합성(`approved_by: rehearsal-operator`, 근거 "isolated rehearsal grant, not a real approval") — 실제 승인 근거 아님 |
| 5 | 사용량 원장 | `--target-migrate oracle`(v10) → `--target-load oracle --isolated-database`: 15,671행, 제외·거부 0, 대조 `consistent`, 열린 레거시 사용량 0행, 사용량 확인 계정 3개(Instagram 설정 계정 포함) |
| 6 | Oracle 준비 점검 | 이전 전 `ready: false`, 수집원마다 `LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED`만 |
| 7 | Writer 이전 | 4개 모두 `EXCLUSIVE`(cycle 1). 다시 실행 → `unchanged: true`. 이전 뒤 점검 `ready: true` |
| 8 | 가짜 전송 Live 요청 | 관리 API로 RSS 수집(항목 3·새 항목 2, **운영 사본의 실제 최신 항목 1개 재관측**) → 후보 2 → 음식 승인·별칭 → 3개 수집원 추적 → 계획 → Live 예약(Instagram 2·YouTube 3·NAVER 1 작업) → 실행 |
| 9 | 작업·호출·예산 기록 | 아래 3절. 요청 7회(상한 9), 대상 기록과 수집원별로 일치, 두 번째 실행 추가 요청 0 |
| 10 | Writer 철회 | YouTube의 마지막 요청(videos)이 전송 중일 때 `--writer-release youtube` → `DRAINING`(종료 코드 2). 그 요청은 저장되고 남은 YouTube 작업 2개는 요청 없이 `CANCELLED`/`WRITER_RELEASED`. 이어서 YouTube `RELEASED`, 나머지 3개 수집원은 그때도 `ORACLE`(다른 수집원 영향 없음). 이후 Instagram(대기 작업 1개 정리)·NAVER·RSS 철회 |
| 11 | 진행 중 요청·Lease 종료 | 철회 뒤 `--writer-status`: 4개 모두 `RELEASED`, 활성 Live 작업 0, Live Lease 0 |
| 12 | 사용량 인계 | `--writer-handover --isolated-database`: Instagram 고유 태그 1(창 끝 11-14 13:29 KST), YouTube search 1·videos 1(11-07 17:00 KST = 태평양 자정), NAVER 1(11-08 00:00 KST), RSS 0. 누락 레거시 사용량 0, Oracle 기간 레거시 쓰기 0. 다시 실행 → 4개 모두 0건 |
| 13 | Oracle → SQLite 복구 | 계획: 대상 전용 234행, mutable 갱신 3(재관측 항목의 `rss_items`·`rss_classifications`·`source_items`), 충돌 0. `--recovery apply --apply-updates`: 기준선 `verified`(인계가 남긴 파일 지문으로 확인, `matches_recorded_handover: true`) → 삽입 232·원문 2·갱신 3, 남은 대상 전용·갱신·충돌 0, `converged`. 다시 실행 → 쓰기 0 |
| 14 | 재개 판정 | 인계 전: Instagram·YouTube·NAVER `ORACLE_USAGE_NOT_HANDED_OVER`(각 창 끝까지), RSS `ORACLE_RSS_NOT_RECOVERED`. 인계·복구 뒤: 4개 모두 `ready: true` |
| 15 | 재실행·충돌 | 철회 재실행 4개 모두 `unchanged`, 인계·복구 재실행 0건. DRAINING 중 Writer 재이전 409, 인계 409(`ORACLE_WRITER_NOT_RELEASED`), 재개 판정 `ORACLE_WRITER_NOT_RELEASED`·`ORACLE_LIVE_JOBS_ACTIVE`·`ORACLE_USAGE_NOT_HANDED_OVER` |

중간 실패 후 재실행 수렴(철회 첫 단계 뒤 실패·인계 반복)은 `tests/test_writer_release.py`가 대역과 MZ_TEST에서 검증한다. 리허설의 DRAINING 처리(요청 단계만 커밋된 철회를 다시 실행해 완료)도 같은 경로다.

## 3. 요청 수·예산 (검증 상한 아래)

| 수집원 | 요청(가짜 전송이 받은 수) | 대상 기록 | 작업 결과 | 계획 상한 |
| --- | ---: | --- | --- | ---: |
| Google Trends RSS | 1 | RSS 회차 1 | 성공 | 1 |
| Instagram | 3(최근 검색 확인 1·해시태그 조회 1·최근 게시물 1) | 호출 3 SUCCEEDED | 1 성공, 두 번째 새 태그 작업은 **요청 0**으로 `RETRY_WAIT`(budget) → 철회가 정리 | 5 |
| YouTube | 2(search 1·videos 1) | 호출 2 SUCCEEDED | 1 성공, 2개는 철회 표시 뒤 요청 0으로 취소 | 2 |
| NAVER DataLab | 1 | 호출 1 SUCCEEDED | 성공 | 1 |
| 합계 | **7** | 일치 | 다시 실행(예약 포함 2회): 추가 요청 0 | 9 |

인계 뒤 기존 SQLite 예산 코드(검증 설정 그대로)가 격리 사본에서: YouTube 새 검색 `budget` 거부, NAVER 오늘 호출 1, Instagram 7일 고유 태그 1·새 태그 `budget` 거부. 모두 요청 전에 막았다.

## 4. 데이터 정합성

| 대상 | 결과 |
| --- | --- |
| MZ_TEST | FK 오류 0, 풀리지 않은 원문 참조 0, 정책 위반 행 0, 만료 잔존 행 0. 실행 회차 origin 전부 `live_api` |
| 격리 사본(복구 후) | v11, integrity ok, FK 0 |
| 복구 손실 목록(설계상) | 슬롯 생명주기 3, 원래 해시태그 표기 1, 캡션 수집 시각 1(만료 시각 유지), YouTube 원문 재직렬화 1(원래 해시는 스냅샷에 보존), 원본 RSS 피드 본문 1 |

## 5. SQLite 연결 (Phase D-3)

| 단계 | 연결 | 구분 |
| --- | --- | --- |
| Live 라운드(RSS 수집 → 승인 → 추적 → 계획 → Live 실행 → B4/B5 → 관측) | **0** | 정상 Live 저장 경로 |
| 격리 사본 생성 | 운영 파일 읽기 전용 5, 사본 311 | 예외: 격리 마이그레이션 |
| 적재·사용량 가져오기 | 사본 5 | 예외: 사용량 가져오기 |
| 철회 중 운영자 명령(인계·재개 판정 시도) | 사본 5 | 예외: 운영자 점검 |
| 사용량 인계 | 사본 11 | 예외: 인계 |
| 역방향 복구 | 사본 49 | 예외: 복구 |
| 레거시 예산 확인·재개 판정 | 사본 15·12 | 예외: 레거시 코드 점검 |
| 그 밖의 리허설 점검(재실행·사본 사실) | 사본 34 | 리허설 자체 점검 |

## 6. 운영 전환과의 차이

- Writer 이전은 MZ_TEST의 `quota_accounts`에만 기록됐다. 운영 SQLite의 수집기 설정·소유권·사용량 원천은 그대로다.
- 합성 registry와 합성 자격 증명을 썼다. 이 결과는 실제 권한·제공자 사용량의 근거가 아니다.
- 경로 A의 인계·복구 대상은 격리 사본이다. 운영 SQLite로의 인계·복구는 G-OPS-3 이후 경로 B에서 `--allow-operational-database`로만 한다.
