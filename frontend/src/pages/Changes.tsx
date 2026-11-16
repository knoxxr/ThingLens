import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Change } from "../api";
import { ChangeLabel, ErrorBox, Loading } from "../components/bits";
import { CHANGE_LABEL, fmtChangeValue, fmtTime } from "../format";

export default function Changes() {
  const [type, setType] = useState("");
  const q = useQuery({
    queryKey: ["changes-all", type],
    queryFn: () => api<Change[]>(`/api/changes?limit=500${type ? `&change_type=${type}` : ""}`),
  });
  return (
    <>
      <div className="page-head">
        <div>
          <h1>변경 이력</h1>
          <div className="sub">동기화 주기마다 감지한 메타데이터 변경. 관계·속성은 최대 24시간 늦게 감지될 수 있습니다 ('지금 동기화'로 즉시 확인).</div>
        </div>
      </div>
      <div className="filters">
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="변경 종류">
          <option value="">전체 변경</option>
          {Object.entries(CHANGE_LABEL).map(([k, v]) => <option key={k} value={k}>{v.text}</option>)}
        </select>
        {q.data && <span className="sub">{q.data.length}건{q.data.length === 500 ? " (최근 500건)" : ""}</span>}
      </div>
      {q.error && <ErrorBox error={q.error} />}
      <div className="card" style={{ padding: 0 }}>
        {q.isLoading ? <Loading /> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>시각</th><th>엔티티</th><th>변경</th><th>항목</th><th>이전</th><th>이후</th><th className="num">주기</th></tr></thead>
              <tbody>
                {q.data!.map((c) => (
                  <tr key={c.id}>
                    <td className="sub nowrap">{fmtTime(c.ts_ms)}</td>
                    <td>{c.entity_name ? <Link to={`/entities/${c.entity_id}`}>{c.entity_name}</Link> : <span className="sub">{c.entity_id}</span>}</td>
                    <td><ChangeLabel type={c.change_type} /></td>
                    <td className="mono">{c.field ?? ""}</td>
                    <td title={JSON.stringify(c.before)}>{fmtChangeValue(c.before)}</td>
                    <td title={JSON.stringify(c.after)}>{fmtChangeValue(c.after)}</td>
                    <td className="num sub">#{c.cycle_id}</td>
                  </tr>
                ))}
                {!q.data!.length && <tr><td colSpan={7} className="empty">아직 감지된 변경이 없습니다. 첫 동기화는 기준선이라 변경으로 기록하지 않습니다.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
