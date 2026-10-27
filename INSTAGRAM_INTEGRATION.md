# Instagram Graph API 통합 보고서

검증일: 2026-10-26 (KST). 초기 구현 이후 유효 토큰으로 실제 Meta 연결·두쫀쿠 게시물 67건 저장·화면 및 재수집 검증을 완료했습니다. 이 문서의 초기 검증 기록보다 [최신 실연동 결과](INSTAGRAM_LIVE_VERIFICATION.md)를 우선 참고하세요.

## 1. 변경 파일

| 파일 | 역할 |
| --- | --- |
| `food_trends/config.py` | .env 로딩, Instagram 설정과 입력 검증, 비밀정보 repr 제외 |
| `food_trends/instagram_graph.py` | 공식 Facebook Graph 요청, ID 검색, 페이지네이션, 재시도, 오류 정제 |
| `food_trends/instagram_storage.py` | 기존 SQLite에 추가 스키마, 관계·집계·실행 이력·DB 잠금 |
| `food_trends/instagram_service.py` | 승인, 예산, 수집, 스케줄링, 연결·토큰 검사 |
| `food_trends/__main__.py` | 기존 서버에 Instagram 연결, dotenv, 관리 명령 |
| `food_trends/api.py` | Instagram 조회·승인·수집·일시 정지·연결 확인 API |
| `static/index.html`, `static/app.js` | 기존 화면의 개요 및 후보 상세 영역 연결 |
| `static/instagram.js`, `static/style.css` | 승인 폼, 상태·게시물, 관측 차트, 기존 녹색 테마 유지 |
| `tests/test_instagram.py` | 외부 호출 없는 Instagram 자동 검증 18개 |
| `tests/instagram_browser_check.py` | 실제 앱 미연결 화면 및 임시 Mock 서버의 Chrome 검증 |
| `.env.example`, `.gitignore` | 환경변수 예시 및 비밀정보 파일 제외 |
| `README.md`, `INSTAGRAM_INTEGRATION.md` | 실행 방법, 설계 및 검증 보고 |

로컬 `.env`는 기존 파일이 없어 빈 인증 값으로 생성했습니다. 이후 덮어쓰지 않습니다. 이 작업 폴더는 Git 저장소가 아니어서 Git 변경 비교나 추적 파일 검사는 불가능했습니다. Git 초기화나 커밋은 하지 않았으며 `.env` 제외 규칙을 추가했습니다.

## 2. 기존 구조와 통합

기존 Python 3.11+ / SQLite / 표준 HTTP 서버 / HTML·CSS·JS를 유지했습니다. 새 런타임 패키지나 DB는 없습니다. RSS 파서·분류기·저장소·수집 서비스 파일은 수정하지 않았습니다. `candidates.item_id`와 Instagram 해시태그를 연결하므로 같은 후보에 여러 태그, 같은 태그에 여러 RSS 후보를 연결할 수 있습니다.

기존 스케줄러는 RSS 전용 스레드 루프였습니다. 동일한 `threading.Thread` / `Event` 방식을 사용해 Instagram 작업을 별도 주기로 실행합니다. RSS의 실행 간격과 잠금은 그대로입니다. Instagram 인증 실패가 RSS 작업을 중단하지 않습니다. 새 수집원을 등록하는 범용 스케줄러 프레임워크는 도입하지 않았습니다.

RSS 후보 발견과 재분류는 Instagram API를 호출하지 않습니다. UI는 후보명·태그 입력·예산을 보여 주고 필수 승인 체크박스를 받은 뒤 호출합니다. CLI의 `--instagram-start`도 명시적 승인 명령입니다. 아직 승인된 태그는 없으며 테스트 대상은 `두쫀쿠` 하나를 사용하도록 안내했습니다. 이미 활성화된 태그를 다시 승인하면 후보 연결만 추가하고 외부 요청은 하지 않습니다. 기존 ID는 수집 때 재사용합니다.

## 3. 추가 DB 테이블과 컬럼

기존 테이블의 컬럼 변경이나 데이터 삭제는 없습니다. `CREATE TABLE IF NOT EXISTS`로 다음을 추가합니다.

| 테이블 | 주요 컬럼 / 의미 |
| --- | --- |
| `instagram_hashtags` | `id`, `user_id`, `name`, `instagram_id`, `created_at`, `last_searched_at`, `active`, `approved_at`; 계정·이름 고유 |
| `instagram_candidate_hashtags` | `candidate_id`, `hashtag_id`; 기존 후보와 다대다 연결 |
| `instagram_media` | `id`(전역 고유 Media ID), `published_at`, `first_seen`, `last_seen`, `media_type`, `permalink`, `source` |
| `instagram_hashtag_media` | `hashtag_id`, `media_id`, 태그별 `first_seen`, `last_seen`; 복합 고유키 |
| `instagram_runs` | `id`, `hashtag_id`, `started_at`, `finished_at`, `status`, `request_count`, `returned_count`, `new_count`, `duplicate_count`, `error_type`, `error_code`, `error_subcode`, `truncated` |
| `instagram_searches` | `id`, `user_id`, `name`, `instagram_id`, `searched_at`; 7일 창 검색 예산 기록 |
| `instagram_metrics` | `hashtag_id`, `grain`(hour/day), `bucket`(KST), `new_count`, `run_count`, `successful_runs` |
| `instagram_state` | `user_id`, `key`, `value`; 서버 검색 목록 스냅샷, 연결·만료 검사, 비밀 원문 없는 자격증명 지문 |
| `instagram_leases` | `user_id`, `owner`, `expires_at`; 계정별 여러 프로세스의 중복 작업 방지 |

수집 실행 상태는 `running`, `success`, `limited`, `partial`, `failed`, `interrupted`입니다. 페이지 제한에 도달하면 `limited`와 `truncated=1`로 표시합니다. 후속 페이지 실패 시 이미 관측한 데이터는 저장하고 `partial`로 기록합니다. 반환 건수는 API 행 수, 신규 건수는 해당 태그에서 처음 본 고유 게시물, 중복은 검증된 반환 행 중 신규가 아닌 행입니다. 전역 게시물 원본은 태그가 달라도 한 번만 저장합니다.

최초 구현에서는 캡션을 저장하지 않았으나, 후속 실연동 요청에 따라 `instagram_media.caption` 컬럼을 추가해 API의 공개 캡션을 보존합니다. 화면에는 일부만 표시합니다. 작성자·프로필 정보는 별도 수집하지 않습니다. 원본 게시 시각과 최초·최근 관측 시각은 구분하며, 신규 집계 기준은 **태그별 최초 관측 시각**입니다. 최신 검증 결과는 [실연동 검증 보고서](INSTAGRAM_LIVE_VERIFICATION.md)를 참고하세요.

지표는 시간별·일별 별도 테이블에 저장합니다. 성공한 빈 응답은 0건 관측으로 기록할 수 있지만, 수집하지 않은 날짜와 실패한 빈 회차는 지표 행을 만들지 않습니다. 그래프의 빈 구간은 미관측으로 표시하고 과거 데이터를 복원하지 않습니다. 최근 24시간과 직전 24시간의 신규 관측 차이도 제공하되 비교 기간이 부족하면 null입니다. 원본 시각은 UTC, 집계와 화면은 KST입니다.

## 4. 환경변수

루트 `.env`를 읽고 이미 설정된 프로세스 환경변수가 우선합니다. 기존 .env 내용은 수정하지 않습니다.

| 변수 | 기본값 / 용도 |
| --- | --- |
| `INSTAGRAM_GRAPH_API_VERSION` | `v26.0` |
| `INSTAGRAM_ACCESS_TOKEN` | 빈 값, 사용자가 직접 입력 |
| `INSTAGRAM_USER_ID` | `17841473668787556` |
| `FACEBOOK_PAGE_ID` | `1397807253414629` |
| `META_APP_ID` | 빈 값, 토큰 검사·교환 시 필요 |
| `META_APP_SECRET` | 빈 값, 토큰 검사·교환 시 필요 |
| `INSTAGRAM_COLLECTION_ENABLED` | `true` |
| `INSTAGRAM_COLLECTION_INTERVAL_MINUTES` | `60` |
| `INSTAGRAM_TIMEOUT_SECONDS` | `20`, 최대 60초 |
| `INSTAGRAM_MAX_PAGES` | `20`, 회차당 최대 페이지 (1~100) |
| `INSTAGRAM_SEARCH_SOFT_LIMIT` | `28`, 신규 탐색을 막기 시작할 기준 (1~30) |

기존 `DATABASE_PATH`, `PORT`, `COLLECT_INTERVAL_SECONDS`, `RSS_TIMEOUT_SECONDS`도 유지합니다. 설정 변경은 서버 재시작 후 반영합니다. Instagram 환경변수 형식이 잘못되면 해당 수집을 비활성화하고 RSS 서버는 유지합니다.

## 5. 직접 입력할 인증 정보와 토큰 관리

일반 수집에는 `.env`의 `INSTAGRAM_ACCESS_TOKEN`을 직접 채우면 됩니다. 토큰 교환·만료 검사에는 `META_APP_ID`, `META_APP_SECRET`도 채우세요. 비밀정보를 채팅, 프론트엔드 또는 코드에 넣을 필요가 없습니다.

일반 Graph 요청은 Authorization 헤더를 사용하고 인증 실패는 고정 한국어 메시지와 숫자 오류 코드로만 보존합니다. Meta 오류 메시지, 원본 JSON 응답, 인증이 포함될 수 있는 `paging.next` URL은 저장하거나 출력하지 않습니다. 다음 페이지는 신뢰한 고정 Graph 주소에 커서만 넣어 재구성하고 외부 리다이렉트를 거절합니다.

`--instagram-token-check`는 `debug_token` 응답의 유효성과 토큰·데이터 접근 만료 중 이른 시각을 저장합니다. 검사 전 만료 시각은 미확인입니다. 서버가 새 토큰으로 재시작하면 과거 토큰의 검사 상태를 초기화합니다.

`--instagram-exchange-token`은 `oauth/access_token`의 `fb_exchange_token` 교환을 사용합니다. 교환 방식은 [Meta가 보관한 공식 SDK 구현](https://github.com/facebookarchive/web-friend-smash/blob/master/payments/facebook-php-sdk/src/Facebook/Entities/AccessToken.php)의 `extend()`에서도 확인했습니다. 이 오래된 구현만으로 현재 토큰 수명이나 재교환 정책을 확정하지는 않습니다. 반환 토큰은 지정한 **새 `.secret` 파일**에만 쓰며 콘솔에 출력하거나 `.env`를 교체하지 않습니다. 출력 파일이 이미 있으면 교환 요청 전에 중단합니다. 사용자가 직접 토큰을 반영하고 재시작해야 합니다.

현재 Facebook Login 장기 사용자 토큰의 자동 연장 조건·최대 수명은 [공식 장기 토큰 문서](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/)와 [공식 갱신 문서](https://developers.facebook.com/docs/facebook-login/access-tokens/refreshing/) 접근 시 429 또는 접근 오류로 최신 본문을 확인하지 못했습니다. 따라서 무기한 자동 갱신 가능 여부를 검증 완료로 보고하지 않습니다. 자동 연장이나 Instagram Login용 갱신 API 호출은 구현하지 않았으며 만료 시 명시적 재인증·교환을 운영자가 수행해야 합니다.

## 6. 실행 및 관리 명령

먼저 기존 서버를 종료하고 다시 실행하세요. 검증 당시 이전 버전 서버가 8000에서 실행 중이어서 새 서버 검증에는 8001을 사용했습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
# 상태 조회 (외부 호출 없음)
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-status
# /me/accounts 연결 및 서버 최근 검색 목록 확인
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-check
# 두쫀쿠 하나만 승인하고 ID 검색 / 초기 수집
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-start 두쫀쿠
# 기존 후보에 연결하려면 실제 ID를 사용
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-start 두쫀쿠 --candidate-id 123
# 활성 태그 전체 또는 특정 내부 ID만 수동 수집
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-collect
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-collect --hashtag-id 1
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-token-check
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-exchange-token --token-output data/instagram-long-lived.secret
# 외부 호출 없는 전체 자동 테스트
& "$env:USERPROFILE\anaconda3\python.exe" -m unittest discover -s tests -v
```

브라우저 QA는 websocket-client가 있는 QA Python, 앱 서버, CDP 9224로 실행한 헤드리스 Chrome이 필요합니다. 앱 런타임에는 추가 패키지가 필요하지 않습니다. `BROWSER_APP_URL`로 앱 주소를 지정하고 `tests/instagram_browser_check.py`를 실행합니다. 이 스크립트는 실제 앱이 토큰 미설정인 상태를 전제로 합니다.

추가 API:

- `GET /api/instagram?candidate_id=123&days=7`: 저장 데이터 조회만 수행. 기간은 7/14/30일.
- `POST /api/instagram/start`: JSON `{candidate_id, name, approved:true}`. 후보와 명시적 승인 필수.
- `POST /api/instagram/collect`: JSON `{hashtag_id}` 또는 `{}`로 전체 활성 대상 수집.
- `POST /api/instagram/pause`: JSON `{hashtag_id}`로 일시 정지.
- `POST /api/instagram/check`: JSON `{}`로 페이지 연결과 검색 목록 확인.

변경 API는 같은 로컬 출처와 application/json 요청을 검사합니다. 기존 앱과 마찬가지로 루프백 관리 앱이며 인터넷 공개용 사용자 인증 기능은 추가하지 않았습니다.

## 7. 실제 연결 검증 결과

| 항목 | 결과 |
| --- | --- |
| 기존 서버 실행 | 정상, 새 코드의 화면은 8001에서 검증 |
| .env 로딩 / 토큰 누락 | 정상, Instagram만 미연결로 표시 |
| Google Trends RSS 실제 요청 | HTTP 200, 회차 57, 10개 반환 / 신규 RSS 항목 3개 |
| 기존 RSS 저장 상태 | 고유 항목 240개, 관측 기록 530개 (검증 시점) |
| `/me/accounts` 실제 연결 | 토큰 미설정으로 건너뜀 |
| `두쫀쿠` 실제 ID 검색 / recent_media | 토큰 미설정으로 건너뜀 |
| 실제 Instagram 게시물 저장·중복·화면 | 실제 API 데이터 기준으로는 미검증 |
| 토큰 실제 검사·장기 교환 | 토큰·App 인증 정보 미설정으로 건너뜀 |

처음 샌드박스에서 실행한 RSS 요청은 네트워크 연결 거부로 실패했고, 권한이 허용된 환경에서 기존 수집기를 1회 실행해 정상 수집했습니다. RSS 수집기 코드를 변경하거나 캐시를 실제 응답으로 가장하지 않았습니다.

## 8. 실제 수집 대상과 게시물 수

- 실제 등록/승인된 Instagram 해시태그: **0개**.
- 실제 API 관측 게시물: **0개**.
- 초기 실연결 테스트 권장 태그: **두쫀쿠 하나**.
- Mock 데이터는 임시 DB에서만 사용했으며 `data/trends.sqlite3`에 넣지 않았습니다.

## 9. 자동·브라우저 검증

전체 unittest **24개**: 기존 RSS 6개 + Instagram 18개. 페이지네이션과 요청 횟수, ID 캐시, 중복·다대다 관계, 예산 교차 확인 및 7일 창, 인증·권한·네트워크·Rate Limit, 실패/부분 저장/페이지 상한, 스케줄러·재시작·동시 실행 방지, 토큰 교환의 비노출과 파일 덮어쓰기 방지, 환경변수 보존, API 승인을 검증했습니다. 자동 테스트는 외부 Meta API를 호출하지 않습니다.

Chrome에서 실제 서버의 RSS 목록·상세와 Instagram 미연결 표시·승인 비활성화, 데스크톱/모바일 화면을 확인했습니다. 별도 임시 서버의 Mock 데이터로 승인 → 최초 수집 → 일별/시간별 차트 → 7/14/30일 변경 → 재수집 → 정지·재개를 확인했습니다. JavaScript 예외는 없었습니다. JS 파일 2개의 Node 문법 검사도 통과했습니다.

증거 파일 (모두 `data/`의 로컬 검증 산출물):

- `instagram-browser-verification.json`
- `instagram-unconfigured-desktop.png`, `instagram-unconfigured-mobile.png`
- `instagram-mock-preview.png` (Mock임을 화면에 표시)

## 10. API 한계와 남은 검증

이 값은 **API 관측 게시물 수**이며 전체 Instagram 발화량을 나타내지 않습니다. 공개 범위, API가 허용하는 조회 기간, 페이징·회차 상한, 인증 중단 때문에 누락될 수 있습니다. 장기간 미수집 후 과거 데이터를 완전히 복원할 수 있다고 가정하지 않습니다.

검색 예산은 계정별로 분리하고 최근 7일 기록과 `/{user_id}/recently_searched_hashtags`를 교차 확인합니다. 로컬 기록은 참고용이며 다른 앱·도구의 호출과 실패 예약 때문에 서버와 다를 수 있습니다. 7일 창 안의 반복 조회는 창 시작을 연장하지 않고, 새 예산이 필요할 때 서버 목록을 확인하지 못하면 수집을 보류합니다. 서버 스냅샷 유효기간은 5분이며 보수적인 로컬 추정도 함께 적용합니다. API 자체의 한도 응답이 최종 기준입니다.

한 요청은 최대 3회 시도하고 네트워크·서버 오류·Rate Limit에 2초/4초 백오프를 적용합니다. Retry-After는 최대 30초까지 반영합니다. 실패한 태그는 다음 수집 주기에 재시도합니다. DB 잠금은 요청마다 갱신되는 5분 임대이며 비정상 종료 후 최대 5분이 지나면 중단된 회차를 기록하고 재개합니다. 주기는 실행 중인 서버에만 적용됩니다.

[Meta 공식 Instagram API 컬렉션](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-af579d08-121e-4897-8f45-5fd41ace49df)에서 Facebook Login의 프로페셔널 계정·페이지 연결과 커서 기반 페이지네이션 구조를 확인했습니다. 해시태그 관련 [공식 가이드](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/hashtag-search/)와 [최근 검색 목록](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/recently_searched_hashtags/) 본문은 접근 제한으로 최신 세부 정책을 확인하지 못했습니다. 구현은 사용자가 제공한 v26.0 검증 요청 형태를 따릅니다. 실제 앱 권한·버전·필드 지원과 토큰 수명 정책의 최종 확인이 남아 있습니다.

토큰을 입력한 뒤 `--instagram-check`, `--instagram-start 두쫀쿠`, `--instagram-collect`를 순서대로 실행하면 실제 연결·저장·재수집을 최소 요청으로 검증할 수 있습니다. 새 검색 시 1회의 최근 검색 예산 조회, 1회의 ID 검색, API가 반환하는 recent_media 페이지 수만큼 요청합니다. 이미 승인·조회한 대상의 재수집에서는 ID 검색을 생략합니다. App ID/Secret을 입력한 경우 토큰 검사를 별도로 실행하세요.

YouTube와 네이버 API는 이번 작업에 추가하지 않았습니다.
