# B3 잔여 과제 · B4 공통 Ingestion/DQ · B5 음식 관측 통합

2026-10-28. 네 수집원의 데이터를 공통 Envelope·스테이징·품질 검사로 처리하고 FOOD_ID에 연결했다. B3 잔여 과제 4건을 해결했다. **검증은 격리 DB와 Mock Transport, 보존 스냅샷의 임시 복제본에서만 했다. 실제 외부 API 호출 0, 운영 DB·서버 변경 0.** Mock 성공은 실제 운영 수집 성공이 아니다.

상태 표기: IMPLEMENTED(구현), TESTED_MOCK(Mock·격리 검증), TESTED_LIVE(실제 API 검증), POLICY_BLOCKED(정책상 차단 유지), APPROVAL_REQUIRED(운영 승인 대기), NOT_IMPLEMENTED.

## 1. 시작 시 현황

| 항목 | 값 |
| --- | --- |
| Git | `main` = `origin/main` = `59376f2`, 미커밋 변경 없음, worktree 1개 |
| 기준 테스트 | 245개 재현 통과 ([baseline](baseline-tests/test-summary.json)) |
| 운영 DB | `data/trends.sqlite3`, v0(30개 테이블), WAL, integrity ok. 읽기 전용으로만 열었다 |
| 서버 | 8000(PID 50584, 스케줄러 포함)·8002(PID 51280, `--no-schedulers`). 둘 다 10-26 시작, 중지·재시작하지 않았다 |
| 코드 migration | 시작 v7 → 종료 v10 |

## 2. B3 잔여 과제

| 과제 | 결과 | 상태 |
| --- | --- | --- |
| 4.1 부분 저장 후 FAILED | 실행 상태와 독립된 `result_coverage`(COMPLETE/PARTIAL/LIMITED/NONE)와 `committed_pages`(v8). 모든 종결 경로(`_terminal`)가 결과 계약을 기록한다: committed_steps, resume_state, remote_duplicate_possible. 커밋된 페이지가 있는 실패 작업은 PARTIAL 범위와 대상 계보(execution_results)를 남기고 last_success_at은 전진하지 않는다 | TESTED_MOCK |
| 4.2 Instagram 반복 확인 | `recently_searched_hashtags`는 고유 태그를 소모하는 `ig_hashtag_search` 전에만 필요하다고 판단했다. 같은 작업에서 태그 조회를 마쳤고 계정·API 버전·정책·태그 fingerprint가 같으며 7일 이내면 재개 시 생략한다. 조회 전 재시도는 15분 이내 같은 fingerprint의 검증 결과만 재사용한다. 실패한 확인은 저장하지 않는다. 원격 고유 태그 수는 로컬 예산과 분리해 체크포인트에 기록한다 | TESTED_MOCK. recent_media가 고유 태그를 소모하지 않는다는 전제는 **Live 검증 필요** |
| 4.3 Live Controller | `live_readiness`: 스키마 버전, `B3_LIVE_ENABLED`, registry 승인, 자격 증명, 해당 출처 legacy 스케줄러 명시적 off, 소유권 이전, 계정 차단을 읽기 전용으로 검사(fail-closed). CLI `--execution-readiness`, `--execution-mode preview|live`, `--execution-automatic`(env도 필요). 독점·승인 출처에만 어댑터를 만든다. Mock은 격리 `food_demo` 전용 | IMPLEMENTED·TESTED_MOCK. 실제 Live 기동은 APPROVAL_REQUIRED |
| 4.4 actor_ref·인증 | 루프백 바인딩만 허용, 모든 요청의 Host 검사(DNS rebinding 차단), 서버 프로세스별 관리 토큰을 모든 상태 변경 POST에 요구. 토큰은 같은 출처 화면에만 전달되고 `admin.js`가 붙인다. 실패 응답은 원인을 구분하지 않고 토큰을 반환·기록하지 않는다. actor_ref는 감사 표시로만 남는다 | TESTED_MOCK |

외부 공개 서비스로 전환하려면 사용자 계정·역할 기반 권한, HTTPS, CSRF 토큰 회전, 감사 로그의 신원 연결, 요청 속도 제한이 추가로 필요하다.

## 3. B4 데이터 처리 구조

```text
기존 출처 저장소(RSS runs/items, execution receipts→youtube_videos/instagram_media, legacy 행)
  → 출처 어댑터 → A2 Source Envelope(contracts.envelope) → 정책 게이트
  → 스테이징(ingest_runs, source_items, item_snapshots, run_items) → 품질(quality_results)
NAVER: DataLab 응답 → 메모리 Metric 레코드 → B3 페이지 트랜잭션 안에서 DQ → 저장 없음
```

- 원문을 새 테이블에 복사하지 않는다. 스냅샷은 payload 해시·필드 상태·기존 저장소 참조만 가진다.
- 결정적 키로 같은 입력을 다시 처리해도 건수가 변하지 않는다.
- B3 종결 작업과 RSS 회차(실패 회차 포함)는 자동으로 스테이징된다. 실패하면 `pending_retry`로 남고 `--ingest-pending`으로 재처리한다.
- legacy 출처 불명 행은 `--ingest-legacy`로 `legacy:<source>:origin-unknown` 논리 회차(coverage UNKNOWN)에 적재된다.

## 4. B5 음식 연결 모델

| 개념 | 생성 조건 |
| --- | --- |
| ITEM_QUERY_MATCH | 계획 질의(또는 legacy 질의·태그)가 반환한 콘텐츠. 항상 SEARCH_ONLY |
| FOOD_MENTION | 승인된 정확 별칭이 정책상 분석 가능한 근거에 등장할 때. RSS 검색어·뉴스 제목(`existing_food_rules`), Instagram 캡션 해시태그(`existing_caption_hashtags`). 단어 경계 정확 일치만, 공백 변형·유사도 병합 없음 |
| METRIC_OBSERVATION | RSS 대략적 검색량 원문 표시값(콘텐츠 단위), Instagram 질의별 관측 고유 게시물 수(음식 단위). 실패 회차는 NOT_OBSERVED·NULL, 빈 성공은 OBSERVED 0 |
| 개체 변경 | `food_events`(불변) 별칭 승인·취소·이동·병합마다 한 번 재처리. 무효 언급은 INVALIDATED로 이력 보존, 유효 근거에서 새 언급 생성, 만료 근거는 재처리 불가로 집계, 영향 음식은 `aggregate_invalidations` |
| FOOD_ID 조회 | `FoodObservationService.food_view`: 출처별 고유 콘텐츠·언급·검색 근거·지표(단위·문맥 포함). 병합 음식은 대상 ID로 리다이렉트. 출처 간 합산 없음 |

## 5~6. 변경 테이블과 데이터 계약

[SCHEMA.md](SCHEMA.md): v8~v10 테이블·제약, Envelope 항목별 위치, Oracle 이관 영향.

## 7. 출처별 처리 차이

| 출처 | 스테이징 | 언급 | 지표 |
| --- | --- | --- | --- |
| Google RSS | 회차별 관측, items 원본 참조, 실패 회차는 범위 NONE | 검색어(원 검색어)와 뉴스 제목을 field_code로 구분 | 대략적 검색량 원문 문자열(정확한 수로 간주하지 않음) |
| Instagram | 작업 receipt의 게시물, 캡션 만료 시 field state `expired` | 캡션 해시태그만. 검색 태그는 SEARCH_ONLY. 캡션 만료 후 재생성 안 함 | 질의별 관측 고유 게시물 수. PARTIAL/LIMITED 범위 표시 |
| YouTube | 영상 ID 단위, 원 29일 기한 그대로, 삭제·비공개 시 DELETED | POLICY_BLOCKED(0건) | 저장 안 함(POLICY_BLOCKED). 통계는 현재 영상 스냅샷일 뿐 시계열 아님 |
| NAVER | 콘텐츠 없음, 회차 메타데이터와 DQ만 | NOT_APPLICABLE | 메모리 전용. 요청 전체(그룹·기간·timeUnit·필터)를 비교 문맥으로 해시, 데이터 없는 그룹은 NOT_PROVIDED |

## 8. 데이터 품질 결과

규칙(`dq-rules-v1`): 필수 ID, ID 형식, payload JSON, 문자열 인코딩, 시각과 시간대, 정확한 정수 문자열, 필드 상태, 소스 스키마·API 버전, 요청 메타데이터 credential 검사, 수집 범위, 지표 단위·값·기간·저장 정책. 실패 레코드는 승격하지 않고 subject 해시와 규칙 코드만 남긴다.

보존 스냅샷 복제본의 실데이터 결과([copy-b45-processing.json](copy-b45-processing.json)): FAIL 0, WARN 62(수집 범위 7: 실패 RSS 5와 legacy 2 / 필드 결측 상태 55), 나머지 PASS·NOT_APPLICABLE.

## 9. 중복 제거와 개체 매핑

Mock 전체 경로([acceptance.json](acceptance.json)) 기준:
- YouTube: 검색 근거 3, 고유 영상 1, 언급 0.
- Instagram: 검색 근거 2, 고유 게시물 1, 언급 1.
- RSS: 한 항목의 언급 3(검색어 1, 뉴스 제목 안의 두 별칭)을 고유 콘텐츠 1로 집계.
- 재전달·재처리 후 HTTP 0, 행 변화 0.
- Worker 중단 주입 후 재개해도 추가 HTTP 0.

## 10. 보존·파생 정책 준수

NAVER 응답·ratio의 DB 저장 0(덤프 검사, DB CHECK). YouTube 음식 언급·지표 0. Instagram 언급은 캡션 보존 기한과 함께 만료된다. 스테이징·연결은 출처 행이 만료·삭제되면 EXPIRED·DELETED로 전파되며 재수집 없이 되살아나지 않는다. 새 derived operation을 추가하지 않았다.

## 11. 테스트

| 단계 | 결과 |
| --- | --- |
| 기준 | 245 통과 |
| B3 보완 후 | 256 통과(+11) |
| B4 후 | 270 통과(+14) |
| B5 후(최종) | 280 통과(+10), 실패·오류·skip 0, 외부 소켓 0, 운영 DB 접근 차단. [final](final-tests/test-summary.json) |
| 브라우저(Chrome headless, 관리 토큰 적용) | A4/A5 음식, B1 추적, B2 계획, B3 실행 모두 통과, JS 오류 0, 외부 요청 0. [browser](browser/) |
| 대조 실험 | Instagram 판정을 "항상 확인"으로 바꾸면 재개·재사용 테스트 2개가 실패해 테스트가 실제 경로를 검증함을 확인 |

최종 실행 소요 1,285초는 세션 초반에 남은 전체 파일시스템 검색(`find`) 프로세스의 디스크 경합 때문이다. 해당 프로세스를 종료한 뒤 정상 속도로 돌아왔다. 같은 코드의 직전 실행은 108초였다. 결과에는 영향이 없다.

요청서 8.1~8.3의 34개 항목은 `tests/test_execution_handoff.py`, `tests/test_ingestion.py`, `tests/test_food_observations.py`에, 8.4 전체 경로는 `tests/b45_scenario.py`에 있다.

## 12. 격리 DB 마이그레이션

보존 스냅샷(SHA-256 `09e323f9…`, 전후 동일)의 임시 복제본에서 0→10을 적용했다. 원래 30개 테이블의 열·행 해시 일치, FK 오류 0, integrity ok, legacy 승격 0, 재적용 0건([copy-migration.json](copy-migration.json)). 같은 복제본에서 스테이징·연결을 두 번 실행해 원래 테이블 불변과 멱등을 확인했다(콘텐츠 711, 검색 근거 400, RSS 지표 820, 2회차 변화 0).

## 13~14. 운영 영향과 외부 호출

운영 DB는 v0이며 신규 테이블이 없다. integrity ok, 8000/8002는 같은 PID로 유지된다([production-final.json](production-final.json)). 행 수 증가는 기존 Writer의 정상 수집이다. 실제 외부 API 호출 0. 테스트 러너가 외부 소켓을 차단했고, 시나리오는 소켓 연결 시 예외를 일으킨다.

## 15. Git

| 커밋 | 내용 |
| --- | --- |
| `ea08fe9` | fix: resolve B3 execution handoff and safety gaps |
| `051e310` | feat: implement common ingestion staging and data quality |
| `594f47b` | feat: unify food entity mentions and metric observations |
| 이 보고서 커밋 | docs: document B4 B5 integration and deployment gates |

모두 `main`에 fast-forward Push했다. force push·reset은 쓰지 않았다.

## 16. 운영 승인 게이트

[DEPLOYMENT_GATES.md](DEPLOYMENT_GATES.md). G-OPS-1 백업, G-OPS-2 Writer 정지, G-OPS-3 `--migrate`, G-OPS-4 소유권 이전, G-OPS-5 Live 검증. 각 항목에 실행 작업·대상·영향·한도·복구·선행 조건·승인 사항을 적었다. **모두 APPROVAL_REQUIRED이며 하나도 실행하지 않았다.**

## 17. 미완료·미검증·차단

| 항목 | 상태 |
| --- | --- |
| 실제 Live 호출(전 출처), 공급자 quota·권한 확인 | APPROVAL_REQUIRED, TESTED_LIVE 없음 |
| Instagram recent_media의 고유 태그 미소모 전제 | Live 검증 필요 |
| YouTube 음식 언급·지표, NAVER 지수 저장 | POLICY_BLOCKED |
| legacy 수집기가 남긴 live_api 출처 IG/YT 행의 스테이징 | 소유권 이전 후 B3 경로로 처리. 이전 행 이관은 B7 범위(NOT_IMPLEMENTED) |
| 출처 간 동일·유사 콘텐츠 병합 | 설계상 하지 않음 |
| 일 집계·유행 지수·대시보드(B8), Oracle Repository(B6), 이관(B7) | NOT_IMPLEMENTED(범위 외) |
| 관리 토큰의 다중 사용자 인증 | 1인 로컬 범위만. 외부 공개 시 보완 필요(2장) |

## 18. B6~B8 선행 조건

[B6_B8_HANDOFF.md](B6_B8_HANDOFF.md).

## 재실행

```powershell
py -3.12 -B tests/run_offline.py --output reports/development-b4-b5-2026-10-28/final-tests
py -3.12 -B tests/b45_scenario.py
py -3.12 -B tests/verify_preserved_copy.py data/b3-preservation-20261027T082023Z/snapshot.sqlite3 --output <out>.json
py -3.12 -B tests/verify_b45_copy.py data/b3-preservation-20261027T082023Z/snapshot.sqlite3 --output <out>.json
py -3.12 -B tests/execution_browser_check.py
```
