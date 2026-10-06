"""Prometheus HTTP API adapter. Also works for Mimir, Thanos, Cortex and VictoriaMetrics."""

from __future__ import annotations

from datetime import datetime

from ._http import build_client, get_json, grafana_explore_url
from .base import BackendError, MetricsBackend, Series, Target, VectorSample


class PrometheusMetrics(MetricsBackend):
    def __init__(self, url: str, *, grafana_url: str | None = None, grafana_datasource_uid: str | None = None,
                 grafana_org_id: int = 1, name: str = "prometheus", **client_opts):
        self.name = name
        self._client = build_client(url, **client_opts)
        self._grafana_url = grafana_url
        self._ds_uid = grafana_datasource_uid
        self._org = grafana_org_id

    async def query(self, promql: str, at: datetime | None = None) -> list[VectorSample]:
        params = {"query": str(promql)}
        if at:
            params["time"] = f"{at.timestamp():.3f}"
        data = _unwrap(await get_json(self._client, "/api/v1/query", params, "Prometheus query"))
        rtype, result = data.get("resultType"), data.get("result", [])
        if rtype == "vector":
            return [VectorSample(r["metric"], _num(r["value"][1]), float(r["value"][0])) for r in result]
        if rtype == "scalar":
            return [VectorSample({}, _num(result[1]), float(result[0]))]
        if rtype == "matrix":  # range selector evaluated instantly
            return [VectorSample(r["metric"], _num(r["values"][-1][1]), float(r["values"][-1][0]))
                    for r in result if r.get("values")]
        return []

    async def query_range(self, promql: str, start: datetime, end: datetime, step_s: int) -> list[Series]:
        params = {"query": str(promql), "start": f"{start.timestamp():.3f}",
                  "end": f"{end.timestamp():.3f}", "step": str(step_s)}
        data = _unwrap(await get_json(self._client, "/api/v1/query_range", params, "Prometheus range query"))
        return [Series(r["metric"], [(float(t), _num(v)) for t, v in r.get("values", [])])
                for r in data.get("result", [])]

    async def targets(self) -> list[Target]:
        data = _unwrap(await get_json(self._client, "/api/v1/targets", {"state": "active"}, "Prometheus targets"))
        return [Target(labels=t.get("labels", {}), health=t.get("health", "unknown"),
                       last_error=t.get("lastError", ""), last_scrape=t.get("lastScrape"),
                       scrape_url=t.get("scrapeUrl", ""))
                for t in data.get("activeTargets", [])]

    async def label_values(self, label: str, match: str | None = None) -> list[str]:
        params = {"match[]": match} if match else None
        return _unwrap(await get_json(self._client, f"/api/v1/label/{label}/values", params, "Prometheus labels")) or []

    def explore_url(self, query: str, start: datetime, end: datetime) -> str | None:
        return grafana_explore_url(self._grafana_url, self._ds_uid, "prometheus", str(query),
                                   int(start.timestamp() * 1000), int(end.timestamp() * 1000), self._org)

    async def aclose(self) -> None:
        await self._client.aclose()


def _unwrap(payload: dict):
    if payload.get("status") != "success":
        raise BackendError(f"Prometheus error: {payload.get('errorType')}: {payload.get('error')}")
    return payload.get("data")


def _num(v: str) -> float:
    return float(v)  # handles "NaN", "+Inf"
