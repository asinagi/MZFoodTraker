# 선행 의존성에 따른 개발 로드맵

2026-10-26 제안. 구현·배포·실제 수집은이번에수행하지않았다. 작업의범위와인수 기준이며개발소요일을확정한견적은아니다.

## 1. 탐지·추적 연결 이전에 반드시 완료

| ID·목적 | 변경 대상 | 의존성·위험 | 테스트 및 완료 기준 |
| --- | --- | --- | --- |
| A1 후보재저장중연결보존(P0) | storage._classify UPSERT, 후보FK, 기존테스트 | 선행 없음. FK/CASCADE와분류 이력변경 위험 | 동일RSS반복수집·분류내용변경·재분류후YouTube후보연결유지. 새후보/이전분류 이력정상. 이번격리재현1→0이1→1로바뀜 |
| A2 출처정책·계약확정(P0) | source capability/policy registry, Envelope/metric/origin 계약, config검증 | 선행 없음. 무허가보존·단위혼합위험 | NAVERraw/지수저장0, YT파생 분석off/29일기한유지, 미확인IG장기저장차단, legacy를live로승격0. 모든metric에unit/time/context/origin지정 |
| A3 읽기조회·마이그레이션분리(P0) | 각service.status, 생성자DDL/purge/reprocess, migration version | A1/A2와병행가능. 기존기동호환·만료maintenance누락위험 | read-only연결로상태API실행성공·DB변경0. 명시migration만DDL. 별도maintenance로TTL준수. 기존97테스트회귀통과 |
| A4 음식 유형·ID·정확 별칭·승인(P0) | FOOD_ENTITY/ALIAS/RELATION, 후보및검토모델, classifier소비gate | A1,A2. 잘못된음식병합·브랜드오승격위험 | 브랜드/매장은음식target불가. 정확 별칭은한food_id, 짭쫀쿠는자동 병합0, 동형어는검토. merge/split전후매핑 이력복구검증 |
| A5 수동음식 등록의공통 진입(P0) | 사용자 음식UI/API, 후보resolution·승인경로 | A4. RSS데이터를테스트용후보로오염시킬위험 | RSSFOOD0상태에서사용자 음식1개가resolved food를만듦. 원RSS items/candidates불변. 같은request key재시도시음식/후보중복0 |

게이트 G1: A1~A5 완료. 원본 보존·개체판정·정책gate가준비되기전전출처자동 추적을켜지않는다. 공식사용 조건확인이계속안되는출처는저장/파생기능을차단한상태로다른출처개발을계속할수있다.

## 2. 탐지·추적을 연결하면서 구현

| ID·목적 | 변경 대상 | 의존성·위험 | 테스트 및 완료 기준 |
| --- | --- | --- | --- |
| B1 공통target상태·스케줄 | TRACKING_TARGET, next_due·generation, pause/resume/stop | G1. 공유query중단·늦은결과유입 위험 | 중단target의새작업0, 늦은응답음식 집계유입0, 재개중복작업0. 사용자/자동음식이같은경로사용 |
| B2 query planner와API예산 | TRACKING_QUERY/QUERY_TARGET, source별serializer, quota reserve | B1,A2. 별칭중복 호출·배치문맥혼합위험 | 동일음식/동일 요청공유, NAVERgroup중복0, IGrolling7일·YT PT/DST·Naver KST경계·동시예산검증. 모든시도사전예약 |
| B3 신뢰성있는실행전달 | outbox, lease/fencing, API_CALL·INGEST_RUN | B1/B2. 외부호출과DB원자성없음·worker중복위험 | crash/retry/lease만료주입. 논리결과중복0, 늦은owner쓰기0, 실패 상태누락0. 원격중복불명은숨기지않음 |
| B4 source adapter·staging·DQ | 기존4개파서재사용, Envelope, SOURCE_ITEM/snapshot/RUN_ITEM/QUALITY_RESULT | B2/B3,A2. 원문 손실·권한범위초과·legacy위장위험 | RSS원본/관측대조0불일치, IGmissing/empty/purged분리, YT수치정밀도·TTL, DataLab응답미저장. 부분/실패/0구분 |
| B5 검색 근거·음식 언급·공통관측 | ITEM_QUERY_MATCH/FOOD_MENTION/METRIC_OBSERVATION, 정책별분석gate | A4,B4. 검색 일치오해·중복 집계위험 | 동일 콘텐츠두별칭검색에서콘텐츠1/관계2/음식distinct1. 여러음식mention분리. YT미검증영상에서음식mention0, ratio혼합금지 |
| B6 Oracle repository·환경 | python-oracledb, pool, 명시적 Oracle migrations, 자료형 매핑 | A2/A3, B4 계약 안정화. 기존 sqlite SQL을 직접 이식할 위험 | 실제 선택 버전에서 FK/UNIQUE/CHECK·동시 UPSERT·한글·빈값·UTC/KST·큰 정수·BLOB hash·rollback 통과. 드라이버/DB 버전 기록 |
| B7 단계이관·대조·복구 | LEGACY_KEY_MAP, 허용데이터적재, outbox shadow, 출처별writer전환 | B3~B6. 두DB불일치·TTL연장·원문불법복제위험 | source/테이블별건수·키·hash·관측·NULL대조통과, 예상제외외누락0, TTL연장0, 추가 API수집없이shadow검증, 전환 후신규데이터포함rollback리허설 |
| B8 승인출처일 집계·화면 | FOOD_METRIC_DAILY, coverage표시, 출처별단위패널 | B5, Oracle사용시B6. 단위혼합·미관측0처리위험 | 단위/context별분리, 실패null·정상0, 별칭union, 집계재실행건수불변, source별거부정책유지. 통합유행공식미확정 |

게이트 G2: 임시 DB/격리Oracle에서 [통합 시나리오](TRACKING_DESIGN.md)의정상·예외경로전체통과. 이후사용범위가확인된출처만소량실제 호출검증계획수립. 네출처모두에서음식자동 발굴이가능해야만G2가되는것은아니다. 정책상차단된기능은차단상태가정확히검증되어야한다.

## 3. 연결 완료 이후 구현 가능

| ID·목적 | 변경 대상 | 의존성·위험 | 테스트 및 완료 기준 |
| --- | --- | --- | --- |
| C1 탐지정확도개선 | 독립사람라벨·신조어양성셋, 보수적규칙/로컬임베딩비교 | G2, A2. 자체라벨을정답으로삼거나fuzzy병합위험 | 사람 검증라벨·학습/평가분리, FOOD/REVIEW구분, TP/FP/FN·분모·UNKNOWN보고. 임계값은평가후결정 |
| C2 발견원다양화 | IG공동태그후보추천, YT음식 추출또는추가발견adapter | G2,C1, 해당출처의명시허용/승인. 무허가파생·비음식유입 위험 | 정책 승인없는경로off, 후보근거·사람 승인·실제양성검증. 기존 수집기보존 |
| C3 주·월지표·성능·보존 | 기간distinct집계, 증분재계산, 인덱스/LOB관리 | B8, 실제다일관측. 일고유수합산·만료데이터복제위험 | 기간집합에서재집계, 공급자period의미유지, p95지연/용량목표를실측설정. 삭제 전파·만료후백업복구검증 |
| C4 운영확장 | 대시보드·실패알림·재처리큐·문서화, 필요시다중worker | G2, 안정된quota/lease. 알림비밀노출·중복실행위험 | run→call→item→food→metric추적가능, stale run·부분 수집·기한 임박경보, 로그비밀값0. 다중worker는fencing검증후 |
| C5 유행점수실험 | 승인된출처별지표와평가데이터 | C1/C3 및출처정책. 단위혼합·자의가중치위험 | 목적/정답·기간·coverage·비교범위·유효성검증을먼저정의. 이번 감사에서공식·가중치확정안함 |

의존성 흐름: `A1/A2/A3 → A4 → A5 → B1 → B2 → B3 → B4 → B5 → B8`, Oracle경로는`A3+B4 → B6 → B7`, 후속확장은`G2 → C1/C3/C4 → 정책 허용 범위의C2/C5`다. A2의정책 확인은출처별로독립추진할수있고, 허용미확인출처를위해다른출처개발을멈출필요는없다.
