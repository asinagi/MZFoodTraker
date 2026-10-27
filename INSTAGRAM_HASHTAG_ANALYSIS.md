# Instagram 해시태그 추이·공동 출현 분석 검증

2026-10-26 KST 기준. 메인 화면에서 추적 해시태그의 요약, 관측 추이, 공동 출현 순위와 관계 표를 확인할 수 있도록 변경했다. 기존 인증·수집·스케줄러와 Google Trends RSS 기능을 유지했다. 기존 게시물 67건은 모두 보존했고 실제 추가 수집 후 총 70건이다.

## 실제 수집 결과

실제 Graph API의 `#두쫀쿠` 최근 게시물 한 페이지(50건)를 조회했다. 신규 3건, 중복 47건이었다. 검증 수집은 최대 한 페이지로 제한했으며 다음 페이지가 있어 회차 상태는 `limited`다. 전체 Instagram 발화량이나 전수 수집을 뜻하지 않는다.

| 항목 | 결과 |
| --- | ---: |
| 누적 고유 API 관측 게시물 | 70 |
| 기존 67건 보존 | 67 |
| 선택 기간 공동 출현율 분모 | 70 |
| 캡션이 있는 고유 게시물 | 69 |
| 캡션 누락 고유 게시물 | 1 |
| 해시태그 추출 가능 고유 게시물 | 50 |
| 캡션은 있지만 태그가 없는 게시물 | 19 |
| 발견한 공동 출현 태그 | 164 |
| 추적 대상으로 등록된 해시태그 | 1 |
| 이번 수집에서 자동 생성한 분석 관측 | 50 |

실제 상위 공동 출현 태그(7일, 분모 70건):

| 태그 | 고유 게시물 수 | 공동 출현율 |
| --- | ---: | ---: |
| #두바이쫀득쿠키 | 11 | 15.71% |
| #카다이프 | 5 | 7.14% |
| #버터떡 | 4 | 5.71% |
| #피스타치오 | 4 | 5.71% |
| #협찬 | 4 | 5.71% |
| #홈카페 | 3 | 4.29% |
| #aivideo | 2 | 2.86% |
| #fyp | 2 | 2.86% |
| #두바이여수 | 2 | 2.86% |
| #두바이초콜릿 | 2 | 2.86% |

집계 원본: `data/instagram-hashtags-verification.json`. 이는 검증 시점의 스냅샷이며 이후 수집에 따라 달라진다.

## 추출·집계 기준

- 한글·영문·숫자·결합문자·밑줄 등 Unicode 문자와 전각 `＃`를 처리한다. URL의 fragment는 제외한다. NFC·공백 정리에는 기존 정규화 함수를 사용하며 영문 대소문자를 통합한다. 원본 표기 목록과 정규화 태그를 구분한다.
- 한 게시물의 중복 태그와 API 재수집 게시물을 중복 집계하지 않는다. `두쫀쿠`와 `두바이쫀득쿠키`처럼 다른 태그는 별도로 유지한다. 음식 검색용 별칭 그룹을 태그 식별자로 사용하지 않는다.
- 신규 관측량은 기준 태그별 최초 관측 시각을 KST 시간·일 단위로 집계한다. 게시 시각, 최초·최근 관측 시각은 기존 DB에 유지한다. 재수집은 신규량에 더하지 않는다.
- 공동 출현율 = 해당 기간 기준 태그 API에서 관측한 고유 게시물 중 공동 태그가 확인된 게시물 / 해당 기간 API 관측 고유 게시물 전체. 캡션 누락도 분모에 포함한다. 신규 게시물만을 분모로 사용하지 않는다.
- 일별 공동 출현은 각 날짜의 실제 관측을 기준으로 계산한다. 같은 게시물이 다른 날짜에 다시 관측되면 각 날짜에는 포함되지만 전체 기간에는 한 번만 포함된다. 일별 합계와 전체 기간 고유 수는 다를 수 있다.
- 캡션 누락, 빈 캡션, 태그 없는 캡션, 추출 성공을 구분한다. 나중에 캡션이 누락된 응답을 받았다고 이전에 저장된 태그를 그 회차에 재확인한 것으로 처리하지 않는다. 캡션 변경 시 과거 관측 스냅샷은 보존한다.
- 미수집·실패한 빈 구간은 그래프의 미관측 구간으로 남긴다. 성공한 수집에서 신규량이 0인 구간만 실제 0으로 표시한다. 비교 기간이 부족하거나 이전 값이 0이면 증가율은 계산 불가로 표시한다.
- 기존 67건은 저장된 캡션으로 재분석 가능했다(캡션 누락 1건 제외). 과거 캡션 버전은 없으므로 기존 태그 관측은 각 게시물의 마지막 관측 시각에 한 번만 생성하고 `legacy_snapshot`으로 기록했다. 과거 날짜에 태그가 있었다고 추정해 채우지 않는다.

## 저장·개인정보 처리

기존 DB를 유지하고 다음 테이블만 추가했다: `instagram_caption_analysis`, `instagram_caption_tags`, `instagram_analysis_observations`, `instagram_observation_tags`. `instagram_media`에는 `caption_captured_at`, `caption_purged_at`을 추가했다. 기존 게시물·기준 태그 연결·게시 시각·관측 시각·수집 회차를 재사용한다.

원본 캡션은 기본 30일만 보관한다. `INSTAGRAM_CAPTION_RETENTION_DAYS`로 1~365일을 지정할 수 있다. 신규 또는 변경된 캡션을 확보한 시점부터 계산하며 동일 캡션의 반복 수집으로 기한을 연장하지 않는다. 수집 후, 로컬 재분석, 실행 중 시간별 유지 관리에서 만료 원문만 비운다. 추출 태그와 관측 이력은 보존한다. 앱이 중지되어 있으면 다음 실행 시 정리한다. 만료 후 원문 자체의 재추출은 불가능하지만 기존 태그 분석은 유지된다.

기본 상태 API와 화면에는 게시물 캡션을 전송하지 않는다. `GET /api/instagram/media?hashtag_id=1`은 명시적인 디버깅용이며 캡션을 제외한 원본 링크·식별자·시각만 제공한다. 작성자 프로필 등 추가 개인정보를 조회하지 않는다.

## 화면·API

- 추적 대상 요약: 누적 관측, 최근 24시간 신규량, 최초 관측일, 최근 수집, 연결·수집 상태, 비교 가능한 경우 증가율.
- 그래프: 7일 / 14일 / 30일, 시간별 / 일별. 표시는 **API 관측 게시물 수**.
- 순위·관계 표: 기준 태그 → 공동 태그, 고유 게시물 수와 비율, 일별 상세, 원본 표기, 최초·최근 관계 관측 시각. 관계 강도는 공동 출현 게시물 수다.
- 캡션 누락과 태그 추출 가능 수를 함께 표시한다. 디버깅 원본 링크는 접힌 상세에만 둔다.
- `GET /api/instagram?days=7&hashtag_id=1`은 `analysis`를 포함한다. `days`는 7/14/30이다. 기존 수집·추적 API는 유지한다.

관련 태그의 API 검색이나 자동 추적 등록을 수행하지 않는다. 기존 7일간 30개 고유 태그 예산 관리, 보수적인 신규 탐색 제한, 요청·페이징 상한, 부분 수집 표시를 유지한다.

## 토큰 관리 검증

현재 `.env`의 토큰은 **2026-10-26 16:00 KST** 만료로 확인했다. 저장된 앱 자격증명을 이용한 실제 장기 USER 토큰 교환에 성공했다. 교환 결과를 다시 검사하여 앱 일치, USER 유형, 필요한 권한과 만료일을 확인했다. 새 만료일은 **2026-12-25 09:55:03 KST**다.

- 새 토큰: `data/instagram-long-lived-validated.secret` (내용은 출력하지 않음)
- 비밀값 없는 검증 결과: `data/instagram-long-lived-validated.metadata.json`
- **후속 적용 완료:** 사용자 승인 후 `.env`의 토큰 값만 교체하고 재시작·실연동을 검증했다. 적용 후 최신 수치는 72건·185개다. [토큰 적용 결과](INSTAGRAM_TOKEN_APPLICATION.md)를 참고한다.

교환은 명시적 관리 명령에서만 수행하며 기존 파일 덮어쓰기와 자동 적용을 허용하지 않는다. 검사·교환의 비밀값은 POST 본문으로 전송한다. 유효하지 않은 토큰, 잘못된 앱·권한·유형, 이미 장기임이 확인된 토큰 등은 보류한다. 발급 시각이 제공되지 않아 수명을 확정할 수 없더라도 유효한 만료 예정 USER 토큰이면 명시적 교환을 Meta에 요청할 수 있다. 교환 토큰을 별도로 검사하고 검증 실패 시 새 비밀 파일을 제거한다. 만료 안내와 인증 실패 시 자동 수집 차단은 유지한다. 실제 적용에는 요청서 7항에 따른 사용자 승인이 필요하다.

## 실행·재현

```powershell
# 앱 실행: http://127.0.0.1:8000
powershell -ExecutionPolicy Bypass -File .\run.ps1
# 로컬 캡션 재분석: API 요청 0회
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-reanalyze
# 상태 / 수동 수집
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-status
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-collect --hashtag-id 1
# 토큰 상태 확인
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-token-check
# 새 파일에 교환 결과 저장. 기존 파일 경로 사용 금지. 자동 적용 없음.
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-exchange-token --token-output data/instagram-long-lived-next.secret
# 회귀 테스트 / JS 문법
& 'C:\Users\gjgjs\anaconda3\python.exe' -m unittest discover -s tests -v
node --check static/instagram.js
```

브라우저 검증 스크립트는 `tests/instagram_analysis_browser_check.py`다. `websocket-client`가 설치된 QA Python, CDP 9224 Chrome, 실제 DB를 사용하는 별도 검증 서버가 필요하다. 기본 대상은 `http://127.0.0.1:8001`이며 `BROWSER_APP_URL`로 변경할 수 있다. 검증 서버는 `INSTAGRAM_MAX_PAGES=1`을 프로세스 환경변수로 지정해 실행한다. 이 스크립트는 모의 검증 후 **실제 수동 수집 1회**를 실행하므로 실제 API 요청을 원할 때만 실행한다.

## 검증 결과·변경 파일

전체 자동 테스트 **90개 통과**: Instagram 기존·신규 테스트, Google Trends RSS 및 현재 저장소의 다른 수집원 테스트 포함. JS 문법 검사 통과. 브라우저에서 7/14/30일, 시간/일 집계, 중복 방지, 2/3=66.67% 계산, 캡션 비노출, 관련 태그 추가 검색 없음, 작업 중 버튼 상태, 로컬 재처리 API 호출 없음 등을 확인했다.

실제 브라우저 수집 이후 회차의 분석 관측 50건이 자동 저장되었고, 독립 SQL 집계·API·화면의 고유 수와 공동 출현율이 일치했다. Google Trends의 실제 성공 회차 10개 항목도 유지됐다. 데스크톱·모바일 화면과 브라우저 JS 오류 없음도 확인했다.

| 파일 | 변경 내용 |
| --- | --- |
| `food_trends/instagram_analysis.py` | 추출·보관 기간·재분석·관측 이력·공동 출현 집계 |
| `food_trends/instagram_storage.py` | 기존 수집 트랜잭션과 분석 연결·상태 요약 |
| `food_trends/instagram_service.py` | 분석 상태 응답·토큰 후보 검사·로컬 보관 기간 유지 관리 |
| `food_trends/instagram_graph.py` | OAuth 교환 form POST |
| `food_trends/instagram_tokens.py` | 안전한 명시적 교환·별도 검증·파일 저장 |
| `food_trends/config.py`, `.env.example` | 원본 캡션 보관 기간 설정 |
| `food_trends/__main__.py` | 로컬 재분석·교환 관리 명령 |
| `food_trends/api.py` | 캡션 제외 디버깅 API |
| `static/instagram.js`, `static/style.css`, `static/index.html` | 해시태그 중심 화면·반응형 표·배치 |
| `tests/test_instagram.py`, `tests/test_instagram_analysis.py` | 회귀·집계·중복·누락·만료·토큰 검증 |
| `tests/instagram_analysis_browser_check.py`, `tests/instagram_browser_check.py` | 실제·모의 브라우저 검증 및 스크린샷 도우미 |
| `README.md`, `INSTAGRAM_HASHTAG_ANALYSIS.md` | 사용법·검증 결과 |

스크린샷: `data/instagram-hashtags-desktop.png`, `data/instagram-hashtags-mobile.png`, `data/instagram-hashtags-ranking-mobile.png`.

과거 날짜의 실제 일별 추이는 아직 충분히 축적되지 않았다. 여러 날짜·변경·누락·재관측의 집계는 테스트로 검증했지만, 실제 장기간 추이는 앞으로 저장되는 관측에 의존한다. API가 반환하지 않은 게시물이나 과거 캡션은 복원하지 않는다.
