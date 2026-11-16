"""httpx.MockTransport 로 동작하는 가짜 ThingsBoard (CE/PE 모드, 장애 주입, 요청 기록)."""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import uuid

import httpx

NULL_UUID = "13814000-1dd2-11b2-8080-808080808080"


def uid() -> str:
    return str(uuid.uuid4())


class FakeTb:
    def __init__(self, edition: str = "CE"):
        self.edition = edition
        self.entities: dict[str, dict] = {}   # id -> {type, name, label, profile, owner, version, ts, attrs}
        self.customers: dict[str, dict] = {}  # id -> {title, parent}
        self.relations: list[tuple] = []      # (from_id, from_type, to_id, to_type, type)
        self.requests: list[tuple[str, str]] = []
        self.fail: dict[str, list[int]] = {}  # path prefix -> [status, ...] 순서대로 1회씩
        self.token_gen = 0
        self.now_ms = int(time.time() * 1000)
        self.agg_requests: list[tuple[str, int]] = []
        self.groups: dict[str, dict] = {}     # [PE] id -> {type, name, members}

    def add_group(self, gtype, name, members) -> str:
        gid = uid()
        self.groups[gid] = {"type": gtype, "name": name, "members": list(members)}
        return gid

    def points(self, spec, start, end, limit):
        """spec: 주기(초) 또는 {"interval": 초, "gen": fn(ts)->값, "until": 마지막 ts}. 최신부터 limit개."""
        if isinstance(spec, dict):
            iv, gen, until = spec["interval"], spec.get("gen"), spec.get("until", self.now_ms)
        else:
            iv, gen, until = spec, None, self.now_ms
        gen = gen or (lambda t: f"{20 + 5 * math.sin(t / 3_600_000):.3f}")
        step = int(iv * 1000)
        t = min(until, end)
        t -= (t - until) % step if t < until else 0
        n = 0
        while t >= start and n < limit:
            yield t, gen(t)
            t -= step
            n += 1

    # -- 모델 조작 ---------------------------------------------------------------
    def add_customer(self, title, parent=None) -> str:
        cid = uid()
        self.customers[cid] = {"title": title, "parent": parent}
        return cid

    def add_entity(self, etype, name, profile, owner=None, ts=None, attrs=None, label=None) -> str:
        eid = uid()
        self.entities[eid] = {"type": etype, "name": name, "label": label, "profile": profile, "owner": owner,
                              "version": 1, "ts": ts or {}, "attrs": attrs or {}}
        return eid

    def relate(self, from_id, to_id, rtype="Contains"):
        self.relations.append((from_id, self.entities[from_id]["type"], to_id, self.entities[to_id]["type"], rtype))

    def touch(self, eid, **changes):
        self.entities[eid].update(changes)
        self.entities[eid]["version"] += 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- 응답 ------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        qs = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        self.requests.append((request.method, path))
        for prefix, statuses in list(self.fail.items()):
            if path.startswith(prefix) and statuses:
                code = statuses.pop(0)
                return httpx.Response(code, json={"message": "injected"})
        if path == "/api/auth/login":
            body = json.loads(request.content)
            if body.get("password") != "pw":
                return httpx.Response(401, json={"message": "bad credentials"})
            return httpx.Response(200, json={"token": f"t{self.token_gen}", "refreshToken": "r"})
        if path == "/api/auth/token":
            self.token_gen += 1
            return httpx.Response(200, json={"token": f"t{self.token_gen}", "refreshToken": "r"})
        if request.headers.get("X-Authorization") != f"Bearer t{self.token_gen}":
            return httpx.Response(401, json={"message": "expired"})

        if path == "/api/customers":
            items = [{"id": {"entityType": "CUSTOMER", "id": k}, "title": v["title"],
                      **({"parentCustomerId": {"entityType": "CUSTOMER", "id": v["parent"]}} if v["parent"] else {})}
                     for k, v in self.customers.items()]
            return self._page(items, qs)
        info_paths = {"CE": {"/api/tenant/deviceInfos": "DEVICE", "/api/tenant/assetInfos": "ASSET"},
                      "PE": {"/api/deviceInfos/all": "DEVICE", "/api/assetInfos/all": "ASSET"}}[self.edition]
        if path in info_paths:
            et = info_paths[path]
            return self._page([self._info(k, v) for k, v in self.entities.items() if v["type"] == et], qs)
        if path.startswith("/api/entityGroups/"):
            if self.edition != "PE":
                return httpx.Response(404, json={"message": "no groups in CE"})
            gt = path.rsplit("/", 1)[1]
            items = [{"id": {"entityType": "ENTITY_GROUP", "id": k}, "name": g["name"], "type": gt}
                     for k, g in self.groups.items() if g["type"] == gt]
            return httpx.Response(200, json=items)  # PE 3.x 형식: 목록
        if path.startswith("/api/entityGroup/") and path.endswith("/entities"):
            g = self.groups.get(path.split("/")[3])
            if g is None:
                return httpx.Response(404, json={"message": "no group"})
            return self._page([{"id": {"entityType": g["type"], "id": m}} for m in g["members"]], qs)
        if path == "/api/relations/info":
            rows = [{"from": {"entityType": f_t, "id": f}, "to": {"entityType": t_t, "id": t}, "type": rt,
                     "typeGroup": "COMMON"} for f, f_t, t, t_t, rt in self.relations if f == qs.get("fromId")]
            return httpx.Response(200, json=rows)
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "plugins", "telemetry"] and len(parts) >= 6:
            ent = self.entities.get(parts[4])
            if ent is None:
                return httpx.Response(404, json={"message": "not found"})
            tail = "/".join(parts[5:])
            if tail.startswith("values/attributes/"):
                scope = parts[-1]
                return httpx.Response(200, json=[{"key": k, "value": v, "lastUpdateTs": self.now_ms}
                                                 for k, v in ent["attrs"].get(scope, {}).items()])
            if tail == "keys/timeseries":
                return httpx.Response(200, json=list(ent["ts"].keys()))
            if tail == "values/timeseries":
                start, end = int(qs["startTs"]), int(qs["endTs"])
                agg = qs.get("agg", "NONE")
                if agg != "NONE":  # Phase 0 결정: 구간 1개짜리 집계만 허용
                    assert int(qs["interval"]) == end - start, "구간이 여러 개인 서버 집계 사용 금지"
                    self.agg_requests.append((agg, end - start))
                out = {}
                for k in qs["keys"].split(","):
                    spec = ent["ts"].get(k)
                    if spec is None:
                        continue
                    if agg == "NONE":
                        pts = list(self.points(spec, start, end, int(qs["limit"])))
                        if pts:
                            out[k] = [{"ts": t, "value": v} for t, v in pts]
                    else:
                        vals = [float(v) for _, v in self.points(spec, start, end, 10**9)]
                        if vals:
                            r = {"MIN": min(vals), "MAX": max(vals), "COUNT": len(vals)}[agg]
                            out[k] = [{"ts": (start + end) // 2, "value": str(r)}]
                return httpx.Response(200, json=out)
        return httpx.Response(404, json={"message": f"no route {path}"})

    def _info(self, eid, v) -> dict:
        owner = {"entityType": "CUSTOMER", "id": v["owner"]} if v["owner"] else {"entityType": "CUSTOMER",
                                                                                   "id": NULL_UUID}
        d = {"id": {"entityType": v["type"], "id": eid}, "name": v["name"], "label": v["label"],
             "createdTime": 1, "version": v["version"], "active": True}
        if v["type"] == "DEVICE":
            d["deviceProfileName"] = v["profile"]
        else:
            d["assetProfileName"] = v["profile"]
        if self.edition == "PE":
            d["ownerId"] = owner if v["owner"] else {"entityType": "TENANT", "id": uid()}
            d["ownerName"] = self.customers[v["owner"]]["title"] if v["owner"] else "Tenant"
        else:
            d["customerId"] = owner
            d["customerTitle"] = self.customers[v["owner"]]["title"] if v["owner"] else None
        return d

    @staticmethod
    def _page(items, qs):
        size, page = int(qs.get("pageSize", 100)), int(qs.get("page", 0))
        chunk = items[page * size:(page + 1) * size]
        return httpx.Response(200, json={"data": chunk, "hasNext": (page + 1) * size < len(items),
                                         "totalElements": len(items)})


def build_fleet(tb: FakeTb) -> dict:
    """작은 시나리오: 고객 2곳(하위 고객 포함), 라인 2개, 1초 키 펌프 등."""
    ids = {}
    ids["cust_a"] = tb.add_customer("Customer A")
    ids["cust_a_sub"] = tb.add_customer("Customer A-Sub", parent=ids["cust_a"])
    ids["cust_b"] = tb.add_customer("Customer B")
    ids["line_a"] = tb.add_entity("ASSET", "Line-A", "Line", owner=ids["cust_a"],
                                  attrs={"SERVER_SCOPE": {"location": "Line-A"}})
    ids["line_b"] = tb.add_entity("ASSET", "Line-B", "Line", owner=ids["cust_b"])
    ids["ts_a1"] = tb.add_entity("DEVICE", "TS-A1", "TempSensor", owner=ids["cust_a"],
                                 ts={"temperature": 60, "battery": 3600},
                                 attrs={"SERVER_SCOPE": {"location": "Line-A"}, "SHARED_SCOPE": {"interval": 60},
                                        "CLIENT_SCOPE": {"fw": "1.0"}})
    ids["pump_a"] = tb.add_entity("DEVICE", "PUMP-A", "Pump", owner=ids["cust_a_sub"],
                                  ts={"current": 1, "p1_out": 1, "dead": None})
    ids["ts_b1"] = tb.add_entity("DEVICE", "TS-B1", "TempSensor", owner=ids["cust_b"], ts={"temperature": 60})
    ids["orphan"] = tb.add_entity("DEVICE", "ORPHAN", "TempSensor", ts={"temperature": 900})
    tb.relate(ids["line_a"], ids["ts_a1"])
    tb.relate(ids["line_a"], ids["pump_a"])
    tb.relate(ids["line_b"], ids["ts_b1"])
    return ids
