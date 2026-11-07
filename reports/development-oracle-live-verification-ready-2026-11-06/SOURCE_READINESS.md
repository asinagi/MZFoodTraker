# 수집원별 실제 검증 준비도 (Phase B)

점검 2026-11-07 13:33 KST, `tests/audit_source_readiness.py --oracle`(읽기 전용, 외부 요청 0, DB 쓰기 0). 근거: [source-readiness.json](source-readiness.json). 이름·참거짓·건수·날짜·사유 코드만 기록했고 자격 증명 값은 어디에도 출력하지 않았다.

**판정 원칙:** 자격 증명이 있다는 사실만으로 유효하다고 보지 않는다. 실제 권한은 승인된 실제 요청으로만 확인되므로, 모든 수집원의 최선 판정은 `REQUIRES_APPROVAL`이다. 기술적 차단 사유가 있으면 `BLOCKED`다.

## 공통 사실

| 항목 | 상태 |
| --- | --- |
| 운영 SQLite | v0, 30개 테이블 7,701행, integrity ok, WAL 0바이트, SHA-256 `38b880f5…689548c0`(이전 기록과 같음) |
| 기존 SQLite 수집기 | `.env`에서 `INSTAGRAM_COLLECTION_ENABLED=true`, YouTube·NAVER 스위치는 미설정(기본 켜짐). **그러나 운영 DB가 v0이므로 SQLite 서버는 시작 자체가 거부된다**(`Storage`가 v11이 아니면 `MigrationRequired`). 8000·8002 등 서버 포트 없음, RSS 스케줄러 없음 |
| 환경 변수 우선순위 | 프로세스 환경 변수가 `.env`보다 우선(`load_env`는 setdefault). 현재 프로세스에 수집 관련 변수 없음, 모두 `.env`에서 옴. `DATABASE_PATH` 미설정 → 기본 `data/trends.sqlite3` |
| 승인 registry | **없음**(저장소·`data/` 어디에도 실제 registry 파일 없음). 리허설용 합성 registry만 임시 폴더에서 썼다 |
| Oracle MZ_TEST | 리허설 뒤 초기화됨(대상 스키마 0, 필요 v10 → 절차서 4단계 `--target-migrate oracle`). 운영 Oracle은 코드에서 거부 |
| 제공자 측 사용량 | 확인 불가(제공자 API 호출 없이는 알 수 없음). 로컬 기록만 근거로 남은 예산을 추정했다 |
| 실제 Live 게이트 | `--execution-readiness`(registry·자격 증명·`B3_LIVE_ENABLED`·전용 수집원·기존 스케줄러 정지·Writer 이전·차단·운영·Shadow) — 승인 전에는 registry가 없어 열리지 않는다 |

## 수집원별

| 항목 | Google Trends RSS | Instagram Graph | YouTube Data API | NAVER DataLab |
| --- | --- | --- | --- | --- |
| `credentials_present` | 불필요(공개 피드) | `INSTAGRAM_ACCESS_TOKEN`·`INSTAGRAM_USER_ID`·`FACEBOOK_PAGE_ID` 있음(토큰 교환용 `META_APP_ID`·`META_APP_SECRET`도 있음) | `YOUTUBE_DATA_API_V3_KEY` 있음 | `NAVER_DATALAB_CLIENT_ID`·`NAVER_DATALAB_CLIENT_SECRET` 있음 |
| `credential_validity_verified` | 해당 없음 | **false** | **false** | **false** |
| 로컬 기록(마지막 실제 확인) | — | 2026-10-26: 토큰 valid, 권한 instagram_basic·pages_show_list·pages_read_engagement, 해시태그 조회 성공. 교체 토큰 만료 기록 **2026-12-25 00:55Z(47일 남음)**. `.env` 토큰이 그 토큰인지는 제공자 확인 없이는 알 수 없음 | 2026-10-26 검증 통과 | 2026-10-26(KST) HTTP 200 5회 |
| `registry_status` | ABSENT | ABSENT | ABSENT | ABSENT |
| `policy_status` | 계약 `rss-existing-v1`: 항목 저장, 대상에는 원본 피드 본문 미보관. 수집 성공과 후보 생성은 별도 기록(회차 성공 ≠ 음식 후보) | `instagram-existing-v1`: 응답 저장 없음, 캡션 30일, 캡션 해시태그만 파생. 7일 고유 해시태그 로컬 28·제공자 30 | `youtube-existing-v1`: 29일 만료, 음식 언급 파생 차단(검색 결과 = 검색 근거일 뿐) | `naver-no-response-storage-v1`: 응답·지수 저장 없음(요청 메타데이터만) |
| `writer_state` | 운영 SQLite: 레거시 소유이나 시작 불가(v0). MZ_TEST: 이전 없음 | 같음 | 같음 | 같음 |
| `legacy_collector_state` | SQLite 서버 RSS 스케줄러: 서버 미실행·시작 불가 | 스위치 `true`(`.env`) — 검증 세션에서 `false` 필요 | 스위치 미설정=기본 `true` — `false` 필요 | 스위치 미설정=기본 `true` — `false` 필요 |
| 레거시 사용 기록(운영 DB) | 회차 95개, 마지막 2026-10-28 13:49 KST | 검색 1건, 마지막 2026-10-26, 열린 7일 창 0. 기록 계정 = 설정 계정(true) | 호출 38건, 마지막 2026-10-28, 오늘(태평양) 0 | 호출 10건, 마지막 2026-10-26, 오늘(KST) 0 |
| `quota_coverage` | 예산 없음 | 로컬 원장 기준 열린 사용 0. 제공자 측 UNKNOWN(작업이 실행 직전 `recently_searched_hashtags`로 확인) | 로컬 0, 제공자 측 UNKNOWN | 로컬 0, 제공자 측 UNKNOWN |
| `estimated_remaining_budget`(로컬) | — | 새 태그 28(검증 설정 1) | 검색 40·videos 1000(검증 설정 각 1) | 100(검증 설정 1) |
| 검증 요청 상한 | 1 | 5 | 2 | 1 |
| 검증 세션에 필요한 설정 | `B3_LIVE_ENABLED=true` | 위 + `INSTAGRAM_COLLECTION_ENABLED=false`, `INSTAGRAM_MAX_PAGES=1`, `INSTAGRAM_SEARCH_SOFT_LIMIT=1` | 위 + `YOUTUBE_COLLECTION_ENABLED=false`, `YOUTUBE_DAILY_SEARCH_LIMIT=1`, `YOUTUBE_DAILY_VIDEO_LIMIT=1` | 위 + `NAVER_DATALAB_ENABLED=false`, `NAVER_DATALAB_DAILY_LIMIT=1` |
| `blockers` | 없음 | 없음 | 없음 | 없음 |
| `required_approval` | G-OPS-4/5, 검토된 registry | G-OPS-4/5, 검토된 registry(Meta 권한·해시태그 검색 근거), 실제 요청 1회로 토큰 확인 | G-OPS-4/5, 검토된 registry(프로젝트·할당량 근거), 실제 요청으로 키 확인 | G-OPS-4/5, 검토된 registry, 실제 요청으로 자격 증명 확인 |
| **`decision`** | **REQUIRES_APPROVAL** | **REQUIRES_APPROVAL** | **REQUIRES_APPROVAL** | **REQUIRES_APPROVAL** |

## 수집 정책 점검 근거

| 정책 | 검증 |
| --- | --- |
| 허용된 출처만 | 실행 승인은 registry(`permission_verified`·`policy_allowed`·`approved_by`·`evidence`)가 없으면 `NO_REGISTRY_GRANT`로 거부 |
| Instagram 고유 해시태그 제한 | 로컬 7일 원장(소프트 28) + 실행 직전 제공자 최근 검색 확인. 리허설: `INSTAGRAM_SEARCH_SOFT_LIMIT=1`에서 두 번째 새 태그 작업은 요청 0으로 대기, 인계 뒤 기존 SQLite 예산 코드도 새 태그를 `budget`으로 거부 |
| YouTube 검색 결과를 음식 언급으로 바꾸지 않음 | 대상에 YouTube `food_mentions` 0(E2E·리허설 무결성 점검), 검색 일치는 `SEARCH_ONLY` 근거 |
| NAVER 지수·본문 미저장 | NAVER `metric_observations` 0, `naver_calls` 인계 행은 요청 본문·후보·값 없이 호출 사실만 |
| RSS 수집 성공과 후보 생성 구분 | 회차 성공(`success`, 항목 수)과 후보 수(`entity_resolution.candidates`)를 따로 기록. 리허설: 항목 3·새 항목 2·후보 2 |

## 사전 확인이 필요한 위험

- **같은 자격 증명을 쓰는 다른 실행 주체**: 제공자 측 사용량은 로컬에서 볼 수 없다. 이 PC 밖에서 같은 키·토큰을 쓰는 프로그램이 없음을 운영자가 확인한다. Instagram은 작업이 제공자의 최근 검색 목록을 먼저 확인하고, YouTube·NAVER는 검증 요청이 일일 할당량의 극히 일부(YouTube 101 units / 10,000, NAVER 1 / 1,000)라 여유가 크다.
- **Instagram 토큰**: 로컬 기록상 만료 47일 전이다. `.env` 토큰이 그 기록의 토큰인지, 권한이 유지되는지는 첫 실제 요청에서만 확인된다. 401/403이면 계정이 자동 차단되고 재시도하지 않는다.
- **검증 뒤 운영 예산**: 경로 A의 사용량 인계는 격리 사본으로 간다. 운영 SQLite는 검증 사용량을 모르므로, 운영 수집기는 G-OPS-3 이후에도 검증의 `open_until`(최대 7일)이 지나기 전에는 경로 B 인계 없이 켜지 않는다([절차서](LIVE_PILOT_RUNBOOK.md) 13단계).
