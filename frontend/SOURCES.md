# 기준 자료

- 사용자 원본 HTML: Library `libfile_aad13fa0d920819197c9c81af56ce079`.
- 최신 디자인 가이드: Library `libfile_54b1a92f6d1081919e0b6148142a7155` (mz-design-standard, v2.1).
- 디자인 토큰: Library `libfile_2216e173ca108191bce45b1f5cd83c7f`.
- 팀 저장소: https://github.com/asinagi/MZFoodTraker
- 참고 checkout: `feature/foodsafety`, commit `ee2b01b7eb44e248e09b1320d3b49ad00b91349b`.
- 실제 schema: `food_trends/foodsafety.py`의 `build_report`, `lookup`.
- B8 계약: `docs/app-api/openapi.json`, `README.md`, `DATA_DICTIONARY.md`.
- public/fixtures: 해당 저장소 `docs/app-api/examples/`의 명명된 JSON을 그대로 복사한 가상 응답.
- tests/synthetic-report.json: 위 build_report 함수로 생성한 명시적 합성 입력 결과.

기존 코드와 자료의 저작권·라이선스를 변경하지 않습니다. Pretendard OFL 문구는 원본 index.html의 font-license에 보존했습니다. 원본 이미지와 그 용도 표기도 유지했습니다. 본 작업을 이유로 제3자 이미지의 새로운 재배포 권한을 주장하지 않습니다.

초기 서비스 흐름 대조: https://app.notion.com/p/3f7d497a8b8c813da0b7e9044ea105b2 (2026-10-12 기획·제안발표 초안). 네이버 관심도와 현재 B8 관측 지표는 구분했습니다.

## 제품 자료 연결 개선의 확인 근거

- 팀 food_trends/foodsafety.py: build_report, build_ingredient_report, main --suppliers --json 출력. feature/foodsafety ee2b01b7eb44e248e09b1320d3b49ad00b91349b.
- 팀 main docs/app-api/openapi.json blob 5283dd6f80d5b3b0b170969f6ed87e615c0354da: health/foods/timeline/metrics/sources 경로. 제품 조회 API 경로는 없음.
- 공식 I1250 명세: https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I1250
- 공식 C002 명세: https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=C002
- 실제 조회 기록과 해석 한계: https://app.notion.com/p/414d497a8b8c8031b9f2dffa69f8a6db. 문서에 적힌 집계 수치를 원본 응답 검증 완료로 취급하지 않음.
- 이번 회귀 검증의 제품·제조사·재료 입력은 TEST_ 식별자를 쓰는 합성 데이터. 원본 API 수집 기록이나 라이브 응답이 아님.
