import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Entity } from "../api";
import { ErrorBox, Loading } from "../components/bits";
import { fmtAgo } from "../format";

type Row = Entity & { key_count: number; relation_count: number };
const PAGE = 50;

export default function Entities() {
  const [sp, setSp] = useSearchParams();
  const type = sp.get("type") ?? "";
  const profile = sp.get("profile") ?? "";
  const q = sp.get("q") ?? "";
  const page = Number(sp.get("page") ?? 0);
  const set = (k: string, v: string) => {
    const n = new URLSearchParams(sp);
    if (v) n.set(k, v); else n.delete(k);
    if (k !== "page") n.delete("page");
    setSp(n, { replace: true });
  };

  const facets = useQuery({ queryKey: ["facets"], queryFn: () => api<{ entity_type: string; profile: string | null; n: number }[]>("/api/entities-facets") });
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
  if (type) params.set("entity_type", type);
  if (profile) params.set("profile", profile);
  if (q) params.set("q", q);
  const list = useQuery({ queryKey: ["entities", params.toString()], queryFn: () => api<{ total: number; items: Row[] }>(`/api/entities?${params}`) });

  const profiles = (facets.data ?? []).filter((f) => !type || f.entity_type === type);
  const total = list.data?.total ?? 0;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>엔티티</h1>
          <div className="sub">ThingsBoard에서 수집한 디바이스·에셋 (권한 범위 내)</div>
        </div>
      </div>
      <div className="filters">
        <select value={type} onChange={(e) => { set("type", e.target.value); }} aria-label="유형">
          <option value="">전체 유형</option><option value="DEVICE">디바이스</option><option value="ASSET">에셋</option>
        </select>
        <select value={profile} onChange={(e) => set("profile", e.target.value)} aria-label="프로파일">
          <option value="">전체 프로파일</option>
          {profiles.map((f) => <option key={`${f.entity_type}:${f.profile}`} value={f.profile ?? ""}>{f.profile ?? "(없음)"} ({f.n})</option>)}
        </select>
        <input placeholder="이름·라벨 검색" defaultValue={q} onKeyDown={(e) => { if (e.key === "Enter") set("q", (e.target as HTMLInputElement).value); }} aria-label="검색" />
        <span className="sub">{total.toLocaleString()}개</span>
      </div>
      {list.error && <ErrorBox error={list.error} />}
      <div className="card" style={{ padding: 0 }}>
        {list.isLoading ? <Loading /> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>이름</th><th>유형</th><th>프로파일</th><th>소유 고객</th><th className="num">텔레메트리 키</th><th className="num">관계</th><th>마지막 확인</th></tr></thead>
              <tbody>
                {list.data!.items.map((e) => (
                  <tr key={e.id}>
                    <td><Link to={`/entities/${e.id}`}>{e.name}</Link>{e.label ? <span className="sub"> · {e.label}</span> : null}</td>
                    <td>{e.entity_type === "DEVICE" ? "디바이스" : "에셋"}</td>
                    <td>{e.profile ?? "-"}</td>
                    <td>{e.owner_name ?? <span className="sub">테넌트</span>}</td>
                    <td className="num">{e.key_count}</td>
                    <td className="num">{e.relation_count === 0 ? <span className="badge" title="어떤 엔티티와도 관계가 없습니다">관계 없음</span> : e.relation_count}</td>
                    <td className="sub">{fmtAgo(e.last_seen_ms)}</td>
                  </tr>
                ))}
                {!list.data!.items.length && <tr><td colSpan={7} className="empty">조건에 맞는 엔티티가 없습니다</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {total > PAGE && (
        <div className="filters" style={{ marginTop: 12 }}>
          <button disabled={page === 0} onClick={() => set("page", String(page - 1))}>이전</button>
          <span className="sub">{page + 1} / {Math.ceil(total / PAGE)}</span>
          <button disabled={(page + 1) * PAGE >= total} onClick={() => set("page", String(page + 1))}>다음</button>
        </div>
      )}
    </>
  );
}
