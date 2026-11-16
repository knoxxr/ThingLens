import type { Tier } from "../api";
import { CHANGE_LABEL, TIER_LABEL } from "../format";

export function TierBadge({ tier }: { tier: Tier | null }) {
  if (!tier) return <span className="badge">측정 전</span>;
  return (
    <span className="badge">
      <span className="tier-dot" style={{ background: `var(--tier-${tier})` }} />
      {TIER_LABEL[tier]}
    </span>
  );
}

export function ChangeLabel({ type }: { type: string }) {
  const c = CHANGE_LABEL[type] ?? { text: type, kind: "update" as const };
  const icon = c.kind === "add" ? "＋" : c.kind === "remove" ? "－" : "△";
  return <span className={`chg ${c.kind}`}>{icon} {c.text}</span>;
}

export function Status({ kind, children }: { kind: "good" | "warning" | "critical"; children: React.ReactNode }) {
  return <span className={`status ${kind}`}><span className="dot" aria-hidden />{children}</span>;
}

export function Tile({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function Loading() {
  return <div className="empty">불러오는 중…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  return <div className="banner critical">오류: {error instanceof Error ? error.message : String(error)}</div>;
}
