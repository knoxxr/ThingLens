# 사전 진단 스크립트 (`tb_diagnose.py`)

운영 ThingsBoard의 규모와 키별 보고 주기를 **아주 적은 부하로** 측정합니다.
결과는 수집 속도·백필 범위·일정 확정에 사용합니다 (개발 계획서 Phase 0).

## 안전성

| 항목 | 내용 |
|---|---|
| 읽기 전용 | GET 조회와 조회용 POST(`/api/entitiesQuery/find`)만 사용. TB 데이터를 변경하지 않음 |
| 부하 | 기본 초당 2건. 응답이 느려지거나 429/5xx가 나오면 자동으로 절반씩 감속, 연속 오류 5회면 60초 정지 |
| Cassandra | 키당 **최근 원시값 20개**만 조회(최근 1일 → 부족하면 30일). 집계 요청(`agg`)을 쓰지 않음 |
| 중단 | Ctrl+C로 언제든 중단 가능. 중단해도 그때까지 결과로 리포트 작성 |
| 설치 | Python 3.8 이상, **표준 라이브러리만 사용** (pip 설치 불필요, 폐쇄망에서 파일 하나만 복사) |

## 준비

1. TB PE에 읽기 전용 사용자 준비 (권장: Generic Role에 `READ`, `READ_ATTRIBUTES`, `READ_TELEMETRY`만 부여)
   - 대상: Device, Asset, Device/Asset Profile, Entity Group, Customer
   - Tenant Administrator 계정으로도 실행 가능 (읽기만 수행)
2. TB에 접속 가능한 사내 서버(앱 설치 예정 서버 권장)에 `tb_diagnose.py` 복사

## 실행

```bash
# 비밀번호는 실행 시 입력 (또는 TB_PASSWORD 환경변수)
python3 tb_diagnose.py --url https://tb.사내주소 --username thinglens-reader@회사도메인

# 먼저 소규모로 시험 (엔티티 20개만)
python3 tb_diagnose.py --url ... --username ... --max-entities 20 --out test_report

# 사내 CA로 서명된 인증서
python3 tb_diagnose.py --url ... --username ... --ca-bundle /path/company-ca.pem
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--rate` | 2.0 | 최대 요청 속도 (req/s). 피크 시간이면 `1` 이하 권장 |
| `--plan-rate` | 2.0 | 리포트의 수집량·소요 시간 추정에 쓸 운영 수집 속도 |
| `--sample-points` | 20 | 키당 조회할 최근 원시값 수 |
| `--max-entities` | 0(전체) | 시험용 엔티티 수 제한 |
| `--skip-attributes` | - | 속성 키 조회 생략 (요청 약 20% 감소) |
| `--skip-relations` | - | 관계 조회 생략 (요청 약 40% 감소) |
| `--out` | `tb_diagnose_report` | 출력 디렉터리 |

**예상 소요 시간**: 디바이스 500 + 에셋 100 기준 약 3,200건 → 초당 2건이면 **약 25~30분** (실행 시작 시 예상치를 화면에 표시)
(`--skip-relations --skip-attributes` 사용 시 약 12분)

## 결과물

| 파일 | 내용 | 공유 |
|---|---|---|
| `summary.md` | 규모, 보고 주기 분포, 수집량 추정, API 지연, 프로파일별 키 목록 | **공유 가능** — 엔티티 이름·ID 없음 (프로파일·키 이름은 포함) |
| `summary.json` | 위 내용의 JSON | 공유 가능 |
| `details.jsonl` | 엔티티별 이름·ID·키·주기·관계 수 | **사내 보관** |

`summary.md`(또는 `summary.json`)를 개발팀에 전달해 주세요. 프로파일·키 이름도 외부 공유가 곤란하면 알려 주세요 — 익명화 옵션을 추가하겠습니다.

## 리포트로 결정하는 것

- 1초 주기 키 수 → 고빈도 백필 범위(7일/30일)와 야간 수집 일수
- API 응답 지연(p50/p95) → 적응형 스로틀링 기준값
- 관계 없는 디바이스 수, 관계 유형 → 토폴로지 분석 우선순위
- 키 이름 분포 → 의미 추론 사전 초기값
- TB 버전 (리포트에 표시되지 않으면 TB 화면에서 확인)

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
모의 TB 서버(`tests/mock_tb_server.py`)로 로그인·토큰 갱신·429 재시도·리포트 익명성까지 검증합니다.
