# 실제 API 소량 검증 — 운영 Oracle `MZ_APP`

실행 2026-11-08 08:26 KST(2026-11-07 23:26Z), `scripts/ops/live_pilot.py`. 근거: [live-pilot.json](live-pilot.json)(요청 수·상태·코드만, Instagram 계정 ID는 가림), [live-storage-verification.json](live-storage-verification.json). 응답 본문·캡션·토큰은 어디에도 기록하지 않았다.

## 승인과 상한

- 승인: 수집원 4개 모두 사용자(프로젝트 소유자)가 이 세션에서 직접 승인했다. registry(`data/ops-prod/registry.json`, Git 제외)의 `approved_by`는 사용자, `evidence`는 2026-10-26 실제 검증 기록 + 사용자의 제공자 약관 확인 + 같은 자격 증명을 다른 프로그램이 쓰지 않음. 검증용 음식 `두쫀쿠`(별칭 없음)의 운영 승인도 사용자가 승인했다.
- 상한: 전체 9회(RSS 1·Instagram 5·YouTube 2·NAVER 1). 예산 설정(`INSTAGRAM_MAX_PAGES=1`, `INSTAGRAM_SEARCH_SOFT_LIMIT=1`, YouTube 일일 search·videos 각 1, NAVER 일일 1)에 더해, 기존 클라이언트 아래의 계수 opener가 수집원별 상한을 넘는 요청을 **보내기 전에** 거부하도록 했다(거부 0건).
- 순서: 이관 사본에서 사용량 다시 가져오기 → 4개 수집원 Writer 이전(EXCLUSIVE) → Live 준비 점검 `ready` → 관리 API로 1회 실행 → B8 확인. 같은 하네스를 먼저 MZ_TEST에서 가짜 전송으로 건식 실행해 통과시킨 뒤 실행했다.

## 수집원별 결과

| 수집원 | 실제 요청 | HTTP | 작업 결과 | 대상 기록 | 저장 | 정책 |
| --- | ---: | --- | --- | --- | --- | --- |
| Google Trends RSS | 1 | 200 ×1 | 회차 성공, 항목 10·새 항목 10 | RSS 회차 1(`live_api`), B4 회차 `COMPLETE` | 새 항목 10. **음식 후보 0**(그날 피드에 음식 없음 — 사실대로 기록, 후보를 지어내지 않음) | 분류 규칙 그대로, 수집 성공과 후보 생성 구분 |
| Instagram Graph | 3(최근 검색 확인 1·해시태그 조회 1·최근 게시물 1쪽) | 200 ×3 | `LIMITED`(1쪽 상한, 다음 쪽 있음) | 실행 호출 3 SUCCEEDED·HTTP 200, B4 `LIMITED` | 새 게시물 40, 관측 `instagram.observed_posts` 1건(`OBSERVED`) | 7일 고유 태그 1, 캡션 30일 창, 원래 해시태그만 파생 |
| YouTube Data API | 2(search 1·videos 1) | 200 ×2 | `LIMITED`(1쪽 상한) | 실행 호출 2 SUCCEEDED, B4 `LIMITED` | 영상 50(29일 만료) | 음식 언급 파생 0(정책 차단 유지), 검색 일치는 근거로만 |
| NAVER DataLab | 1 | 200 ×1 | `SUCCEEDED` | 실행 호출 1 SUCCEEDED, B4 `COMPLETE` | **값·본문 저장 0**(정책: 요청 메타데이터만) — 실패가 아님 | 응답 원문·상대 지수 미저장 |
| 합계 | **7 / 상한 9** | 200 ×7 | | 실행 호출 6 + RSS 회차 1 = 실제 요청 7과 일치 | | |

- 품질 검사(이번 회차분): PASS 656, WARN 38, NOT_APPLICABLE 10.
- Oracle 무결성: FK 오류 0, 미해결 원문 참조 0, 정책 위반 행 0, 살아 있는 만료 행 0.
- 수집 경로의 SQLite 연결: **0건**(하네스가 Live 라운드 전체에서 sqlite3 연결을 셈).
- `두쫀쿠` 승인과 동시에 별칭 재처리가 이관된 Instagram 게시물도 연결해 활성 언급 143건(이관분 + 새 수집분).

## 관리 API와 B8에서 본 결과

| 확인 | 결과 |
| --- | --- |
| 관리 API `GET /api/execution/jobs/{id}` 호출 기록 | Instagram 3·YouTube 2·NAVER 1, 모두 SUCCEEDED/200 |
| 관리 API `GET /api/status` | 마지막 RSS 회차 origin `live_api`, 저장소 `oracle_production` |
| B8 `GET /api/v1/foods` | 승인 음식 `두쫀쿠` 1개(`FOOD_3007c29a43654e0fb9571c2a2c8c1c86`) |
| B8 `GET /api/v1/foods/{id}/timeline?period=7d&end_date=2026-11-08&source=instagram_graph` | 2026-11-08 값 28, `OBSERVED`, coverage `LIMITED`(1쪽 수집). 이전 날은 `NOT_OBSERVED`(0이 아님) |
| B8 기본 기간 | 계약대로 어제(KST)까지 — 당일 관측은 `end_date`를 오늘로 줄 때 표시 |
| B8 `GET /api/v1/sources/status` | RSS·Instagram 마지막 성공 2026-11-07T23:26Z |
| OpenAPI v1 검증 | 확인한 6개 엔드포인트 응답 모두 200, 계약 위반 0 |

## 사용량

| 수집원 | 이번 사용 | 로컬 원장(운영 Oracle) | 제공자 측 |
| --- | --- | --- | --- |
| Instagram | 새 고유 태그 1(7일 창, 2026-11-15까지) | 런타임 단독 Writer(EXCLUSIVE) 기록 | 실행 직전 `recently_searched_hashtags`로 확인(1쪽), 그 밖은 **UNKNOWN** |
| YouTube | search 1·videos 1(101 units, 태평양 시간 하루) | 같음 | **UNKNOWN**(Google Cloud 콘솔에서만 확인 가능) |
| NAVER | 1회(KST 하루) | 같음 | **UNKNOWN** |
| RSS | 1회 | 예산 없음 | 해당 없음 |

`PROVIDER_CONFIRMED`는 만들지 않았다.

## 검증 후 상태

- 4개 수집원의 Writer는 **Oracle 런타임(`MZ_APP`)에 남아 있다**(운영 SoR). 기존 SQLite 수집기는 운영 DB가 v0라 시작 자체가 불가능해 이중 Writer가 생길 수 없다.
- 지속 수집: **CONFIGURED_NOT_ENABLED**. RSS 30분 예약과 B3 자동 실행은 구현돼 있지만(`--execution-mode live`에서 `--no-schedulers` 없이, `--execution-automatic` + `B3_AUTOMATIC_ENABLED=true`), 반복 호출은 최초 소량 검증과 별개의 지속 사용 권한·예산 승인이 필요해 켜지 않았다. 운영 기본 실행(`start-backend.ps1`)은 수집기 없는 관리·관측 모드다.
- 오류 처리: 이번에는 401·403·429가 없었다. 발생 시 계정 차단·재시도 대기 경로는 MZ_TEST 회귀가 검증한다.
