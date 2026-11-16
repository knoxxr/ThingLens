# ThingLens

ThingsBoard(PE)에 저장된 디바이스·에셋 정보의 상관관계를 분석하는 앱입니다.
ThingsBoard에 부하를 주지 않도록 백그라운드에서 천천히 수집하고, 로컬 캐시로 분석합니다.

- 개발 계획서: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)
- 사전 진단 스크립트·테스트 환경 (Phase 0): [tools/README.md](tools/README.md)
- 백엔드 (Phase 1): [backend/README.md](backend/README.md)

## 현재 단계

Phase 0 — 사전 진단 스크립트, 테스트 TB 환경(`deploy/test-env`), 시드 데이터, Cassandra 실측 완료.
Phase 1 — 백엔드(저부하 스케줄러, 메타데이터 동기화, 변경 이력, 접근 제어, REST API) 구현·실제 TB 검증 완료. 웹 화면 진행 예정.

## 테스트

```bash
python3 -m unittest discover -s tests -v      # 진단·시드 도구
cd backend && .venv/bin/pytest -q             # 백엔드
```
