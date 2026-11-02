# G-OPS-0 개발용 Oracle 환경과 B6~B7 실제 Oracle 검증

2026-11-02. 개발용 Oracle을 확보해 B6 Repository 계약과 B7 이관·대조·Shadow를 **실제 Oracle에서** 실행했다. 모두 통과했으므로 **G-OPS-0은 완료로 판정한다.** 운영 SQLite·서버·외부 API는 변경하거나 호출하지 않았다. 이전 보고서의 Oracle 관련 NOT_VERIFIED 항목은 이 문서의 결과로 대체한다([B6~B7 REPORT](../development-b6-b7-2026-11-02/REPORT.md)).

## 1. OS와 설치 요구사항

| 항목 | 공식 요구사항(Windows판) | 이 PC | 판단 |
| --- | --- | --- | --- |
| OS | Windows 10/11 x64 Pro·Enterprise·Education, Windows Server | Windows 11 **Home** 10.0.26200 x64 | Windows판은 지원 대상 아님 → 컨테이너 방식 선택 |
| 권한 | Administrators 직접 구성원 | 구성원(UAC 승격 필요) | 충족(UAC 승인으로 실행) |
| RAM | 최소 2GB | 15.7GB(가용 0.7~1.7GB로 변동) | 충족, 여유 메모리 적음 |
| 디스크 | 8.5GB + 임시 2GB | D: 432GB 여유, C: 17.9GB | D:에 WSL 디스크 배치 |

근거: [Oracle AI Database Free for Windows Requirements](https://docs.oracle.com/en/database/oracle/oracle-database/26/xeinw/requirements.html).

## 2. 설치 방식과 승인 기록

| 단계 | 내용 | 승인 |
| --- | --- | --- |
| G0-A | 읽기 전용 점검(OS·RAM·디스크·권한·기존 Oracle·포트·Python·드라이버) | 요청서 범위 |
| 방식 선택 | WSL2 + Oracle Free 컨테이너(권장안) | 사용자 선택 |
| 드라이버 | 프로젝트 `.venv`에 `python-oracledb==26.1.0` | 사용자 선택 |
| WSL 활성화 | `wsl --install --no-distribution`(가상 머신 플랫폼) | UAC 확인 창에서 사용자 승인 |
| 재부팅 | 1회 | 사용자가 직접 수행 |
| 배포판·컨테이너 | Ubuntu 24.04.5를 `D:\WSL\Ubuntu-24.04`에 설치, Podman 4.9.3, 공식 이미지 `container-registry.oracle.com/database/free:latest` | 승인된 방식의 범위 |

Windows에 Oracle 서비스를 등록하지 않았다. 유료 클라우드 자원은 쓰지 않았다. 환경 상세는 [environment.json](environment.json)에 있다.

## 3. 서버·드라이버·테스트 스키마

- 서버: Oracle AI Database 26ai Free **23.26.3.0.0**, PDB `FREEPDB1`, 문자셋 AL32UTF8, 포트 `127.0.0.1:1521`만 게시, 데이터는 Podman 이름 볼륨.
- 드라이버: `python-oracledb` 26.1.0 Thin 모드(의존: cryptography 50.0.2, cffi 2.1.1), Python 3.12.1 `.venv`(Git 제외).
- 테스트 스키마 `MZ_TEST`: `CREATE SESSION`·`CREATE TABLE`, `USERS` 할당량 2GB, 역할 없음. 관리자·테스트 비밀번호는 무작위 생성해 Git 제외 경로 `data/oracle-dev/`에만 저장했고 출력·커밋하지 않았다.
- 초기화 보호: `tests/oracle_support.reset_target_schema`가 매번 다음을 확인한 뒤에만 테이블을 지운다. 접속 사용자가 `ORACLE_TEST_USER`이고, 내장·DBA 계정이 아니며, `ORACLE_TEST_ENVIRONMENT=test`이고, 스키마에 대상 명세 밖 객체가 없어야 한다.

## 4. 테스트 결과

| 실행 | 결과 |
| --- | --- |
| 실제 Oracle 포함 전체 | **356 실행, 실패·오류·skip 0**([oracle-full-tests](oracle-full-tests/test-summary.json)) |
| 그중 실제 Oracle | 계약 16(기존 15 + DB 저장 순간값 검사 1), B7 이관 8, Shadow 8 = **32 전부 통과** |
| 기본 환경(Oracle 없음) | 324 실행 통과, Oracle 32 skip([default-tests](default-tests/test-summary.json)) |

이전 기준 323에서 비Oracle 테스트가 324가 됐다. 순증 +1이며 내역은 다음과 같다.
- +1: SQLite 저장 순간값 검사
- +1: FK 고아 검출 검사
- −1: 테스트로 잘못 집계되던 픽스처 생성기 제외

계약 15개 범위: 연결·트랜잭션, 반복 마이그레이션, PK·FK·UNIQUE·CHECK, 한글·이모지·조합형, UTC/KST/DST 왕복, ''와 NULL, JSON, 큰 정수, 동시 업서트, UNIQUE 경쟁 재조회(ORA-00001), Outbox 재전달, origin 거부, NAVER·YouTube 저장 차단, 만료·불변 열, FOOD_ID 조회.

## 5. 실제 Oracle 이관과 Shadow (보존 스냅샷 복제본)

보존 스냅샷(SHA-256 `09e323f9…`, 전후 동일)의 임시 복제본을 사용했다. 순서: v0→v11 → B4/B5 보강 → 복제본 안에서만 테스트용 음식 승인 → 실제 Oracle `MZ_TEST`로 이관([b7-oracle-copy-migration.json](b7-oracle-copy-migration.json)).

| 항목 | 결과 |
| --- | --- |
| 적재 | 16,576행, 제외 0, 거부 0 |
| 대조 | 29개 테이블 누락·초과·변경 0, 정규화 해시 전부 일치, NULL 수 일치 |
| FK | 고아 행 0(실측), 검증되지 않은 FK 제약 0 |
| 원본 참조·정책·TTL | 미해결 `payload_ref` 0, 정책 위반 행 0, 대상 내 만료 행 0, 출처 불명 711건 `legacy_unknown` 유지 |
| FOOD_ID 조회 | SQLite와 Oracle 결과 일치 |
| 재이관 | 쓰기 0, 건수 불변 |
| Shadow | 초기 이관 후 변경 7건 캡처, 7건 전달, 실패 0, 전달 후 정합성 일치 |

이 스냅샷은 YouTube(29일)·Instagram 캡션(30일) 기한 안이라 실데이터 만료 제외는 0건이다. 만료 제외·연쇄 제외·캡션 제거·TTL 원값 유지는 실제 Oracle 이관 테스트에서 별도로 통과했다.

## 6. SQLite 대역과 Oracle의 동작 차이

| 항목 | SQLite 대역 | 실제 Oracle | 영향·조치 |
| --- | --- | --- | --- |
| 세션 시간대 | 해당 없음 | 클라이언트 PC 기준 `+09:00`으로 설정됨 | 드라이버가 시간대 있는 값을 오프셋과 함께 바인드해 저장값은 정확히 `+00:00`. DB 저장값을 직접 확인하는 계약 테스트 추가 |
| TSTZ 조회 | UTC ISO 문자열 | 오프셋 없는 naive datetime 반환 | 모든 쓰기를 UTC로 정규화하므로 naive=UTC 해석이 정확. 다른 Writer가 비UTC 오프셋으로 쓰면 깨지는 전제이므로 계약으로 유지 |
| 지연 FK 위반 | COMMIT 시 `FOREIGN KEY constraint failed` | COMMIT 시 ORA-02091/ORA-02291 | 기존 매핑이 정확히 `foreign_key_violation`으로 분류(Shadow 순서 테스트로 확인) |
| UNIQUE 경쟁 | `BEGIN IMMEDIATE`로 직렬화 | 두 번째 세션이 대기 후 ORA-00001 | 재조회·갱신 경로로 수렴(동시 업서트 테스트 통과) |
| FK 검증 방식 | `PRAGMA foreign_key_check` | 이전에는 측정하지 않고 "제약으로 보장"이라고 기록 | **결함으로 보고 수정**: 두 DBMS 모두 고아 행 실측과 Oracle 제약 상태 확인 |
| 처리 속도(참고) | 약 1,800행/초 | 약 590행/초, 단건 업서트 2ms, FOOD_ID 조회 7.7ms | 개발 PC 기준 참고치 |
| 용량 | 11.4MB | 세그먼트 29.2MB | Oracle 세그먼트 할당 단위 차이 |

## 7. 결함과 수정

| 결함 | 원인 | 수정 | 커밋 |
| --- | --- | --- | --- |
| Oracle 대조의 FK 결과가 측정값이 아니었음 | 대조 함수가 Oracle에서 고정 문자열을 반환 | 선언된 모든 FK의 고아 행 실측과 Oracle 제약 상태 확인, 0일 때만 정합 판정. 고아를 실제로 잡는지 검증하는 테스트 추가 | `c6403f7` |
| 테스트 수 1개 과대 집계 | 픽스처 생성기의 빈 `runTest`가 테스트로 발견됨 | 생성기를 비테스트 메서드로 생성 | `c6403f7` |

Repository·DDL·변환 코드는 실제 Oracle에서 수정 없이 통과했다. 계약을 바꾼 것은 없다.

## 8. Git

| 커밋 | 내용 |
| --- | --- |
| `eafef71` | test: validate Oracle repository against real database |
| `c6403f7` | fix: measure foreign-key orphans in Oracle reconciliation |
| 이 보고서 커밋 | docs: record G-OPS-0 verification results |

`main`에 fast-forward Push했다. `.venv`, `data/oracle-dev/`(자격 증명), 테스트 로그는 Git 제외 상태다.

## 9. 운영 영향

- 운영 SQLite는 v0·30개 테이블·integrity ok이며 행 수가 이번 단계 시작 때와 같다([production-final.json](production-final.json)).
- 8000/8002 서버는 꺼진 상태 그대로다.
- 실제 Instagram·YouTube·NAVER 호출은 0건이다.
- PC 수준 변경은 승인 범위 안에서만 했다: WSL 3.0.3 설치·가상 머신 플랫폼 활성화, D:\WSL 배포판, 프로젝트 `.venv`.

운영 메모: WSL은 연결된 세션이 없으면 배포판을 유휴 종료한다. 검증 중에 한 번 종료되어 Oracle 접속이 거부된 일이 있었다. 이후 `podman start --attach mz-oracle-free`를 백그라운드로 유지하는 방식으로 진행했다. 다시 쓸 때는 다음 명령으로 기동한다. 데이터와 `MZ_TEST`는 볼륨에 남아 있다.

```powershell
wsl -d Ubuntu-24.04 -u root -- podman start mz-oracle-free
```

## 10. 게이트 상태

| 게이트 | 상태 |
| --- | --- |
| G-OPS-0 개발용 Oracle·드라이버 | **완료** |
| G-OPS-1 운영 SQLite 백업 | 승인 대기, 미실행 |
| G-OPS-2 Writer 정지 | 승인 대기(현재 서버 정지 상태), 미실행 |
| G-OPS-3 운영 SQLite v0→v11 | 승인 대기, 미실행 |
| G-OPS-4 수집 소유권 이전 | 승인 대기, 미실행 |
| G-OPS-5 Live API 검증 | 승인 대기, 미실행(Instagram recent_media 전제 포함) |
| G-OPS-6 Oracle 운영 전환 | 승인 대기, 미실행 |

## 11. 후속 과제와 우선순위

1. **B3~B5 서비스의 Oracle 직접 실행**: Lease 선점(`FOR UPDATE SKIP LOCKED`), B4 스테이징, B5 연결의 쓰기 경로. G-OPS-6의 전제다.
2. **전환 후 역방향 복구**: Oracle에만 생긴 행을 SQLite나 복구 저장소로 되돌리는 동기화.
3. **제외한 legacy 테이블의 행별 이관 정책**: 원시 호출 기록, 태그 단위 집계 등.
4. **TSTZ 조회 강건성**: 비UTC 오프셋으로 쓰는 다른 Writer가 생길 경우에 대비해 조회 시 UTC 추출(output type handler 등)을 검토한다. 현재는 단일 Writer·UTC 정규화로 충분하다.
5. **실제 운영 규모 성능 측정**: G-OPS-6 대상 서버에서 다시 측정한다.
