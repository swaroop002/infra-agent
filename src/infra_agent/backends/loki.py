"""Grafana Loki HTTP API adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from ._http import build_client, get_json, grafana_explore_url
from .base import BackendError, LogLine, LogsBackend, Series, VectorSample


def _ns(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1_000_000_000))


class LokiLogs(LogsBackend):
    def __init__(self, url: str, *, tenant_id: str | None = None, grafana_url: str | None = None,
                 grafana_datasource_uid: str | None = None, grafana_org_id: int = 1, name: str = "loki",
                 **client_opts):
        self.name = name
        headers = dict(client_opts.pop("headers", None) or {})
        if tenant_id:
            headers["X-Scope-OrgID"] = tenant_id
        self._client = build_client(url, headers=headers, **client_opts)
        self._grafana_url = grafana_url
        self._ds_uid = grafana_datasource_uid
        self._org = grafana_org_id

    async def query_metric(self, logql: str, at: datetime) -> list[VectorSample]:
        params = {"query": str(logql), "time": _ns(at)}
        data = _unwrap(await get_json(self._client, "/loki/api/v1/query", params, "Loki query"))
        if data.get("resultType") != "vector":
            raise BackendError(f"expected a metric (vector) LogQL query, got {data.get('resultType')}")
        return [VectorSample(r.get("metric", {}), float(r["value"][1]), float(r["value"][0]))
                for r in data.get("result", [])]

    async def query_range_metric(self, logql: str, start: datetime, end: datetime, step_s: int) -> list[Series]:
        params = {"query": str(logql), "start": _ns(start), "end": _ns(end), "step": f"{step_s}s"}
        data = _unwrap(await get_json(self._client, "/loki/api/v1/query_range", params, "Loki range query"))
        if data.get("resultType") != "matrix":
            raise BackendError("expected a metric LogQL query for a range evaluation")
        return [Series(r.get("metric", {}), [(float(t), float(v)) for t, v in r.get("values", [])])
                for r in data.get("result", [])]

    async def query_logs(self, logql: str, start: datetime, end: datetime, limit: int = 20,
                         direction: Literal["backward", "forward"] = "backward") -> list[LogLine]:
        params = {"query": str(logql), "start": _ns(start), "end": _ns(end), "limit": str(limit),
                  "direction": direction}
        data = _unwrap(await get_json(self._client, "/loki/api/v1/query_range", params, "Loki log query"))
        if data.get("resultType") != "streams":
            raise BackendError("expected a log (stream) LogQL query")
        lines = []
        for stream in data.get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                lines.append(LogLine(datetime.fromtimestamp(int(ts) / 1e9, UTC), labels, line))
        lines.sort(key=lambda entry: entry.ts, reverse=direction == "backward")
        return lines[:limit]

    async def label_names(self, start: datetime | None = None, end: datetime | None = None) -> list[str]:
        params = {"start": _ns(start), "end": _ns(end)} if start and end else None
        return _unwrap(await get_json(self._client, "/loki/api/v1/labels", params, "Loki labels")) or []

    async def label_values(self, label: str, start: datetime | None = None, end: datetime | None = None) -> list[str]:
        params = {"start": _ns(start), "end": _ns(end)} if start and end else None
        return _unwrap(await get_json(self._client, f"/loki/api/v1/label/{label}/values", params,
                                      "Loki label values")) or []

    def explore_url(self, query: str, start: datetime, end: datetime) -> str | None:
        return grafana_explore_url(self._grafana_url, self._ds_uid, "loki", str(query),
                                   int(start.timestamp() * 1000), int(end.timestamp() * 1000), self._org)

    async def aclose(self) -> None:
        await self._client.aclose()


def _unwrap(payload: dict):
    if payload.get("status") != "success":
        raise BackendError(f"Loki error: {payload.get('error') or payload}")
    return payload.get("data")
