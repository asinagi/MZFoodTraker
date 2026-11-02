# STORAGE-1 저장 위치 감사

2026-11-02. 프로젝트와 핵심 데이터는 모두 이미 D 드라이브에 있어 **이동한 항목이 없다(감사 완료).** 감사는 알려진 프로젝트 경로·설정·WSL 위치만 대상으로 했고, C 드라이브 전체를 검색하지 않았다. 비밀값은 출력·기록하지 않았다.

## 분류

| 항목 | 실제 경로 | 크기 | 분류 | 비고 |
| --- | --- | --- | --- | --- |
| Git 저장소·작업 트리 | `D:\mz 제조기` (`.git`, worktree 1개) | 코드 5.5MB, `.git` 2.3MB | ALREADY_ON_D | HEAD `5793d39` = origin/main |
| 운영 SQLite | `D:\mz 제조기\data\trends.sqlite3` (+ `-wal` 0B, `-shm`) | 8.9MB | ALREADY_ON_D | v0, 30개 테이블, integrity ok, 읽기 전용으로만 확인 |
| 보존 스냅샷·이전 백업 | `D:\mz 제조기\data\*-preservation-*`, `data\audit-20261026` | 약 220MB | ALREADY_ON_D | B3 스냅샷 SHA-256 `09e323f9…` 불변 |
| 브라우저 QA 프로필·로그 | `D:\mz 제조기\data\*chrome*` 등 | 약 680MB | ALREADY_ON_D | Git 제외 |
| Oracle 테스트 자격 증명 | `D:\mz 제조기\data\oracle-dev\` | 수 KB | ALREADY_ON_D | Git 제외, 값 비공개 |
| Python 가상환경 | `D:\mz 제조기\.venv` | 36MB | ALREADY_ON_D | D 경로에서 생성됨, `oracledb` 26.1.0 정상. 재생성 불필요 |
| WSL 배포판 | `D:\WSL\Ubuntu-24.04\ext4.vhdx` (레지스트리 BasePath 동일) | 16GB | ALREADY_ON_D | Ubuntu 24.04.5 |
| Podman 이미지·컨테이너·볼륨 | vhdx 내부 `/var/lib/containers/storage` | 이미지 10.1GB, 볼륨 `mz-oracle-data` 4.7GB | ALREADY_ON_D | 컨테이너 `mz-oracle-free` 정지 상태 유지 |
| 컨테이너 env 파일 | vhdx 내부 `/root/mz-oracle/oracle.env` (권한 600) | 38B | ALREADY_ON_D | |
| 전역 Python 3.12 | `C:\Users\gjgjs\AppData\Local\Programs\Python\Python312` | — | KEEP_ON_C | `.venv`의 기반 인터프리터. 전역 설치 변경 금지 |
| pip 캐시 | `C:\Users\gjgjs\AppData\Local\pip\cache` | 277MB | KEEP_ON_C | 사용자 전역 캐시, 프로젝트 비의존 |
| WSL 프로그램 | `C:\Program Files\WSL` | 1.2GB | KEEP_ON_C | 시스템 설치 위치 |
| WSL 실행 중 swap | `%TEMP%\{GUID}\swap.vhdx` | 각 약 0.04GB, 최대 약 RAM 25% | APPROVAL_REQUIRED(선택) | VM 실행 중 C에 생성. D로 옮기려면 사용자 전역 `.wslconfig`의 `swapFile` 설정 필요 |
| 시스템 페이지 파일 | `C:\pagefile.sys` | 13.8GB | KEEP_ON_C | Windows 시스템 영역, 범위 밖 |
| 테스트 임시 DB | `%TEMP%` 아래 임시 폴더 | 실행 중 수 MB | KEEP_ON_C | 실행 후 자동 삭제 |
| VS Code | 프로젝트 `.vscode` 없음, 인터프리터 선택은 VS Code 전역 상태 | — | 변경 없음 | 원하면 `.venv\Scripts\python.exe` 선택 |

MOVE_REQUIRED, RECREATE_RECOMMENDED 항목은 없다.

## 설정 경로

- `DATABASE_PATH`는 `.env`와 환경변수 어디에도 설정되지 않았다. 기본값은 상대경로 `data/trends.sqlite3`다.
- `run.ps1`은 스크립트 위치로 작업 디렉터리를 옮기므로 항상 `D:\mz 제조기\data\trends.sqlite3`를 쓴다.
- Oracle 테스트 설정(`data/oracle-dev/oracle-test.env`)은 `localhost:1521/FREEPDB1`이라 경로 의존성이 없다.

변경한 설정은 없다.

## 검증 (이동 없음, 현 상태 확인)

| 항목 | 결과 |
| --- | --- |
| Git | 원격 연결 정상, 작업 트리 깨끗 |
| 기본 전체 테스트 | 324 통과, Oracle 32 skip(정상), 외부 소켓 0 |
| 운영 SQLite | v0 유지, integrity ok, 행 수 불변(runs 95, items 370, instagram_media 185, youtube_videos 431) |
| 보존 스냅샷 | 해시 불변 |
| Oracle | 기존 컨테이너를 일시 기동해 `MZ_TEST` 로그인 확인(23.26.3, 대상 스키마 v1, 34개 테이블·데이터 보존) 후 다시 정지 |
| 디스크 여유 | C 21.7GB, D 416.7GB (D 감소분은 G-OPS-0의 WSL 디스크 16GB) |

운영 서버는 정지 상태 그대로이고 외부 API 호출과 운영 데이터 변경은 없다.

## 후속·수동 조치

1. **(선택, 승인 필요) WSL swap 위치를 D로 옮기기.** Oracle 실행 중 C에 최대 약 4GB의 swap이 생길 수 있다. `%USERPROFILE%\.wslconfig`에 `[wsl2]` / `swapFile=D:\\WSL\\swap.vhdx`를 넣으면 옮겨진다. 이 설정은 모든 WSL 배포판에 적용되는 사용자 전역 설정이다.
2. **(후속 과제) 기본 DB 경로가 상대경로다.** `run.ps1` 밖, 예를 들어 다른 폴더에서 `python -m food_trends --migrate`를 실행하면 그 위치에 새 DB가 생길 수 있다. 보호 장치가 있지만 범위가 좁다. 일반 기동은 미이관 DB에서 중단되지만, `--migrate`는 그 상대경로에 새 DB를 만들 수 있다. 실제로 이전이 없어서 이번에는 수정하지 않았다.
3. **(선택) 남은 이전 swap 파일.** `%TEMP%\BC59D5E6-…\swap.vhdx`(0.04GB)는 이전 WSL 실행의 잔여물로 보인다. 크기가 작아 그대로 두었다.
4. **(선택) VS Code 인터프리터.** Oracle 테스트를 IDE에서 돌리려면 `.venv\Scripts\python.exe`를 선택한다.
