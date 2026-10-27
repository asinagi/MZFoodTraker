# YouTube Data API v3 통합

검토일: 2026-10-26 KST. Python 표준 라이브러리와 기존 SQLite를 사용하며 유료 서비스·외부 LLM·별도 DBMS를 추가하지 않았습니다.

## 기존 프로젝트 감사

| 영역 | 확인 및 재사용 |
|---|---|
| Google RSS | collector HTTP/XML 파서 → CollectionService → Storage.finish → 후보 분류. 기본 30분 스레드 수집, 원본/관측/분류 이력 보존 |
| NAVER DataLab | FOOD 후보, 명시적 keyword_group 별칭, 요청 예약/일일 예산/중복 제어. 상대 검색 지수는 YouTube 수치와 합산하지 않음 |
| FOOD 분류 | classifier의 정규화·검색어 중심 근거 규칙, FOOD/REVIEW/NON_FOOD 유지. YouTube 제목을 RSS 뉴스 근거로 주입하지 않음 |
| 후보 ID | candidates.item_id는 RSS 항목을 참조. YouTube 영상을 RSS 항목으로 위장하지 않고 별칭 그룹의 canonical ID와 기존 후보의 대응 관계를 별도 저장 |
| 스케줄/오류 | 기존 서비스별 thread/stop_event 재사용. YouTube는 SQLite lease, 타임아웃, 재시도, 체크포인트 추가 |
| DB | Storage.connect 트랜잭션/외래키, additive CREATE TABLE IF NOT EXISTS. Oracle 드라이버·연결·스키마 미구현 |
| 실행/보안 | dotenv 로더 재사용. .env와 .env.*는 gitignore 제외, 예제만 허용. 현재 .git 디렉터리가 없어 추적 이력 확인은 불가 |
| 실제 DB | 검증 시 RSS 261개, FOOD 0개, REVIEW 1개. Mock 후보를 실제 DB에 추가하지 않음 |

## 데이터 처리

설정 발굴 검색어 / 기존 FOOD 명칭·별칭 → search.list → ID 검사 → videos.list(50개씩) → 공개 상태 확인 → 영상 upsert → 검색어별 매칭 저장.

검색 파라미터는 part=snippet,type=video,regionCode=KR,relevanceLanguage=ko,order=date,maxResults=50,publishedAfter,publishedBefore입니다. 검색 snippet의 복사본은 따로 저장하지 않고 상세 응답에서 최신 제목·설명·게시 시각·채널·영상/썸네일 URL·조회수/좋아요/댓글수·ISO 8601 길이·공개/게시/임베드 상태를 저장합니다. 누락 통계는 NULL입니다. 큰 정수 손실을 막기 위해 제공 통계는 문자열로 보존합니다. 댓글 본문은 요청하지 않습니다.

KR/ko는 한국 제작 영상의 보장이 아닙니다. 검색 표본은 전체 발화량이 아니며 음식 여부가 확인되지 않은 영상도 포함될 수 있습니다.

| 테이블 | 용도 |
|---|---|
| youtube_foods | 기존 FOOD 명시적 별칭 그룹별 canonical ID |
| youtube_candidate_foods | 기존 candidates.item_id ↔ canonical ID |
| youtube_queries | 발굴/추적 검색어, FOOD ID, 검색 구간·페이지 토큰·최근 시도 |
| youtube_videos | video_id PK, 최신 API 필드, 최초 수집·최근 갱신·만료 시각 |
| youtube_matches | 검색어↔영상 다대다, 매칭 시각. 영상 삭제 cascade |
| youtube_calls | PT 날짜, API 종류, 검색어 ID, 시도/완료 시각, HTTP/성공 상태. 키·URL·응답 본문 없음 |
| youtube_state | lease, 다음 실행, quota 중단일, cooldown, 최근 상태 |
| youtube_runs | 실행 ID·경로(자동/수동/갱신/검증), 실제/Mock 출처, HTTP·검색·저장·중복·FOOD 연결 건수 |
| youtube_video_provenance | 영상의 마지막 상세 API 갱신 출처·시각·실행 ID. 기존 행의 미기록 출처를 임의로 확정하지 않음 |

youtube_calls에는 origin, run_id, result_count를 additive migration으로 추가했습니다. 이전 기록은 legacy_unknown으로 유지합니다. HTTP 200을 받은 호출과 HTTP 응답을 못 받은 로컬 시도는 구분합니다. 주입된 테스트 transport는 mock으로 기록하며, 기본 고정 Google HTTPS transport만 live_api로 기록합니다. 영상 목록은 최근 갱신순입니다. 갱신할 영상이 없어 HTTP 요청을 보내지 않았다면 no_request이며 실제 연결 성공으로 표시하지 않습니다.

두쫀쿠 / 두바이 쫀득 쿠키 / 두바이쫀득쿠키는 기존 NAVER 명시적 별칭을 재사용합니다. 짭쫀쿠는 자동 병합하지 않습니다. 복수 검색 관계는 보존하되 search_match_unverified로 표시합니다. **앱의 검색 조건 이력이며 영상 내용의 음식 분류가 아닙니다.**

## 예산·스케줄·장애

- 공식 기본 search 별도 100회/일, 기타 API 합산 10,000유닛/일, videos.list 1유닛/호출. PT 자정 초기화. tzdata가 없는 Windows도 Pacific DST 계산 지원.
- 기본 6시간, 검색어 최대 8개/회차, 1페이지. 후보가 있으면 발굴 6개+추적 2개, 없으면 발굴 6개로 평상시 24~32회/일. 자체 search 40회, 상세 1,000회 상한으로 여유 확보. 설정 변경 시 호출량 변동.
- 매 페이지/재시도를 별도 예약. 잔여량은 앱의 기록 기반 자체 예산이며 Cloud 전역 사용량이 아님.
- 모든 작업이 SQLite lease 공유. 수동/자동/갱신/삭제 간 프로세스 동시 실행 방지. 요청 전 소유 확인·180초 연장, 예외 시 해제.
- 페이지마다 고정 상한/토큰을 저장해 재시작 후 이어감. 완료 시 상한-10분 watermark. 최초 최근 7일, 토큰 7일 뒤 폐기 후 기존 하한에서 재검색/ID 중복 제거. 검색 색인 지연·API 검색 한계로 완전 수집 보장은 없음.
- 네트워크/5xx 최대 2회 지수 백오프(1초,2초). 429 60초 대기. 인증/권한 오류 6시간 대기. quota 오류는 해당 bucket을 PT 다음 날까지 중단. 기본 timeout 20초.
- 잘못된 JSON/필드 실패 시 watermark 전진 안 함. 상태 조회/화면 새로고침은 외부 호출 없음. 저장된 다음 실행 시각으로 재시작 직후 재수집 방지.

## 정책·보존

공식 근거: [개발자 정책 III.E.2, III.E.4, III.L](https://developers.google.com/youtube/terms/developer-policies), [추가 지표·저장 승인 정책](https://developers.google.com/youtube/terms/derived-metrics-policy), [현행 quota](https://developers.google.com/youtube/data/determine_quota_cost), [검색](https://developers.google.com/youtube/data/reference/rest/v3/search/list), [영상](https://developers.google.com/youtube/data/reference/rest/v3/videos/list).

공개 API 데이터와 통계에 **29일 TTL**을 적용하고 최신 스냅샷만 보관합니다. 정상 수집 시 하루 지난 영상 최대 500개씩 갱신합니다. 누락·비공개·삭제·거부 영상은 매칭과 함께 삭제합니다. 갱신 실패 시 TTL을 연장하지 않습니다. 검색 매칭의 만료는 메타데이터 갱신과 별개입니다.

키 없음/자동 수집 꺼짐/--no-schedulers에서도 60초 정리 스레드가 작동합니다. 초기화·조회·수집에서도 만료를 정리하며 UI에서도 만료 데이터는 제거합니다. 브라우저 영구 저장소/응답 캐시를 사용하지 않습니다. 앱 종료 중에는 정리할 수 없으므로 재시작 시 먼저 정리합니다. 별도 DB 백업을 만들면 운영자도 같은 기한으로 삭제해야 합니다. 기존 RSS export에 YouTube 데이터는 포함하지 않습니다. 응답 원본·통계 이력·실영상 화면 캡처를 보고서에 영구 복사하지 않습니다.

기본 정책의 파생 데이터 제한 및 추가 정책의 명시적 승인 요건에 따라, 여러 소유자의 조회수/좋아요 집계·유행 점수·제목에서 신규 음식명/별칭 추출·콘텐츠 분류는 비활성화했습니다. 환경변수 하나로 이를 활성화할 수 없으며, 향후 승인 범위 확인 후 별도 분석 모듈과 검증이 필요합니다. 현재 후보 생성은 기존 RSS 경로만 사용합니다.

## 사용·설정

기존 .env의 YOUTUBE_DATA_API_V3_KEY를 지원하고 YOUTUBE_API_KEY가 있으면 우선 사용합니다. 키 값/파일은 변경하지 않았습니다. 설정은 .env.example 참조. API 키는 공식 고정 HTTPS 주소에 인증 헤더로만 전송하고 리디렉션을 거부합니다. URL/로그/에러/보고서에 키를 넣지 않습니다.

```powershell
# 외부 호출 없음
py -3.12 -m food_trends --youtube-status
# 실제 API 소량 검증 (외부 네트워크 접근 허용 필요)
py -3.12 -m food_trends --youtube-verify
py -3.12 -m food_trends --youtube-collect
py -3.12 -m food_trends --youtube-refresh
py -3.12 -m food_trends --youtube-delete
# 외부 자동 수집 없이 UI와 보존 정리
$env:PORT='8002'
py -3.12 -m food_trends --no-schedulers
# 정상 자동 수집 서버
.\run.ps1
```

verify는 추적 대상이 있으면 추적 1개, 없으면 발굴 1개 검색어를 최근 7일·maxResults=5로 검색하고 상세 배치 1회로 확인합니다. 빈 검색이면 상세 ID가 없어 검증 미완료로 처리합니다. 평상시 페이지 체크포인트·최근 시도·다음 수집 시각은 변경하지 않습니다. 재시도도 예산에 포함됩니다. 검증 JSON에는 HTTP 코드·건수·기존 RSS/NAVER 테이블 지문만 저장하고 API 키·제목·영상 ID·통계 원문을 넣지 않습니다. status 출력에는 캐시 영상이 포함되므로 별도 영구 파일로 저장하지 마세요. 이 JSON과 Markdown은 검증 시점의 기록이며 현재 UI 상태는 /api/youtube와 youtube_runs가 기준입니다.

GET /api/youtube, POST /api/youtube/collect, /refresh, /delete. POST는 로컬 Host·동일 Origin·JSON 빈 객체를 검사합니다. 기존 화면에 연결·마지막 수집·요청 예산·최근 최대 25개 영상·FOOD 검색 관계를 표시합니다. 로컬 삭제는 YouTube 원본을 삭제하지 않습니다.

## 변경 파일

신규: food_trends/youtube.py, youtube_storage.py, youtube_service.py, youtube_verify.py; static/youtube.js; tests/test_youtube.py, youtube_browser_check.py; YOUTUBE_INTEGRATION.md, YOUTUBE_VERIFICATION.md.

수정: food_trends/config.py, __main__.py, api.py; static/index.html, app.js; .env.example; run.ps1; README.md. run.ps1은 정상 설치 Python 런처를 conda보다 먼저 사용합니다. 이 작업에서 기존 RSS/분류기/NAVER/Instagram 수집 코드는 수정하지 않았습니다.
