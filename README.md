# MZFoodTraker

MZ 유행 음식의 발견, 음식 개체 정규화, 여러 플랫폼의 추적을 목표로 개발하는 로컬 MVP입니다. Google Trends RSS에서 음식 후보를 분류하고, Instagram·NAVER·YouTube를 각각 조회하거나 추적합니다. 공통 FOOD_ID·정확 별칭 승인·수동 음식 등록을 구현했으며, 이 공통 개체를 각 출처의 자동 추적에 연결하는 B1~B2는 후속 과제입니다.

아래 구현 구분은 통합 감사, [A1~A3 개발 결과](reports/development-a1-a3-2026-10-27/REPORT.md), [A4~A5 개발 결과·API·스키마·복구 안내](reports/development-a4-a5-2026-10-27/REPORT.md)를 반영합니다. A4~A5는 명시적 마이그레이션 버전 3을 사용합니다. 운영 DB는 버전 0으로 남아 있으며 이번 개발에서 마이그레이션·서버 재시작을 수행하지 않았습니다.

## 현재 구현 현황

| 기능 | 구현 범위와 한계 |
| --- | --- |
| Google Trends RSS 후보 탐지 | 한국 급상승 RSS 수집, 원본·회차별 관측 보존, 규칙 기반 FOOD / REVIEW / NON_FOOD 분류, 검색·상세·내보내기 |
| 음식 개체 식별 및 별칭 정규화 | 영속 FOOD_ID, 범위별 정확 별칭, 공통 후보·사람 승인·이력, 브랜드/매장 gate, 파생/연관 관계, 별칭 취소·복구, 제한된 사람 승인 병합, 수동 등록 UI/API 구현. 일반적인 split/unmerge와 자동 추적 연결은 미구현 |
| Instagram 해시태그 추적 | 사용자가 승인한 태그의 공식 Graph API 수집, 중복 방지, 캡션 해시태그 추출, 공동 출현 및 시간·일별 관측 집계 구현. 자동 음식 발굴은 미구현 |
| NAVER DataLab 관심도 추적 | FOOD 후보 및 사용자 지정 검색어의 7/30/90일 상대 관심도 조회·그래프. 응답 본문과 지수는 DB에 저장하지 않으며 사용자 지정 조회는 공통 음식 등록이 아님 |
| YouTube 영상 데이터 추적 | 설정 검색어 및 RSS FOOD 후보 기반 검색, 영상 상세·조회수 등 저장, 호출 예산·재시도·출처 구분·29일 TTL 구현. 영상에서 음식을 추출하는 파생 분석은 비활성화 |
| 추이 분석 및 시각화 | 출처별 상태·그래프·관계 표 구현. 출처 공통 관측 모델, 통합 일 집계, 통합 유행 점수는 미구현 |

2026-10-27 A1~A3 개발 검증에서는 기존 97개와 신규 17개를 합친 114개 자동화 테스트가 통과했습니다. 2026-10-26 통합 감사의 최신 RSS 표본은 FOOD 0건이어서 실제 음식 양성의 전 출처 자동 연결은 검증되지 않았습니다. 테스트 통과는 탐지 정확도나 이용약관 준수의 인증이 아닙니다. 과거 실제 호출 결과와 제약은 [통합 감사](reports/integrated-audit-2026-10-26/REPORT.md)를 참고하세요.

## 디렉터리 구조

```text
.
├── food_trends/               # Python 애플리케이션
│   ├── __main__.py            # CLI, 서비스와 로컬 서버 실행
│   ├── config.py              # .env 로더와 출처별 설정
│   ├── collector.py           # Google Trends RSS HTTP·파싱
│   ├── classifier.py          # 문맥 규칙 기반 후보 분류
│   ├── storage.py             # RSS SQLite 저장·관측·분류 이력
│   ├── contracts.py           # 출처·단위·origin·제공 필드 메타데이터
│   ├── migrations.py         # 명시적 버전 마이그레이션
│   ├── migration_definitions.py # 기존 추가형 스키마 정의
│   ├── service.py             # RSS 수집·잠금·스케줄
│   ├── api.py                 # 로컬 HTTP API
│   ├── audit.py               # 저장 데이터 감사
│   ├── instagram_*.py         # Graph API·저장·분석·토큰·검증
│   ├── naver_*.py             # DataLab 요청·서비스·검증
│   └── youtube*.py            # 영상 API·저장·서비스·검증
├── static/                    # HTML / CSS / JavaScript 화면
├── tests/                     # unittest 및 별도 브라우저 검증 스크립트
├── reports/
│   ├── development-a1-a3-2026-10-27/ # A1~A3 개발·검증 결과
│   └── integrated-audit-2026-10-26/
│       ├── REPORT.md          # 현황·위험·감사 결과
│       ├── ORACLE_MODEL.md    # Oracle 목표 모델 (미구현)
│       ├── TRACKING_DESIGN.md # 탐지→추적 계약 제안
│       ├── ROADMAP.md         # 선행 의존성과 인수 기준
│       └── *.json            # 비밀값 없는 감사 증거·검증 요약
├── data/                      # 로컬 DB·수집·검증 자료 (Git 제외)
├── .env                       # 실제 로컬 설정 (Git 제외)
├── .env.example               # 공개 가능한 설정 템플릿
├── .gitignore
├── run.ps1                    # Windows 실행 도우미
├── README.md
└── *_INTEGRATION.md 등        # 출처별 연동·검증 보고서
```

`data/`와 `.env`는 기존 로컬 환경에만 존재하며 clone에 포함되지 않습니다. 로그·DB·원본 수집 파일도 업로드 대상에서 제외합니다. 보고서의 로컬 증거 파일 링크 중 일부는 공개 저장소에서 열리지 않을 수 있습니다.

## 기술 스택

- Python 3.11 이상, 표준 라이브러리 `urllib`, `http.server`, `sqlite3`, `threading`, `unittest`
- SQLite와 직접 작성한 SQL; ORM 없음
- HTML / CSS / JavaScript 기반 정적 UI
- Google Trends RSS, Instagram Graph API, NAVER API HUB DataLab, YouTube Data API v3

기본 실행과 단위 테스트에 별도 Python 패키지 설치가 필요하지 않습니다. 브라우저 검증 스크립트는 별도 도구·실행 서버가 필요한 선택 검사입니다. SQLite는 명시적 버전 마이그레이션을 사용하며 Oracle 저장소·드라이버 연동은 향후 개발 항목입니다.

## 로컬 실행

SQLite를 포함한 정상 Python 설치를 사용하세요. 저장소 루트에서 실행합니다.

```powershell
# 새 환경에서만 템플릿 복사: 기존 .env는 덮어쓰지 않습니다.
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}

# 새 환경의 빈 DB에만 최초 스키마 준비
py -3.12 -B -m food_trends --migrate

# 자동 외부 수집 없이 UI 실행
powershell -ExecutionPolicy Bypass -File .\run.ps1 --no-schedulers
```

http://127.0.0.1:8000 에 접속하고 Ctrl+C로 종료합니다. 기본 DB 경로는 `data/trends.sqlite3`입니다. 스키마가 준비되지 않으면 서버는 마이그레이션을 안내하고 종료합니다. **기존 DB는 구버전 writer 종료와 백업 후 명시적으로 마이그레이션해야 합니다.** 실행 중인 구버전 서버의 DB를 동시에 업그레이드하지 마세요. 자세한 절차는 [A1~A3 개발 보고서](reports/development-a1-a3-2026-10-27/REPORT.md)를 따릅니다. `--no-schedulers`도 로컬 만료 관리를 실행하므로 읽기 전용 감사 명령이 아닙니다.

정상 Python을 직접 선택해 `python -m food_trends --no-schedulers`로 실행할 수도 있습니다. `run.ps1`은 Python Launcher가 있으면 `py -3`, 없으면 사용자 Anaconda, 그다음 PATH의 `python`을 사용합니다. 번들 Python이 SQLite를 제공하지 않는 경우 정상 Python 경로를 명시하세요.

```powershell
# RSS 및 설정에서 활성화한 출처의 스케줄러 실행
powershell -ExecutionPolicy Bypass -File .\run.ps1

# RSS 1회 수집 (외부 호출 및 로컬 저장)
python -m food_trends --collect-once

# NAVER 사용자 지정 검색어 실시간 조회 (인증 필요)
python -m food_trends --naver-keyword '두쫀쿠' --naver-days 30
```

서버는 루프백에 바인딩하며 실행 중에만 스케줄러가 동작합니다. RSS 기본 주기는 30분입니다. 기존 서버·DB를 유지하려면 별도 포트와 DB 경로를 지정하세요. `--reanalyze` 등 유지 관리 명령은 저장 결과를 변경하므로 각 보고서를 확인한 뒤 실행하세요.

## 환경변수

루트 `.env`를 자동으로 읽으며 **프로세스 환경변수가 파일보다 우선**합니다. `.env.example`은 빈 인증값과 가상 계정 ID를 제공하며 선택 API 수집을 기본 비활성화합니다. 실제 계정 ID와 비밀값은 로컬 `.env`에서만 설정하세요.

| 용도 | 변수 |
| --- | --- |
| 서버·RSS | `PORT`, `DATABASE_PATH`, `COLLECT_INTERVAL_SECONDS`, `RSS_TIMEOUT_SECONDS` |
| Instagram 연결 | `INSTAGRAM_GRAPH_API_VERSION`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, `FACEBOOK_PAGE_ID` |
| Instagram 토큰 검사·교환 | `META_APP_ID`, `META_APP_SECRET` |
| Instagram 수집·보존 | `INSTAGRAM_COLLECTION_ENABLED`, `INSTAGRAM_COLLECTION_INTERVAL_MINUTES`, `INSTAGRAM_TIMEOUT_SECONDS`, `INSTAGRAM_MAX_PAGES`, `INSTAGRAM_SEARCH_SOFT_LIMIT`, `INSTAGRAM_CAPTION_RETENTION_DAYS` |
| NAVER 인증 | `NAVER_DATALAB_CLIENT_ID`, `NAVER_DATALAB_CLIENT_SECRET` |
| NAVER 수집 | `NAVER_DATALAB_ENABLED`, `NAVER_DATALAB_INTERVAL_SECONDS`, `NAVER_DATALAB_DAILY_LIMIT`, `NAVER_DATALAB_TIMEOUT_SECONDS` |
| YouTube 인증 | `YOUTUBE_API_KEY` 또는 `YOUTUBE_DATA_API_V3_KEY` (대체 변수) |
| YouTube 수집 | `YOUTUBE_COLLECTION_ENABLED`, `YOUTUBE_INTERVAL_SECONDS`, `YOUTUBE_DISCOVERY_QUERIES`, `YOUTUBE_QUERIES_PER_RUN`, `YOUTUBE_MAX_PAGES`, `YOUTUBE_DAILY_SEARCH_LIMIT`, `YOUTUBE_DAILY_VIDEO_LIMIT`, `YOUTUBE_TIMEOUT_SECONDS` |

`YOUTUBE_DISCOVERY_QUERIES`는 JSON 문자열 배열입니다. 선택 출처를 사용할 때는 인증·권한·사용 조건을 확인한 뒤 해당 `*_ENABLED` 값을 `true`로 설정합니다. API 키·토큰·쿠키·인증 JSON·인증서·실제 응답·DB는 커밋하지 마세요. `.gitignore`는 이미 추적 중인 파일이나 과거 커밋을 지우지 않으므로 이후 커밋도 별도로 검토해야 합니다.

## 데이터 출처와 해석

- Google Trends: 한국 급상승 RSS `https://trends.google.com/trending/rss?geo=KR`. 제공 뉴스 링크만 사용하며 기사 본문은 수집하지 않습니다. RSS 트래픽 표시는 정확한 전체 검색량이나 음식 유행 확정값이 아닙니다.
- Instagram: 공식 Graph API의 승인 해시태그 관측입니다. 그래프는 API에서 관측한 고유 게시물의 최초 관측 시각 기준이며 전체 Instagram 게시량이 아닙니다. 검색 예산·권한·토큰 만료를 확인해야 합니다. 캡션 TTL 구현과 별개로 장기 파생 데이터 보존 권한은 통합 감사에서 미확인으로 남았습니다.
- NAVER: 현재 코드는 NAVER API HUB의 DataLab 검색 트렌드 엔드포인트를 사용합니다. 인증은 API HUB 발급값을 사용합니다. 지수는 요청의 기간·그룹·필터에 상대적이며 서로 다른 요청의 지수를 그대로 합치거나 비교할 수 없습니다. 응답 본문·지수의 DB 저장과 캐시는 제공하지 않습니다.
- YouTube: Data API v3의 검색 및 공개 영상 상세를 사용합니다. 검색 일치가 해당 음식의 실제 언급이라는 뜻은 아닙니다. 기존 API 데이터의 로컬 TTL은 29일이며 음식 추출·통합 점수 등 파생 분석은 비활성화되어 있습니다.

이 문서는 현재 코드와 기존 감사 결과를 설명하며, 제공자의 현행 사용 조건을 새로 검증한 문서는 아닙니다. 실제 수집·재배포 전에는 해당 API의 권한·보존·표시·삭제 조건을 확인하세요. 출처별 단위와 관측 범위가 달라 수치를 합산한 통합 점수는 제공하지 않습니다. 미관측·실패를 정상 0건으로 해석해서는 안 됩니다.

## API와 검증

대표 RSS API는 `GET /api/status`, `GET /api/trends`, `GET /api/trends/{id}`, `POST /api/collect`, `GET /api/export`입니다. 내보내기에는 로컬 RSS 원본이 포함되므로 공개 업로드 대상에 넣지 마세요. 출처별 API와 관리 CLI는 아래 연동 보고서에 설명되어 있습니다.

```powershell
# 외부 접속과 운영 DB를 차단하고 합성 응답·임시 DB로 검증
py -3.12 -B tests/run_offline.py --output data/publication-tests
```

`*_browser_check.py`, `*_verify` 명령은 별도 검증이며 실행 중인 서버·실제 API·로컬 데이터에 접근할 수 있습니다. 단위 테스트와 구분해서 실행하세요. A3 이후 생성자는 스키마 버전만 확인하며 데이터 조회는 읽기 전용 연결을 사용합니다. 마이그레이션·만료 정리·대상 동기화·재분류는 명시적 쓰기 경로로 분리했습니다. 백그라운드 수집·유지관리가 실행 중이면 해당 작업은 DB를 변경할 수 있습니다.

## 향후 개발

[개발 로드맵](reports/integrated-audit-2026-10-26/ROADMAP.md)의 순서와 인수 기준을 따릅니다.

1. A1 후보 연결 보존, A2 출처 계약 메타데이터, A3 읽기 조회·마이그레이션 분리를 구현했습니다. 출처별 보존·파생 분석 권한 확인과 운영 배포 검증은 계속 필요합니다.
2. 공통 음식 개체·정확 별칭·승인·매핑 이력, 수동 음식 등록의 공통 진입 구현
3. 공통 추적 대상·쿼리 계획·예산·outbox·lease/fencing·관측 및 품질 모델 구현
4. Oracle repository와 명시적 마이그레이션·이관·대조·복구 검증
5. 승인된 출처별 일 집계와 커버리지 표시, 독립 사람 라벨 기반 정확도 평가, 정책이 허용하는 범위의 발굴·기간 집계 확장

통합 유행 점수의 공식·가중치는 확정하지 않았습니다. 정책상 허용이 확인되지 않은 경로의 자동 수집·저장·파생 기능은 공통 설계의 완료로 간주하지 않습니다.

## 관련 문서

- [통합 감사](reports/integrated-audit-2026-10-26/REPORT.md), [Oracle 모델](reports/integrated-audit-2026-10-26/ORACLE_MODEL.md), [탐지→추적 설계](reports/integrated-audit-2026-10-26/TRACKING_DESIGN.md), [로드맵](reports/integrated-audit-2026-10-26/ROADMAP.md)
- [A1~A3 개발·마이그레이션·검증 결과](reports/development-a1-a3-2026-10-27/REPORT.md)
- [RSS 검증](VERIFICATION.md), [분류 감사](AUDIT_REPORT.md)
- [Instagram 연동](INSTAGRAM_INTEGRATION.md), [해시태그 분석](INSTAGRAM_HASHTAG_ANALYSIS.md), [실환경 검증](INSTAGRAM_LIVE_VERIFICATION.md), [토큰 적용 검증](INSTAGRAM_TOKEN_APPLICATION.md)
- [NAVER 연동](NAVER_DATALAB_INTEGRATION.md), [실환경 검증](NAVER_LIVE_VERIFICATION.md)
- [YouTube 연동](YOUTUBE_INTEGRATION.md), [검증](YOUTUBE_VERIFICATION.md)
