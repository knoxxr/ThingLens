import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type QualityFindingRow, type QualitySummary } from "../api";
import { ErrorBox, Loading, ScoreBadge, SeverityLabel, Tile } from "../components/bits";
import { fmtAgo } from "../format";

export default function Quality() {
  const [severity, setSeverity] = useState("");
  const [check, setCheck] = useState("");
  const summ = useQuery({ queryKey: ["quality-summary"], queryFn: () => api<QualitySummary>("/api/quality/summary") });
  const params = new URLSearchParams();
  if (severity) params.set("severity", severity);
  if (check) params.set("check", check);
  const list = useQuery({ queryKey: ["quality-findings", params.toString()], queryFn: () => api<QualityFindingRow[]>(`/api/quality/findings?${params}`) });
  if (summ.isLoading) return <Loading />;
  if (summ.error) return <ErrorBox error={summ.error} />;
  const s = summ.data!;
  const sev = (k: string) => s.open_by_check.filter((c) => c.severity === k).reduce((a, c) => a + c.count, 0);
  const checks = [...new Map(s.open_by_check.map((c) => [c.check, c.check_ko])).entries()];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>데이터 품질</h1>
          <div className="sub">키별 최근 샘플과 기간 범위(최솟값·최댓값·개수)로 점검합니다. 품질이 나쁜 데이터는 상관 분석에서 제외하거나 경고합니다.</div>
        </div>
      </div>
      <div className="grid tiles" style={{ marginBottom: 14 }}>
        <Tile label="평균 품질 점수" value={<ScoreBadge score={s.average} />} hint={`엔티티 ${s.entities_scored}개 · 100점 만점`} />
        <Tile label="심각" value={sev("critical")} hint="값 고정·범위 이탈·보고 중단 등" />
        <Tile label="주의" value={sev("warning")} hint="수신률 저하·튐·혼재 등" />
        <Tile label="참고" value={sev("info")} hint="값 변화 없음·카운터 리셋" />
        <div className="card tile">
          <div className="label">점수 분포 (엔티티 수)</div>
          <table style={{ marginTop: 4 }}><tbody>
            {Object.entries(s.buckets).map(([b, n]) => (
              <tr key={b}><td style={{ padding: "1px 0", border: 0 }} className="sub">{b}점</td><td className="num" style={{ padding: "1px 0", border: 0 }}><b>{n}</b></td></tr>
            ))}
          </tbody></table>
        </div>
      </div>
      <div className="grid two" style={{ marginBottom: 14 }}>
        <div className="card" style={{ padding: 0 }}>
          <h2 style={{ padding: "16px 18px 0" }}>점검 항목별 미해결 건수</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>항목</th><th>심각도</th><th className="num">건수</th></tr></thead>
              <tbody>
                {s.open_by_check.map((c) => (
                  <tr key={c.check + c.severity} style={{ cursor: "pointer" }} onClick={() => { setCheck(c.check); setSeverity(c.severity); }}>
                    <td>{c.check_ko}</td><td><SeverityLabel s={c.severity} /></td><td className="num">{c.count}</td></tr>
                ))}
                {!s.open_by_check.length && <tr><td colSpan={3} className="empty">미해결 문제가 없습니다</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
        <div className="card" style={{ padding: 0 }}>
          <h2 style={{ padding: "16px 18px 0" }}>점수가 낮은 엔티티</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>엔티티</th><th>프로파일</th><th className="num">점수</th></tr></thead>
              <tbody>
                {s.worst.filter((w) => w.score < 100).map((w) => (
                  <tr key={w.id}><td><Link to={`/entities/${w.id}`}>{w.name}</Link></td><td>{w.profile ?? "-"}</td><td className="num"><ScoreBadge score={w.score} /></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="filters">
        <select value={severity} onChange={(e) => setSeverity(e.target.value)} aria-label="심각도">
          <option value="">전체 심각도</option><option value="critical">심각</option><option value="warning">주의</option><option value="info">참고</option>
        </select>
        <select value={check} onChange={(e) => setCheck(e.target.value)} aria-label="점검 항목">
          <option value="">전체 항목</option>
          {checks.map(([c, ko]) => <option key={c} value={c}>{ko}</option>)}
        </select>
        {(severity || check) && <button onClick={() => { setSeverity(""); setCheck(""); }}>필터 해제</button>}
        {list.data && <span className="sub">{list.data.length}건</span>}
      </div>
      <div className="card" style={{ padding: 0 }}>
        {list.isLoading ? <Loading /> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>심각도</th><th>엔티티</th><th>키</th><th>항목</th><th>내용</th><th>처음 감지</th></tr></thead>
              <tbody>
                {list.data!.map((f) => (
                  <tr key={f.id}>
                    <td><SeverityLabel s={f.severity} /></td>
                    <td><Link to={`/entities/${f.entity_id}`}>{f.entity_name}</Link></td>
                    <td className="mono">{f.key}</td>
                    <td>{f.check_ko}</td>
                    <td>{f.message}</td>
                    <td className="sub nowrap">{fmtAgo(f.first_detected_ms)}</td>
                  </tr>
                ))}
                {!list.data!.length && <tr><td colSpan={6} className="empty">해당하는 문제가 없습니다</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
