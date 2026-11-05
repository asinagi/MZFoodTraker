# Phase 0 — 잘못 만든 임시 디렉터리 정리 기록

2026-11-05, ORACLE-LIVE-INTEGRATION-1. 이전 작업(ORACLE-BACKEND-COMPLETION-1)의 관리 화면 점검을 상대 출력 경로로 실행하면서 `D:\` 드라이브에 사용자 프로필처럼 보이는 경로가 잘못 만들어졌다.

## 확인

| 항목 | 결과 |
| --- | --- |
| 구조 | `D:\Users` 아래 디렉터리 9개(`gjgjs\AppData\Local\Temp\claude\d--mz----\7006427b-…\scratchpad\browser1`까지 한 줄), **파일 0개** |
| 생성 시각 | `D:\Users`를 포함한 10개 모두 2026-11-05 19:45:52(점검 실행 시각)에 생성 |
| 원래 파일 | 점검 결과 2개(`oracle-admin-browser-check-standin.json` 762바이트, `oracle-admin-ui-standin.png` 179,999바이트)는 이미 세션 scratchpad(`C:\Users\gjgjs\AppData\Local\Temp\claude\…\scratchpad\browser1`)로 옮겨져 있었고, 보고용 사본은 `reports/development-oracle-backend-completion-2026-11-05/`에 별도로 다시 만들어져 커밋돼 있음 |

## 삭제한 경로 (빈 디렉터리만, 가장 깊은 곳부터 하나씩, 재귀 삭제 없음)

각 디렉터리가 비어 있고 생성 시각이 점검 시각과 같은지 확인한 뒤 비재귀 삭제(`[System.IO.Directory]::Delete(path, $false)`)했다. `Remove-Item -Recurse -Force`는 쓰지 않았다.

1. `D:\Users\gjgjs\AppData\Local\Temp\claude\d--mz----\7006427b-19cc-40ef-bb73-e185e79dc7d7\scratchpad\browser1`
2. `D:\Users\gjgjs\AppData\Local\Temp\claude\d--mz----\7006427b-19cc-40ef-bb73-e185e79dc7d7\scratchpad`
3. `D:\Users\gjgjs\AppData\Local\Temp\claude\d--mz----\7006427b-19cc-40ef-bb73-e185e79dc7d7`
4. `D:\Users\gjgjs\AppData\Local\Temp\claude\d--mz----`
5. `D:\Users\gjgjs\AppData\Local\Temp\claude`
6. `D:\Users\gjgjs\AppData\Local\Temp`
7. `D:\Users\gjgjs\AppData\Local`
8. `D:\Users\gjgjs\AppData`
9. `D:\Users\gjgjs`

## 보존한 경로

| 경로 | 이유 |
| --- | --- |
| `D:\Users` | 같은 시각에 만들어진 빈 디렉터리지만 요청대로 `D:\Users` 자체는 삭제하지 않음(현재 비어 있음). 필요하면 사용자가 직접 지우면 된다 |
| `D:\mz 제조기\data\b8-app-api-worktree-logs` | 요청대로 유지(존재 확인) |
| 세션 scratchpad의 `browser1` 결과 2개 | 원래 산출물. 저장소에는 보고 디렉터리 사본이 있음 |

처음 시도한 경로 계산 스크립트(PowerShell `Split-Path -LiteralPath … -Parent` 조합이 5.1에서 매개변수 집합 오류로 반복)는 삭제 단계 전에 멈췄고, 삭제 전후 개수(디렉터리 9개 → 0개)로 아무것도 지워지지 않았음을 확인한 뒤 고정 목록으로 다시 실행했다.
