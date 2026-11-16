import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, type CatalogMeta, type Change, type EntityDetail as Detail, type Overview } from "../api";
import { ChangeLabel, Confidence, ErrorBox, Loading, ScoreBadge, SemStatusBadge, SeverityLabel, TierBadge } from "../components/bits";
import { fmtAgo, fmtChangeValue, fmtInterval, fmtTime, short } from "../format";

const SCOPE_LABEL: Record<string, string> = { SERVER_SCOPE: "서버", SHARED_SCOPE: "공유", CLIENT_SCOPE: "클라이언트" };

export default function EntityDetail() {
  const { id } = useParams();
  const d = useQuery({ queryKey: ["entity", id], queryFn: () => api<Detail>(`/api/entities/${id}`) });
  const ch = useQuery({ queryKey: ["changes", id], queryFn: () => api<Change[]>(`/api/changes?entity_id=${id}&limit=50`) });
  const ov = useQuery({ queryKey: ["overview", id], queryFn: () => api<Overview>(`/api/entities/${id}/overview`) });
  const meta = useQuery({ queryKey: ["catalog-meta"], queryFn: () => api<CatalogMeta>("/api/catalog/meta"), staleTime: Infinity });
  const semKo = (s: string | null) => !s ? "-" : s === "unknown" ? "모름" : meta.data?.semantics[s]?.ko ?? s;
  if (d.isLoading) return <Loading />;
  if (d.error) return <ErrorBox error={d.error} />;
  const e = d.data!;
  const noRel = !e.relations_in.length && !e.relations_out.length;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumbs">
            <Link to="/entities">엔티티</Link>
            {ov.data?.path.map((p) => <Link key={p.id} to={`/entities/${p.id}`}>{p.name}</Link>)}
          </div>
          <h1>{e.name}</h1>
          <div className="sub">{e.entity_type === "DEVICE" ? "디바이스" : "에셋"} · {e.profile ?? "프로파일 없음"}{e.label ? ` · ${e.label}` : ""}</div>
        </div>
        <Link className="btn" to={`/topology?focus=${e.id}`}>그래프에서 보기</Link>
      </div>

      {ov.data && (
        <div className="grid two" style={{ marginBottom: 14 }}>
          <div className="card">
            <h2>360° 요약</h2>
            <ul className="summary">{ov.data.summary.map((s, i) => <li key={i}>{s}</li>)}</ul>
            {ov.data.issues.length > 0 && <div style={{ marginTop: 8 }}>{ov.data.issues.map((i, k) => <div key={k}><SeverityLabel s={i.severity} /> {i.type_ko}</div>)}</div>}
            {ov.data.groups.length > 0 && <div className="sub" style={{ marginTop: 8 }}>엔티티 그룹: {ov.data.groups.join(", ")}</div>}
            <div className="sub" style={{ marginTop: 8 }}>상관이 높은 엔티티 Top 10 은 상관 분석 단계(Phase 5)에서 추가됩니다.</div>
          </div>
          {e.entity_type === "ASSET" && ov.data.descendants > 0 ? (
            <div className="card">
              <h2>하위 요약 <span className="sub">· 하위 엔티티 {ov.data.descendants}개</span></h2>
              <dl className="kv">
                <dt>직속 자식</dt><dd>{Object.entries(ov.data.children_by_profile).map(([k, n]) => `${k.split(":")[1]} ${n}`).join(", ")}</dd>
                <dt>평균 품질</dt><dd><ScoreBadge score={ov.data.subtree_quality.avg} /></dd>
                <dt>품질 문제</dt><dd>심각 {ov.data.subtree_quality.critical} · 주의 {ov.data.subtree_quality.warning}</dd>
                <dt>구조 이슈</dt><dd>{ov.data.subtree_issues.length}건</dd>
              </dl>
              {ov.data.subtree_issues.length > 0 && <ul className="summary" style={{ marginTop: 8 }}>{ov.data.subtree_issues.slice(0, 6).map((i, k) => <li key={k}><SeverityLabel s={i.severity} /> <Link to={`/entities/${i.entity_id}`}>{i.entity_name}</Link> — {i.type_ko}</li>)}</ul>}
            </div>
          ) : (
            <div className="card" style={{ padding: 0 }}>
              <h2 style={{ padding: "16px 18px 0" }}>형제 엔티티 ({ov.data.siblings.length})</h2>
              {ov.data.missing_vs_siblings.length > 0 && <div className="banner warning" style={{ margin: "0 18px 10px" }}>형제 대부분이 측정하는 <b>{ov.data.missing_vs_siblings.map(semKo).join(", ")}</b> 이(가) 이 엔티티에는 없습니다.</div>}
              <div className="table-wrap" style={{ maxHeight: 260 }}>
                <table>
                  <thead><tr><th>이름</th><th>프로파일</th><th>측정 의미</th><th className="num">품질</th></tr></thead>
                  <tbody>
                    {ov.data.siblings.map((s) => (
                      <tr key={s.id}><td><Link to={`/entities/${s.id}`}>{s.name}</Link></td><td>{s.profile ?? "-"}</td>
                        <td className="sub">{s.semantics.map(semKo).join(", ") || "-"}</td><td className="num"><ScoreBadge score={s.quality_score} /></td></tr>
                    ))}
                    {!ov.data.siblings.length && <tr><td colSpan={4} className="empty">형제 엔티티 없음</td></tr>}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="grid two" style={{ marginBottom: 14 }}>
        <div className="card">
          <h2>기본 정보</h2>
          <dl className="kv">
            <dt>품질 점수</dt><dd><ScoreBadge score={e.quality_score} /></dd>
            <dt>소유 고객</dt><dd>{e.owner_name ?? "테넌트"}</dd>
            <dt>TB ID</dt><dd className="mono">{e.id}</dd>
            <dt>생성</dt><dd>{fmtTime(e.created_time)}</dd>
            <dt>처음 수집</dt><dd>{fmtTime(e.first_seen_ms)}</dd>
            <dt>상세 확인</dt><dd>{fmtAgo(e.detail_synced_ms)}</dd>
            <dt>주기 측정</dt><dd>{fmtAgo(e.profile_synced_ms)}</dd>
          </dl>
        </div>
        <div className="card">
          <h2>관계</h2>
          {noRel && <div className="banner warning" style={{ marginBottom: 0 }}>어떤 에셋·디바이스와도 관계가 없습니다. 누락된 관계일 수 있습니다.</div>}
          {e.relations_in.length > 0 && <>
            <div className="sub">상위 (이 엔티티를 포함)</div>
            <ul>{e.relations_in.map((r) => <li key={r.from_id + r.type}><Link to={`/entities/${r.from_id}`}>{r.from_name}</Link> <span className="badge">{r.type}</span></li>)}</ul>
          </>}
          {e.relations_out.length > 0 && <>
            <div className="sub">하위 (이 엔티티가 포함)</div>
            <ul>{e.relations_out.map((r) => <li key={r.to_id + r.type}><Link to={`/entities/${r.to_id}`}>{r.to_name}</Link> <span className="badge">{r.type}</span></li>)}</ul>
          </>}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14, padding: 0 }}>
        <h2 style={{ padding: "16px 18px 0" }}>텔레메트리 키 ({e.timeseries_keys.length})</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>키</th><th>의미</th><th>데이터 종류</th><th>판정</th><th>보고 주기</th><th className="num">품질</th><th>문제</th><th>마지막 값</th></tr></thead>
            <tbody>
              {e.timeseries_keys.map((k) => (
                <tr key={k.key}>
                  <td className="mono">{k.key}</td>
                  <td title={k.evidence.join("\n")}><b>{semKo(k.semantic)}</b>{k.unit ? <span className="sub"> {k.unit}</span> : null}</td>
                  <td>{meta.data?.data_types[k.data_type ?? ""] ?? "-"}</td>
                  <td><SemStatusBadge s={k.semantic_status} /> <Confidence v={k.confidence} /></td>
                  <td><TierBadge tier={k.tier} /> <span className="sub">{fmtInterval(k.interval_s)}{k.irregular ? " 불규칙" : ""}</span></td>
                  <td className="num"><ScoreBadge score={k.quality_score} /></td>
                  <td>{k.findings.length ? k.findings.map((f) => <div key={f.check}><SeverityLabel s={f.severity} /> {f.message}</div>) : <span className="sub">없음</span>}</td>
                  <td className="sub nowrap">{fmtAgo(k.last_ts)}</td>
                </tr>
              ))}
              {!e.timeseries_keys.length && <tr><td colSpan={8} className="empty">텔레메트리 키 없음</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid two">
        <div className="card" style={{ padding: 0 }}>
          <h2 style={{ padding: "16px 18px 0" }}>속성 ({e.attributes.length})</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>범위</th><th>키</th><th>역할</th><th>값</th></tr></thead>
              <tbody>
                {e.attributes.map((a) => {
                  const r = e.attribute_roles[`${a.scope}.${a.key}`];
                  return (
                    <tr key={a.scope + a.key}><td>{SCOPE_LABEL[a.scope] ?? a.scope}</td><td className="mono">{a.key}</td>
                      <td>{r ? <>{meta.data?.roles[r.role] ?? r.role}{r.dynamic ? <span className="sub"> · 동적</span> : null}</> : "-"}</td>
                      <td title={JSON.stringify(a.value)}>{short(a.value)}</td></tr>
                  );
                })}
                {!e.attributes.length && <tr><td colSpan={4} className="empty">속성 없음</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
        <div className="card" style={{ padding: 0 }}>
          <h2 style={{ padding: "16px 18px 0" }}>변경 이력</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>시각</th><th>변경</th><th>내용</th></tr></thead>
              <tbody>
                {(ch.data ?? []).map((c) => (
                  <tr key={c.id}><td className="sub nowrap">{fmtTime(c.ts_ms)}</td><td><ChangeLabel type={c.change_type} /></td>
                    <td><span className="mono">{c.field ?? ""}</span> {c.before !== null || c.after !== null ? <span className="sub">{fmtChangeValue(c.before, 30)} → {fmtChangeValue(c.after, 30)}</span> : null}</td></tr>
                ))}
                {ch.data && !ch.data.length && <tr><td colSpan={3} className="empty">기록된 변경 없음</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}
