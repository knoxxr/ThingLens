import type { SemStatus, Severity, Tier } from "../api";
import { CHANGE_LABEL, SEM_STATUS_LABEL, SEVERITY_LABEL, TIER_LABEL } from "../format";

export function SeverityLabel({ s }: { s: Severity }) {
  return <span className={`status ${s === "info" ? "info" : s}`}><span className="dot" aria-hidden />{SEVERITY_LABEL[s]}</span>;
}

export function SemStatusBadge({ s }: { s: SemStatus | null }) {
  if (!s) return <span className="badge">분석 전</span>;
  return <span className={`badge sem-${s.toLowerCase()}`}>{s === "REVIEW" ? "? " : s === "CONFIRMED" ? "✓ " : ""}{SEM_STATUS_LABEL[s]}</span>;
}

export function ScoreBadge({ score }: { score: number | null | undefined }) {
  if (score == null) return <span className="sub">-</span>;
  const kind = score >= 90 ? "good" : score >= 60 ? "warning" : "critical";
  return <span className={`status ${kind}`} title="품질 점수 (100점 만점)"><span className="dot" aria-hidden />{score}</span>;
}

export function Confidence({ v }: { v: number | null }) {
  if (v == null) return <span className="sub">-</span>;
  return (
    <span className="conf" title={`신뢰도 ${Math.round(v * 100)}%`}>
      <span className="conf-bar"><span style={{ width: `${Math.round(v * 100)}%` }} /></span>
      <span className="sub">{Math.round(v * 100)}%</span>
    </span>
  );
}

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
