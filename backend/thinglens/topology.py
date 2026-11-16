"""관계 구조 분석 (계획서 §5.1, Phase 3). 저장된 엔티티·관계로만 계산하는 순수 함수 — TB 요청 없음.

계층 관계: 기본 'Contains' (THINGLENS_HIERARCHY_RELATIONS 로 변경). 부모 → 자식 방향.

이슈 종류
  NO_RELATION      : 어떤 관계도 없는 디바이스 (경고)
  OUTSIDE_HIERARCHY: 다른 관계는 있지만 계층(Contains 등) 부모가 없는 디바이스 (경고)
  MULTI_PARENT     : 계층 부모가 둘 이상 (경고)
  CYCLE            : 계층 순환 (심각)
  EMPTY_ASSET      : 자식이 없는 에셋 (참고)
  TOO_DEEP         : 루트에서 깊이가 기준 초과 (참고)
  OWNER_MISMATCH   : 부모·자식 소유 고객이 다름 (하위 고객 관계는 정상) (경고 / 부모가 테넌트 소유면 참고)
  GROUP_MISMATCH   : [PE] 형제 대부분이 속한 엔티티 그룹에 혼자 빠짐 (참고)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

ISSUE_KO = {
    "NO_RELATION": "관계 없음", "OUTSIDE_HIERARCHY": "계층 밖", "MULTI_PARENT": "부모 여럿", "CYCLE": "순환 관계",
    "EMPTY_ASSET": "빈 에셋", "TOO_DEEP": "과도한 깊이", "OWNER_MISMATCH": "소유 고객 불일치",
    "GROUP_MISMATCH": "그룹 불일치",
}


@dataclass
class Issue:
    type: str
    severity: str
    entity_id: str
    message: str
    related: list[str] = field(default_factory=list)


@dataclass
class Topology:
    nodes: dict[str, dict]                       # id -> entity row(dict)
    edges: list[tuple[str, str, str]]            # (from, to, rel_type) — 양 끝이 nodes 안에 있는 것만
    hierarchy_types: set[str]
    parents: dict[str, list[str]] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self):
        self.parents = defaultdict(list)
        self.children = defaultdict(list)
        for f, t, rt in self.edges:
            if rt in self.hierarchy_types:
                self.parents[t].append(f)
                self.children[f].append(t)

    @property
    def roots(self) -> list[str]:
        return sorted((n for n in self.nodes if not self.parents.get(n) and self.children.get(n)),
                      key=lambda n: self.nodes[n].get("name") or "")

    def descendants(self, nid: str) -> set[str]:
        seen, stack = set(), list(self.children.get(nid, []))
        while stack:
            c = stack.pop()
            if c in seen or c == nid:
                continue
            seen.add(c)
            stack.extend(self.children.get(c, []))
        return seen

    def path_to_root(self, nid: str) -> list[str]:
        """첫 번째 부모를 따라 루트까지 (순환 방지). [루트, …, 부모]"""
        path, cur, seen = [], nid, {nid}
        while self.parents.get(cur):
            p = sorted(self.parents[cur])[0]
            if p in seen:
                break
            path.append(p)
            seen.add(p)
            cur = p
        return list(reversed(path))

    def depths(self) -> dict[str, int]:
        depth: dict[str, int] = {}
        frontier = [(r, 0) for r in self.roots]
        while frontier:
            n, d = frontier.pop()
            if n in depth and depth[n] <= d:
                continue
            if d > len(self.nodes):  # 순환 보호
                continue
            depth[n] = d
            frontier.extend((c, d + 1) for c in self.children.get(n, []))
        return depth

    def components(self) -> int:
        adj = defaultdict(set)
        for f, t, _ in self.edges:
            adj[f].add(t)
            adj[t].add(f)
        seen, n = set(), 0
        for v in self.nodes:
            if v in seen or v not in adj:
                continue
            n += 1
            stack = [v]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(adj[x] - seen)
        return n


def find_cycles(topo: Topology) -> list[list[str]]:
    """계층 관계의 순환 (각 순환을 한 번씩)."""
    color, stack, cycles, seen_sets = {}, [], [], set()

    def visit(n):
        color[n] = 1
        stack.append(n)
        for c in topo.children.get(n, []):
            if color.get(c) == 1:
                cyc = stack[stack.index(c):]
                key = frozenset(cyc)
                if key not in seen_sets:
                    seen_sets.add(key)
                    cycles.append(list(cyc))
            elif color.get(c) is None:
                visit(c)
        stack.pop()
        color[n] = 2

    for n in sorted(topo.nodes):
        if color.get(n) is None:
            visit(n)
    return cycles


def analyze(topo: Topology, *, customer_parent: dict[str, str | None] | None = None,
            groups_of: dict[str, set[str]] | None = None, max_depth: int = 6) -> list[Issue]:
    issues: list[Issue] = []
    nodes = topo.nodes
    any_rel = defaultdict(int)
    for f, t, _ in topo.edges:
        any_rel[f] += 1
        any_rel[t] += 1

    for nid, e in nodes.items():
        name = e.get("name")
        if e["entity_type"] == "DEVICE":
            if not any_rel.get(nid):
                issues.append(Issue("NO_RELATION", "warning", nid, f"{name} 은(는) 어떤 에셋·디바이스와도 관계가 없습니다"))
            elif not topo.parents.get(nid):
                issues.append(Issue("OUTSIDE_HIERARCHY", "warning", nid,
                                    f"{name} 은(는) 다른 관계는 있지만 상위 에셋({'/'.join(sorted(topo.hierarchy_types))})이 없습니다"))
        if e["entity_type"] == "ASSET" and not topo.children.get(nid):
            issues.append(Issue("EMPTY_ASSET", "info", nid, f"에셋 {name} 아래에 아무것도 없습니다"))
        ps = sorted(set(topo.parents.get(nid, [])))
        if len(ps) > 1:
            issues.append(Issue("MULTI_PARENT", "warning", nid,
                                f"{name} 의 상위가 {len(ps)}개입니다: {', '.join(nodes[p]['name'] for p in ps)}", ps))

    for cyc in find_cycles(topo):
        names = " → ".join(nodes[c]["name"] for c in cyc + cyc[:1])
        for c in cyc:
            issues.append(Issue("CYCLE", "critical", c, f"계층 관계가 순환합니다: {names}", [x for x in cyc if x != c]))

    for nid, d in topo.depths().items():
        if d > max_depth:
            issues.append(Issue("TOO_DEEP", "info", nid, f"{nodes[nid]['name']} 의 계층 깊이 {d} (기준 {max_depth})"))

    # 소유 고객 불일치 — 자식 소유자가 부모 소유자 자신 또는 그 하위 고객이면 정상
    cp = customer_parent or {}

    def under(owner, ancestor):
        seen = set()
        while owner and owner not in seen:
            if owner == ancestor:
                return True
            seen.add(owner)
            owner = cp.get(owner)
        return False

    for f, t, rt in topo.edges:
        if rt not in topo.hierarchy_types:
            continue
        po, co = nodes[f].get("owner_id"), nodes[t].get("owner_id")
        if po == co or (po and co and under(co, po)):
            continue
        if po is None:  # 부모는 테넌트 소유, 자식만 고객에 할당 — 흔하지만 고객은 상위를 볼 수 없음
            issues.append(Issue("OWNER_MISMATCH", "info", t,
                                f"{nodes[t]['name']} 은(는) {nodes[t].get('owner_name') or '고객'} 소유인데 상위 {nodes[f]['name']} 은(는) 테넌트 소유입니다",
                                [f]))
        else:
            issues.append(Issue("OWNER_MISMATCH", "warning", t,
                                f"{nodes[t]['name']} 의 소유 고객({nodes[t].get('owner_name') or '테넌트'})이 상위 {nodes[f]['name']} 의 소유 고객({nodes[f].get('owner_name')})과 다릅니다",
                                [f]))

    # [PE] 그룹 불일치 — 형제 3개 이상, 60% 이상이 공유하는 그룹에 혼자 빠진 자식
    if groups_of:
        for parent, kids in topo.children.items():
            kids = [k for k in set(kids) if nodes[k]["entity_type"] == "DEVICE"]
            if len(kids) < 3:
                continue
            cnt = Counter(g for k in kids for g in groups_of.get(k, set()))
            for g, n in cnt.items():
                if n >= 0.6 * len(kids) and n < len(kids):
                    for k in kids:
                        if g not in groups_of.get(k, set()):
                            issues.append(Issue("GROUP_MISMATCH", "info", k,
                                                f"{nodes[k]['name']} 은(는) 형제 {n}/{len(kids)}개가 속한 그룹 '{g}' 에 없습니다",
                                                [parent]))
    return issues


def stats(topo: Topology) -> dict:
    nodes = topo.nodes
    per_asset = [sum(1 for c in topo.children.get(a, []) if nodes[c]["entity_type"] == "DEVICE")
                 for a, e in nodes.items() if e["entity_type"] == "ASSET"]
    depth = topo.depths()
    return {
        "entities": Counter(e["entity_type"] for e in nodes.values()),
        "profiles": Counter(f'{e["entity_type"]}:{e.get("profile") or "(없음)"}' for e in nodes.values()),
        "relation_types": Counter(rt for _, _, rt in topo.edges),
        "edges": len(topo.edges),
        "roots": len(topo.roots),
        "components": topo.components(),
        "max_depth": max(depth.values(), default=0),
        "devices_per_asset": {"min": min(per_asset, default=0), "max": max(per_asset, default=0),
                              "avg": round(sum(per_asset) / len(per_asset), 1) if per_asset else 0},
    }
