# 데이터 이관 보고서 — 운영 SQLite → 영구 Oracle 운영 스키마 `MZ_APP`

실행 2026-11-08 00:37~00:45 KST(2026-11-07 15:37~15:45Z). 근거: [backup-verification.json](backup-verification.json), [production-load-1.json](production-load-1.json)·[production-load-2.json](production-load-2.json), [migration-verification.json](migration-verification.json). 건수·코드·해시만 기록했다.

## 1. 운영 SQLite 원본

| 항목 | 값 |
| --- | --- |
| 경로 | `data/trends.sqlite3`(Git 제외) |
| 스키마 | v0(마이그레이션 이력 없음), WAL 모드, WAL 0바이트 |
| 규모 | 30개 테이블, 7,701행, 9,273,344바이트 |
| SHA-256 | `38b880f535dded56e84a11570a1f7793bed1668580950172a417687b689548c0`(이전 기록과 같음) |
| 무결성 | integrity ok, FK 오류 0 |
| 수정 시각 | 2026-10-28 14:20:01 KST — 작업 전후 동일(원본은 읽기 전용으로만 열었다) |

원본은 **변경하지 않았다**(v0 그대로). 요청서 C-3의 우선 경로대로, 원본을 보존한 채 검증된 v11 사본으로 이관했다.

## 2. 백업 (G-OPS-1)

| 항목 | 값 |
| --- | --- |
| 경로 | `data/ops-backup-20261107T153742Z/trends-v0-backup.sqlite3`(Git 제외) |
| 생성 | 2026-11-07 15:37:42Z, SQLite 백업 API(원본은 `mode=ro`·`query_only`) |
| 크기·SHA-256 | 9,273,344바이트, `e211a8c1430fce8ec6abd28816b2073c208a247abe2fecf41edd18604084943a`(페이지 배치가 달라 원본과 파일 해시는 다름) |
| 스키마·규모 | v0, 30개 테이블, 7,701행(원본과 같음) |
| 내용 대조 | 모든 테이블의 모든 열·행 SHA-256 다이제스트가 원본과 같음 |
| 무결성 | integrity ok, FK 오류 0 |
| 복원 시험 | 백업을 새 파일로 다시 복원해 열어 integrity ok·FK 0·다이제스트 동일 |

## 3. SQLite 마이그레이션 (G-OPS-3, 사본에서만)

`py -3.12 -B -m food_trends --isolate-database data/ops-prod/production-source-v11.sqlite3`

- 원본 읽기 전용 백업 → 사본에 격리 표시(`application_id`) → 사본만 v0→v11(마이그레이션 1~11).
- 원래 30개 테이블의 열·행 보존(변경 테이블 0), 사본 v11·integrity ok·FK 0, 72개 테이블 21,777행.
- B4/B5: RSS 회차 95개(성공 90·실패 5) 스테이징, 출처 불명 Instagram 185건 논리 회차, 연결 97회차. YouTube `live_api` 영상 431건은 스테이징 경로가 없어 원문만 이관(기존 설계).
- 보존 정리(2026-11-08 기준): 정리 대상 0(캡션 30일·영상 29일 창 안). 만료 원문 복원 0.
- 이 사본은 **SQLite 보관·롤백 기준 파일**로 남는다(`data/ops-prod/`, Git 제외). B7 적재가 이 파일의 지문을 기록했으므로 역방향 복구의 갱신 기준선이 된다.

## 4. Oracle 운영 스키마

| 항목 | 값 |
| --- | --- |
| 스키마 | `MZ_APP`(개발용 `MZ_TEST`와 별도 계정), FREEPDB1, 기본 테이블스페이스 USERS(할당 4GB) |
| 권한 | CREATE SESSION, CREATE TABLE만(롤·DBA 없음). 계정 생성은 컨테이너 안 OS 인증 SYSDBA로 한 번만 수행 |
| 접속 정보 | `data/oracle-prod/oracle-prod.env`(Git 제외, 무작위 24자 비밀번호, 출력·기록 없음) |
| 저장소 | Podman 이름 있는 볼륨 `mz-oracle-data`(`/opt/oracle/oradata`) — 컨테이너 재시작에 영속 |
| 대상 스키마 | v10, 107개 객체, 마이그레이션 체크섬 기록(`--target-migrate oracle`) |
| 운영 접근 | `ORACLE_ENVIRONMENT=production` + `ORACLE_ALLOW_PRODUCTION=true` + `ORACLE_PRODUCTION_SCHEMA=MZ_APP` + 승인 기록 `data/oracle-prod/production-approval.json`(G-OPS-1·2·3·6 근거) — 하나라도 없으면 거부 |

## 5. 적재 결과

`py -3.12 -B -m food_trends --target-load oracle --isolated-database data/ops-prod/production-source-v11.sqlite3`(운영 환경변수)

| 항목 | 1차 | 2차(재실행) |
| --- | --- | --- |
| 적재 행 | 15,671 | 15,671(완료 배치 재사용, 새 쓰기 없음) |
| 제외·거부·묘비 | 0·0·0 | 0·0·0 |
| 대조 | `consistent`, missing·extra·changed 0, 해시·NULL 불일치 표 0 | 같음 |
| FK 오류·미해결 원문 참조·정책 위반 행·살아 있는 만료 행 | 0·0·0·0 | 0·0·0·0 |
| 사용량 원장 | 열린 창 0행, 사용량 확인 계정 3(Instagram 설정 계정·YouTube·NAVER) | 같음 |

| 대상 테이블(이관분) | 행 |
| --- | ---: |
| `source_contents`(RSS 370·YouTube 431·Instagram 185 원문) | 986 |
| `source_items`(RSS 370·Instagram 185) | 555 |
| `item_snapshots` | 840 |
| `run_items` | 1,085 |
| `quality_results` | 10,402 |
| `metric_observations`(RSS 근사 트래픽) | 900 |
| `rss_classifications` / `rss_classification_history` | 370 / 251 |
| `ingest_runs` | 97 |
| `item_query_matches`(Instagram 해시태그 검색 근거) | 185 |
| 적재 기록(`legacy_key_map` 991, `migration_batches` 29), 소유권·사용량 계정 | 1,028 |

- 자연키 중복 0, 키 형식 오류 0, B7 배치 1개 `COMPLETED`.
- B8 동등성: 같은 요청 17개 × origin 2종(`live_api`·`mock`)에서 SQLite 사본과 Oracle 응답(상태·본문·헤더)이 모두 같음(불일치 0). 범주 비교(음식 정체성·후보·추적·계획·실행·회차·항목·품질·언급·지표) 전부 같음.

## 6. 이관 범위와 제외 사유

| 요청 범위 | 원본 상태 | 결과 |
| --- | --- | --- |
| 음식·FOOD_ID·별칭·관계·승인 이력 | v0 원본에 음식 개체 테이블 자체가 없음(0건) | 이관 0건(임의 FOOD_ID 생성 없음). 운영 음식은 전환 후 Oracle에서 승인(`두쫀쿠`, [LIVE_VERIFICATION](LIVE_VERIFICATION.md)) |
| 후보 | RSS 분류 370건 | `rss_classifications` 370·이력 251 |
| RSS 항목·회차 | 회차 95·항목 370·관측 900 | B4 회차 95(+출처 불명 논리 회차 2)·항목 370·관측 900·트래픽 지표 900 |
| Instagram | 게시물 185, 해시태그 검색 근거 185, 검색 1건(7일 창 밖) | 원문·항목·검색 근거 185, 사용량 원장 0(창 종료) |
| YouTube | 영상 431(`live_api`), 검색 일치 473, 호출 38(창 밖) | 원문 431(만료 2026-11-25경). 검색 일치는 스테이징된 영상만 대상이라 0, 음식 언급 파생은 정책상 차단 |
| NAVER | 호출 메타데이터 10(창 밖), 값 없음 | 값·본문은 원래 저장하지 않음. 열린 창 사용량 0 |
| 추적 대상·계획·실행·예산 | v0에 없음 | 0건 |
| Staging·DQ·관측 | 사본에서 생성 | 위 표 |
| 제외(설계) | 수집기 지역 상태·리스·잠금, 레거시 검색 정의·매핑, 파생 집계, Shadow 캡처, SQLite 이력 등 24개 테이블 | 각 사유는 `cutover.OTHER_TABLES`(분류되지 않은 테이블 0) |

## 7. 만료 근거 묘비 (C-5 재점검)

B8 통합 보고서의 결함(B7이 이미 만료된 음식 언급 근거를 빼서 B8이 `EVIDENCE_EXPIRED` 대신 과대 집계)은 ORACLE-CUTOVER-PREP-1에서 고쳐졌다: B7이 만료된 B5 근거를 내용 없는 `EXPIRED` 묘비로 옮긴다(`test_app_oracle.LiveFixtureLoadedAfterExpiry`·`OracleLiveFixtureLoadedAfterExpiry`가 대역·MZ_TEST에서 확인, 이번 회귀에도 포함). 이번 원본에는 음식 언급이 없어 묘비 대상 0이고, 이관 뒤 승인한 `두쫀쿠`가 이관된 Instagram 게시물과 연결돼 언급 143건이 생겼다(만료 시 같은 경로로 묘비 처리).

## 8. 운영 데이터와 격리 테스트 데이터의 구분

| 데이터 | 위치 |
| --- | --- |
| 운영 데이터 | Oracle `MZ_APP`(이관분 + 실제 수집분), SQLite 보관 사본 `data/ops-prod/production-source-v11.sqlite3`, 백업 `data/ops-backup-20261107T153742Z/` |
| 테스트 데이터 | Oracle `MZ_TEST`(회귀가 매번 초기화, 운영 데이터 없음) |
| 분리 장치 | 테스트 초기화는 `*_TEST` 계정이면서 선언된 운영 스키마가 아닌 계정만 허용, 운영 접근은 `*_TEST`·테스트 계정을 거부, 테스트 라벨(`ORACLE_TEST_*`)로 운영 스키마를 열면 `oracle_test_schema_required` |
| 운영에 남긴 검증 흔적 | `ORACLE-PRODUCTION-CUTOVER-1 verification` 후보 1건(거절 상태, 음식 아님, 앱 API 미노출) — 재기동 영속성 확인용 |
