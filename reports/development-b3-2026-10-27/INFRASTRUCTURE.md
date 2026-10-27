# B3 실행 인프라

2026-10-27. 기준 HEAD c159a47, origin/main 동일. 시작 시 미커밋 변경 없음. baseline.json의 clean_at_start=false는 기준 테스트 산출물을 새 보고서 디렉터리에 만든 후의 측정이며 기존 사용자 변경을 뜻하지 않는다.

명시적 migration v6에 실행 작업·매핑·Outbox·Lease·Run·Call·Checkpoint·결과 대상·계정 차단 테이블을 추가했다. 기존 테이블 데이터의 재분류·변경은 하지 않는다. 운영 DB는 v0을 유지했고, 읽기 전용 online backup의 integrity_check=ok를 확인했다. 기존 8000/8002 listener를 중지하거나 재시작하지 않았다.

QueryPlanner의 불변 spec/hash·계약을 재사용한다. enqueue는 유효한 계획을 확인하고 job+outbox+tracking slot을 같은 트랜잭션에 저장한다. 공유 query/slot의 대상 매핑을 합치며 실행을 시작한 세대의 매핑은 고정한다. 재개로 바뀐 대상 generation은 별도 execution_generation으로 관리한다. RSS 실행은 기존 고정 feed scheduler 소유로 차단한다.

Worker claim은 BEGIN IMMEDIATE로 직렬화하고 owner/fencing_generation/만료를 결과 저장 트랜잭션 안에서 재검사한다. 각 요청 전에 quota 예약·시도 의도·Call을 확정하고 DB 연결을 닫은 뒤 HTTP를 호출한다. quota.reserve/mark_attempted에는 기존 동작을 보존하는 선택적 connection 인수를 추가해 fencing과 같은 트랜잭션에 넣었다.

페이지별 저장은 호출 완료·기존 출처 저장·checkpoint를 하나의 fenced transaction으로 확정한다. checkpoint에는 cursor/ID/페이지 메타데이터만 허용하고 7일 후에는 재생하지 않는다. Outbox/Call에 응답 본문을 넣지 않는다. 성공한 페이지 이후 중단되면 이미 저장한 페이지의 HTTP를 반복하지 않는다. 전송 의도 이후 중단은 실제 전송 여부를 알 수 없으므로 UNKNOWN 및 remote_duplicate_possible로 남기고 기존 예약을 환불하지 않는다. 원격 exactly-once는 보장하지 않는다.

상태는 READY/CLAIMED/RUNNING/SUCCEEDED/PARTIAL/LIMITED/RETRY_WAIT/BLOCKED/FAILED/CANCELLED다. 성공 Call이 없으면 SUCCEEDED를 확정할 수 없다. 정상 0건은 성공 호출 기록과 함께 저장한다. 승인 철회·정책 변경·중단 시 대상 매핑을 다시 검사한다. 공유 요청의 다른 활성 대상은 유지한다.

기본 live_enabled=false, automatic=false다. Live에는 승인된 registry와 출처별 legacy writer 소유권 이전 설정이 추가로 필요하다. 인증/권한 오류는 계정 범위를 차단한다. Rate limit은 Retry-After를 줄이지 않는다. 최대 workflow 시도는 기본 3회, 지수 지연은 10/20/40초이며 공급자 지연이 더 길면 그 값을 사용한다. 공급자 사용량 확인은 항상 별도이며 현재 null/false다.

신규 18개 infrastructure 회귀 및 기존 199개 검증은 infrastructure-tests/test-summary.json에 기록한다. 개발 검증은 격리 DB와 가상 실행 함수만 사용한다. 출처 adapter·UI/API·실행 배포 계약은 다음 작업 단위에서 추가한다.
