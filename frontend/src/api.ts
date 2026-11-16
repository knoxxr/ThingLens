// ThingLens REST API 클라이언트 (토큰은 localStorage 보관)

const TOKEN_KEY = "thinglens.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 저장소를 못 쓰는 환경 — 새로고침 시 다시 로그인 */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && !path.startsWith("/api/auth/login")) {
    setToken(null);
    window.dispatchEvent(new Event("thinglens:logout"));
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : msg;
    } catch {
      /* 본문 없음 */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

// -- 타입 -------------------------------------------------------------------
export type Role = "ADMIN" | "ANALYST" | "CUSTOMER_VIEWER";
export interface User { id: number; login: string; role: Role; customer_ids: string[]; active: boolean }

export interface Cycle { id: number; started_ms: number; finished_ms: number | null; status: string; entities_seen: number; changes: number; full: number }
export interface SchedulerStatus {
  paused: number; paused_by: string | null; paused_ms: number | null;
  current_rate: number | null; window_name: string | null; circuit_open_until_ms: number | null;
  last_heartbeat_ms: number | null; last_error: string | null; worker_alive: boolean;
  requests_15m: { count: number; per_sec: number; p50_ms: number | null; p95_ms: number | null; load_errors: number };
  queue: Record<string, Record<string, number>>; pending: number; cycles: Cycle[];
  rate_max: number | null; rate_windows: { name: string; days: string; start: string; end: string; factor: number }[];
}
export interface MetricBucket { ts_ms: number; requests: number; p95_ms: number | null; load_errors: number }

export interface Entity {
  id: string; entity_type: "DEVICE" | "ASSET"; name: string; label: string | null; profile: string | null;
  owner_id: string | null; owner_name: string | null; active: boolean | null; created_time: number | null;
  first_seen_ms: number; last_seen_ms: number; detail_synced_ms: number | null; profile_synced_ms: number | null;
}
export interface KeyFinding { check: string; check_ko: string; severity: Severity; message: string }
export type Severity = "critical" | "warning" | "info";
export type SemStatus = "AUTO" | "REVIEW" | "CONFIRMED";
export interface TsKey {
  key: string; first_seen_ms: number; interval_s: number | null; irregular: number | null; tier: Tier | null;
  last_ts: number | null; profiled_ms: number | null;
  data_type: string | null; semantic: string | null; unit: string | null; unit_source: string | null;
  confidence: number | null; semantic_status: SemStatus | null; evidence: string[];
  quality_score: number | null; findings: KeyFinding[];
}
export interface EntityDetail extends Entity {
  relations_out: { type: string; to_id: string; to_name: string; to_type: string }[];
  relations_in: { type: string; from_id: string; from_name: string; from_type: string }[];
  attributes: { scope: string; key: string; value: unknown; last_update_ts: number | null }[];
  timeseries_keys: TsKey[];
  attribute_roles: Record<string, { role: string; dynamic: boolean }>;
  quality_score: number | null;
}
export interface CatalogMeta {
  semantics: Record<string, { ko: string; default_unit: string | null }>;
  roles: Record<string, string>; data_types: Record<string, string>; checks: Record<string, string>;
}
export interface CatalogKey {
  profile: string | null; key: string; entities: number; status: SemStatus; semantic: string; semantic_split: boolean;
  unit: string | null; unit_source: string | null; data_type: string | null; confidence: number;
  evidence: string[]; alternatives: { semantic: string; score: number }[]; tiers: Record<string, number>;
  interval_s: number | null; quality_avg: number | null;
}
export interface SemanticGroup { semantic: string; ko: string; entities: number; units: string[]; keys: { key: string; entities: number; unit: string | null }[] }
export interface AttrCatalog { scope: string; key: string; role: string; entities: number; dynamic: boolean }
export interface Rule { id: number; profile: string | null; key: string; semantic: string; unit: string | null; created_by: string; created_ms: number }
export interface QualitySummary {
  entities_scored: number; average: number | null; buckets: Record<string, number>;
  open_by_check: { check: string; check_ko: string; severity: Severity; count: number }[];
  worst: { id: string; name: string; entity_type: string; profile: string | null; score: number }[];
}
export interface QualityFindingRow {
  id: number; entity_id: string; entity_name: string; profile: string | null; key: string; check_type: string;
  check_ko: string; severity: Severity; message: string; first_detected_ms: number; last_detected_ms: number;
  resolved_ms: number | null; detail: unknown;
}
export type Tier = "high" | "mid" | "low" | "inactive";
export interface Change {
  id: number; cycle_id: number; ts_ms: number; entity_id: string; entity_type: string; entity_name: string | null;
  change_type: string; field: string | null; before: unknown; after: unknown;
}
export interface Customer { id: string; title: string; parent_id: string | null; entities: number }
export interface AuditRow { id: number; ts_ms: number; login: string | null; action: string; target: string | null; detail: unknown }
