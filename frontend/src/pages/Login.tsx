import { useState } from "react";
import { api, ApiError } from "../api";

export default function Login({ onLogin }: { onLogin: (token: string) => void }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ token: string }>("/api/auth/login", { method: "POST", body: JSON.stringify({ login, password }) });
      onLogin(r.token);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "아이디 또는 비밀번호가 올바르지 않습니다." : "로그인할 수 없습니다. 서버 상태를 확인하세요.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <div className="brand" style={{ padding: 0 }}>Thing<span>Lens</span></div>
        <div className="sub" style={{ marginTop: -8 }}>ThingsBoard 디바이스·에셋 상관관계 분석</div>
        <label>아이디<input autoFocus value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" required /></label>
        <label>비밀번호<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></label>
        {error && <div className="error" role="alert">{error}</div>}
        <button className="primary" disabled={busy}>{busy ? "로그인 중…" : "로그인"}</button>
      </form>
    </div>
  );
}
