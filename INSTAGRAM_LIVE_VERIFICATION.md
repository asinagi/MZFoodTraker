# Instagram 실연동 검증 결과 — 2026-10-26

**새 토큰으로 실제 연결·수집·DB 저장·조회·Chrome 화면 검증을 완료했습니다.** 두쫀쿠 공개 게시물 **67건**을 저장했고, UI에서 **50건**을 재조회해 신규 0건·중복 50건을 확인했습니다. 기본 주소 **http://127.0.0.1:8000**에서 관측 67건과 그래프를 확인했습니다. USER 토큰이며 현재 앱과 일치하고 필요한 권한 3개가 있습니다. 토큰은 **2026-10-26 16:00 KST (07:00 UTC)**에 만료됩니다. 기존 값 삭제·초기화·자동 교환은 하지 않았으며 실제 비밀값이나 토큰 지문은 이 보고서에 포함하지 않습니다.

## 실제 검증 상태

| 항목 | 결과 |
| --- | --- |
| .env 로딩 | 정상; 필요한 인증정보 모두 인식 |
| 토큰 유효성·유형·만료·권한·앱 일치 | 유효 / USER / 앱 일치 / 필요한 권한 3개 있음 / 16:00 KST 만료 |
| Facebook 페이지 / Instagram 연결 | `/me/accounts`에서 기대한 페이지 및 연결 Instagram 계정 확인 |
| 두쫀쿠 ID 검색 / 실제 recent_media | 성공, ID 캐시 저장 및 재사용 |
| 실제 등록·수집된 Instagram 해시태그 | 두쫀쿠 1개 |
| 실제 반환·저장된 Instagram 게시물 | 초기 67건 반환·67건 신규 저장, 고유 67건 |
| 실제 데이터 중복·수동 수집 | 화면 버튼으로 50건 재조회, 신규 0건·중복 50건, 고유 67건 유지 |
| 정기 수집 | 서버 기동·활성 태그 1개·60분 설정 확인. 스케줄러 자동 테스트 통과; 실시간 60분 대기는 하지 않음 |
| 프론트엔드 실제 Instagram 데이터 표시 | 기본 8000에서 67건·원본 링크·시작일·일별/시간별 차트 확인 |
| Google Trends RSS 실제 수집 | HTTP 200, 10개 반환, 회차 69, 신규 0개 |
| 자동화 검증 | unittest 54개 통과 (현재 작업공간의 RSS·감사·Instagram·기존 네이버 테스트 포함) |
| Chrome 검증 | 실제 관측 데이터, 7/14/30일, 일별/시간별 DB 일치, 수동 재수집, 중복 방지, 데스크톱·모바일 확인 |

실연동 검사 결과는 `data/instagram-live-verification.json`에 `status=passed`로 기록했습니다. 초기 검증은 토큰·페이지·검색 예산·ID 검색·페이징·재시도를 합쳐 25회 HTTP 요청을 사용했습니다. 초기 수집은 설정된 20페이지 상한에 도달해 `limited`, `truncated=1`로 기록했습니다. 검증용 재수집은 8001 서버의 프로세스 환경에서만 페이지 상한을 1로 제한해 추가 2회 HTTP 요청으로 확인했습니다. `.env`의 운영 페이지 설정은 바꾸지 않았습니다. API가 반환한 관측 범위만 보존했으며 전체 게시물을 수집했다고 주장하지 않습니다.

원본 게시 시각과 관측 시각은 67건 모두 별도로 저장됐으며 원본 링크도 67건 있습니다. 캡션은 API가 제공한 66건을 저장했고 미제공 1건은 null입니다. 최초 관측은 **2026-10-26 14:27:22 KST**이며, 시간별·일별 신규 관측 합계와 고유 관측 건수가 모두 67입니다. 비교할 과거가 없어 이전 기간 대비 증가율은 미확인으로 표시하고 과거 날짜를 0으로 채우지 않았습니다. 화면용 DB에 Mock Instagram 데이터를 넣지 않았습니다.

실제 화면 검증에서 수집 중 상태를 읽은 상세 화면의 버튼이 완료 후에도 잠겨 있는 문제를 발견해 `static/instagram.js`에서 상태 갱신과 버튼 활성화를 연결했습니다. 완료 후 활성화와 진행 중 비활성화 보호를 Chrome에서 별도로 확인했습니다. `tests/instagram_live_browser_check.py`는 실제 저장 데이터와 수동 수집·재수집·DB/차트 일치를 검증하며 샘플 데이터를 삽입하지 않습니다. JavaScript 예외는 없었습니다.

## 이번 작업에서 보완한 부분

- `food_trends/instagram_graph.py`: `debug_token`과 토큰 교환의 민감 파라미터를 HTTP URL에서 제거. Graph GET을 배치 POST 본문에 담고 배치 결과를 정제. 원본 오류·토큰·App Secret 출력 및 URL 방식으로의 자동 폴백 없음.
- `food_trends/instagram_service.py`: 공개 캡션 수집, 토큰 유형·필요 권한·앱 일치·발급/만료 시각 기준 수명 검사, 인증 오류 확인 시 자동 수집 보류, 태그별 조회. `/me/accounts`의 id/name/Instagram 연결 조회 요청 유지.
- `food_trends/instagram_storage.py`: 기존 SQLite에 `instagram_media.caption TEXT`만 추가. 원래 데이터를 보존하는 반복 실행 가능한 마이그레이션. 최근 두 24시간 구간의 신규 관측 증가율 산출. 기존 다대다 관계와 ID 중복 방지 유지.
- `food_trends/instagram_verify.py`: 승인 대상은 두쫀쿠 하나로 고정한 최소 실연동 검사. 토큰 검사 → 페이지 연결·검색 예산 → 캐시 확인·검색/수집 → 저장 건수와 시간별·일별 집계 비교. 인증정보가 없으면 외부 요청 전에 종료. Chrome 검증은 별도 pending으로 구분.
- `food_trends/__main__.py`: `--instagram-verify` 명령과 안전한 JSON 보고서 저장. 토큰 교환 전에 유효성·유형·권한·앱 일치·단기 여부 검사. 검사에 실패하면 교환 요청과 파일 생성을 보류. 기존 RSS 감사 JSON 내보내기 등 관련 없는 기능은 보존.
- `food_trends/api.py`: `hashtag_id`별 조회와 기존 승인 태그의 `/api/instagram/resume` 지원.
- `static/instagram.js`: 두쫀쿠 우선 표시. 개요에서 태그의 그래프·게시물을 직접 열고 실제 수집/정지/재개 API 호출. 수집 시작일·캡션·증가율과 토큰 검사 결과 표시. 관측되지 않은 구간은 0으로 채우지 않음.
- `tests/test_instagram.py`, `tests/instagram_browser_check.py`: 새 보안·저장·지표·전역 화면 검증 추가. 외부 API 반복 호출 없이 임시 DB와 Mock 사용.
- `README.md`, `INSTAGRAM_INTEGRATION.md`, `INSTAGRAM_LIVE_VERIFICATION.md`: 최신 동작과 보류 사유·운영 명령 문서화.

새 프레임워크나 DB는 추가하지 않았습니다. YouTube·네이버 기능은 이 Instagram 작업에서 수정하거나 구현하지 않았습니다. 작업 폴더에 Git 메타데이터가 없어 브랜치와 diff를 확인할 수 없었고 Git 초기화·커밋을 하지 않았습니다. `.env`와 `.secret`의 기존 Git 제외 규칙을 유지했습니다.

## 저장 후 실행할 명령

현재 `.env`의 새 인증정보는 정상 인식됩니다. 앱은 기본 8000 주소에서 확인했고, 추가 검증 명령은 필요할 때만 실행하세요. 비밀값을 채팅에 보낼 필요는 없습니다.

```powershell
# 두쫀쿠 하나의 실제 연결·수집·DB 집계 검증
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-verify
# 상태 조회: 외부 API 호출 없음
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-status
# 기존 앱 서버를 종료한 뒤 새 설정으로 재시작: 활성 승인 태그를 기본 60분마다 수집
powershell -ExecutionPolicy Bypass -File .\run.ps1
# 수동 수집 (필요할 때만)
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-collect
```

Instagram 개요에서 **#두쫀쿠 관측 데이터 보기**를 열면 RSS 후보 연결이 없어도 7/14/30일·일별/시간별 그래프, 실제 게시물 목록과 수동 수집을 사용할 수 있습니다. 수집 시작 후 최초 관측 기준으로 집계하며 원본 게시 시각을 혼용하지 않습니다. 이 수치는 **API 관측 게시물 수**이며 전체 Instagram 발화량이 아닙니다. 관측 시작 이전을 복원하지 않으며, 비교 기간 부족/직전 0건은 증가율 계산 불가로 표시합니다.

검증용 서버는 기존 8000 서버와 충돌하지 않도록 8001에서 실행했고, 기본 8000에서도 실제 67건과 그래프 표시를 최종 확인했습니다. 앞으로 환경변수를 바꿀 때는 앱을 재시작해 반영하세요. 간격은 `INSTAGRAM_COLLECTION_INTERVAL_MINUTES`로 변경합니다. 활성 태그만 수집하고 계정별 DB 임대로 동시 작업을 방지하며 재시작 후 주기를 재개합니다.

## 토큰 교환과 만료 관리

새 토큰은 USER 유형이며 검사 시 유효했습니다. `instagram_basic`, `pages_show_list`, `pages_read_engagement`는 모두 확인됐고 앱도 일치합니다. 현재 토큰의 만료는 **2026-10-26 16:00 KST**입니다. 발급 시각으로 수명 간격을 계산할 수 없어 단기/장기 분류는 미확인입니다. 발급·만료 시각이 모두 있을 때 수명 구간으로 단기/장기를 **추정**하며, 자료가 없으면 미확인입니다. 단순히 토큰 문자열의 길이로 판단하지 않습니다. 실제 토큰 수명이나 재교환 가능 여부는 Meta 응답이 기준이며, 영구 유효성을 보장하지 않습니다. 이번 작업에서는 토큰을 교환하거나 .env를 변경하지 않았습니다.

사용자가 필요성을 확인하고 명시적으로 실행할 때만:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-token-check
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-exchange-token --token-output data/instagram-long-lived.secret
```

검사에서 단기 USER 토큰이며 앱·권한 조건을 만족할 때 `fb_exchange_token` 교환을 시도합니다. 현재처럼 수명 분류가 미확인이면 명령은 안전하게 교환을 보류하므로 실제 장기 토큰 적용은 남은 운영 과제입니다. 이미 출력 파일이 있으면 요청 전에 중단하고 덮어쓰지 않습니다. 새 토큰은 새 `.secret` 파일에만 저장하며 콘솔·응답·.env에 자동 반영하지 않습니다. 해당 파일을 로컬에서 확인해 사용자가 직접 `.env`에 반영한 뒤 서버를 재시작합니다. 확인된 만료일 전에 재검사하고, 만료·취소 시 수집을 보류한 뒤 새 인증정보 적용 또는 명시적 재검사로 복구합니다.

배치 POST의 공식 근거는 [Meta Python Business SDK의 FacebookAdsApiBatch 구현](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/api.py)입니다. 토큰 검사 파라미터는 [Meta 공식 Debug Token 예시](https://www.postman.com/meta/whatsapp-business-platform/request/i1mz7w8/debug-token)에서 확인했습니다. 이번 실제 Meta 검사에서 코드의 단일 배치 GET 방식으로 메타데이터 응답을 받았습니다. 안전하지 않은 URL 방식으로 우회하지 않았습니다.

실제 데이터 브라우저 결과와 캡처는 `data/instagram-live-browser-verification.json`, `data/instagram-live-desktop.png`, `data/instagram-live-mobile.png`에 저장했습니다. 이전 미연결·Mock 검증 파일도 그대로 보존했으며 Mock 캡처에는 실제 Instagram 데이터가 아님을 표시했습니다. 테스트 실행으로 네이버 API를 실제 호출하거나 네이버 기능을 수정하지 않았습니다.
