# 실행 검증 결과 — 2026-10-13 KST

## 실제 수집

- Google Trends KR RSS HTTP 200, 초기 원본 응답 19,764 bytes, 10개 항목 파싱.
- 앱 수집기로 HTTP 200 및 SQLite 저장 성공. 서버 시작 즉시 수집 성공.
- Chrome의 실제 “지금 수집” 버튼 클릭으로 HTTP 200 재수집 성공.
- 마지막 검증 수집 시각: 2026-10-13 14:32:19 KST (`2026-10-13T05:32:19.206078+00:00`).
- 최종 저장 항목 10개, FOOD 0개, REVIEW 0개, NON_FOOD 10개.
- 같은 RSS 3회 성공 수집: 항목은 10개 유지, 관측은 30개. 마지막 신규 0개.
- 앞선 Python 3.12 시도에서 2회 타임아웃. 실패 이력 보존, 이후 Anaconda Python 3.11.7로 실제 수집 성공. 기본 `python`이 MySQL 번들인 문제를 회피하는 `run.ps1` 제공.

실제 검색어: 정웅인, 정대세, 은행, rj 마이트, 파주시, 농협, 건설업, 펄어비스, 헌법재판소, 심정지.

이번 응답에는 음식 관련으로 분류된 실제 후보가 없습니다. 농협/은행 등의 이름을 임의로 음식으로 분류하지 않았습니다. RSS에 음식 항목이 없거나 문맥 신호가 부족하면 후보가 비어 있을 수 있습니다. 음식 후보가 실제로 나타나는 상황의 실데이터 분류 검증은 이번 응답으로 할 수 없었으며, 신규 음식명/문맥 분류 동작은 자동화 테스트로 확인했습니다.

제공 필드: title, pubDate, approx_traffic, description, link, picture, picture_source, news_item. 관련 뉴스는 제목/URL/출처가 제공됐고 snippet은 빈 값입니다. 관련 검색어 필드는 이번 응답에 없습니다. 원본 전체 응답은 DB runs.raw_response에, 항목 XML은 payload에 저장됩니다.

## 검증 범위

- 자동화 테스트 6개 통과: RSS 필드/미제공 값/UTC 변환, FOOD·REVIEW·NON_FOOD 및 미등록 이름 문맥 분류, 중복 및 다른 게시 시점 재등장, 원본 재분석, 실패 시 기존 데이터 유지, 동시 실행 차단, API/수동 POST.
- 오류 테스트: 타임아웃, HTTP 503, HTML로 바뀐 응답. 모두 실패 상태를 기록하며 기존 항목 조회 유지. 테스트는 임시 DB만 사용.
- 실제 Chrome 브라우저: HTML과 JS 실행, 전체 항목의 검색어 목록이 실제 API/SQLite와 일치, 키워드 검색, 상세 창, 원본 XML, 수동 수집 버튼 및 신규 0개 확인.
- FOOD 기본 화면에서 후보가 없다는 문구 표시 확인. FOOD/REVIEW 실제 행은 이번 RSS에 없으므로 실데이터 화면 검증 범위에서 제외.
- JavaScript `node --check`, Python 컴파일 확인.
- QA 세부 결과: `verification.json` (실행 환경 산출물).

## 파일과 확장

생성한 소스: `food_trends/__init__.py`, `__main__.py`, `collector.py`, `classifier.py`, `storage.py`, `service.py`, `api.py`; `static/index.html`, `app.js`, `style.css`; `tests/test_mvp.py`, `tests/browser_check.py`; `run.ps1`, `README.md`, `VERIFICATION.md`, `.gitignore`.

실행 산출물: `data/trends.sqlite3`, `live-rss.xml`, `verification.json`, 서버 로그. QA 브라우저 프로필은 data 내부에 두며 앱 실행에는 필요하지 않습니다. `browser_check.py`의 websocket-client는 기존 QA 환경 패키지만 사용했고 앱 런타임 의존성은 추가하지 않았습니다.

YouTube 확장 시 수집기 어댑터/소스 식별자 및 소스별 스케줄을 추가합니다. 분류기, 원본/후보 분리 저장, API와 UI는 재사용 가능합니다. 규칙만으로는 모든 새 음식명이나 정확한 식품 브랜드를 식별할 수 없으며 RSS가 전체 음식 유행을 포괄하지 않습니다. 장기 운영 전 원본 보관 기간, 페이지네이션, 분류 평가용 라벨 데이터 검토가 필요합니다.
