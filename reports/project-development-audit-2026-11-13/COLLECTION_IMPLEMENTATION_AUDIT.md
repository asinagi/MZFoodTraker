# 수집 구현 감사 — 4개 수집원 (RSS · Instagram · YouTube · NAVER)

감사일: 2026-11-13 KST. **읽기 전용 감사**다. 코드·DB·설정은 바꾸지 않았고 외부 API 호출, 서버 기동, 수집기·테스트 실행도 하지 않았다. SQLite는 `sqlite3.connect('file:...?mode=ro', uri=True)`로만 열었다. Oracle(`MZ_APP`)에는 접속하지 않았다. 그래서 Oracle 쪽 수치는 2026-11-08 보고서와 JSON 증거로만 인용하고, 그 뒤의 값은 **미확인**으로 적는다. 비밀값(`.env`, `*.secret`, 토큰, 키)은 열지도 인용하지도 않았다.

> **교차 확인(통합 정리 시 추가):** [DATABASE_EVOLUTION_AUDIT.md §6](DATABASE_EVOLUTION_AUDIT.md)에서 2026-11-13에 `MZ_APP`을 읽기 전용으로 직접 조회했다. 수집원별 Oracle 행 수와 마지막 기록 시각(2026-11-07 23:26Z)이 이 문서가 인용한 11-08 증거와 같았다. 파일럿 이후 새 수집 기록이 없으므로, 이 문서의 "Oracle 마지막 성공 2026-11-07 23:26Z"는 감사일 기준으로 확인된 값이다.

표기 규칙: **확인**은 코드나 저장 데이터로 직접 대조한 것이다. **문서 주장**은 보고서에만 있고 독립적으로 재현하지 못한 것이다. **미확인**은 판단할 근거가 없는 것이다.

---

## 0. 전체 그림 (먼저 읽을 것)

| 항목 | 사실 | 근거 |
| --- | --- | --- |
| 수집 런타임은 둘이다 | (1) **구(舊) SQLite 런타임**: 2026-10-13~10-28 실제 연속 수집, `data/trends.sqlite3`. (2) **Oracle 런타임**: 11-07 컷오버 뒤의 SoR(`MZ_APP`). 실제 수집은 11-07 23:26Z 소량 파일럿 7회뿐 | DB 조회, `reports/development-oracle-production-cutover-2026-11-07/LIVE_VERIFICATION.md` |
| `data/trends.sqlite3`는 **스키마 v0**다 | `schema_migrations` 테이블이 없다(`user_version=0`). 현재 코드의 `Storage.__init__` → `migrations.require_current()`는 v11을 요구하므로 **현재 코드로는 이 DB를 열 수 없다**(`MigrationRequired`). `runs`·`instagram_runs`·`naver_calls`에 `origin`·`contract_json` 컬럼이 없다 | `food_trends/migrations.py` `LATEST_VERSION = 11`, `schema_version()`; 테이블 DDL 조회 |
| 마지막 SQLite 쓰기 | 파일 mtime 2026-10-28 14:20 KST(05:20Z). 마지막 RSS 성공 04:49:48Z, 마지막 Instagram 회차 시작 05:19:56Z | `ls -la`, DB 조회, `production-sqlite-final.json`(`main_file_modified_at` 일치) |
| 이 데이터를 쓴 프로세스 | Git 저장소가 생기기(10-27 15:13 KST, `97b3c35`) **전부터** 떠 있던 구버전 서버(8000/8002)였다. A1~B5 개발은 모두 격리 사본에서 했고 운영 DB에는 마이그레이션을 적용하지 않았다 | `reports/development-a1-a3-2026-10-27/REPORT.md` 69행, `reports/development-b4-b5-2026-10-28/REPORT.md` 13·105행 |
| 수집원 코드의 Git 이력 | 4개 수집원의 transport·파서·저장 코드는 모두 초기 커밋 `97b3c35`에 **한꺼번에** 들어갔다. Git 이전의 변경 과정은 루트 `*.md` 보고서와 `data/audit-20261026/before-*.py` 스냅샷으로만 재구성할 수 있다 | `git log -- food_trends/<file>` |
| 지속 수집 | **꺼져 있다**(`CONFIGURED_NOT_ENABLED`). 운영 기본 실행은 "수집 없음(관리·관측)" 모드다 | `data/ops-prod/logs/admin.log` 첫 줄, LIVE_VERIFICATION.md "검증 후 상태" |

---

## 1. Google Trends Korea RSS

### 1.1 구현 사실

| 항목 | 내용 | 근거(파일·함수) |
| --- | --- | --- |
| 피드 | 단일 피드 `https://trends.google.com/trending/rss?geo=KR`. **하드코딩된 상수**이며 환경변수로 바꿀 수 없다 | `food_trends/collector.py` `SOURCE` |
| HTTP | `urllib.request.urlopen`, User-Agent `FoodTrendsLocalMVP/1.0`, 응답 5 MB 상한, 타임아웃 `RSS_TIMEOUT_SECONDS`(기본 20초) | `collector.fetch()`, `__main__.py` |
| XML 파서 | 표준 라이브러리 `xml.etree.ElementTree`. `<!DOCTYPE`/`<!ENTITY`가 있으면 거부한다(XXE 방어). 루트 `rss/channel`을 검증한다 | `collector.parse()` |
| 추출 필드 | `title`→keyword, `pubDate`→UTC ISO(`published_at`, 원문은 `published_raw`), `approx_traffic`→traffic, `news_item`(title/url/source/snippet), `related_*`(관련 검색어), 항목 XML 원문(`raw_xml`), 제공된 태그 목록(`provided_fields`) | `collector.parse()` |
| 실제 제공 필드 | 370건 모두 `approx_traffic, description, link, pubDate, picture, picture_source, title`이 있다. 358건에는 `news_item`도 있고 12건은 뉴스가 없다. **관련 검색어는 0건**(피드가 주지 않음) | `items.payload` 조회 |
| 중복 제거 키 | `source_id = sha256(SOURCE ␟ title ␟ published(UTC) ␟ link)`. `items.source_id UNIQUE` + `INSERT OR IGNORE`. 같은 검색어라도 게시 시각이 다르면 다른 항목이다(370 항목 / 360 고유 검색어, 예: `카드뉴스` 3건) | `collector.parse()`, `storage.Storage.finish()` |
| 관측 | 회차마다 `observations(run_id,item_id)` PK. 반복 관측은 관측으로만 쌓는다 | `storage.Storage.finish()` |
| 주기 | `COLLECT_INTERVAL_SECONDS` 기본 1800초(30분) 스레드 루프 + 수동 `POST`/`--collect-once` | `service.CollectionService.start()`, `__main__.py` |
| 저장 | `runs`(원본 응답 바이트 `raw_response`, HTTP 상태, 경고, 회차별 FOOD/REVIEW 수, 분류기 버전), `items`(payload JSON), `observations`(payload + 당시 분류 JSON), `candidates`(현재 분류), `classification_history`(바뀌기 전 분류) | `storage.py` |
| 실패 처리 | 예외가 나도 회차를 `success=0`과 오류 문자열로 마감한다. 실패 회차는 "측정된 0"이 아니라 "미관측"으로 스테이징한다 | `service.CollectionService.collect()` |

### 1.2 음식 판별 규칙의 변천

**초기 구현 — `context-rules-v1`** (`data/audit-20261026/before-classifier.py`)
- 검색어와 뉴스 제목에서 `DIRECT`(음식·디저트·빵·떡…)와 `CONTEXT`(먹거리·맛집·카페·편의점·메뉴…)를 **부분 문자열로** 찾았다.
- "뉴스 과반에 음식 단어가 있으면" FOOD로 판정했다. `출시/품절/신상`이 있으면 REVIEW로 올렸다.

**발견된 문제 — 2026-10-26 감사** (`AUDIT_REPORT.md` §4)
- **광주송정역이 FOOD로 분류됐다**(뉴스 3건 중 2건에 '먹거리'가 있었다). REVIEW 오탐도 15건이었다: 김천시, 렉서스, 렉서스 es, 애플, 애플 홈뷰, 종신보험, 저비용 항공사('새 먹거리' 비유), 아리엘 후라도('떡잎'), 레임덕('어**떡**하나'), 최지우('붕어**빵** 딸'), 식품의약품안전처장, 여행, 청바지, 김민석, 농민.
- 실제 식재료인 **호박은 NON_FOOD로 누락됐다**.
- **DB 대조(확인)**: `classification_history`의 v1 기록은 정확히 FOOD 1(광주송정역)·REVIEW 15·NON_FOOD 224, 합계 240건이다. 문서의 위 목록 16건과 이름까지 일치한다.

**수정한 코드·방법 — `target-context-rules-v2`** (`food_trends/classifier.py` `classify()`, `target_pattern()`)
1. 검색어 자체의 형태로 개체 유형을 추정한다: LOCATION(역·시·군·구·식당), PERSON(직책), ORGANIZATION, NON_FOOD_PRODUCT, EVENT. 특정 이름을 차단 목록에 넣는 방식이 아니다.
2. 뉴스 제목에 **검색어가 실제로 등장해야** 근거로 쓴다. 조사만 허용하는 경계 정규식을 써서 '떡'⊂'어떡하나' 같은 부분 일치를 막았다.
3. 검색어에 **직접 붙은** 음식 범주 정의, 맛·식감·레시피 속성, 식품·메뉴 출시 연결만 근거로 인정한다. 인물 역할 뉴스와 금융·기업 뉴스는 제외한다. 나머지는 'background'로만 기록한다.
4. 점수: FOOD 70(검색어 자체가 음식 형태)·85(직접 정의)·75(브랜드+출시), REVIEW 40(속성)·25(농산물 무게·선발대회), 그 외 0. 확률이 아니라고 명시한다(`SCORE_BASIS`).
5. 저장 보강: `candidates`에 entity_type·rule_score·evidence_news를 추가했다. 결과가 바뀔 때만 `classification_history`에 남긴다(`Storage._classify`). 원본에서 다시 분류하는 `--reanalyze`(`Storage.reanalyze`)를 넣었다.

**실제 검증**
- 240건 전수 재분석 결과 FOOD 1→0, REVIEW 15→1(호박), NON_FOOD 224→239(`AUDIT_REPORT.md` §7, `data/audit-20261026/after-reanalysis/`, `historical-context-review.json`).
- 현재 DB(확인): 370건 **모두 `target-context-rules-v2`**이고 NON_FOOD 369 / REVIEW 1(호박, UNKNOWN, 25점) / **FOOD 0**이다. 유형 분포는 UNKNOWN 269, EVENT 31, ORGANIZATION 31, NON_FOOD_PRODUCT 17, LOCATION 14, PERSON 8이다.
- v2 이후 이력 11건은 모두 NON_FOOD→NON_FOOD이고 entity_type만 바뀌었다(예: 윤하 UNKNOWN→PERSON→UNKNOWN, 대신증권 UNKNOWN→NON_FOOD_PRODUCT→UNKNOWN). 같은 항목을 다시 관측할 때 뉴스가 바뀌면 개체 유형이 흔들린다는 뜻이다. 라벨은 그대로였다.
- 정밀도·재현율은 **평가하지 않았다**(사람 라벨 정답셋 없음). 문서도 같은 한계를 명시한다.

**그 밖의 결함과 수정**
- run 60(2026-10-26 02:21Z): `OperationalError: table observations has 4 columns but 3 values were supplied`. 마이그레이션 뒤 이전 프로세스가 위치 기반 INSERT를 한 것이 원인이다. 이력을 보존하고 포트 중복 바인딩을 방지했다(`AUDIT_REPORT.md` §8).
- 실패 5건(확인): run 1·2 TimeoutError(10-13, Python 3.12 환경), run 55·56 연결 거부(WinError 10061, 샌드박스), run 60(위의 스키마 오류).
- 수집기 자체(`collector.py`)는 감사 전 스냅샷 `before-collector.py`와 **바이트 단위로 같다**(줄바꿈 정규화 후 diff 0). 개선은 모두 분류·저장 쪽에서 이뤄졌다.

### 1.3 현재 상태와 수치

| 항목 | SQLite `data/trends.sqlite3`(확인) | Oracle `MZ_APP`(문서 근거) |
| --- | --- | --- |
| 회차 | 95(성공 90·실패 5), 첫 회차 2026-10-13T05:28:55Z | 이관 95 + 파일럿 1(`live_api`) |
| 마지막 성공 | **2026-10-28T04:49:48Z** | **2026-11-07T23:26Z**(파일럿, 항목 10·새 항목 10·후보 0) |
| 항목/관측/후보 | 370 / 900 / 370 | 원문 370+10(`live-storage-verification.json`의 `source_items_by_source_origin`) |
| 날짜별 회차 | 10-13 41, 10-14 7, 10-16 3, 10-26 29, 10-27 8, 10-28 7. **10-17~10-25는 공백**(앱 미실행) | — |
| 회차당 항목 | 성공 회차는 항상 10건(피드 상한) | 파일럿도 10건 |

**판정: 수집·저장·분류는 동작이 확인됐다.** 다만 실제 수집분 380건(SQLite 370 + 파일럿 10)에서 **FOOD 후보는 한 번도 나오지 않았다**. 그래서 "RSS → FOOD → 다른 수집원 자동 추적"으로 이어지는 전체 흐름은 실제 데이터에서 관찰되지 않았다. 문서도 같은 결론이다(`NAVER_LIVE_VERIFICATION.md`, `AUDIT_REPORT.md` §9).

---

## 2. Instagram (Meta Graph API 해시태그 검색)

### 2.1 구현 사실

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 인증 방식 | Facebook Login 경로의 **USER 액세스 토큰**. 헤더는 `Authorization: Bearer`, Graph `v26.0`. 토큰 검사는 `debug_token`(App ID\|Secret 앱 토큰)이고 민감 파라미터는 batch POST 본문에 담아 URL에 노출하지 않는다 | `instagram_graph.GraphClient.get()`, `instagram_service.check_token()` |
| 필요 권한 | `instagram_basic`, `pages_show_list`, `pages_read_engagement`. 셋 다 있고 앱이 일치해야 success다 | `check_token()` |
| 계정 연결 확인 | `me/accounts`에서 설정 페이지의 `instagram_business_account.id`가 설정 IG User ID와 같은지 본다 | `check_connection()` |
| 해시태그 조회 | `ig_hashtag_search?user_id&q` → 첫 결과의 숫자 ID를 캐시(`instagram_hashtags.instagram_id`). 이후에는 검색을 생략한다 | `GraphClient.search()`, `_collect_tag()` |
| 게시물 조회 | **`{hashtag_id}/recent_media`만 쓴다.** fields=`id,caption,timestamp,media_type,permalink`, limit=50. **`top_media`는 코드 어디에도 없다**(grep 0건) | `GraphClient.recent()` |
| 페이지네이션 | `paging.next` URL을 따라가지 않고 고정 Graph URL에 `after` 커서만 넣어 다시 만든다. 커서가 반복되면 `invalid_response`. 회차당 최대 `INSTAGRAM_MAX_PAGES`(기본 20) | `GraphClient.pages()` |
| 30개/7일 한도 | (1) 로컬 원장 `instagram_searches`(태그를 처음 요청할 때 **요청 전에** 예약, 7일 창 안에서는 재예약 안 함). (2) 제공자 원장 `recently_searched_hashtags`(최대 3쪽, 5분 신선도). 두 집합의 합집합이 소프트 상한 `INSTAGRAM_SEARCH_SOFT_LIMIT`(기본 28) 이상이면 새 태그를 막는다(`budget`) | `instagram_service.reserve()`, `budget()`, `sync_budget()`; `instagram_storage.record_usage()` |
| 오류 분류 | 102/190·401→authentication, 4/17/32/613/80004/80000·429→rate_limit, 10/200/294·403→permission, 5xx→upstream. 네트워크·rate_limit·upstream만 최대 3회 지수 백오프(Retry-After가 30초를 넘으면 중단). Meta 오류 원문은 저장하지 않고 숫자 코드만 남긴다 | `GraphClient.error()`, `get()` |
| 토큰 만료 | 검사 결과(`expires_at`)를 `instagram_state`에 둔다. 상태 조회 때 로컬에서만 계산한다: 7일 이내면 경고, 만료되면 자동 수집 중단(`token_blocked()`). **자동 갱신은 없다.** `--instagram-exchange-token`은 `fb_exchange_token`으로 교환해 **새 `.secret` 파일에만** 쓰며, 사람이 직접 `.env`에 반영해야 한다 | `token_status()`, `token_blocked()`, `instagram_tokens.exchange()` |
| 동시 실행 방지 | 계정별 DB lease 5분(요청 전마다 갱신). 다음 획득 때 남은 `running` 회차를 `interrupted`로 마감한다 | `instagram_storage.acquire()/renew()` |
| 중복 제거 | `instagram_media.id`(Meta 미디어 ID) PK upsert + `instagram_hashtag_media(hashtag_id,media_id)` 다대다. 신규 수는 태그별 최초 관측 기준 | `instagram_storage.finish_run()` |
| 저장 필드 | id, published_at(UTC), first_seen/last_seen, media_type, permalink(instagram.com https만), caption, caption_captured_at. 캡션은 30일 보관(`INSTAGRAM_CAPTION_RETENTION_DAYS`) | `clean_media()`, `finish_run()` |
| 파생 분석 | 캡션 속 해시태그만 추출한다(`caption-hashtags-v1`, 외부 호출 없음). 공동 출현 태그는 `instagram_caption_tags`·`instagram_observation_tags` | `instagram_analysis.py` |
| 주기 | `INSTAGRAM_COLLECTION_INTERVAL_MINUTES` 기본 60분. 60초마다 due 여부를 확인하고, 1시간마다 캡션 만료 정리 | `InstagramService.start()`, `due()` |

### 2.2 초기 구현 → 문제 → 수정 → 검증

1. **초기**: 토큰을 넣기 전에는 Mock transport와 미연결 상태 화면만 있었다(`data/instagram-mock-preview.png`, `instagram-unconfigured-*.png`).
2. **문제 1 — 민감 파라미터가 URL에 노출됨**: `debug_token`과 토큰 교환이 URL 쿼리를 썼다. **수정**: batch POST 본문으로 옮기고 URL 방식으로의 폴백을 없앴다(`INSTAGRAM_LIVE_VERIFICATION.md` "보완한 부분").
3. **문제 2 — 단기 토큰**: 첫 실검증 토큰은 2026-10-26 07:00Z에 만료되는 단기 USER 토큰이었다. **수정**: `fb_exchange_token`으로 교환해 `.secret`에 저장했다. 메타데이터상 `expires_in` 5,165,662초(약 59.8일), `long_lived: true`, 새 만료 **2026-12-25T00:55:03Z**(`data/instagram-long-lived-validated.metadata.json`). 사용자 승인 뒤 `.env`에 적용했다(`INSTAGRAM_TOKEN_APPLICATION.md`).
4. **문제 3 — 화면 버튼 잠김**: 수집이 끝난 뒤에도 버튼이 잠겨 있었다. `static/instagram.js`에서 고쳤다.
5. **실제 검증**: 10-26 05:25Z `#두쫀쿠` 최초 수집에서 67건이 반환·저장됐고 20쪽 상한에 걸려 `limited`가 됐다. 재조회에서는 50건 반환·신규 0을 확인했다(`data/instagram-live-verification.json` `status=passed`).

### 2.3 저장 데이터 대조 (SQLite, 확인)

| 항목 | 값 |
| --- | --- |
| 해시태그 | 1개(`두쫀쿠`, active, 승인 10-26 05:25Z). 7일 검색 원장 1건(10-26) |
| 회차 16 | limited 12, failed 2(07·08번, `network`), **success 1**(11번, 10-27 08:09Z, 50건, 다음 쪽 없음), **running 1**(16번) |
| 게시물 | **185건**, 게시 시각 2026-10-25T05:57Z ~ 10-28T04:06Z, 최초 관측 10-26 05:27Z ~ 마지막 10-28 04:18Z. 캡션 있음 184 / 없음 1. 캡션 정리(purge) 0 |
| 파생 | caption_analysis 185, analysis_observations 700, caption_tags 635, observation_tags 2,386. metrics(day 3·hour 11) 신규 합계 185 = 게시물 수 |
| 마지막 성공(limited 포함) | **2026-10-28T04:18:46Z**(15번) |
| 토큰 상태 | valid, long_lived, `expires_at` 2026-12-25T00:55:03Z. 2026-11-13 기준 **약 42일 남음**. 연결 상태 `connected`(10-28 04:18Z) |
| 요청 효율 | 회차마다 약 20~22번 요청에 50~72건 반환. recent_media가 페이지당 평균 3건 정도만 돌려줘서 20쪽 상한에 자주 닿는다(`limit=50`과 실제 반환 수가 다르다) |

**'running' 상태로 멈춘 16번 회차의 원인 분석**
- 16번은 05:19:56Z에 `start_run()`으로 행이 생겼지만 `request_count=0`이고 `finish_run()`이 호출되지 않았다. lease 행(만료 05:25:00Z)이 그대로 남아 있다.
- DB 파일 mtime(05:20Z)과 겹치므로 **회차 시작 직후 구버전 서버 프로세스가 끝났다**(종료·절전·강제 종료 중 어느 것인지는 **미확인**). 11-02 G-OPS-0 보고서는 "현재 서버 정지 상태"라고 적었다.
- 코드에는 복구 경로가 있다. 다음 `acquire()`가 `running`을 `interrupted`로 마감한다. 그러나 이 DB는 v0라 현재 코드가 열 수 없고, 그 뒤로 이 DB에 Instagram 작업이 다시 실행된 적이 없다. 그래서 **영구히 running으로 남았다.** 결함이라기보다 "마지막 프로세스 중단 흔적"이다.
- Oracle로 옮길 때 이 회차를 어떻게 처리했는지는 MIGRATION_REPORT에 명시가 없다(**미확인**). Instagram 185건은 "출처 불명 논리 회차"로 이관됐다(`MIGRATION_REPORT.md` 36·86행).

### 2.4 현재 상태
- **실호출과 저장은 성공했다**(SQLite 10-26~10-28, Oracle 파일럿 11-07 23:26Z: 요청 3번 — 최근 검색 1·해시태그 조회 1·recent_media 1쪽, 모두 200 → `LIMITED`, 새 게시물 40, 관측 1건 `OBSERVED`).
- Oracle 원문: Instagram legacy 185 + live 40 = 225(`live-storage-verification.json`). 승인 음식 `두쫀쿠`와 연결된 활성 언급 143건.
- **자동 수집: 꺼져 있다.** 파일럿 설정은 `INSTAGRAM_COLLECTION_ENABLED=false`였고, 운영 서버는 수집 없음 모드다. 루트 `.env`에는 `INSTAGRAM_COLLECTION_ENABLED=true`가 남아 있지만, 이 값은 v0 DB를 쓰는 SQLite 경로에만 영향을 주며 그 경로는 지금 시작할 수 없다.
- 남은 위험: (a) 토큰 만료가 2026-12-25이고 자동 갱신은 없다. (b) SQLite v0 DB와 백업 사본(`data/ops-backup-20261107T153742Z/`, `data/*-preservation-*/`)의 캡션 184건은 수집 후 30일(2026-11-25~27)이 지나도 정리할 프로세스가 없다. `--maintenance`도 v0에서는 거부된다. (c) `config.py` 기본값에 실제 IG User ID·Page ID가 하드코딩돼 있다. 비밀은 아니지만 보고서에서는 가리는 값이다.

---

## 3. YouTube Data API v3

### 3.1 구현 사실

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 인증 | API 키를 헤더 `X-Goog-Api-Key`로 보낸다(URL·로그·DB에는 없음). `YOUTUBE_API_KEY`가 없으면 `YOUTUBE_DATA_API_V3_KEY`(실제 `.env`는 이쪽을 쓴다는 것이 문서 주장). 리디렉트 거부 | `youtube.YouTubeClient.get()`, `config.YouTubeConfig.from_env()` |
| search.list | `part=snippet,type=video,regionCode=KR,relevanceLanguage=ko,order=date,maxResults=50,publishedAfter/Before,q`. 영상 ID만 얻는 용도다 | `youtube_service._search()` |
| videos.list | 50개씩 `part=snippet,statistics,contentDetails,status`. 실제 메타데이터는 여기서 받는다. 비공개·삭제·거부된 영상은 저장하지 않고 지운다 | `_details()`, `youtube_storage.save_videos()` |
| 검색어 선정 | (1) **발굴**: `YOUTUBE_DISCOVERY_QUERIES`, 기본 6개(`신상 디저트, 유행 음식, 요즘 먹방, 편의점 신상, 신상 간식, 카페 신메뉴`). 배포 설정이며 API 데이터에서 뽑지 않는다. (2) **추적**: RSS FOOD 후보의 이름과 명시적 별칭(`naver_service.keyword_group`). 회차당 최대 `queries_per_run` 8개, `last_attempt`가 오래된 순으로 돌린다 | `config.py`, `youtube_storage.sync_queries()`, `collect()` |
| 체크포인트 | 검색어마다 after/before/page_token을 저장한다. 완료하면 before−10분을 watermark로 삼는다. 토큰은 7일 뒤 버린다 | `_search()`, `purge()` |
| 할당량 | 자체 예산은 PT 하루 search 40회·videos 1,000회. **요청 시도마다**(재시도 포함) `youtube_calls`에 미리 예약한다. 단가는 search 100 units·videos 1 unit이므로 상한은 4,000+1,000=5,000 units/일 < 기본 10,000. 기본 운영(6시간 간격 × 최대 8 검색)은 32회 = 3,200 units + videos 수십 units 정도다. quotaExceeded면 해당 endpoint를 PT 다음 날까지 멈추고, 429면 60초, 인증·권한 오류면 6시간 쉰다 | `youtube_storage.reserve()/finish()`, `pacific_day()` |
| 주기 | `YOUTUBE_INTERVAL_SECONDS` 기본 21600(6시간, 그보다 짧으면 설정 거부). 60초 틱. `next_run_epoch`를 저장해 재시작 직후 다시 수집하지 않는다 | `config.__post_init__`, `start()` |
| 중복 제거 | `youtube_videos.video_id`(11자 정규식 검증) PK upsert. 검색어↔영상 관계는 `youtube_matches`(다대다) | `save_videos()`, `match()` |
| TTL | 수집·갱신 시각부터 **29일**(`expires_at`). 하루 지난 영상은 최대 500개씩 다시 조회해 갱신한다. `purge()`는 수집 시작·끝, 60초 스레드, `--maintenance`에서 돈다 | `save_videos()`, `_refresh()`, `purge()` |
| 메타데이터만 | 제목·설명·게시 시각·채널·썸네일 URL·조회/좋아요/댓글 수(문자열로 정밀도 보존)·길이·공개 상태. **영상·자막·댓글 본문은 요청하지 않는다**(코드상 endpoint는 `search`·`videos` 두 개만 허용) | `parse_video()`, `get()` |
| 파생 분석 | **비활성.** `food_extraction: 'disabled_pending_policy_approval'`, `policy.food_extraction_enabled/content_classification_enabled/derived_metrics_enabled = False`. 환경변수로 켤 수 없다. FOOD 연결은 "검색어 출처(search_match_unverified)"일 뿐이다 | `collect()` 결과·`status()` |

### 3.2 초기 구현 → 문제 → 수정 → 검증
1. **초기**: 10-26 06:03Z 첫 시도는 샌드박스 연결 거부로 search 3번이 모두 `network`였다. 06:22Z 수동 수집에서 search 6·videos 6번이 HTTP 200을 받아 253개를 저장했지만, **호출 출처를 기록하지 않았다**.
2. **발견된 문제**(`YOUTUBE_VERIFICATION.md`): UI에는 9/40·6/1,000으로 표시되는데 이전 보고서는 "0개"였다. 실패 시점에 고정된 보고서와 실제 DB가 어긋난 것이다. 응답 수·중복·HTTP 코드도 기록되지 않았다.
3. **수정**: `youtube_runs`와 `youtube_calls.origin/run_id/result_count`, `youtube_video_provenance`를 추가했다. 출처는 `live_api`/`mock`/`legacy_unknown`으로 나눈다. HTTP 요청 0번이면 `no_request`로 기록한다. 소량 검증기 `youtube_verify`는 search 1+videos 1, 최대 5건이다.
4. **실제 검증**: 10-26 06:31Z 검증 회차 `56dcbcab…`에서 search 1·videos 1번 모두 200, 5건이 갱신됐다.

### 3.3 저장 데이터 대조 (SQLite, 확인)

| 항목 | 값 |
| --- | --- |
| 검색어 | 6개, 모두 discovery. **tracking 0**(FOOD 후보가 없어서). `youtube_foods`·`youtube_candidate_foods` 0 |
| 회차 | 3: verification(10-26 06:31Z, success), automatic(10-27 05:06Z, `network`), automatic(10-28 01:11Z, success) |
| 호출 38 | search 19(legacy_unknown 성공 6·network 3, live_api 성공 7·network 3) + videos 19(legacy_unknown 6, live_api 13, 모두 200). 즉 HTTP 200은 32번, 응답 없음은 6번 |
| PT 날짜별 시도 | 10-25: search 10·videos 7, 10-26: search 3, 10-27: search 6·videos 12. 성공 기준 추정치는 10-25 약 707 units, 10-27 약 612 units. 응답 없는 시도가 차감됐는지는 **미확인** |
| 영상 | **431개**, `collected_at` 10-26 06:22Z ~ 10-28 01:11Z. provenance는 **431개 모두 `live_api`**. 10-28 회차의 `_refresh()`가 하루 지난 영상을 모두 다시 조회했기 때문이다 |
| 만료 | `expires_at`이 **431개 모두 2026-11-26T01:11Z**다(10-28 갱신으로 연장됨). **2026-11-13 현재 TTL을 넘긴 영상은 0개**다 |
| 검색 일치 | `youtube_matches` 473 |
| 마지막 성공 | **2026-10-28T01:11:39Z** |

**문서와 다른 점**
- `YOUTUBE_VERIFICATION.md`는 "248개 legacy_unknown"이라고 썼지만 이는 10-26 시점 스냅샷이다. 현재 DB의 출처는 431/431 `live_api`다. 모순이 아니라 이후 갱신의 결과다.
- 감사 의뢰서의 전제("11-13까지 TTL을 많이 넘겼을 수 있음")는 **사실이 아니다**. 만료는 11-26이다. 다만 v0 DB는 현재 코드가 열 수 없어 **11-26 이후 `purge()`를 실행할 프로세스가 없다**. 운영 사본·백업 사본도 같다(A1-A3 보고서 122행이 운영자의 수동 삭제 책임을 경고한다).
- `YOUTUBE_INTEGRATION.md`와 `status()`의 문구 "기본 search 별도 100회/일"은 공식 단가표(search.list 100 units)와 표현이 다르다. 별도 버킷이 실제로 있는지는 공식 문서와 대조하지 않았으므로 **미확인**이다. 코드는 두 가지 해석 모두에서 안전한 보수적 예산(40회)을 쓴다.

### 3.4 현재 상태
- Oracle 파일럿(11-07 23:26Z): search 1·videos 1 = 101 units, `LIMITED`(1쪽), 영상 50건 저장(29일 만료), 음식 언급 파생 0.
- Oracle의 YouTube 원문 431건은 "스테이징 경로가 없어 원문만 이관"됐고 만료는 2026-11-25경이다(`MIGRATION_REPORT.md` 36·87행). `source_items`에는 YouTube live 50건만 있다.
- **자동 수집: 꺼져 있다**(파일럿 `YOUTUBE_COLLECTION_ENABLED=false`, 운영 수집 없음 모드).

---

## 4. NAVER DataLab 검색어 트렌드

### 4.1 구현 사실

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 엔드포인트 | `POST https://naverapihub.apigw.ntruss.com/search-trend/v1/search` — **NAVER Cloud API HUB 경로**다. 흔히 알려진 `openapi.naver.com/v1/datalab/search`가 아니다 | `naver_datalab.ENDPOINT` |
| 인증 헤더 | `X-NCP-APIGW-API-KEY-ID`, `X-NCP-APIGW-API-KEY`(`NAVER_DATALAB_CLIENT_ID/SECRET`). 리디렉트 거부 | `NaverClient.search()` |
| 요청 본문 | `startDate, endDate, timeUnit(date/week/month), keywordGroups[1~5]{groupName, keywords[1~5]}` + 선택 필터(device/gender/ages). 종료일은 KST 어제, 시작일은 종료일−(days−1), 2016-01-01 이후만 허용 | `request_body()` |
| 기간 | 내부적으로 1~3660일을 허용하지만, API(`api.py` 223행)와 CLI(`--naver-days`)는 **7/30/90**만 받는다 | `api.py`, `__main__.py` 261행 |
| 키워드 그룹 | 음식명과 **명시적 별칭을 한 그룹**에 넣는다. `ALIASES = {'두쫀쿠': ['두쫀쿠','두바이 쫀득 쿠키','두바이쫀득쿠키']}`. 별칭이 없으면 정규화한 이름 하나. 퍼지 매칭은 없다 | `naver_service.keyword_group()` |
| 응답 해석 | `period`·`ratio`(0~100 유한수) 검증. 요청한 기간·그룹·키워드와 정확히 일치해야 한다. **상대 지수**다. 요청마다 기준(최댓값=100)이 다르므로 다른 요청끼리, 다른 기간끼리 비교하지 않는다는 안내가 있다 | `parse_response()`, `status()['notice']` |
| 저장 여부 | **응답 값과 시계열은 DB에 저장하지 않는다(메모리 전용).** 저장하는 것은 `naver_calls`(요청 본문·지문·상태·HTTP 코드), `naver_targets`(대상 메타), `naver_guards`(중복 잠금)뿐이다. 응답은 `Cache-Control: no-store`. 자동 수집은 결과를 버리고 success/status만 남긴다 | `naver_service._query()`, `collect()` |
| 중복 방지 | 요청 본문의 sha256 지문 잠금 10분(실행 중) → 끝나면 `cooldown_seconds` 60초. 429면 전체(`*`) 60초 이상. KST 하루 자체 한도 `NAVER_DATALAB_DAILY_LIMIT` 100 | `_query()` |
| 재시도 | network·5xx·429에 최대 2번 재시도(Retry-After가 8초를 넘으면 즉시 중단) | `NaverClient.search()` |
| 자동 범위 | 대상은 RSS `classification='FOOD'` 후보(차단 유형 제외)만이다. 24시간(86400초)에 한 번, 5그룹씩 배치. **수동 범위**: `--naver-keyword`/화면 "사용자 지정 음식"(`origin=user_keyword`, RSS 후보를 만들지 않음, 자동 대상 아님) | `collect()`, `query_keywords()` |

### 4.2 초기 구현 → 문제 → 수정 → 검증
1. **초기**(`NAVER_DATALAB_INTEGRATION.md`): FOOD 후보 기반 자동 조회만 있었다. 실제 FOOD가 0개라 실데이터로 검증할 대상이 없었다.
2. **문제 1 — 한글 인코딩 깨짐(확인)**: `naver_calls` 1~4번 요청 본문의 그룹명이 `"???"`와 `"??? ?? ??"`로 저장돼 있다. PowerShell 표준입력에서 한글이 `?`로 바뀐 것이다. 1~3번은 네트워크 실패(첫 시도 + 재시도 2번), **4번은 깨진 `???` 검색어로 HTTP 200을 받았다**. 게다가 별칭이 인식되지 않아 `두쫀쿠`와 `두바이 쫀득 쿠키`가 **서로 다른 2개 그룹**으로 나가는 별칭 분리·중복 요청이 됐다.
   **수정**: UTF-8 소스 파일 `food_trends/naver_verify.py`로 실행하게 했다. 5번부터는 3개 별칭이 한 그룹(`두쫀쿠`)으로 올바르게 나갔다.
3. **문제 2 — FOOD 후보가 없어 조회 불가**: **수정**: `query_keywords()`와 화면의 사용자 지정 음식 경로를 추가했다(`NAVER_LIVE_VERIFICATION.md`).
4. **문제 3 — 같은 요청과 별칭 재요청**: 같은 지문이면 409 `duplicate`로 막는다. 그러나 잠금이 **60초뿐이라** 같은 30일 `두쫀쿠` 요청(지문 `68ab73e7…`)이 05:23, 05:25, 05:32, 05:54Z에 **4번** 나갔다(확인). 60초 이내 재요청만 막고, 같은 날 반복 호출은 막지 않는다.
5. **실제 검증**: 7/30/90일 각각 HTTP 200, 지점 7/30/90개, 그래프 값이 응답과 일치(`data/naver-live-verification.json`).

### 4.3 저장 데이터 대조 (SQLite, 확인)

| 항목 | 값 |
| --- | --- |
| 호출 | **10건, 모두 2026-10-26**(05:22~05:54Z). success/200 7건, network 3건 |
| 내용별 | 깨진 `???` 요청 4건(network 3 + 200 1), 정상 `두쫀쿠` 그룹 6건(7일 1·30일 4·90일 1) |
| `naver_targets` | **0**(FOOD 후보 없음) |
| `naver_guards` | 5(모두 오래전 만료) |
| 마지막 성공 | 2026-10-26T05:54:59Z |

**문서와 다른 점**: `NAVER_LIVE_VERIFICATION.md`와 `naver-live-verification.json`은 "로컬 시도 8건, HTTP 200 5건"이라고 썼다. DB에는 **10건, 200 7건**이 있다. 9·10번(05:32Z, 05:54Z, 같은 30일 요청)은 보고서 작성 뒤에 추가됐다. 보고서는 "재시작 검사 NAVER 추가 호출 0회"라고 했으므로 이 두 건은 다른 수동 조회로 보인다. **누가 어떤 경로로 보냈는지는 미확인**이다.

### 4.4 현재 상태
- Oracle 파일럿: 요청 1번, 200, `SUCCEEDED`. **값·본문 저장 0**(정책상 정상). `naver_values_stored: 0`(`live-storage-verification.json`). Oracle 경로에서도 `NaverExecutor.persist()`가 값을 메모리에서 품질 검사만 하고 쓰지 않는다(`execution_sources.py`).
- 결과적으로 **NAVER 지수는 어느 DB에도 누적되지 않는다.** "시계열 추이 축적"은 설계상 없는 기능이고, 화면 조회 때마다 다시 요청한다.
- **자동 수집: 꺼져 있다**(파일럿 `NAVER_DATALAB_ENABLED=false`). 자동 대상(FOOD)도 실제로 0개였다.

---

## 5. Oracle 실행 경로와 컷오버 실호출 7회

### 5.1 구조 (코드 확인)
- `food_trends/oracle_live.py` `oracle_live_backend()`: `oracle_live_readiness()`(registry 승인·EXCLUSIVE Writer·사용량 원장)를 통과해야만 실제 클라이언트(`GraphClient`, `YouTubeClient`, `NaverClient`, `collector.fetch`)를 붙인다. 통과하지 못하면 `FoodError 403`이다. **조립만 하고, 아무것도 시작하거나 보내지 않는다.**
- `food_trends/execution_sources.py`: 기존 클라이언트와 파서를 재사용하되 `single_attempt=True`로 B3 실행기가 할당량과 재시도를 관리한다. 검색어는 RSS FOOD가 아니라 **승인된 추적 대상(B1/B2 tracking → planner)**에서 온다.
  - `YouTubeExecutor`: search → videos 체크포인트, 29일 보존.
  - `InstagramExecutor`: 실호출 전에 `recently_searched_hashtags`를 최대 3쪽 확인하고 `min(30, soft_limit)`를 넘으면 `budget`(7일 대기). lookup → recent_media 페이지 체크포인트. 같은 계정·API 버전·정책·태그 조합으로 이미 조회했으면 원격 확인 결과를 15분 동안 재사용한다(`remote_usage_check_needed`).
  - `NaverExecutor`: 값을 저장하지 않는다.
- RSS: `OracleRssService`가 매 수집 전에 `gate()`로 readiness를 다시 확인한다. 예약 실행은 `--execution-mode live`에서 `--no-schedulers` 없이 켤 때만 돈다(`__main__.oracle_command`).

### 5.2 2026-11-07 23:25:50~23:26:20Z 파일럿 (`live-pilot.json`)

| 수집원 | 상한 | 실제 전송 | HTTP | 결과 |
| --- | ---: | ---: | --- | --- |
| Google Trends RSS | 1 | 1 | 200 | 항목 10·새 항목 10·후보 0 |
| Instagram Graph | 5 | 3 | 200×3 | LIMITED, 새 게시물 40 |
| YouTube | 2 | 2 | 200×2 | LIMITED, 영상 50 |
| NAVER DataLab | 1 | 1 | 200 | SUCCEEDED, 값 저장 0 |
| **합계** | **9** | **7** | **200×7** | 상한 때문에 거부된 요청 0, Live 라운드 중 SQLite 연결 0 |

- 사용 설정: `B3_AUTOMATIC_ENABLED=false`, 수집원별 자동 수집 플래그 모두 false, `INSTAGRAM_MAX_PAGES=1`, `INSTAGRAM_SEARCH_SOFT_LIMIT=1`, YouTube·NAVER 일일 1.
- Writer는 4개 수집원 모두 `ORACLE`(EXCLUSIVE)이다. 제공자 측 사용량은 모두 **UNKNOWN**이며 `PROVIDER_CONFIRMED`는 만들지 않았다.
- 무결성: FK 오류 0, 정책 위반 행 0, 만료 행 0. 품질 PASS 656·WARN 38·N/A 10(`live-storage-verification.json`).
- **이 7회가 현재 SoR에 남은 마지막 실제 수집이다.** 그 뒤 `data/ops-prod/logs/admin.log`(11-08 18:26 KST)는 "수집 없음(관리·관측)" 모드로 떠서 GET만 받았다. 11-08 이후 Oracle 행 수는 접속하지 않았으므로 **미확인**이다.

---

## 6. 요약표 — 수집원별 최종 상태

| 수집원 | 데이터 형식 | 주요 테이블(SQLite v0) | 누적 행(SQLite) | SQLite 마지막 성공 | Oracle 마지막 성공 | 자동 수집 | 완성도 판정 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| RSS | RSS 2.0 XML → JSON payload, 원본 바이트 보존 | runs, items, observations, candidates, classification_history | 회차 95 / 항목 370 / 관측 900 / 후보 370 / 이력 251 | 2026-10-28 04:49Z | 2026-11-07 23:26Z | 꺼짐(30분 예약은 구현됨) | 수집·분류 동작 확인. **실제 FOOD 0건** |
| Instagram | Graph JSON → 정제 필드(캡션 30일) | instagram_hashtags, _runs, _media, _hashtag_media, _searches, _metrics, 분석 5종 | 태그 1 / 회차 16 / 게시물 185 | 2026-10-28 04:18Z | 2026-11-07 23:26Z | 꺼짐(60분 구현됨) | 실호출·저장 확인. 태그 1개(`두쫀쿠`)만, `recent_media`만 사용 |
| YouTube | search/videos JSON → 영상 스냅샷(29일 TTL) | youtube_queries, _calls, _runs, _videos, _matches, _video_provenance | 검색어 6 / 회차 3 / 호출 38 / 영상 431 / 일치 473 | 2026-10-28 01:11Z | 2026-11-07 23:26Z | 꺼짐(6시간 구현됨) | 메타데이터 수집 확인. 추적 검색어 0, 파생 분석 비활성 |
| NAVER | JSON 상대 지수(메모리 전용) | naver_calls, naver_targets, naver_guards | 호출 10 / 대상 0 / 잠금 5 | 2026-10-26 05:54Z | 2026-11-07 23:26Z | 꺼짐(24시간 구현됨) | 수동 조회 확인. **값은 설계상 저장하지 않음**, 자동 대상 0 |

---

## 7. 문서와 다르거나 주의할 점 (요약)

1. **운영 SQLite가 v0이고 현재 코드로 열 수 없다.** 그래서 YouTube TTL 정리(11-26 이후)와 Instagram 캡션 30일 정리(11-25~27 이후)를 자동으로 할 수 없다. 백업·보존 사본도 같다. 문서는 "이중 Writer 방지"라는 장점만 강조한다(LIVE_VERIFICATION.md).
2. **Instagram 16번 회차가 'running'인 이유**는 회차 시작 직후 구버전 프로세스가 끝났기 때문이다(05:19:56Z 시작, 파일 마지막 쓰기 05:20Z, lease 만료 05:25Z). 자동 `interrupted` 마감 코드는 있지만 이 DB에서는 다시 실행되지 않았다.
3. **YouTube TTL은 아직 지나지 않았다.** 431개 모두 2026-11-26 만료이고, 10-28 갱신으로 연장됐다. 문서의 "legacy_unknown 248개"는 지금 0개다.
4. **NAVER 호출 수**: 문서는 8건(200 5건), DB는 10건(200 7건)이다. 추가 2건의 출처는 미확인이다. 깨진 `???` 요청 4건 가운데 1건은 실제로 200을 받았다. 같은 요청의 60초 잠금은 같은 날 반복 호출(4회)을 막지 못했다.
5. **Instagram은 `top_media`를 쓰지 않는다**(`recent_media`만 사용). recent_media가 페이지당 평균 약 3건만 반환해 20쪽 상한에 자주 걸린다(`limited` 12/16).
6. **RSS 피드 URL은 설정이 아니라 코드 상수**다. 여러 피드나 다른 지역은 지원하지 않는다.
7. **"RSS FOOD → NAVER/YouTube 자동 추적" 경로는 실제 데이터로 한 번도 실행되지 않았다**(FOOD 0). Oracle 파일럿의 추적은 사람이 승인한 음식(`두쫀쿠`)으로 수행했다.
8. Git 이력은 2026-10-27부터만 있다. 그 전의 구현 과정(v1→v2 분류기, 토큰 교환, YouTube 출처 기록 등)은 Git으로는 증명할 수 없고 루트 보고서와 `data/audit-20261026/before-*.py`로만 뒷받침된다.

---

## 8. 근거 파일 목록

- 코드: `food_trends/collector.py`, `classifier.py`, `service.py`, `storage.py`, `config.py`, `migrations.py`, `instagram_graph.py`, `instagram_service.py`, `instagram_storage.py`, `instagram_tokens.py`, `instagram_analysis.py`, `youtube.py`, `youtube_service.py`, `youtube_storage.py`, `naver_datalab.py`, `naver_service.py`, `execution_sources.py`, `execution_ownership.py`, `oracle_live.py`, `oracle_rss.py`, `__main__.py`
- 커밋: `97b3c35`(2026-10-27, 4개 수집원 코드 일괄 도입), `59376f2`(10-28, B3 단일 Writer 연결), `051e310`·`594f47b`(10-28, 스테이징·관측 통합), `8f293ca`(11-02, DB 경로), `8d481a7`·`f88c94c`·`3b8c7cb`(11-05, Oracle RSS·Live 경로·Writer 철회), `dea4b33`(11-08, 운영 접근)
- 데이터: `data/trends.sqlite3`(읽기 전용 조회), `data/audit-20261026/`(before-*.py, after-reanalysis, historical-context-review.json), `data/instagram-live-verification.json`, `data/instagram-long-lived-validated.metadata.json`(토큰 값 제외 필드만), `data/naver-live-verification.json`, `data/ops-prod/logs/admin.log`
- 문서: `VERIFICATION.md`, `AUDIT_REPORT.md`, `INSTAGRAM_LIVE_VERIFICATION.md`, `INSTAGRAM_TOKEN_APPLICATION.md`, `INSTAGRAM_INTEGRATION.md`, `YOUTUBE_VERIFICATION.md`, `YOUTUBE_INTEGRATION.md`, `NAVER_LIVE_VERIFICATION.md`, `NAVER_DATALAB_INTEGRATION.md`, `reports/development-a1-a3-2026-10-27/REPORT.md`, `reports/development-b4-b5-2026-10-28/REPORT.md`, `reports/development-g-ops-0-2026-11-02/REPORT.md`, `reports/development-oracle-production-cutover-2026-11-07/{LIVE_VERIFICATION.md, MIGRATION_REPORT.md, live-pilot.json, live-storage-verification.json, production-sqlite-final.json}`
