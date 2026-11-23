# MZ제조기 모바일 프론트엔드

기존 static/ 관리 화면 및 food_trends/ 수집·DB 코드는 변경하지 않습니다.

## 실행 (저장소 루트)
```bash
python3 frontend/serve.py
```
http://127.0.0.1:8088 에서 확인합니다.
같은 PC에서 팀 B8 서버가 실행 중이면 `python3 frontend/serve.py --api-port 8030`으로 연결합니다. 실제 B8 포트를 확인하고, 화면의 자료 출처 설정에서 로컬 API를 선택하세요. 제품 JSON은 제품 자료 열기에서 가져옵니다.

## 검증
```bash
node --test frontend/tests/data-core.test.cjs
python3 -B frontend/tests/test_server.py
```
DOM 검증은 jsdom 설치 환경에서 `node frontend/tests/product-dom.cjs`를 실행합니다.

## 배포와 한계
`python3 frontend/build_static.py`는 frontend/dist/를 생성합니다. 기존 개인 Sites ID, 배포 자격정보, Git 이력은 이관하지 않습니다. 팀 배포 설정은 합의 후 별도로 연결합니다. 현재 계정은 시안, 검토 저장은 브라우저 로컬이며 B8은 읽기 전용입니다.
서비스 화면과 API의 연결 범위는 SERVICE_FLOW.md를 참고하세요.
