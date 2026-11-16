import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type TopologyResp, type TreeNode, type TreeResp } from "../api";
import { ErrorBox, Loading, ScoreBadge, SeverityLabel, Tile } from "../components/bits";
import { Graph } from "../components/Graph";

function collect(n: TreeNode, out: Set<string>) {
  out.add(n.id);
  n.children.forEach((c) => collect(c, out));
  return out;
}

function TreeItem({ n, sel, onSel, depth }: { n: TreeNode; sel: string | null; onSel: (id: string) => void; depth: number }) {
  const [open, setOpen] = useState(depth < 1);
  return (
    <li>
      {n.children.length ? <button className="tog" onClick={() => setOpen(!open)} aria-label={open ? "접기" : "펼치기"} aria-expanded={open}>{open ? "▾" : "▸"}</button> : <span className="tog" />}
      <button className={`node ${sel === n.id ? "sel" : ""}`} onClick={() => onSel(n.id)}>
        <span className={`shape ${n.entity_type === "ASSET" ? "asset" : "device"}`} aria-hidden />{n.name}
        {n.children.length ? <span className="sub"> {n.children.length}</span> : null}
      </button>
      {open && n.children.length > 0 && <ul>{n.children.map((c) => <TreeItem key={c.id} n={c} sel={sel} onSel={onSel} depth={depth + 1} />)}</ul>}
    </li>
  );
}

export default function Topology() {
  const [sp, setSp] = useSearchParams();
  const selected = sp.get("focus");
  const setSelected = (id: string | null) => {
    const n = new URLSearchParams(sp);
    if (id) n.set("focus", id); else n.delete("focus");
    setSp(n, { replace: true });
  };
  const [scopeRoot, setScopeRoot] = useState<string | null>(null);
  const [sev, setSev] = useState("");
  const [showIsolated, setShowIsolated] = useState(false);
  const topo = useQuery({ queryKey: ["topology"], queryFn: () => api<TopologyResp>("/api/topology") });
  const tree = useQuery({ queryKey: ["topology-tree"], queryFn: () => api<TreeResp>("/api/topology/tree") });

  const visible = useMemo(() => {
    if (!scopeRoot || !tree.data) return null;
    const find = (ns: TreeNode[]): TreeNode | null => {
      for (const n of ns) { if (n.id === scopeRoot) return n; const f = find(n.children); if (f) return f; }
      return null;
    };
    const r = find(tree.data.roots);
    return r ? collect(r, new Set()) : null;
  }, [scopeRoot, tree.data]);

  if (topo.isLoading || tree.isLoading) return <Loading />;
  if (topo.error) return <ErrorBox error={topo.error} />;
  const t = topo.data!;
  const byId = new Map(t.nodes.map((n) => [n.id, n]));
  const sel = selected ? byId.get(selected) : null;
  const selIssues = t.issues.filter((i) => i.entity_id === selected);
  const count = (s: string) => t.issues.filter((i) => i.severity === s).length;
  const issues = t.issues.filter((i) => !sev || i.severity === sev);
  const scopeNode = scopeRoot ? byId.get(scopeRoot) : null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>관계 구조</h1>
          <div className="sub">계층 관계: {t.hierarchy_relations.join(", ")} · 권한 범위 안의 엔티티만 표시합니다</div>
        </div>
      </div>
      <div className="grid tiles" style={{ marginBottom: 14 }}>
        <Tile label="엔티티" value={(t.stats.entities.ASSET ?? 0) + (t.stats.entities.DEVICE ?? 0)} hint={`에셋 ${t.stats.entities.ASSET ?? 0} · 디바이스 ${t.stats.entities.DEVICE ?? 0}`} />
        <Tile label="관계" value={t.stats.edges} hint={Object.entries(t.stats.relation_types).map(([k, v]) => `${k} ${v}`).join(" · ") || "-"} />
        <Tile label="계층" value={`최상위 ${t.stats.roots}`} hint={`최대 깊이 ${t.stats.max_depth} · 연결 묶음 ${t.stats.components}`} />
        <Tile label="에셋당 디바이스" value={t.stats.devices_per_asset.avg} hint={`최소 ${t.stats.devices_per_asset.min} · 최대 ${t.stats.devices_per_asset.max}`} />
        <Tile label="구조 이슈" value={t.issues.length} hint={`심각 ${count("critical")} · 주의 ${count("warning")} · 참고 ${count("info")}`} />
      </div>

      <div className="topo" style={{ marginBottom: 14 }}>
        <div className="card tree">
          <h2>계층 트리</h2>
          <ul>{tree.data!.roots.map((r) => <TreeItem key={r.id} n={r} sel={selected} onSel={setSelected} depth={0} />)}</ul>
          {tree.data!.outside.length > 0 && <>
            <div className="sub" style={{ marginTop: 10 }}>계층 밖 ({tree.data!.outside.length})</div>
            <ul>{tree.data!.outside.map((o) => (
              <li key={o.id}><span className="tog" /><button className={`node ${selected === o.id ? "sel" : ""}`} onClick={() => setSelected(o.id)}>
                <span className={`shape ${o.entity_type === "ASSET" ? "asset" : "device"}`} aria-hidden />{o.name}</button></li>
            ))}</ul>
          </>}
        </div>
        <div className="card">
          <div className="filters" style={{ justifyContent: "space-between" }}>
            <h2 style={{ margin: 0 }}>관계 그래프 {scopeNode ? <span className="sub">· {scopeNode.name} 하위만</span> : null}</h2>
            <div className="actions">
              {!scopeRoot && tree.data!.outside.length > 0 && (
                <label className="sub" style={{ display: "flex", gap: 4, alignItems: "center" }}>
                  <input type="checkbox" checked={showIsolated} onChange={(e) => setShowIsolated(e.target.checked)} />관계 없는 엔티티도 표시
                </label>
              )}
              {sel?.entity_type === "ASSET" && scopeRoot !== sel.id && <button onClick={() => setScopeRoot(sel.id)}>이 에셋 하위만 보기</button>}
              {scopeRoot && <button onClick={() => setScopeRoot(null)}>전체 보기</button>}
            </div>
          </div>
          <Graph data={t} visible={visible} showIsolated={showIsolated || tree.data!.outside.some((o) => o.id === selected)} selected={selected} onSelect={setSelected} />
          <div className="legend">
            <span><span className="shape asset" />에셋</span><span><span className="shape device" />디바이스</span>
            <span><span className="solid" />계층 관계</span><span><span className="dash" />기타 관계</span>
            <span><span className="ring" />구조 이슈·심각한 품질 문제</span>
          </div>
        </div>
        <div className="card side">
          {sel ? (
            <>
              <h2><span className={`shape ${sel.entity_type === "ASSET" ? "asset" : "device"}`} aria-hidden />{sel.name}</h2>
              <dl className="kv">
                <dt>프로파일</dt><dd>{sel.profile ?? "-"}</dd>
                <dt>소유 고객</dt><dd>{sel.owner_name ?? "테넌트"}</dd>
                <dt>품질</dt><dd><ScoreBadge score={sel.quality_score} />{sel.critical_findings ? <span className="sub"> 심각 {sel.critical_findings}건</span> : null}</dd>
                {sel.groups.length > 0 && <><dt>그룹</dt><dd>{sel.groups.join(", ")}</dd></>}
              </dl>
              {selIssues.length > 0 && <ul className="summary" style={{ marginTop: 10 }}>{selIssues.map((i, k) => <li key={k}><SeverityLabel s={i.severity} /> {i.message}</li>)}</ul>}
              <div style={{ marginTop: 12 }}><Link to={`/entities/${sel.id}`}>360° 보기 →</Link></div>
            </>
          ) : <div className="sub">그래프나 트리에서 엔티티를 고르면 여기에 요약이 나옵니다.</div>}
        </div>
      </div>

      <div className="filters">
        <h2 style={{ margin: 0 }}>구조 이슈</h2>
        <select value={sev} onChange={(e) => setSev(e.target.value)} aria-label="심각도">
          <option value="">전체</option><option value="critical">심각</option><option value="warning">주의</option><option value="info">참고</option>
        </select>
        <span className="sub">{issues.length}건</span>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <div className="table-wrap" style={{ maxHeight: 420 }}>
          <table>
            <thead><tr><th>심각도</th><th>종류</th><th>엔티티</th><th>내용</th><th /></tr></thead>
            <tbody>
              {issues.map((i, k) => (
                <tr key={k}>
                  <td><SeverityLabel s={i.severity} /></td><td className="nowrap">{i.type_ko}</td>
                  <td><Link to={`/entities/${i.entity_id}`}>{i.entity_name}</Link></td><td>{i.message}</td>
                  <td><button onClick={() => { setScopeRoot(null); setSelected(i.entity_id); window.scrollTo({ top: 0, behavior: "smooth" }); }}>그래프에서</button></td>
                </tr>
              ))}
              {!issues.length && <tr><td colSpan={5} className="empty">구조 이슈가 없습니다</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
