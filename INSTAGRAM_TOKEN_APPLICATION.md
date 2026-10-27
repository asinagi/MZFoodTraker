# Instagram 장기 토큰 적용 결과

2026-10-26 15:28 KST 검증 완료. 사용자가 명시적으로 승인한 검증된 장기 USER 토큰을 적용했다. 추가 토큰 교환은 수행하지 않았다.

| 확인 항목 | 결과 |
| --- | --- |
| 토큰 교체 | 성공. `.env`의 `INSTAGRAM_ACCESS_TOKEN` 값만 변경 |
| 다른 환경변수 | 원본과 바이트 단위 비교로 보존 확인 |
| 원본 `.env` | Git 제외 경로의 별도 `.secret` 백업으로 보존 |
| 서버 | 8000 포트 재시작, 새 자격증명 로드 확인 |
| 토큰 | 유효한 USER, 앱 일치, 필수 권한 3개 확인 |
| 확인된 만료일 | **2026-12-25 09:55:03 KST** (`2026-12-25T00:55:03+00:00`) |
| 계정 연결 | Facebook 페이지와 설정된 Instagram 계정 연결 확인 |
| 해시태그 조회 | 기존 `#두쫀쿠` 한 태그만 조회, 기존 ID 일치 |
| 수집 | 게시물 50건: 신규 2건·중복 48건 |
| 기존 데이터 | 게시물 70건·공동 출현 태그 164개 모두 보존 |
| 현재 데이터 | 고유 게시물 **72건**, 공동 출현 태그 **185개** |
| DB 무결성 | `integrity_check=ok`, 외래 키 검사 통과 |
| 중복 방지 | 중복 행 추가 0건, 누적 고유 수와 시간·일 집계 일치 |
| 추적 대상 | 기존 1개 유지, 발견 태그 추가 검색·등록 없음 |
| 테스트 | 전체 **91개 통과**, JS 문법·Chrome 경고 표시 검증 통과 |

## 최소 실제 API 검증

실제 HTTP 요청 총 **5회**만 실행했다. 토큰 검사 1회, 서버 자체의 계정 연결·최근 검색 예산 확인 2회, 기존 기준 해시태그 조회 1회, 게시물 한 페이지 수집 1회다. 수집 검증 프로세스에만 최대 한 페이지를 지정했으며 `.env`나 메인 서버의 기존 페이징 설정은 변경하지 않았다. 추가 페이지가 있어 해당 회차는 `limited`로 기록했다. API 전체 발화량을 완전히 수집한 것으로 표시하지 않는다.

서버 재시작 직후 자격증명 지문이 승인된 토큰과 일치하고 토큰 상태가 새 검사 전 `unchecked`인 것을 확인했다. 계정 연결 검사는 실행 중인 서버의 API로 요청하여 서버가 새 토큰을 사용하는지도 확인했다. 이후 토큰 검사 결과를 내부 DB에 저장하고, 서버 API가 새 만료일과 정상 연결 상태를 반환하는 것을 확인했다. 지문과 자격증명 자체는 보고서에 포함하지 않았다.

최초 70건의 Media ID 집합과 164개 공동 태그 집합을 적용 전 스냅샷과 비교했다. 전부 남아 있으며, 신규 2건만 누적 수에 추가됐다. 재수집된 48건은 신규량에 더하지 않았다. 기존 원문·태그·관측 이력과 다른 수집원은 삭제하지 않았다.

## 만료 관리

유효성 검사로 확인한 만료일을 기존 `instagram_state`에 저장한다. 화면 상태 조회 시 서버가 남은 기간을 계산하며, 외부 API를 추가 호출하지 않는다. **만료 7일 전부터** Instagram 관리 영역 상단에 경고를 표시한다. 만료 후에는 자동 수집 중단과 새 토큰 설정·재검사 안내를 표시한다. 지금 토큰은 정상 기간이므로 경고를 표시하지 않는다. 상세의 **연결 · 토큰 · 검색 예산**에서 만료 시각을 확인할 수 있다.

임시 DB와 실제 Chrome으로 만료 예정·만료 상태의 경고가 보이고 정상 토큰에서는 사라지는 것을 검증했다. 이 검증은 실제 DB 만료일을 변경하거나 Meta API를 호출하지 않는다. 앱이 종료된 동안에는 화면 경고가 표시되지 않으며 다음 실행 시 저장된 만료일로 다시 계산한다. 자동 토큰 교환·자동 교체는 수행하지 않는다.

## 보존·비밀값 검사

`.env`의 줄바꿈·주석·인코딩과 토큰 외 모든 바이트를 보존했다. 기존 파일 전체는 `data/instagram-env-before-approved-apply.secret`에 백업했다. `.env`, `*.secret`, `data/`는 기존 `.gitignore`의 제외 대상이다. 현재 작업 디렉터리는 Git 저장소가 아니므로 커밋·스테이징·리셋은 수행하지 않았다. 다른 작업의 소스 변경도 보존했다.

기존·새 토큰과 App Secret 문자열이 로그·JSON 결과·Markdown 보고서에 포함되지 않았는지 값 자체를 출력하지 않고 검사했다. 비밀 파일 내용은 콘솔과 보고서에 출력하지 않았다.

## 변경 파일·검증 자료

- `food_trends/instagram_service.py`: 저장된 만료일 기준의 경고 상태·남은 일수 계산.
- `static/instagram.js`: 관리 화면 상단의 접근성 알림(`role=alert`).
- `tests/test_instagram.py`: 정상·7일 이내·만료·무효·미검사 상태, 외부 호출 없는 계산과 수집 차단 검사.
- `tests/instagram_token_warning_browser_check.py`: 실제 Chrome의 경고 표시·해제와 실제 앱 만료일 표시 확인. 외부 API 요청 없음.
- `.env`: 승인한 `INSTAGRAM_ACCESS_TOKEN` 값만 변경.
- `data/instagram-approved-token-verification.json`: 실제 API 5회 검증과 DB 무결성 결과.
- `data/instagram-token-warning-browser-verification.json`: 브라우저 검증 결과.
- `data/instagram-token-expiry-warning.png`: 임시 DB의 만료 예정 경고 화면.
- `data/instagram-approved-token-desktop.png`, `data/instagram-approved-token-mobile.png`: 적용 후 실제 화면.

기존 [해시태그 분석 보고서](INSTAGRAM_HASHTAG_ANALYSIS.md)의 70건·164개는 토큰 적용 이전 스냅샷이다. 최신 적용 결과는 이 문서를 기준으로 확인한다.

```powershell
# 현재 서버: http://127.0.0.1:8000
# 로컬 상태 조회 (외부 요청 없음)
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-status
# 이후 관리자가 필요할 때만 실제 토큰 유효성 재검사
powershell -ExecutionPolicy Bypass -File .\run.ps1 --instagram-token-check
# 테스트
& 'C:\Users\gjgjs\anaconda3\python.exe' -m unittest discover -s tests -v
node --check static/instagram.js
```
