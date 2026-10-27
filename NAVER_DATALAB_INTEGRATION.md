# NAVER DataLab 연동 보고서

이 문서는 최초 구현 시점의 기록입니다. 이후 사용자 인증값을 사용한 실제 연결·7/30/90일 그래프 검증은 완료했으며, 현재 상태와 사용자 지정 음식 조회 방법은 [실환경 검증 보고서](NAVER_LIVE_VERIFICATION.md)를 참고하세요.

검증일: 2026-10-26 (KST). 기존 Python 표준 라이브러리·SQLite·로컬 HTTP 서버와 화면을 확장했습니다. 새 패키지, 외부 인프라, LLM 의존성을 추가하지 않았습니다.

## 설정 및 실행

기존 루트 `.env` 16~17행에 다음 빈 항목을 추가했습니다. 기존 Instagram 등 환경변수는 유지했습니다. `.env.example`에도 동일한 두 항목을 추가했습니다.

```dotenv
# NAVER API HUB - Data Lab Search Trend
NAVER_DATALAB_CLIENT_ID=
NAVER_DATALAB_CLIENT_SECRET=
```

NAVER API HUB에서 검색어 트렌드 API를 사용할 수 있는 Client ID와 Client Secret을 발급받아 **로컬 `.env`에 직접 입력**하세요. 채팅이나 브라우저에 입력할 필요가 없습니다. 기존 dotenv 로더를 재사용하며 프로세스 환경변수가 `.env`보다 우선합니다. 인증값을 입력한 뒤 서버를 재시작하면 코드 수정 없이 조회할 수 있습니다.

```powershell
cd 'D:\mz 제조기'
# 조회 대상 후보와 연결 상태 확인; NAVER 호출 없음
powershell -ExecutionPolicy Bypass -File .\run.ps1 --naver-status
# 서버 실행 및 자동 수집 (기존 서버를 먼저 종료)
powershell -ExecutionPolicy Bypass -File .\run.ps1
# 현재 FOOD 후보 전체를 최대 5개 그룹씩 수동 조회
powershell -ExecutionPolicy Bypass -File .\run.ps1 --naver-collect
# 후보 한 개 조회: 123은 --naver-status에 나타나는 실제 candidate_id로 교체
powershell -ExecutionPolicy Bypass -File .\run.ps1 --naver-query 123 --naver-days 30
```

접속 주소는 기본 http://127.0.0.1:8000 입니다. 기존 결과 화면 아래의 **NAVER 검색 트렌드** 또는 음식 후보 상세에서 키워드·7/30/90일을 선택하고 **검색 추이 조회**를 누르세요. 페이지의 5초 갱신은 로컬 메타데이터만 조회하며 NAVER를 호출하지 않습니다. 화면을 여는 것만으로 NAVER 요청을 보내지 않습니다.

기존 `--collect-once`는 RSS 전용입니다. 필요하면 RSS 수집 후 `--naver-collect`를 순서대로 실행하세요. FOOD 후보가 없으면 네이버 호출도 없습니다. REVIEW/NON_FOOD는 자동으로 전송하지 않습니다.

## 구성과 기존 탐지 기능 연결

| 파일 | 변경 및 역할 |
| --- | --- |
| `food_trends/naver_datalab.py` (신규) | 독립 API HUB 클라이언트, 요청·날짜·필터 검증, 시계열 파싱, 오류 구분·제한 재시도 |
| `food_trends/naver_service.py` (신규) | 기존 FOOD 후보 연결, 명시적 별칭, 5그룹 배치, 일일 예산, DB 기반 중복 잠금, 주기 수집 |
| `food_trends/config.py` | `NaverConfig`; 기존 dotenv 재사용, 비밀값 repr 제외, 설정 검증 |
| `food_trends/api.py` | NAVER 상태·조회·수집 라우트, 기존 서버·no-store 응답 방식 재사용 |
| `food_trends/__main__.py` | NAVER CLI 및 서버 시작·종료 시 수집 스레드 연동 |
| `static/naver.js` (신규) | 키워드·기간 선택, 접근성 레이블이 있는 SVG 선 그래프와 수치 표, 상태별 안내 |
| `static/app.js` | 기존 상세 화면 및 로컬 상태 갱신에 NAVER 패널 연결 |
| `static/index.html` | 기존 결과 화면에 패널과 스크립트 추가 |
| `static/style.css` | 기존 테마를 사용하는 그래프·모바일 스타일 추가 |
| `.env`, `.env.example` | NAVER 인증값 두 항목을 빈 값으로 추가 |
| `tests/test_naver.py` (신규) | 임시 DB와 Mock 전송기로 백엔드·API 검증 |
| `tests/naver_browser_check.py` (신규) | 별도 Chrome 프로필과 임시 DB로 화면·오류·회귀 검증 |
| `README.md` | 사용법과 이 보고서 링크 추가 |
| `NAVER_DATALAB_INTEGRATION.md` (신규) | 변경 파일·명령·설계·정책 확인 범위·테스트 결과 |

기존 RSS 수집·분류·저장 모듈은 변경하지 않았습니다. 기존 `candidates`의 `FOOD`와 `item_id`를 조회해 NAVER 대상에 연결합니다. 기존 NFC/공백 정규화를 재사용합니다. 동일 음식이 다른 게시 시점에 등장한 여러 후보 ID도 하나의 검색 주제로 묶을 수 있습니다.

별칭은 `naver_service.py`의 명시적 `ALIASES`만 사용합니다. 현재 `두쫀쿠`, `두바이 쫀득 쿠키`, `두바이쫀득쿠키`를 같은 주제로 처리하며 다른 음식과 유사 문자열을 자동으로 합치지 않습니다. 별칭 없는 검색어는 원래 정규화 이름 하나로 조회합니다. RSS 트래픽·분류 점수는 변경하거나 네이버 지수와 합산하지 않습니다.

## API와 데이터 의미

- `GET /api/naver`: 설정 여부, 자동 수집 여부, 일일·누적 요청 수, 상태별 횟수, 후보·동의어·최근 시도·마지막 정상 시각. NAVER 외부 호출 없음.
- `POST /api/naver/query`: `{"candidate_ids":[123,456],"days":30}`. 기간 7/30/90, 서로 다른 주제 최대 5개. 같은 요청의 그룹만 공통 기준으로 해석할 수 있으며 요청 ID를 함께 반환합니다.
- `POST /api/naver/collect`: `{}`. FOOD 후보 전체를 최대 5개 주제씩 조회. 서버 로컬 요청과 JSON만 허용합니다.

실제 전송 주소는 `POST https://naverapihub.apigw.ntruss.com/search-trend/v1/search`입니다. `X-NCP-APIGW-API-KEY-ID`와 `X-NCP-APIGW-API-KEY` 헤더를 서버에서만 설정합니다. HTTP 리다이렉트로 키가 다른 호스트에 전달되지 않도록 차단합니다. 응답·오류 본문·인증 헤더는 로그나 DB에 기록하지 않습니다.

기본은 **어제(KST)까지 완료된 30일**, 일간 `date`, 기기·성별·연령 전체입니다. 당일의 불완전한 데이터를 피하며 최소 날짜는 2016-01-01입니다. 내부 `request_body` 함수는 시작·종료일, 기간, 집계 단위와 필터를 변경할 수 있습니다. 미래/당일·역전 날짜와 잘못된 필터는 전송 전에 거부합니다.

응답은 날짜와 **상대 검색 지수 0~100**이며 절대 검색 횟수가 아닙니다. 누락 날짜는 0으로 보간하지 않습니다. 서로 다른 요청·기간·필터·그룹 구성의 지수를 직접 비교하거나 순위·통합 점수를 산정하지 않습니다. 그래프는 각 결과의 날짜순 값만 표시합니다.

공식 [API 명세](https://api.ncloud-docs.com/docs/naver-api-hub-search-trend)는 그룹당 최대 20개 검색어를 기재하지만, [사용 가이드 FAQ](https://guide.ncloud-docs.com/docs/apihub-overview)는 5개로 안내합니다. 문서 간 차이가 있고 요청서도 초기 5개 이하를 요구하므로 구현은 **최대 5그룹 × 그룹당 5검색어**로 제한했습니다.

## 자동 수집과 호출 제한

기존 앱과 동일한 프로세스 내 `threading`/`stop_event` 주기 실행 방식을 사용합니다. 서버가 켜져 있는 동안 60초마다 대상 변경·수집 필요 여부를 점검하고, 같은 음식 주제는 기본 86400초에 한 번 수집합니다. 키가 없으면 NAVER 스레드를 시작하지 않습니다. RSS가 첫 수집에서 새 FOOD를 저장하면 다음 점검에서 발견합니다. 앱 종료 중에는 자동 수집하지 않습니다.

추가 설정은 **프로세스 환경변수**로 선택 지정할 수 있습니다. `.env`에는 요청한 인증 두 항목만 추가했습니다.

| 설정 | 기본값 | 제한 |
| --- | --- | --- |
| `NAVER_DATALAB_ENABLED` | `true` | `false`이면 자동 수집 비활성화; 키가 있으면 수동 조회 가능 |
| `NAVER_DATALAB_INTERVAL_SECONDS` | `86400` | 하루 이상 |
| `NAVER_DATALAB_DAILY_LIMIT` | `100` | 1~100회; 재시도도 포함 |
| `NAVER_DATALAB_TIMEOUT_SECONDS` | `20` | 0초 초과~60초 |

401/403은 인증 오류로 처리하고 재시도하지 않습니다. 네트워크·5xx·429는 최초 요청 후 최대 2회, 지수 백오프를 적용합니다. 긴 `Retry-After`는 앞당기지 않고 호출을 중단하며 다시 호출할 수 있는 시각을 기록합니다. 429 실패 후에는 다른 요청에도 대기 시간을 적용합니다. API 응답의 구체적 오류 본문을 사용자에게 노출하지 않습니다.

SQLite 트랜잭션으로 **KST 일일 요청 예약**을 기록하므로 같은 DB를 사용하는 서버와 CLI의 한도가 합산되고 재시작해도 초기화되지 않습니다. 동일 요청은 실행 중 및 완료 후 기본 60초간 다시 전송하지 않으며, 중복 응답을 캐시에서 반환하지도 않습니다. 응답 저장 없이 중복 안내만 반환합니다. 프로세스 중단 시 실행 잠금은 최대 10분 후 만료됩니다. 대기 대상이 예산보다 많으면 오래 조회하지 않은 주제부터 처리합니다.

공식 가이드의 검색어 트렌드 월 한도는 50,000회, 키당 50 RPS이며 본 앱은 일 100회로 더 낮게 제한합니다. 다른 앱·별도 DB에서 같은 키를 사용하는 양은 본 앱에서 알 수 없으므로 콘솔의 실제 사용량·제한이 최종 기준입니다. 응답 저장이나 키 교체로 제공자 제한을 우회하지 않습니다.

## 보관 정책 검토와 실제 저장 범위

공식 [NAVER API HUB Application 안내](https://guide.ncloud-docs.com/docs/apihub-application)에서 별도 API 약관 동의가 필요함을 확인했고, [공식 공지 목록](https://www.ncloud.com/support/notice/all?page=2)에서 **2026-10-07 시행 개정 공지**를 확인했습니다. 다만 현행 API HUB 약관 전문에서 **검색어 트렌드의 장기 저장·가공 데이터 보관·재배포 허용 범위를 확정하지 못했습니다**. 이전 Search Trend 상품 안내나 Developers 약관을 현행 HUB 저장 허가로 간주하지 않았습니다. 확인된 2026-09-20 개정 공지의 검색 API 조항도 검색어 트렌드 저장 권한의 근거로 사용하지 않았습니다.

따라서 요청서의 불명확 시 처리 원칙에 따라 다음과 같이 구현했습니다.

- API 원본·검색 지수·시계열·파생 검색 통계를 DB/파일에 저장하지 않음. 서버 응답 캐시도 없음.
- 수동 조회 결과는 현재 요청에서 화면 또는 CLI로 한 번 전달하며 응답은 `Cache-Control: no-store`. 브라우저 저장소를 사용하지 않음.
- 자동 수집 결과는 해당 실행에서 파싱한 후 버리고 **성공/실패·시각·호출 횟수만** 남김. 과거 자동 응답을 그래프로 재사용하지 않음. 그래프는 사용자가 실시간 조회해야 함.
- 신규 `naver_targets`는 기존 로컬 후보 ID·로컬 별칭·최근 시도·마지막 정상 시각·상태만 관리.
- 신규 `naver_calls`는 요청 조건·후보 ID·요청/완료 시각·HTTP 상태·성공/오류 종류만 관리. 응답 본문과 인증정보 컬럼 없음.
- 신규 `naver_guards`는 요청 해시·잠금 소유자·만료 시각만 관리. 시계열 캐시가 아님.
- 기존 RSS JSON 내보내기에 NAVER 테이블이나 응답을 추가하지 않음.

장기 응답 저장·캐싱·DB화·재배포 기능은 제공하지 않습니다. 향후 해당 API의 명시적 허용 범위를 확인한 경우에만 별도 저장 정책·설정·만료 처리를 설계해야 합니다. 저장 허용 여부를 확인했다고 주장하지 않습니다.

`.gitignore`에는 이미 `.env`, `.env.*` 제외와 `.env.example` 예외가 있습니다. **현재 작업 폴더와 상위에 Git 저장소가 없어 실제 tracked 여부·브랜치·워크트리·미커밋 변경·커밋 해시는 확인할 수 없었습니다.** 새 Git 저장소나 커밋은 만들지 않았습니다. 추후 Git을 초기화하거나 기존 저장소로 옮길 때 `.env`가 추적되지 않는지 확인해야 합니다.

## 검증 결과

```powershell
# 전체 자동화 테스트; 이 PC에서는 Anaconda Python 사용
& "$env:USERPROFILE/anaconda3/python.exe" -m unittest discover -s tests -v
# NAVER만 검증
& "$env:USERPROFILE/anaconda3/python.exe" -m unittest discover -s tests -p test_naver.py -v
# 별도 Chrome 프로필과 임시 DB를 사용하는 브라우저 QA
& "$env:USERPROFILE/anaconda3/python.exe" tests/naver_browser_check.py
node --check static/naver.js
node --check static/app.js
```

전체 **52개 통과** (NAVER 18개 포함), 실패 0개. 테스트의 의도적 RSS 실패 로그는 오류 보존 경로 검증이며 테스트 실패가 아닙니다.

검증 범위: 키 누락 시 요청 0회와 기존 RSS 실행, API HUB 헤더·본문, KST 7/30/90일 범위·날짜·필터 검증, 5그룹 배치·명시적 별칭·여러 후보 연결, 시계열 파싱·NaN/범위/잘못된 응답 거부, 동시·재시작 중복 방지, 재시도·일일 예산, 401/403/429/5xx·네트워크 처리, 마지막 정상 시각 보존, 응답·인증정보 미저장, no-store 및 기존 API 회귀.

Chrome 검증: 키 미설정과 조회 버튼 비활성화, 7/30/90일 그래프·수치 표, 중복 시 이전 그래프 제거, 401/403/429/503 안내, 빈 데이터 안내, 390px 모바일 가로 넘침 없음, RSS 원본·Instagram 상세 유지, JavaScript 예외 0개. 최초 격리 실행은 Chrome CDP 연결이 불가능해 실패했고 일반 실행 환경에서 재실행해 통과했습니다. 브라우저 종료로 일부 진행 중 로컬 요청이 중단되는 기존 HTTP 서버 로그가 발생했지만 UI 검증은 통과했습니다. 이 테스트는 기존 QA 환경의 `websocket-client`만 사용합니다.

브라우저 결과는 `data/naver-browser-verification.json`에 **검증 상태만** 기록했습니다. Mock 검색 응답은 임시 테스트 DB·프로필에서만 사용하며 실제 서비스 DB·화면에는 넣지 않았습니다. 실제 프로젝트에 `--naver-status`를 실행한 결과 `configured=false`, `status=not_configured`, `automatic_enabled=false`, 일일/누적 요청 0회였습니다. 이 실행은 기존 DB에 메타데이터 테이블만 추가했습니다.

미검증: 실제 키를 이용한 인증 성공·서비스 권한·실제 시계열 응답·제공자 한도 동작, 24시간 실시간 경과 실행. 하루 주기 판단과 재시작 보호는 Mock 테스트로 검증했습니다. 실제 API 성공을 주장하지 않습니다. 후속 작업은 사용자의 키 입력 후 실제 조회 확인과 장기 보관이 필요할 때의 명시적 약관 확인입니다.
