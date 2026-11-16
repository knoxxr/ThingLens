import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type MetricBucket, type SchedulerStatus, type User } from "../api";
import { ErrorBox, Loading, Status, Tile } from "../components/bits";
import { MinuteChart } from "../components/MinuteChart";
import { fmtAgo, fmtTime, TASK_LABEL } from "../format";

const RANGES = [{ m: 60, label: "1시간" }, { m: 360, label: "6시간" }, { m: 1440, label: "24시간" }];

export default function Scheduler({ user }: { user: User }) {
  const qc = useQueryClient();
  const [minutes, setMinutes] = useState(60);
  const st = useQuery({ queryKey: ["scheduler"], queryFn: () => api<SchedulerStatus>("/api/scheduler"), refetchInterval: 5_000 });
  const metrics = useQuery({
    queryKey: ["metrics", minutes],
    queryFn: () => api<MetricBucket[]>(`/api/scheduler/metrics?minutes=${minutes}`),
    refetchInterval: 15_000,
  });
  const act = useMutation({
    mutationFn: (path: string) => api(path, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduler"] }),
  });

  if (st.isLoading) return <Loading />;
  if (st.error) return <ErrorBox error={st.error} />;
  const s = st.data!;
  const isAdmin = user.role === "ADMIN";
  const circuit = s.circuit_open_until_ms && s.circuit_open_until_ms > Date.now();
  const running = s.cycles.find((c) => c.status === "RUNNING");
  const r15 = s.requests_15m;

  const worker = !s.worker_alive
    ? <Status kind="critical">중지됨</Status>
    : s.paused ? <Status kind="warning">일시정지</Status>
    : circuit ? <Status kind="critical">보호 정지</Status>
    : <Status kind="good">수집 중</Status>;

  const queueRows = Object.entries(s.queue)
    .map(([kind, c]) => ({ kind, pending: (c.PENDING ?? 0) + (c.RUNNING ?? 0), done: c.DONE ?? 0, failed: c.FAILED ?? 0 }))
    .sort((a, b) => b.pending - a.pending || a.kind.localeCompare(b.kind));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>수집 상태</h1>
          <div className="sub">ThingsBoard에 부하를 주지 않도록 백그라운드에서 천천히 수집합니다.</div>
        </div>
        {isAdmin && (
          <div className="actions">
            {s.paused
              ? <button className="primary" onClick={() => act.mutate("/api/scheduler/resume")} disabled={act.isPending}>수집 재개</button>
              : <button className="danger" onClick={() => act.mutate("/api/scheduler/pause")} disabled={act.isPending}>일시정지</button>}
            <button onClick={() => act.mutate("/api/scheduler/sync-now")} disabled={act.isPending || !!running}
              title="관계·속성·키까지 모든 엔티티를 즉시 다시 확인합니다">지금 동기화</button>
          </div>
        )}
      </div>

      {!s.worker_alive && <div className="banner critical">수집 워커가 응답하지 않습니다 (마지막 신호 {fmtAgo(s.last_heartbeat_ms)}). 서버에서 <code>thinglens scheduler</code> 실행 상태를 확인하세요.</div>}
      {circuit && <div className="banner critical">TB 오류가 연속으로 발생해 {fmtTime(s.circuit_open_until_ms, false)}까지 요청을 멈췄습니다. 이후 자동으로 재개합니다.</div>}
      {s.paused ? <div className="banner warning">{s.paused_by}님이 {fmtTime(s.paused_ms)}에 수집을 일시정지했습니다.</div> : null}
      {act.error && <ErrorBox error={act.error} />}

      <div className="grid tiles" style={{ marginBottom: 14 }}>
        <Tile label="워커" value={worker} hint={`마지막 신호 ${fmtAgo(s.last_heartbeat_ms)}`} />
        <Tile label="현재 속도" value={`${s.current_rate ?? "-"} req/s`} hint={`${s.window_name ?? "-"} 시간대 · 상한 ${s.rate_max ?? "-"} req/s`} />
        <Tile label="최근 15분 요청" value={r15.count.toLocaleString()} hint={`평균 ${r15.per_sec} req/s`} />
        <Tile label="TB 응답 지연 (15분)" value={r15.p95_ms != null ? `${r15.p95_ms} ms` : "-"} hint={`p95 · 중앙값 ${r15.p50_ms ?? "-"} ms`} />
        <Tile label="부하 오류 (15분)" value={r15.load_errors ? <Status kind="critical">{r15.load_errors}건</Status> : <Status kind="good">0건</Status>} hint="429 · 5xx · 연결 실패" />
        <Tile label="대기 작업" value={s.pending.toLocaleString()} hint={running ? `주기 #${running.id} 진행 중` : "진행 중인 주기 없음"} />
      </div>

      <div className="filters" role="group" aria-label="기간">
        {RANGES.map((r) => (
          <button key={r.m} className={minutes === r.m ? "primary" : ""} onClick={() => setMinutes(r.m)}>{r.label}</button>
        ))}
      </div>
      <div className="grid two" style={{ marginBottom: 14 }}>
        <div className="card">
          <h2>분당 TB 요청 수</h2>
          {metrics.data ? <MinuteChart data={metrics.data} kind="bars" value={(b) => b.requests || null} unit="건" ariaLabel="분당 ThingsBoard 요청 수" /> : <Loading />}
        </div>
        <div className="card">
          <h2>TB 응답 지연 (분별 p95, ms)</h2>
          {metrics.data ? <MinuteChart data={metrics.data} kind="line" value={(b) => b.p95_ms} unit=" ms" ariaLabel="분별 ThingsBoard 응답 지연 p95" /> : <Loading />}
        </div>
      </div>

      <div className="grid two">
        <div className="card">
          <h2>작업 대기열</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>작업</th><th className="num">대기·실행</th><th className="num">완료</th><th className="num">실패</th></tr></thead>
              <tbody>
                {queueRows.map((r) => (
                  <tr key={r.kind}>
                    <td>{TASK_LABEL[r.kind] ?? r.kind}</td>
                    <td className="num">{r.pending}</td><td className="num">{r.done}</td>
                    <td className="num">{r.failed ? <Status kind="critical">{r.failed}</Status> : 0}</td>
                  </tr>
                ))}
                {!queueRows.length && <tr><td colSpan={4} className="empty">작업 없음</td></tr>}
              </tbody>
            </table>
          </div>
          {s.last_error && <div className="sub" style={{ marginTop: 8 }}>최근 오류: <code>{s.last_error}</code></div>}
        </div>
        <div className="card">
          <h2>최근 동기화 주기</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>#</th><th>시작</th><th>상태</th><th className="num">엔티티</th><th className="num">변경</th></tr></thead>
              <tbody>
                {s.cycles.map((c) => (
                  <tr key={c.id}>
                    <td>{c.id}{c.full ? <span className="badge" style={{ marginLeft: 6 }}>전체</span> : null}</td>
                    <td>{fmtTime(c.started_ms)}</td>
                    <td>{c.status === "DONE" ? <Status kind="good">완료</Status>
                      : c.status === "RUNNING" ? <Status kind="warning">진행 중</Status>
                      : <Status kind="critical">{c.status === "PARTIAL" ? "일부 실패" : c.status}</Status>}</td>
                    <td className="num">{c.entities_seen}</td><td className="num">{c.status === "RUNNING" ? "-" : c.changes}</td>
                  </tr>
                ))}
                {!s.cycles.length && <tr><td colSpan={5} className="empty">아직 동기화 기록이 없습니다</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            시간대 예산: {s.rate_windows.map((w) => `${w.name} ${w.start}–${w.end} ×${w.factor}`).join(", ") || "없음"}
          </div>
        </div>
      </div>
    </>
  );
}
