import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type AttrCatalog, type CatalogKey, type CatalogMeta, type Rule, type SemanticGroup, type User } from "../api";
import { Confidence, ErrorBox, Loading, SemStatusBadge, TierBadge } from "../components/bits";
import { fmtInterval, fmtTime } from "../format";

type Tab = "keys" | "groups" | "attrs" | "rules";

export default function Catalog({ user }: { user: User }) {
  const [tab, setTab] = useState<Tab>("keys");
  const meta = useQuery({ queryKey: ["catalog-meta"], queryFn: () => api<CatalogMeta>("/api/catalog/meta"), staleTime: Infinity });
  const keys = useQuery({ queryKey: ["catalog-keys"], queryFn: () => api<CatalogKey[]>("/api/catalog/keys") });
  const review = keys.data?.filter((k) => k.status === "REVIEW").length ?? 0;
  const canEdit = user.role === "ADMIN" || user.role === "ANALYST";
  if (!meta.data) return <Loading />;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>데이터 카탈로그</h1>
          <div className="sub">텔레메트리·속성 키의 데이터 종류와 의미를 자동으로 추론합니다. 신뢰도가 낮은 키는 검토 후 확정하세요.</div>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {([["keys", `텔레메트리 키${review ? ` · 검토 ${review}` : ""}`], ["groups", "의미별 묶음"], ["attrs", "속성 키"], ["rules", "확정 기록"]] as [Tab, string][]).map(([t, l]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{l}</button>
        ))}
      </div>
      {tab === "keys" && <KeysTab keys={keys.data} loading={keys.isLoading} error={keys.error} meta={meta.data} canEdit={canEdit} />}
      {tab === "groups" && <GroupsTab />}
      {tab === "attrs" && <AttrsTab meta={meta.data} />}
      {tab === "rules" && <RulesTab meta={meta.data} canEdit={canEdit} />}
    </>
  );
}

function KeysTab({ keys, loading, error, meta, canEdit }: { keys?: CatalogKey[]; loading: boolean; error: unknown; meta: CatalogMeta; canEdit: boolean }) {
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const rows = (keys ?? []).filter((k) => (!status || k.status === status) && (!q || k.key.toLowerCase().includes(q.toLowerCase())));
  const semKo = (s: string) => s === "unknown" ? "모름" : meta.semantics[s]?.ko ?? s;
  return (
    <>
      <div className="filters">
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="판정 상태">
          <option value="">전체 상태</option><option value="REVIEW">검토 필요</option><option value="AUTO">자동 판정</option><option value="CONFIRMED">확정</option>
        </select>
        <input placeholder="키 이름 검색" value={q} onChange={(e) => setQ(e.target.value)} aria-label="키 검색" />
        <span className="sub">(프로파일, 키) {rows.length}개 · 같은 프로파일의 같은 키는 한 번만 확정하면 됩니다</span>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <div className="table-wrap">
          <table>
            <thead><tr><th>상태</th><th>프로파일</th><th>키</th><th className="num">엔티티</th><th>데이터 종류</th><th>의미</th><th>단위</th><th>신뢰도</th><th>보고 주기</th><th className="num">품질</th><th /></tr></thead>
            <tbody>
              {rows.map((k) => {
                const id = `${k.profile}|${k.key}`;
                return [
                  <tr key={id}>
                    <td><SemStatusBadge s={k.status} /></td>
                    <td>{k.profile ?? "-"}</td>
                    <td className="mono">{k.key}</td>
                    <td className="num">{k.entities}</td>
                    <td>{meta.data_types[k.data_type ?? ""] ?? "-"}</td>
                    <td><b>{semKo(k.semantic)}</b>{k.semantic_split ? <span className="sub"> (엔티티별 다름)</span> : null}</td>
                    <td>{k.unit ?? "-"}{k.unit && k.unit_source === "default" ? <span className="sub"> 추정</span> : null}</td>
                    <td><Confidence v={k.confidence} /></td>
                    <td>{Object.keys(k.tiers).length === 1 ? <TierBadge tier={Object.keys(k.tiers)[0] as never} /> : <span className="sub">혼재</span>} <span className="sub">{fmtInterval(k.interval_s)}</span></td>
                    <td className="num">{k.quality_avg ?? "-"}</td>
                    <td><button onClick={() => setOpen(open === id ? null : id)} aria-expanded={open === id}>{open === id ? "닫기" : k.status === "REVIEW" && canEdit ? "검토" : "근거"}</button></td>
                  </tr>,
                  open === id && (
                    <tr key={id + "-x"}>
                      <td colSpan={11}>
                        <div className="sub">판정 근거</div>
                        <ul className="evidence">{k.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
                        {k.alternatives.length > 0 && <div className="sub" style={{ marginTop: 4 }}>다른 후보: {k.alternatives.map((a) => `${semKo(a.semantic)} ${Math.round(a.score * 100)}%`).join(", ")}</div>}
                        {canEdit && <RuleEditor k={k} meta={meta} onDone={() => setOpen(null)} />}
                      </td>
                    </tr>
                  ),
                ];
              })}
              {!rows.length && <tr><td colSpan={11} className="empty">해당하는 키가 없습니다</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function RuleEditor({ k, meta, onDone }: { k: CatalogKey; meta: CatalogMeta; onDone: () => void }) {
  const qc = useQueryClient();
  const [semantic, setSemantic] = useState(k.semantic);
  const [unit, setUnit] = useState(k.unit ?? "");
  const [allProfiles, setAll] = useState(false);
  const save = useMutation({
    mutationFn: () => api("/api/catalog/rules", { method: "POST", body: JSON.stringify({ profile: allProfiles ? null : k.profile, key: k.key, semantic, unit: unit || null }) }),
    onSuccess: () => { qc.invalidateQueries(); onDone(); },
  });
  return (
    <div className="editor">
      <b>의미 확정</b>
      <select value={semantic} onChange={(e) => { setSemantic(e.target.value); setUnit(meta.semantics[e.target.value]?.default_unit ?? ""); }} aria-label="의미">
        {Object.entries(meta.semantics).map(([id, s]) => <option key={id} value={id}>{s.ko}</option>)}
        <option value="unknown">의미 없음·모름</option>
      </select>
      <input value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="단위" style={{ width: 90 }} aria-label="단위" />
      <label className="sub" style={{ display: "flex", gap: 4, alignItems: "center" }}>
        <input type="checkbox" checked={allProfiles} onChange={(e) => setAll(e.target.checked)} />모든 프로파일의 <code>{k.key}</code> 에 적용
      </label>
      <button className="primary" onClick={() => save.mutate()} disabled={save.isPending}>확정</button>
      {save.error && <span className="error">{(save.error as Error).message}</span>}
    </div>
  );
}

function GroupsTab() {
  const g = useQuery({ queryKey: ["semantic-groups"], queryFn: () => api<SemanticGroup[]>("/api/catalog/semantic-groups") });
  if (g.isLoading) return <Loading />;
  return (
    <>
      <div className="sub" style={{ marginBottom: 10 }}>이름이 달라도 같은 의미로 판정된 키를 묶습니다 (자동 판정·확정만). 상관 분석에서 이 묶음 단위로 키를 고를 수 있습니다.</div>
      <div className="grid two">
        {g.data?.map((s) => (
          <div className="card" key={s.semantic}>
            <h2>{s.ko} <span className="sub">· 엔티티 {s.entities}{s.units.length ? ` · ${s.units.join(", ")}` : ""}</span></h2>
            {s.units.length > 1 && <div className="banner warning" style={{ marginBottom: 8 }}>단위가 섞여 있습니다 ({s.units.join(", ")}). 비교 전에 단위 변환이 필요합니다.</div>}
            <table><tbody>{s.keys.map((k) => <tr key={k.key}><td className="mono">{k.key}</td><td className="num">{k.entities}개 엔티티</td><td className="sub">{k.unit ?? ""}</td></tr>)}</tbody></table>
          </div>
        ))}
      </div>
    </>
  );
}

function AttrsTab({ meta }: { meta: CatalogMeta }) {
  const a = useQuery({ queryKey: ["catalog-attrs"], queryFn: () => api<AttrCatalog[]>("/api/catalog/attributes") });
  if (a.isLoading) return <Loading />;
  const SCOPE: Record<string, string> = { SERVER_SCOPE: "서버", SHARED_SCOPE: "공유", CLIENT_SCOPE: "클라이언트" };
  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="table-wrap">
        <table>
          <thead><tr><th>역할</th><th>범위</th><th>키</th><th className="num">엔티티</th><th>변화</th></tr></thead>
          <tbody>
            {a.data?.map((r) => (
              <tr key={r.scope + r.key}><td>{meta.roles[r.role] ?? r.role}</td><td>{SCOPE[r.scope] ?? r.scope}</td><td className="mono">{r.key}</td>
                <td className="num">{r.entities}</td><td>{r.dynamic ? <span className="badge">수시로 바뀜</span> : <span className="sub">정적</span>}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RulesTab({ meta, canEdit }: { meta: CatalogMeta; canEdit: boolean }) {
  const qc = useQueryClient();
  const r = useQuery({ queryKey: ["rules"], queryFn: () => api<Rule[]>("/api/catalog/rules") });
  const del = useMutation({ mutationFn: (id: number) => api(`/api/catalog/rules/${id}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries() });
  if (r.isLoading) return <Loading />;
  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="table-wrap">
        <table>
          <thead><tr><th>프로파일</th><th>키</th><th>확정한 의미</th><th>단위</th><th>확정자</th><th>시각</th><th /></tr></thead>
          <tbody>
            {r.data?.map((x) => (
              <tr key={x.id}><td>{x.profile ?? <span className="sub">모든 프로파일</span>}</td><td className="mono">{x.key}</td>
                <td>{x.semantic === "unknown" ? "의미 없음·모름" : meta.semantics[x.semantic]?.ko ?? x.semantic}</td><td>{x.unit ?? "-"}</td>
                <td>{x.created_by}</td><td className="sub">{fmtTime(x.created_ms)}</td>
                <td>{canEdit && <button onClick={() => del.mutate(x.id)} disabled={del.isPending}>확정 취소</button>}</td></tr>
            ))}
            {!r.data?.length && <tr><td colSpan={7} className="empty">아직 확정한 키가 없습니다</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
