# ThingLens 백엔드 (Phase 1–2)

ThingsBoard를 **저부하로 천천히** 읽어 로컬 DB(SQLite)에 메타데이터를 쌓고, REST API로 제공합니다.

```
┌──────────────────────┐   읽기 전용·저속   ┌─────────────┐
│ thinglens scheduler  │ ─────────────────▶ │ ThingsBoard │
│  (백그라운드 워커)    │                    └─────────────┘
└──────────┬───────────┘
           │ 쓰기                SQLite (WAL, 두 프로세스 공유)
           ▼
     data/thinglens.db ◀────── 읽기 ────── thinglens api (REST)
```

## Phase 1 구현 범위

| 기능 | 내용 | 코드 |
|---|---|---|
| TB 클라이언트 | 읽기 전용(쓰기 API 호출 차단), JWT 로그인·갱신, 429/5xx 재시도(Retry-After), 요청 로그 | `tb/client.py` |
| CE/PE 흡수 | 엔티티 목록: PE `/api/deviceInfos/all` → CE `/api/tenant/deviceInfos` → `entitiesQuery` 순 자동 선택, 소유 고객 정규화 | `tb/adapter.py` |
| 저부하 제어 | 토큰 버킷 + AIMD(지연 2배·429·5xx 시 절반), 서킷 브레이커(연속 5회 실패 → 10분 정지), 시간대별 예산 | `tb/ratelimit.py`, `scheduler/budget.py` |
| 작업 큐 | SQLite 우선순위 큐, 중복 방지, 지수 백오프 재시도(5회), 재시작 시 이어서 수행 | `scheduler/queue.py` |
| 수집 분할 계획 | 보고 주기 등급별 구간·키 묶음, 요청당 원시값 상한(기본 5만) | `scheduler/planner.py` |
| 메타데이터 동기화 | 고객(하위 고객 포함)·디바이스·에셋·관계·속성(3개 스코프)·텔레메트리 키 | `sync/handlers.py` |
| 보고 주기 측정 | 키당 최근 원시값 20개(1일 → 30일)로 주기·등급 산정 | `sync/handlers.py` |
| **[I] 변경 이력** | 엔티티 추가·삭제·이름/라벨/프로파일/소유자 변경, 관계·속성·키 추가/삭제/변경 | `sync/handlers.py` |
| **[D] 접근 제어** | 로그인(scrypt), 역할(ADMIN/ANALYST/CUSTOMER_VIEWER), 고객(+하위 고객) 범위 필터, 감사 로그 | `auth/users.py`, `api/app.py` |

웹 화면은 `frontend/`(사용법 `frontend/README.md`). `THINGLENS_WEB_DIR`를 지정하면 API 서버가 화면도 함께 제공합니다.

## Phase 2 구현 범위 — 데이터 종류·의미 추론 + 데이터 품질

| 기능 | 내용 | 코드 |
|---|---|---|
| 저부하 샘플·범위 수집 | 키당 최근·1일 전·7일 전 원시값(각 최대 60개) + **구간 1개짜리** MIN·MAX·COUNT (창은 키당 원시값 1만 개 이내) | `sync/analysis.py`, `tb/adapter.py` |
| 데이터 종류 추론 | 연속 수치·누적 카운터·이진·범주형·타임스탬프·JSON·텍스트. 카운터는 '장기 평균 증가율 ≈ 국소 증가율'까지 확인 (느린 파형 오판 방지) | `semantics/infer.py` |
| 의미 추론 | 의미 사전(한/영 32종) × 키 이름·단위 접미사·값 범위·데이터 종류·프로파일 이름 점수화 → 의미·단위·신뢰도·근거. 0.7 이상 자동, 미만은 검토 대기 | `semantics/dictionary.py`, `semantics/infer.py` |
| 사용자 확정 | (프로파일, 키) 또는 키 이름 전체에 의미·단위 확정 → 해당 엔티티만 즉시 재분석 (TB 요청 없음) | `api/app.py` |
| 속성 역할 | 위치·식별·버전·설정·상태·관리 정보, 정적/동적 (변경 이력 기반) | `semantics/infer.py` |
| **[B] 품질 점검** | 데이터 없음·보고 중단·값 고정·값 변화 없음·물리 범위 이탈·급격한 튐·수신률 저하·미래 타임스탬프·카운터 리셋·숫자/문자 혼재 → 키·엔티티 점수(100점) | `quality/checks.py` |
| 일일 품질 점검 | 매일 최근 샘플 + 24시간 범위만 새로 받아 재점검, 해소된 문제는 자동 종료 | `sync/handlers.py` |

수집 일정: 키 분석은 주 1회(또는 키가 바뀌면), 품질 점검은 매일. 엔티티당 키 분석 약 10건, 품질 점검 약 4~7건의 TB 요청.

실제 TB(CE 4.3.1 + Cassandra) 시드 데이터 결과: 키 31개 **데이터 종류 100%, 의미 자동 판정 100%** (LLM 없음),
값 고정·드문 범위 이탈·데이터 없음 센서 모두 검출, 정상 센서 오탐 0, 엔티티 25개에 TB 요청 238건.

### 변경 감지 지연 (저부하를 위한 절충)

| 변경 종류 | 감지 시점 |
|---|---|
| 엔티티 추가·삭제, 이름·라벨·프로파일·소유자 | 매 주기 (기본 6시간) |
| 관계·속성·텔레메트리 키 | 엔티티 버전이 바뀌었거나 상세 재확인 주기(기본 24시간) 경과 시 |
| 모든 변경 | 관리자의 '지금 동기화' (`POST /api/scheduler/sync-now`, 기본 full) 즉시 |

TB는 속성 변경이나 **다른 엔티티 쪽 관계** 변경 시 엔티티 버전을 올리지 않습니다 (TB 4.3.1에서 확인).

## 실행

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

export THINGLENS_DB=data/thinglens.db
export TB_URL=https://tb.사내주소 TB_USERNAME=thinglens-reader@회사 TB_PASSWORD=...
.venv/bin/thinglens init-db
.venv/bin/thinglens user-add admin --role ADMIN          # 비밀번호 입력
.venv/bin/thinglens scheduler &                           # 상주 워커
.venv/bin/thinglens api --host 0.0.0.0 --port 8000        # REST API
```

점검용: `thinglens sync-once --force --full` (주기 1회를 끝까지 실행 후 종료), `thinglens status`

### 주요 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `THINGLENS_RATE_MAX` | 2.0 | TB 요청 상한 (req/s), 시간대 계수를 곱해 적용 |
| `THINGLENS_RATE_WINDOWS` | 야간(01–06) 1.0, 평일 업무(08–19) 0.2 | JSON 목록 `[{"name","days","start","end","factor"}]` |
| `THINGLENS_RATE_DEFAULT_FACTOR` | 0.5 | 어느 창에도 안 맞을 때 계수 |
| `THINGLENS_TZ` | Asia/Seoul | 시간대 창 기준 |
| `THINGLENS_META_SYNC_INTERVAL_H` | 6 | 엔티티 목록 동기화 주기 |
| `THINGLENS_DETAIL_SYNC_INTERVAL_H` | 24 | 관계·속성·키 재확인 주기 |
| `THINGLENS_PROFILE_INTERVAL_D` | 7 | 키 분석(샘플 3개 창 + 7일 범위) 주기 |
| `THINGLENS_QUALITY_INTERVAL_H` | 24 | 품질 점검(최근 샘플 + 24시간 범위) 주기 |
| `THINGLENS_SAMPLE_POINTS` | 60 | 창당 키별 원시값 수 |
| `TB_CA_BUNDLE` / `TB_VERIFY_TLS` | - / true | 사내 인증서 |
| `THINGLENS_WEB_DIR` | - | 빌드된 웹 화면 경로 (`frontend/dist`). 지정 시 `/` 에서 화면 제공 |

수집 관련 설정은 **워커(`thinglens scheduler`) 프로세스의 환경변수**가 기준입니다. 워커가 실제 적용 중인 값을 DB에 기록하고, 화면은 그 값을 보여줍니다.

## REST API

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/api/auth/login`, `/api/auth/logout` | - | 로그인(토큰 발급)·로그아웃 |
| GET | `/api/me` | 로그인 | 내 정보 |
| GET | `/api/scheduler` | ADMIN·ANALYST | 워커 상태, 현재 속도·시간대 창, 최근 15분 요청 수·지연·부하 오류, 큐, 최근 주기 |
| GET | `/api/scheduler/metrics?minutes=` | ADMIN·ANALYST | 분 단위 TB 요청 수·p95 지연·부하 오류 (차트용) |
| POST | `/api/scheduler/pause`, `/resume`, `/sync-now?full=` | ADMIN | 제어 (감사 기록) |
| GET | `/api/entities?entity_type=&q=&profile=` | 로그인(범위 적용) | 엔티티 목록 |
| GET | `/api/entities-facets` | 로그인(범위 적용) | 유형·프로파일별 엔티티 수 (필터용) |
| GET | `/api/entities/{id}` | 로그인(범위 적용) | 관계(in/out)·속성·키(보고 주기·등급) |
| GET | `/api/customers` | 로그인(범위 적용) | 고객 목록·소유 엔티티 수 |
| GET | `/api/changes?entity_id=&change_type=&since_ms=` | 로그인(범위 적용) | 변경 이력 |
| GET | `/api/catalog/tiers` | 로그인(범위 적용) | 키 보고 주기 등급 분포 |
| GET | `/api/catalog/keys?status=&semantic=&q=` | 로그인(범위 적용) | (프로파일, 키)별 데이터 종류·의미·단위·신뢰도·근거·품질 — 검토 대기 먼저 |
| GET | `/api/catalog/semantic-groups` | 로그인(범위 적용) | 같은 의미의 키 묶음 (이름 변형 포함) |
| GET | `/api/catalog/attributes` | 로그인(범위 적용) | 속성 키 역할·정적/동적 |
| GET/POST/DELETE | `/api/catalog/rules` | 확정은 ADMIN·ANALYST | 의미 확정·취소 (감사 기록, 즉시 재분석) |
| GET | `/api/quality/summary` | 로그인(범위 적용) | 평균 점수·분포·항목별 미해결 건수·점수 낮은 엔티티 |
| GET | `/api/quality/findings?severity=&check=&entity_id=` | 로그인(범위 적용) | 품질 문제 목록 |
| GET/POST | `/api/users` | ADMIN | 사용자 관리 |
| GET | `/api/audit` | ADMIN | 감사 로그 |

API 문서(자동): `http://<서버>:8000/docs`

## 테스트

```bash
.venv/bin/pytest -q                                  # 단위·가짜 TB 테스트 (61개)
THINGLENS_IT_TB_URL=http://localhost:8080 .venv/bin/pytest -m integration -s   # 실제 테스트 TB (deploy/test-env + 시드)
```

검증 결과 (TB CE 4.3.1 + Cassandra 4.1, 시드 데이터):
- 엔티티 25개 전체 주기 14.5초 (10 req/s), 요청 139건, 부하 오류 0
- 1초 키·불규칙 키·비활성 키·이름 변형 키 등급, 관계, 속성 모두 정답과 일치
- 스케줄러·API 별도 프로세스로 실행, TB에서 라벨·속성·관계를 바꾼 뒤 '지금 동기화' → 3건 모두 변경 이력에 기록
