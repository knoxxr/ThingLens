import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MetricBucket } from "../api";
import { fmtTime } from "../format";

// 분 단위 단일 계열 차트. kind="bars"(요청 수) | "line"(p95 지연). 계열 하나 → 범례 없음(제목이 이름).
// 부하 오류가 있는 분은 막대 위 ▲ 표시 + 툴팁 글자로 알린다 (색만으로 전달하지 않음).

interface Props {
  data: MetricBucket[];
  kind: "bars" | "line";
  value: (b: MetricBucket) => number | null;
  unit: string;
  height?: number;
  ariaLabel: string;
}

const PAD = { top: 14, right: 8, bottom: 22, left: 40 };

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

export function MinuteChart({ data, kind, value, unit, height = 150, ariaLabel }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [hover, setHover] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(240, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const values = data.map(value);
  const max = niceMax(Math.max(0, ...values.map((v) => v ?? 0)));
  const innerW = width - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;
  const step = data.length ? innerW / data.length : innerW;
  const x = (i: number) => PAD.left + i * step;
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const ticks = [0, max / 2, max];
  const labelEvery = Math.max(1, Math.ceil(data.length / Math.max(2, Math.floor(innerW / 70))));

  const linePath = useMemo(() => {
    if (kind !== "line") return "";
    let d = "";
    let pen = false;
    values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i) + step / 2},${y(v)}`;
      pen = true;
    });
    return d;
  }, [kind, values.join(","), width, max]); // eslint-disable-line react-hooks/exhaustive-deps

  const onMove = (e: React.MouseEvent<SVGRectElement>) => {
    const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
    const i = Math.floor((e.clientX - rect.left - PAD.left) / step);
    setHover(i >= 0 && i < data.length ? i : null);
  };

  const h = hover != null ? data[hover] : null;
  const hv = hover != null ? values[hover] : null;
  const barW = Math.max(1, step - 2); // 막대 사이 2px 간격

  return (
    <div className="chart" ref={ref}>
      <svg height={height} role="img" aria-label={ariaLabel}>
        <g className="axis">
          {ticks.map((t) => (
            <g key={t}>
              <line className="gridline" x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} />
              <text x={PAD.left - 6} y={y(t) + 4} textAnchor="end">{Math.round(t * 10) / 10}</text>
            </g>
          ))}
          {data.map((b, i) => (i % labelEvery === 0 ? (
            <text key={b.ts_ms} x={x(i) + step / 2} y={height - 6} textAnchor="middle">{fmtTime(b.ts_ms, false)}</text>
          ) : null))}
        </g>
        {kind === "bars" && values.map((v, i) => {
          if (!v) return null;
          const top = y(v);
          const hgt = PAD.top + innerH - top;
          const r = Math.min(4, barW / 2, hgt);
          const x0 = x(i) + 1;
          const d = `M${x0},${top + hgt} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x0 + barW - r} Q${x0 + barW},${top} ${x0 + barW},${top + r} V${top + hgt} Z`;
          return <path key={i} d={d} fill="var(--series-1)" opacity={hover == null || hover === i ? 1 : 0.55} />;
        })}
        {kind === "line" && <path d={linePath} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />}
        {kind === "line" && values.filter((v) => v != null).length <= 40 && values.map((v, i) => v == null ? null : (
          <circle key={`p${i}`} cx={x(i) + step / 2} cy={y(v)} r={2.5} fill="var(--series-1)" />
        ))}
        {data.map((b, i) => b.load_errors > 0 ? (
          <path key={`e${i}`} d={`M${x(i) + step / 2},${PAD.top - 2} l-4,-7 h8 z`} fill="var(--status-critical)">
            <title>부하 오류 {b.load_errors}건</title>
          </path>
        ) : null)}
        {hover != null && (
          <line x1={x(hover) + step / 2} x2={x(hover) + step / 2} y1={PAD.top} y2={PAD.top + innerH} stroke="var(--border-strong)" strokeDasharray="3 3" />
        )}
        {kind === "line" && hover != null && hv != null && (
          <circle cx={x(hover) + step / 2} cy={y(hv)} r={4} fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth={2} />
        )}
        <rect x={PAD.left} y={0} width={innerW} height={height} fill="transparent" onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
      </svg>
      {h && (
        <div className="tooltip" style={{ left: Math.min(x(hover!) + step / 2 + 10, width - 150), top: 4 }}>
          <div className="t">{fmtTime(h.ts_ms)}</div>
          <div><b>{hv == null ? "데이터 없음" : `${hv}${unit}`}</b></div>
          {h.load_errors > 0 && <div className="status critical"><span className="dot" />부하 오류 {h.load_errors}건</div>}
        </div>
      )}
      <details className="table-view">
        <summary>표로 보기</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th>시각</th><th className="num">값</th><th className="num">부하 오류</th></tr></thead>
            <tbody>
              {data.filter((_, i) => values[i] != null || data[i].load_errors).map((b) => (
                <tr key={b.ts_ms}><td>{fmtTime(b.ts_ms)}</td><td className="num">{value(b) ?? "-"}{value(b) != null ? unit : ""}</td><td className="num">{b.load_errors}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
