import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { useEffect, useRef } from "react";
import type { TopologyResp } from "../api";

// 관계 그래프: 에셋(파란 사각형) / 디바이스(주황 원), 계층 관계는 실선·그 외는 점선,
// 구조 이슈·심각한 품질 문제가 있는 노드는 빨간 테두리 (의미는 옆 패널·표에서 글자로 제공)

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

interface Props {
  data: TopologyResp;
  visible: Set<string> | null;     // null = 전체
  showIsolated: boolean;           // 관계가 하나도 없는 엔티티 표시 여부
  selected: string | null;
  onSelect: (id: string | null) => void;
}

export function Graph({ data, visible, showIsolated, selected, onSelect }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);

  useEffect(() => {
    if (!box.current) return;
    let nodes = data.nodes.filter((n) => !visible || visible.has(n.id));
    let ids = new Set(nodes.map((n) => n.id));
    const shownEdges = data.edges.filter((e) => ids.has(e.from) && ids.has(e.to));
    const linked = new Set(shownEdges.flatMap((e) => [e.from, e.to]));
    const isolated = nodes.filter((n) => !linked.has(n.id)).map((n) => n.id);
    if (!showIsolated && !visible) {
      nodes = nodes.filter((n) => linked.has(n.id) || n.id === selected);
      ids = new Set(nodes.map((n) => n.id));
    }
    const els: ElementDefinition[] = [
      ...nodes.map((n) => ({
        data: { id: n.id, label: n.name, kind: n.entity_type, flagged: n.topology_issues + n.critical_findings > 0 ? 1 : 0 },
      })),
      ...shownEdges.map((e, i) => ({
        data: { id: `e${i}`, source: e.from, target: e.to, rel: e.type, hier: e.hierarchy ? 1 : 0 },
      })),
    ];
    const c = {
      asset: cssVar("--series-1"), device: cssVar("--series-2"), text: cssVar("--text-secondary"),
      edge: cssVar("--border-strong"), crit: cssVar("--status-critical"), surface: cssVar("--surface-1"), accent: cssVar("--accent"),
    };
    // 표시 중인 엔티티 기준 최상위: 표시 범위 안에 계층 부모가 없고 자식이 있는 노드
    const hasParent = new Set(shownEdges.filter((e) => e.hierarchy).map((e) => e.to));
    const roots = [...new Set(shownEdges.filter((e) => e.hierarchy).map((e) => e.from))].filter((r) => !hasParent.has(r));
    const inst = cytoscape({
      container: box.current,
      elements: els,
      wheelSensitivity: 0.3,
      minZoom: 0.1,
      maxZoom: 3,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, color: c.text, "text-valign": "center", "text-halign": "right", "text-margin-x": 6, width: 16, height: 16, "border-width": 2, "border-color": c.surface, "text-wrap": "ellipsis", "text-max-width": "170px" } },
        { selector: 'node[kind = "ASSET"]', style: { shape: "round-rectangle", "background-color": c.asset, width: 22, height: 22, "text-valign": "top", "text-halign": "center", "text-margin-x": 0, "text-margin-y": -4, "font-weight": "bold" } },
        { selector: 'node[kind = "DEVICE"]', style: { shape: "ellipse", "background-color": c.device } },
        { selector: "node[flagged = 1]", style: { "border-color": c.crit, "border-width": 3 } },
        { selector: "node:selected", style: { "overlay-color": c.accent, "overlay-opacity": 0.18, "overlay-padding": 6, "font-weight": "bold" } },
        { selector: "edge", style: { width: 1.5, "line-color": c.edge, "curve-style": "bezier", "target-arrow-shape": "triangle", "target-arrow-color": c.edge, "arrow-scale": 0.7 } },
        { selector: "edge[hier = 0]", style: { "line-style": "dashed", label: "data(rel)", "font-size": 8, color: c.text } },
      ],
      layout: { name: "preset" },
    });
    // 계층 트리 배치(왼쪽→오른쪽): 깊이당 200px, 말단 1개당 26px, 부모는 자식들의 가운데.
    // 계층 밖이지만 다른 관계가 있는 노드는 오른쪽 열, 관계 없는 노드는 아래 격자.
    const kids = new Map<string, string[]>();
    for (const e of shownEdges) if (e.hierarchy) kids.set(e.from, [...(kids.get(e.from) ?? []), e.to]);
    const nameOf = new Map(nodes.map((n) => [n.id, n.name ?? ""]));
    const pos = new Map<string, { x: number; y: number }>();
    let row = 0;
    const place = (id: string, depth: number): number => {
      if (pos.has(id)) return pos.get(id)!.y;
      pos.set(id, { x: depth * 200, y: 0 });  // 순환 대비 선점
      const ch = (kids.get(id) ?? []).filter((c) => !pos.has(c)).sort((a, b) => (nameOf.get(a) ?? "").localeCompare(nameOf.get(b) ?? ""));
      let y: number;
      if (ch.length) {
        const ys = ch.map((c) => place(c, depth + 1));
        y = (Math.min(...ys) + Math.max(...ys)) / 2;
      } else {
        y = row++ * 26;
      }
      pos.set(id, { x: depth * 200, y });
      return y;
    };
    [...roots].sort((a, b) => (nameOf.get(a) ?? "").localeCompare(nameOf.get(b) ?? "")).forEach((r) => { place(r, 0); row += 1; });
    const maxX = Math.max(0, ...[...pos.values()].map((p) => p.x));
    const isoSet = new Set(isolated);
    const rest = nodes.filter((n) => !pos.has(n.id) && !isoSet.has(n.id));
    rest.forEach((n) => pos.set(n.id, { x: maxX + 260, y: row++ * 26 }));
    const isoShown = nodes.filter((n) => isoSet.has(n.id));
    const cols = Math.max(3, Math.ceil(Math.sqrt(isoShown.length)));
    isoShown.forEach((n, i) => pos.set(n.id, { x: (i % cols) * 180, y: (row + 2 + Math.floor(i / cols)) * 26 }));
    inst.nodes().forEach((n) => { const p = pos.get(n.id()); if (p) n.position(p); });
    inst.fit(undefined, 24);
    if (inst.zoom() > 1.4) {  // 노드가 적을 때 과도한 확대 방지
      inst.zoom(1.4);
      inst.center();
    }
    inst.on("tap", "node", (ev) => onSelect(ev.target.id()));
    inst.on("tap", (ev) => { if (ev.target === inst) onSelect(null); });
    cy.current = inst;
    return () => inst.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, visible, showIsolated]);

  useEffect(() => {
    const inst = cy.current;
    if (!inst) return;
    inst.$(":selected").unselect();
    if (selected) {
      const n = inst.getElementById(selected);
      if (n.nonempty()) {
        n.select();
        inst.animate({ center: { eles: n }, zoom: Math.max(inst.zoom(), 1) }, { duration: 250 });
      }
    }
  }, [selected]);

  return <div ref={box} className="graph-box" role="img" aria-label="엔티티 관계 그래프 (아래 이슈 표와 왼쪽 트리에서 같은 정보를 글로 제공)" />;
}
