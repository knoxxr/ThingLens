"""TB 버전·에디션(CE/PE) 차이를 흡수해 정규화된 데이터를 돌려준다.

엔티티 목록은 다음 순서로 시도하고, 처음 성공한 방식을 기억한다.
  1. PE  : /api/deviceInfos/all, /api/assetInfos/all      (ownerId 포함)
  2. CE  : /api/tenant/deviceInfos, /api/tenant/assetInfos (customerId 포함)  ← TB CE 4.3.1에서 확인
  3. 공통: /api/entitiesQuery/find                        (소유자 정보 없음)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from .client import TbClient, TbError

NULL_UUID = "13814000-1dd2-11b2-8080-808080808080"
DAY_MS = 86_400_000


@dataclass
class EntityRec:
    id: str
    entity_type: str
    name: str | None
    label: str | None
    profile: str | None
    owner_id: str | None
    owner_name: str | None
    active: bool | None
    tb_version: int | None
    created_time: int | None


@dataclass
class TbAdapter:
    client: TbClient
    page_size: int = 100
    _list_mode: dict = field(default_factory=dict)  # entity_type -> "pe" | "ce" | "query"

    # -- 엔티티 ---------------------------------------------------------------
    def iter_entities(self, entity_type: str) -> Iterator[EntityRec]:
        modes = [self._list_mode[entity_type]] if entity_type in self._list_mode else ["pe", "ce", "query"]
        last_err = None
        for mode in modes:
            try:
                gen = self._iter_mode(entity_type, mode)
                first = next(gen, None)  # 첫 페이지로 방식 확인
            except TbError as e:
                if e.status in (400, 403, 404, 405):
                    last_err = e
                    continue
                raise
            self._list_mode[entity_type] = mode
            if first is not None:
                yield first
                yield from gen
            return
        raise last_err or TbError(None, "엔티티 목록 조회 방법을 찾지 못함")

    def _iter_mode(self, entity_type: str, mode: str) -> Iterator[EntityRec]:
        kind = "device" if entity_type == "DEVICE" else "asset"
        if mode == "query":
            yield from self._iter_query(entity_type)
            return
        path = f"/api/{kind}Infos/all" if mode == "pe" else f"/api/tenant/{kind}Infos"
        page = 0
        while True:
            data = self.client.get(path, params={"pageSize": self.page_size, "page": page,
                                                 "sortProperty": "createdTime", "sortOrder": "ASC"},
                                   category="entity_list")
            for r in data.get("data", []):
                yield self._from_info(entity_type, r)
            if not data.get("hasNext"):
                return
            page += 1

    def _from_info(self, entity_type: str, r: dict) -> EntityRec:
        owner = (r.get("ownerId") or r.get("customerId") or {}).get("id")
        owner_type = (r.get("ownerId") or r.get("customerId") or {}).get("entityType")
        if owner == NULL_UUID or owner_type == "TENANT":
            owner = None
        owner_name = r.get("ownerName") or r.get("customerTitle") if owner else None
        profile = r.get("deviceProfileName") or r.get("assetProfileName") or r.get("type")
        return EntityRec(
            id=r["id"]["id"], entity_type=entity_type, name=r.get("name"), label=r.get("label"),
            profile=profile, owner_id=owner, owner_name=owner_name,
            active=r.get("active"), tb_version=r.get("version"), created_time=r.get("createdTime"))

    def _iter_query(self, entity_type: str) -> Iterator[EntityRec]:
        page = 0
        while True:
            body = {
                "entityFilter": {"type": "entityType", "entityType": entity_type},
                "entityFields": [{"type": "ENTITY_FIELD", "key": k} for k in ("name", "label", "type", "createdTime")],
                "latestValues": [],
                "pageLink": {"page": page, "pageSize": self.page_size,
                             "sortOrder": {"key": {"type": "ENTITY_FIELD", "key": "createdTime"}, "direction": "ASC"}},
            }
            data = self.client.query("/api/entitiesQuery/find", body, category="entity_list")
            for row in data.get("data", []):
                f = (row.get("latest") or {}).get("ENTITY_FIELD", {})
                val = lambda k: (f.get(k) or {}).get("value")  # noqa: E731
                ct = val("createdTime")
                yield EntityRec(id=row["entityId"]["id"], entity_type=entity_type, name=val("name"),
                                label=val("label") or None, profile=val("type"), owner_id=None, owner_name=None,
                                active=None, tb_version=None, created_time=int(ct) if ct else None)
            if not data.get("hasNext"):
                return
            page += 1

    # -- 고객 -----------------------------------------------------------------
    def iter_customers(self) -> Iterator[dict]:
        page = 0
        while True:
            try:
                data = self.client.get("/api/customers", params={"pageSize": self.page_size, "page": page},
                                       category="entity_list")
            except TbError as e:
                if e.status in (403, 404):
                    return  # 고객 사용자 계정 등 권한 없음
                raise
            for c in data.get("data", []):
                parent = (c.get("parentCustomerId") or c.get("ownerId") or {})
                pid = parent.get("id") if parent.get("entityType") == "CUSTOMER" else None
                yield {"id": c["id"]["id"], "title": c.get("title"), "parent_id": pid}
            if not data.get("hasNext"):
                return
            page += 1

    # -- 엔티티 상세 -----------------------------------------------------------
    def relations_from(self, entity_type: str, entity_id: str) -> list[dict]:
        rows = self.client.get("/api/relations/info", params={"fromId": entity_id, "fromType": entity_type},
                               category="relations") or []
        return [{"from_id": entity_id, "from_type": entity_type,
                 "to_id": r["to"]["id"], "to_type": r["to"]["entityType"],
                 "rel_type": r.get("type"), "type_group": r.get("typeGroup") or "COMMON"} for r in rows]

    def attributes(self, entity_type: str, entity_id: str, scope: str) -> list[dict]:
        rows = self.client.get(f"/api/plugins/telemetry/{entity_type}/{entity_id}/values/attributes/{scope}",
                               category="attributes") or []
        return [{"key": r["key"], "value": r.get("value"), "last_update_ts": r.get("lastUpdateTs")} for r in rows]

    def ts_keys(self, entity_type: str, entity_id: str) -> list[str]:
        return self.client.get(f"/api/plugins/telemetry/{entity_type}/{entity_id}/keys/timeseries",
                               category="keys") or []

    def ts_agg(self, entity_type: str, entity_id: str, keys: list[str], start_ts: int, end_ts: int,
               agg: str) -> dict[str, list[dict]]:
        """**구간 1개짜리** 집계만 허용 (interval = 전체 구간). 구간이 여러 개면 구간마다 Cassandra 조회가
        생기므로(Phase 0 실측) 이 함수로는 요청할 수 없다."""
        if agg not in ("MIN", "MAX", "COUNT", "AVG", "SUM"):
            raise ValueError(agg)
        return self.client.get(
            f"/api/plugins/telemetry/{entity_type}/{entity_id}/values/timeseries",
            params={"keys": ",".join(keys), "startTs": start_ts, "endTs": end_ts, "interval": end_ts - start_ts,
                    "agg": agg, "limit": 10},
            category="envelope") or {}

    def ts_raw(self, entity_type: str, entity_id: str, keys: list[str], start_ts: int, end_ts: int,
               limit: int, order: str = "DESC") -> dict[str, list[dict]]:
        """원시값 조회 (agg=NONE). Cassandra 실측 결과 서버 집계는 사용하지 않는다 (docs/phase0)."""
        return self.client.get(
            f"/api/plugins/telemetry/{entity_type}/{entity_id}/values/timeseries",
            params={"keys": ",".join(keys), "startTs": start_ts, "endTs": end_ts, "limit": limit,
                    "agg": "NONE", "orderBy": order},
            category="samples") or {}
