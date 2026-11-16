# ThingLens

ThingsBoard(PE)에 저장된 디바이스·에셋 정보의 상관관계를 분석하는 앱입니다.
ThingsBoard에 부하를 주지 않도록 백그라운드에서 천천히 수집하고, 로컬 캐시로 분석합니다.

- 개발 계획서: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)
- 사전 진단 스크립트 (Phase 0): [tools/README.md](tools/README.md)

## 현재 단계

Phase 0 — 사전 진단 스크립트 완료. 운영 TB에서 실행 후 `summary.md`를 공유받아 수집 정책을 확정합니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
