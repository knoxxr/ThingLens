import type { Tier } from "./api";

const TZ = "Asia/Seoul";

export function fmtTime(ms: number | null | undefined, withDate = true): string {
  if (!ms) return "-";
  const d = new Date(ms);
  return d.toLocaleString("ko-KR", {
    timeZone: TZ, hour12: false,
    ...(withDate ? { year: "numeric", month: "2-digit", day: "2-digit" } : {}),
    hour: "2-digit", minute: "2-digit",
  });
}

export function fmtAgo(ms: number | null | undefined): string {
  if (!ms) return "-";
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 0) return "곧";
  if (s < 60) return `${s}초 전`;
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}

export function fmtInterval(s: number | null | undefined): string {
  if (s == null) return "-";
  if (s < 60) return `${+s.toFixed(s < 10 ? 1 : 0)}초`;
  if (s < 3600) return `${+(s / 60).toFixed(1)}분`;
  if (s < 86400) return `${+(s / 3600).toFixed(1)}시간`;
  return `${+(s / 86400).toFixed(1)}일`;
}

export const TIER_LABEL: Record<Tier, string> = {
  high: "고빈도 <10초",
  mid: "중빈도 10초~5분",
  low: "저빈도·불규칙",
  inactive: "비활성",
};

export const CHANGE_LABEL: Record<string, { text: string; kind: "add" | "remove" | "update" }> = {
  ENTITY_ADDED: { text: "엔티티 추가", kind: "add" },
  ENTITY_REMOVED: { text: "엔티티 삭제", kind: "remove" },
  ENTITY_UPDATED: { text: "엔티티 변경", kind: "update" },
  RELATION_ADDED: { text: "관계 추가", kind: "add" },
  RELATION_REMOVED: { text: "관계 삭제", kind: "remove" },
  ATTRIBUTE_ADDED: { text: "속성 추가", kind: "add" },
  ATTRIBUTE_REMOVED: { text: "속성 삭제", kind: "remove" },
  ATTRIBUTE_CHANGED: { text: "속성 값 변경", kind: "update" },
  TS_KEY_ADDED: { text: "텔레메트리 키 추가", kind: "add" },
  TS_KEY_REMOVED: { text: "텔레메트리 키 삭제", kind: "remove" },
};

export const SEVERITY_LABEL = { critical: "심각", warning: "주의", info: "참고" } as const;
export const SEM_STATUS_LABEL = { REVIEW: "검토 필요", AUTO: "자동 판정", CONFIRMED: "확정" } as const;

export const ROLE_LABEL = { ADMIN: "관리자", ANALYST: "분석가", CUSTOMER_VIEWER: "고객 담당자" } as const;

export const TASK_LABEL: Record<string, string> = {
  START_CYCLE: "주기 시작",
  SYNC_CUSTOMERS: "고객 목록",
  SYNC_ENTITY_LIST: "엔티티 목록",
  SYNC_ENTITY_DETAIL: "엔티티 상세",
  PROFILE_KEYS: "키 분석 (샘플·범위)",
  FINISH_CYCLE: "주기 마무리",
  QUALITY_CHECK: "일일 품질 점검",
  ANALYZE_ALL: "전체 재분석",
};

export function fmtChangeValue(v: unknown, n = 40): string {
  if (v && typeof v === "object" && "to_id" in (v as Record<string, unknown>)) {
    const r = v as { to_id: string; to_name?: string | null; to_type?: string };
    const kind = r.to_type === "DEVICE" ? "디바이스" : r.to_type === "ASSET" ? "에셋" : r.to_type ?? "";
    return `${r.to_name ?? r.to_id.slice(0, 8) + "…"}${kind ? ` (${kind})` : ""}`;
  }
  return short(v, n);
}

export function short(v: unknown, n = 60): string {
  if (v === null || v === undefined) return "-";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
