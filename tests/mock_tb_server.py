"""테스트용 최소 ThingsBoard REST 모의 서버 (tb_diagnose 가 쓰는 엔드포인트만)."""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockTb:
    """엔티티·키·보고 주기 구성을 받아 TB 응답을 흉내낸다."""

    def __init__(self, devices, assets=(), relations=None, fail_once=None, expire_token_after=None):
        # devices/assets: [{"id","name","profile","ts_keys":{key: interval_s or None}, "attr_keys":[...]}]
        self.entities = {"DEVICE": list(devices), "ASSET": list(assets)}
        self.relations = relations or []  # [(from_id, to_id, type)]
        self.fail_once = dict(fail_once or {})  # path_prefix -> status
        self.expire_token_after = expire_token_after
        self.token_uses = 0
        self.token_gen = 0
        self.requests = []
        self.now_ms = int(time.time() * 1000)

    def find(self, eid):
        for lst in self.entities.values():
            for e in lst:
                if e["id"] == eid:
                    return e
        return None

    def series(self, interval_s, start, end, limit):
        if interval_s is None:
            return []
        step = int(interval_s * 1000)
        pts = []
        t = self.now_ms
        while t >= start and len(pts) < limit:
            if t <= end:
                pts.append({"ts": t, "value": "21.5"})
            t -= step
        return pts


def make_handler(tb):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, headers=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self):
            tok = self.headers.get("X-Authorization", "")
            if tok != "Bearer tok-%d" % tb.token_gen:
                return False
            tb.token_uses += 1
            if tb.expire_token_after and tb.token_uses > tb.expire_token_after:
                tb.expire_token_after = None
                return False
            return True

        def _check_fail(self, path):
            for prefix, code in list(tb.fail_once.items()):
                if path.startswith(prefix):
                    del tb.fail_once[prefix]
                    self._send(code, {"message": "injected"}, {"Retry-After": "1"} if code == 429 else None)
                    return True
            return False

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            tb.requests.append(("POST", u.path))
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if u.path == "/api/auth/login":
                if body.get("password") != "pw":
                    return self._send(401, {"message": "bad credentials"})
                return self._send(200, {"token": "tok-%d" % tb.token_gen, "refreshToken": "ref"})
            if u.path == "/api/auth/token":
                tb.token_gen += 1
                return self._send(200, {"token": "tok-%d" % tb.token_gen, "refreshToken": "ref"})
            if not self._auth_ok():
                return self._send(401, {"message": "expired"})
            if self._check_fail(u.path):
                return
            if u.path == "/api/entitiesQuery/find":
                etype = body["entityFilter"]["entityType"]
                pl = body["pageLink"]
                items = tb.entities.get(etype, [])
                start = pl["page"] * pl["pageSize"]
                chunk = items[start:start + pl["pageSize"]]
                data = [{
                    "entityId": {"entityType": etype, "id": e["id"]},
                    "latest": {"ENTITY_FIELD": {
                        "name": {"ts": 0, "value": e["name"]},
                        "type": {"ts": 0, "value": e["profile"]},
                        "createdTime": {"ts": 0, "value": "0"},
                    }},
                } for e in chunk]
                return self._send(200, {"data": data, "hasNext": start + pl["pageSize"] < len(items),
                                        "totalElements": len(items)})
            return self._send(404, {"message": "not found"})

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            tb.requests.append(("GET", u.path))
            if not self._auth_ok():
                return self._send(401, {"message": "expired"})
            if self._check_fail(u.path):
                return
            parts = u.path.strip("/").split("/")
            if u.path == "/api/auth/user":
                return self._send(200, {"authority": "TENANT_ADMIN"})
            if u.path.startswith("/api/entityGroups/"):
                return self._send(200, [{"id": "g1"}, {"id": "g2"}])
            if u.path == "/api/relations/info":
                if "fromId" in q:
                    rels = [r for r in tb.relations if r[0] == q["fromId"]]
                else:
                    rels = [r for r in tb.relations if r[1] == q["toId"]]
                return self._send(200, [{"type": r[2]} for r in rels])
            if len(parts) >= 6 and parts[:3] == ["api", "plugins", "telemetry"]:
                ent = tb.find(parts[4])
                if ent is None:
                    return self._send(404, {"message": "no entity"})
                tail = "/".join(parts[5:])
                if tail == "keys/timeseries":
                    return self._send(200, list(ent["ts_keys"].keys()))
                if tail == "keys/attributes":
                    return self._send(200, ent.get("attr_keys", []))
                if tail == "values/timeseries":
                    assert q.get("agg") == "NONE"
                    out = {}
                    for k in q["keys"].split(","):
                        if k in ent["ts_keys"]:
                            pts = tb.series(ent["ts_keys"][k], int(q["startTs"]), int(q["endTs"]), int(q["limit"]))
                            if pts:
                                out[k] = pts
                    return self._send(200, out)
            return self._send(404, {"message": "not found"})

    return H


class MockTbServer:
    def __init__(self, tb):
        self.tb = tb
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tb))
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
