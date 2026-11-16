import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { api, getToken, setToken, type User } from "./api";
import { ROLE_LABEL } from "./format";
import Admin from "./pages/Admin";
import Catalog from "./pages/Catalog";
import Quality from "./pages/Quality";
// 그래프 라이브러리(cytoscape)가 커서 관계 구조 화면은 필요할 때만 불러온다
const Topology = lazy(() => import("./pages/Topology"));
import Changes from "./pages/Changes";
import EntityDetail from "./pages/EntityDetail";
import Entities from "./pages/Entities";
import Login from "./pages/Login";
import Scheduler from "./pages/Scheduler";

export default function App() {
  const qc = useQueryClient();
  const [token, setTok] = useState(getToken());
  useEffect(() => {
    const onLogout = () => { setTok(null); qc.clear(); };
    window.addEventListener("thinglens:logout", onLogout);
    return () => window.removeEventListener("thinglens:logout", onLogout);
  }, [qc]);

  const me = useQuery({ queryKey: ["me", token], queryFn: () => api<User>("/api/me"), enabled: !!token });

  if (!token) return <Login onLogin={(t) => { setToken(t); setTok(t); }} />;
  if (me.isLoading || !me.data) return <div className="empty">불러오는 중…</div>;
  const user = me.data;
  const canOps = user.role === "ADMIN" || user.role === "ANALYST";

  const logout = async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* 이미 만료 */ }
    setToken(null);
    setTok(null);
    qc.clear();
  };

  return (
    <div className="shell">
      <nav className="nav" aria-label="주 메뉴">
        <div className="brand">Thing<span>Lens</span></div>
        {canOps && <NavLink to="/scheduler">수집 상태</NavLink>}
        <NavLink to="/entities">엔티티</NavLink>
        <NavLink to="/topology">관계 구조</NavLink>
        <NavLink to="/catalog">데이터 카탈로그</NavLink>
        <NavLink to="/quality">데이터 품질</NavLink>
        <NavLink to="/changes">변경 이력</NavLink>
        {user.role === "ADMIN" && <NavLink to="/admin">사용자·감사</NavLink>}
        <div className="spacer" />
        <div className="who">
          {user.login} · {ROLE_LABEL[user.role]}
          <div><button onClick={logout} style={{ marginTop: 6, padding: "3px 10px" }}>로그아웃</button></div>
        </div>
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to={canOps ? "/scheduler" : "/entities"} replace />} />
          {canOps && <Route path="/scheduler" element={<Scheduler user={user} />} />}
          <Route path="/entities" element={<Entities />} />
          <Route path="/entities/:id" element={<EntityDetail />} />
          <Route path="/changes" element={<Changes />} />
          <Route path="/catalog" element={<Catalog user={user} />} />
          <Route path="/topology" element={<Suspense fallback={<div className="empty">불러오는 중…</div>}><Topology /></Suspense>} />
          <Route path="/quality" element={<Quality />} />
          {user.role === "ADMIN" && <Route path="/admin" element={<Admin />} />}
          <Route path="*" element={<div className="empty">페이지를 찾을 수 없습니다.</div>} />
        </Routes>
      </main>
    </div>
  );
}
