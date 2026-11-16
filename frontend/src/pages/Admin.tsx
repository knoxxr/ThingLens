import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type AuditRow, type Customer, type Role, type User } from "../api";
import { ErrorBox, Loading } from "../components/bits";
import { fmtTime, ROLE_LABEL, short } from "../format";

const ACTION_LABEL: Record<string, string> = {
  LOGIN: "로그인", LOGIN_FAILED: "로그인 실패", SCHEDULER_PAUSE: "수집 일시정지", SCHEDULER_RESUME: "수집 재개",
  SYNC_NOW: "지금 동기화", USER_CREATE: "사용자 추가",
};

function auditDetail(a: AuditRow): string {
  const d = a.detail as Record<string, unknown> | null;
  if (!d) return "";
  if (a.action === "SYNC_NOW") return d.full ? "관계·속성·키 전체 재확인" : "엔티티 목록만";
  if (a.action === "USER_CREATE") return `역할 ${ROLE_LABEL[d.role as Role] ?? d.role}`;
  return short(d, 50);
}

export default function Admin() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/api/users") });
  const customers = useQuery({ queryKey: ["customers"], queryFn: () => api<Customer[]>("/api/customers") });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api<AuditRow[]>("/api/audit?limit=200") });
  const [form, setForm] = useState({ login: "", password: "", role: "ANALYST" as Role, customer_ids: [] as string[] });
  const create = useMutation({
    mutationFn: () => api<User>("/api/users", { method: "POST", body: JSON.stringify(form) }),
    onSuccess: () => {
      setForm({ login: "", password: "", role: "ANALYST", customer_ids: [] });
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["audit"] });
    },
  });
  const custName = (id: string) => customers.data?.find((c) => c.id === id)?.title ?? id.slice(0, 8);

  return (
    <>
      <div className="page-head"><h1>사용자·감사</h1></div>
      <div className="grid two" style={{ marginBottom: 14 }}>
        <div className="card" style={{ padding: 0 }}>
          <h2 style={{ padding: "16px 18px 0" }}>사용자</h2>
          {users.isLoading ? <Loading /> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>아이디</th><th>역할</th><th>고객 범위</th></tr></thead>
                <tbody>
                  {users.data?.map((u) => (
                    <tr key={u.id}><td>{u.login}</td><td>{ROLE_LABEL[u.role]}</td>
                      <td>{u.role === "CUSTOMER_VIEWER" ? u.customer_ids.map(custName).join(", ") + " (하위 포함)" : <span className="sub">전체</span>}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <form className="card" onSubmit={(e) => { e.preventDefault(); create.mutate(); }} style={{ display: "grid", gap: 10, alignContent: "start" }}>
          <h2>사용자 추가</h2>
          <input placeholder="아이디" value={form.login} onChange={(e) => setForm({ ...form, login: e.target.value })} required aria-label="아이디" />
          <input type="password" placeholder="비밀번호 (8자 이상)" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required minLength={8} aria-label="비밀번호" />
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })} aria-label="역할">
            {(Object.keys(ROLE_LABEL) as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
          </select>
          {form.role === "CUSTOMER_VIEWER" && (
            <div>
              <div className="sub">조회 가능한 고객 (하위 고객 자동 포함)</div>
              {(customers.data ?? []).map((c) => (
                <label key={c.id} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <input type="checkbox" checked={form.customer_ids.includes(c.id)}
                    onChange={(e) => setForm({ ...form, customer_ids: e.target.checked ? [...form.customer_ids, c.id] : form.customer_ids.filter((x) => x !== c.id) })} />
                  {c.title} <span className="sub">({c.entities})</span>
                </label>
              ))}
            </div>
          )}
          {create.error && <ErrorBox error={create.error} />}
          <button className="primary" disabled={create.isPending}>추가</button>
        </form>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <h2 style={{ padding: "16px 18px 0" }}>감사 로그</h2>
        {audit.isLoading ? <Loading /> : (
          <div className="table-wrap" style={{ maxHeight: 420 }}>
            <table>
              <thead><tr><th>시각</th><th>사용자</th><th>작업</th><th>대상</th><th>내용</th></tr></thead>
              <tbody>
                {audit.data?.map((a) => (
                  <tr key={a.id}><td className="sub">{fmtTime(a.ts_ms)}</td><td>{a.login ?? "-"}</td>
                    <td>{a.action === "LOGIN_FAILED" ? <span className="chg remove">{ACTION_LABEL[a.action]}</span> : ACTION_LABEL[a.action] ?? a.action}</td>
                    <td>{a.target ?? ""}</td><td className="sub">{auditDetail(a)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
