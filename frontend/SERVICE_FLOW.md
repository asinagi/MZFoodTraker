# MZ제조기 서비스 연결 및 팀 저장소 이관안

확인일: 2026-11-23. 팀 main `3194f58d8e8dd27826b798d836c9b2865863bba1`, foodsafety `ee2b01b7eb44e248e09b1320d3b49ad00b91349b` 기준.

## 사용자 흐름

메뉴 발견/검색 → 관심 근거 확인 → 제품·재료 후보 비교 → 도입 검토 저장 → 다음 확인사항 실행.

목표 사용자는 개인 카페·소규모 디저트 매장의 운영자다. 메뉴의 관심도만 보고 도입을 결정하지 않고, 관련 제품 형태와 원재료 기록을 확인한 뒤 매장의 장비·구매조건·보관조건에 맞는지 판단하게 한다.

| 서비스 화면 | 기존 팀 코드/계약 | 프론트엔드에서 이어갈 일 | 추가 연결 필요 |
| --- | --- | --- | --- |
| 메뉴 찾기 | GET /api/v1/foods?q=, canonical_name, aliases, food_id | 현재 고정 메뉴 두 개를 API 목록으로 교체. food_id 기반 동적 상세 라우팅. 로딩·검색 결과 없음·재시도 | 운영 API 주소. 이미지/설명은 API에 없으므로 별도 편집 데이터 또는 텍스트 카드 |
| 메뉴 상세/관심 기록 | GET /api/v1/foods/{id}, /timeline, /metrics | 출처별 기간 그래프와 관측 일수 표시. 음식의 승인 관계 탐색 | 공개 조회 가능한 데이터와 API 호스팅 |
| 제품·재료 비교 | feature/foodsafety의 I1250/C002 JSON; PRDLST_REPORT_NO 연결 | JSON 어댑터로 제조업체·식품유형·원재료를 비교. 메뉴 문맥과 선택 이유 유지 | 음식 food_id ↔ 제품 report_no 매핑 및 제품 API. 현재는 파일·사용자 확인 |
| 내 검토 | 현재 프론트엔드 localStorage | 관심 메뉴/후보/이유/메모/검토상태/다음 확인 관리; 음식과 제품 식별자를 구분 | B8에는 사용자·검토 쓰기 API가 없음. 기기 간 저장은 별도 계정/검토 API 필요 |

## 중요한 계약 차이

- 현재 홈/검색은 butter, cookie 고정 키를 사용한다. 데이터 관측 화면에서 B8 응답을 읽는 것과 홈 전체가 실데이터로 동작하는 것은 다르다. 다음 구현은 목록·상세의 임의 food_id 지원부터 한다.
- B8은 NAVER 지수를 저장/반환하지 않으며, YouTube는 비활성 상태다. 기존 0~100 가상 검색 관심 그래프를 B8 수치로 이름만 바꿔 사용하면 안 된다. 출처별 confirmed_items와 coverage를 별도로 보여준다.
- null은 미관측, 0은 관측 후 0건이다. 출처별 수치를 합쳐 종합 유행 점수를 만들지 않는다.
- food_id와 식품안전나라 품목보고번호는 다른 식별자다. 이름이 비슷하다는 이유만으로 같은 제품이라고 확정하지 않는다.
- 식품안전나라 신고 건수는 판매량/공급량이 아니며 제조업체 정보만으로 구매·납품 가능성을 확정하지 않는다.
- ACCESS.md에 따르면 실제 B8은 루프백 고정이다. 공개 Site에서 팀원 PC의 localhost에 직접 연결할 수 없다. 공개 읽기 API/HTTPS 및 출처별 공개 가능 범위는 팀 백엔드 작업이다. 관리 토큰/DB 비밀번호를 프론트엔드에 넣지 않는다.

## 파일 구조

```text
MZFoodTraker/
├── food_trends/       기존 수집·정제·DB·API (유지)
├── static/            기존 운영 화면 (유지)
├── docs/app-api/      기존 API 계약 (유지)
├── tests/             기존 백엔드 검사 (유지)
└── frontend/          새 모바일 서비스
    ├── public/        HTML·CSS·JS·assets·fixtures
    ├── tests/         프론트엔드 검사
    ├── serve.py       로컬 정적 서버 + 읽기 프록시
    ├── build_static.py
    ├── SOURCES.md
    ├── SERVICE_FLOW.md
    └── README.md
```

첫 이관에서는 기존 앱 파일 위치를 유지해 경로가 깨지지 않도록 한다. API 화면과 서비스 화면 통합 이후 기능별 JS 분리는 별도 변경으로 진행한다. 기능 전환과 대규모 파일 재배치를 한 PR에 섞지 않는다.

## 이관 순서

1. 팀 main에서 `feat/mobile-frontend` 브랜치를 만든다.
2. 개인 저장소의 `python3 scripts/export_team_frontend.py /절대경로/MZFoodTraker` 실행. 이미 frontend/가 있으면 덮어쓰지 않고 중단한다.
3. 팀 저장소에서 diff가 frontend/ 아래의 추가뿐인지 확인하고 검사 후 해당 경로만 커밋한다.
4. 팀 원격에 브랜치 push 후 PR을 연다. 검토 후 main에 합친다. 기존 static/, food_trends/, tests/, 루트 실행 설정은 수정하지 않는다.
5. 팀 clone의 frontend/를 개발 기준으로 바꾸고 개인 저장소는 백업으로 유지한다. 기존 발표 Site는 이관이 끝날 때까지 현재 소스를 유지한다.

2026-11-23 협업자 초대 수락 후 계정의 push=true 권한을 확인했다. GitHub 연동 앱의 쓰기 요청은 403 오류로 거절되어, Mac의 GitHub Desktop을 통해 이관한다. 이번 변경은 frontend/ 추가만 포함하며 기존 팀 파일은 수정하지 않는다. 실행 중인 백엔드나 실제 DB 연결은 별도 검증이 필요하다.

## 근거

- https://github.com/asinagi/MZFoodTraker/blob/main/docs/app-api/openapi.json
- https://github.com/asinagi/MZFoodTraker/blob/main/docs/app-api/DATA_DICTIONARY.md
- https://github.com/asinagi/MZFoodTraker/blob/main/docs/app-api/ACCESS.md
- https://github.com/asinagi/MZFoodTraker/blob/feature/foodsafety/food_trends/foodsafety.py
