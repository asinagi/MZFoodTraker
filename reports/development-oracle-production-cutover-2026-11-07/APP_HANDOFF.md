# 앱 개발자 인계 — 실제 운영 데이터 API (B8 v1)

대상: MZFoodTraker 모바일·웹 앱 개발자. 계약은 그대로 [OpenAPI v1](../../docs/app-api/openapi.yaml)(7개 엔드포인트)이고, 바뀐 것은 **실제 운영 데이터를 주는 서버가 생겼다**는 점뿐이다. 자세한 규칙은 [앱 API 가이드](../../docs/app-api/README.md), 인증 범위는 [ACCESS.md](../../docs/app-api/ACCESS.md).

## Base URL

| 환경 | Base URL | 데이터 |
| --- | --- | --- |
| 실제 운영 API(백엔드 운영 PC) | `http://127.0.0.1:8020/api/v1` | Oracle 운영 스키마 `MZ_APP` — 이관된 기존 데이터 + 실제 수집 데이터. `meta.data_mode: database`, `observation_origin: live_api` |
| Mock API(어느 PC든) | `http://127.0.0.1:8030/api/v1` | 가상 데이터, 2026-11-03 KST 고정, `meta.data_mode: mock` |

| 앱 실행 위치 | 운영 API 주소 |
| --- | --- |
| 백엔드 PC의 브라우저·데스크톱 앱 | `http://127.0.0.1:8020/api/v1` |
| 백엔드 PC의 Android 에뮬레이터 | `http://10.0.2.2:8020/api/v1` |
| Android 실기기(USB) | 백엔드 PC에서 `adb reverse tcp:8020 tcp:8020` 후 `http://127.0.0.1:8020/api/v1` |
| Mac(iOS 시뮬레이터)·다른 PC·Wi-Fi 실기기 | **접속 불가** — 운영 API는 루프백 전용이다. Mock API로 개발하고, 실데이터는 백엔드 PC의 에뮬레이터·USB 기기로 확인한다 |

외부 공개(인증·TLS·속도 제한)는 별도 과제다. 관리 서버(8000)의 관리 토큰은 앱에 넣지 않는다.

## 실행 (백엔드 담당)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\start-oracle.ps1
powershell -ExecutionPolicy Bypass -File scripts\ops\start-backend.ps1     # 앱 API :8020 + 관리 서버 :8000
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-backend.ps1      # 재기동: stop-backend → start-backend
py -3.12 -m food_trends.app_mock                                           # Mock API :8030 (기존 방법 그대로)
```

## 지금 조회할 수 있는 데이터 (2026-11-08)

| 엔드포인트 | 내용 |
| --- | --- |
| `GET /api/v1/health` | 200, `data_mode: database` |
| `GET /api/v1/foods` | 승인 음식 1개: `두쫀쿠`, `food_id` `FOOD_3007c29a43654e0fb9571c2a2c8c1c86`(코드에 고정하지 말고 목록에서 받기) |
| `GET /api/v1/foods/{food_id}` | 음식 상세(별칭은 정식 이름 1개) |
| `GET /api/v1/foods/{food_id}/timeline?period=7d&source=instagram_graph&end_date=2026-11-08` | 2026-11-08 값 28 `OBSERVED`(coverage `LIMITED`), 그 이전은 `NOT_OBSERVED` |
| `GET /api/v1/foods/{food_id}/metrics?period=7d` | 출처별 지표(현재 기간·이전 기간 비교) |
| `GET /api/v1/trends/rankings?source=instagram_graph&period=7d` | 출처별 순위(통합 점수 없음) |
| `GET /api/v1/sources/status` | 출처별 마지막 성공 수집 시각(RSS·Instagram 2026-11-07T23:26Z) |

- 기본 기간은 **어제(KST)까지**다. 오늘 수집분은 `end_date`를 오늘로 주면 보인다.
- `null`(관측 없음)과 `0`(관측했고 0)은 다르다. 실패한 날·수집하지 않은 날은 `NOT_OBSERVED`.
- YouTube는 음식별 지표가 없다(검색 결과를 음식 언급으로 바꾸지 않는 정책). NAVER는 값을 저장하지 않으므로 지표가 없다 — 오류가 아니다.
- 운영 데이터는 아직 적다(승인 음식 1개, 실제 수집 1회). 빈 상태·미관측 화면이 기본일 수 있다.

## 응답 예시 (요약)

```json
GET /api/v1/foods
{"data": {"items": [{"food_id": "FOOD_3007c29a43654e0fb9571c2a2c8c1c86", "canonical_name": "두쫀쿠", "...": "..."}]},
 "meta": {"api_version": "...", "data_mode": "database", "observation_origin": "live_api", "generated_at": "...", "timezone": "Asia/Seoul", "...": "..."}}
```

## 연결 확인과 오류

| 증상 | 의미 | 조치 |
| --- | --- | --- |
| 연결 거부 | 앱 API 프로세스 없음 | 백엔드 담당이 `start-backend.ps1` |
| `503 STORAGE_UNAVAILABLE` | Oracle이 내려감 | 백엔드 담당이 `start-oracle.ps1`(앱 API는 재시작 없이 회복) |
| `403 FORBIDDEN_HOST` | 도메인 이름으로 접속 | IP·`localhost` 사용 |
| `400 INVALID_PARAMETER` | 명세에 없는 값·범위 | 메시지의 `field`·`reason` 확인 |

## 미지원

앱 사용자 계정·인증, 외부 공개 주소, 통합 유행 점수, YouTube 음식 지표, NAVER 지수 값, 쓰기 API(앱은 읽기 전용).
