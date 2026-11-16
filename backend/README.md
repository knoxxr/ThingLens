# ThingLens 백엔드 (Phase 1)

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
| `THINGLENS_PROFILE_INTERVAL_D` | 7 | 키 보고 주기 재측정 주기 |
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
| GET/POST | `/api/users` | ADMIN | 사용자 관리 |
| GET | `/api/audit` | ADMIN | 감사 로그 |

API 문서(자동): `http://<서버>:8000/docs`

## 테스트

```bash
.venv/bin/pytest -q                                  # 단위·가짜 TB 테스트 (37개)
THINGLENS_IT_TB_URL=http://localhost:8080 .venv/bin/pytest -m integration -s   # 실제 테스트 TB (deploy/test-env + 시드)
```

검증 결과 (TB CE 4.3.1 + Cassandra 4.1, 시드 데이터):
- 엔티티 25개 전체 주기 14.5초 (10 req/s), 요청 139건, 부하 오류 0
- 1초 키·불규칙 키·비활성 키·이름 변형 키 등급, 관계, 속성 모두 정답과 일치
- 스케줄러·API 별도 프로세스로 실행, TB에서 라벨·속성·관계를 바꾼 뒤 '지금 동기화' → 3건 모두 변경 이력에 기록
