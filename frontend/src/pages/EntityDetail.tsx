import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, type CatalogMeta, type Change, type EntityDetail as Detail } from "../api";
import { ChangeLabel, Confidence, ErrorBox, Loading, ScoreBadge, SemStatusBadge, SeverityLabel, TierBadge } from "../components/bits";
import { fmtAgo, fmtChangeValue, fmtInterval, fmtTime, short } from "../format";

const SCOPE_LABEL: Record<string, string> = { SERVER_SCOPE: "서버", SHARED_SCOPE: "공유", CLIENT_SCOPE: "클라이언트" };

export default function EntityDetail() {
  const { id } = useParams();
  const d = useQuery({ queryKey: ["entity", id], queryFn: () => api<Detail>(`/api/entities/${id}`) });
  const ch = useQuery({ queryKey: ["changes", id], queryFn: () => api<Change[]>(`/api/changes?entity_id=${id}&limit=50`) });
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
          <div className="sub"><Link to="/entities">엔티티</Link> /</div>
          <h1>{e.name}</h1>
          <div className="sub">{e.entity_type === "DEVICE" ? "디바이스" : "에셋"} · {e.profile ?? "프로파일 없음"}{e.label ? ` · ${e.label}` : ""}</div>
        </div>
      </div>

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
