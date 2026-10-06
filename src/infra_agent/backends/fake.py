"""Scenario-driven fake backends for demo mode, tests and model evals.

A scenario YAML describes the world (which hosts are down, error rates, alerts, vibe VMs).
Fakes answer the queries built in `infra_agent.queries` by dispatching on `Query.name`;
anything else (e.g. raw PromQL from the escape-hatch tool) returns an empty result.
Times in the scenario are relative to "now", so a demo always looks live.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from ..knowledge.catalog import Catalog
from ..knowledge.schema import CloudRef, HealthCheck
from ..timeutil import parse_duration, utcnow
from .base import (
    Alert,
    AlertsBackend,
    InstanceState,
    InventoryBackend,
    LogLine,
    LogsBackend,
    MetricsBackend,
    Prober,
    ProbeResult,
    Series,
    VectorSample,
)

_WORLDS: dict[str, FakeWorld] = {}


@dataclass
class BuildContext:
    base_dir: Path
    catalog: Catalog | None


def _ago(text: str | None, now: datetime) -> datetime | None:
    return now - parse_duration(text) if text else None


class FakeWorld:
    def __init__(self, scenario: dict[str, Any], catalog: Catalog | None):
        self.s = scenario
        self.catalog = catalog or Catalog()
        self.now_override: datetime | None = None

    @classmethod
    def get(cls, scenario: str | dict | None, context: BuildContext | None) -> FakeWorld:
        if isinstance(scenario, dict):
            return cls(scenario, context.catalog if context else None)
        path = Path(scenario or "")
        if context and not path.is_absolute():
            path = context.base_dir / path
        key = str(path.resolve())
        if key not in _WORLDS:
            data = yaml.safe_load(path.read_text()) if path.is_file() else {}
            _WORLDS[key] = cls(data or {}, context.catalog if context else None)
        elif context and context.catalog is not None:
            _WORLDS[key].catalog = context.catalog
        return _WORLDS[key]

    def now(self) -> datetime:
        return self.now_override or utcnow()

    def host(self, host_id: str) -> dict[str, Any]:
        return (self.s.get("hosts") or {}).get(host_id, {})

    def service(self, service_id: str) -> dict[str, Any]:
        return (self.s.get("services") or {}).get(service_id, {})

    def host_down_intervals(self, host_id: str) -> list[tuple[datetime, datetime | None]]:
        now = self.now()
        h = self.host(host_id)
        intervals = []
        for spec in h.get("history", []):
            start_s, end_s = spec.split("..")
            intervals.append((now - parse_duration(start_s), now - parse_duration(end_s)))
        if int(h.get("up", 1)) == 0:
            intervals.append((now - parse_duration(h.get("down_for", "10m")), None))
        return intervals

    def host_up_at(self, host_id: str, at: datetime) -> float:
        for start, end in self.host_down_intervals(host_id):
            if start <= at and (end is None or at < end):
                return 0.0
        return 1.0


def _world(scenario, context) -> FakeWorld:
    return FakeWorld.get(scenario, context)


class FakeMetrics(MetricsBackend):
    def __init__(self, scenario: str | dict | None = None, context: BuildContext | None = None, name: str = "fake-prometheus"):
        self.name = name
        self.w = _world(scenario, context)

    async def query(self, promql: str, at: datetime | None = None) -> list[VectorSample]:
        at = at or self.w.now()
        name = getattr(promql, "name", "")
        p = getattr(promql, "params", {})
        ts = at.timestamp()
        cat = self.w.catalog
        if name == "host_up" and p["host_id"] in cat.hosts:
            host = cat.hosts[p["host_id"]]
            return [VectorSample({**host.prometheus, "job": host.prometheus.get("job", "node")},
                                 self.w.host_up_at(host.id, at), ts)]
        if name == "fleet_up":
            out = [VectorSample({"job": "node", **h.prometheus}, self.w.host_up_at(h.id, at), ts)
                   for h in cat.hosts.values() if not self.w.host(h.id).get("absent")]
            for extra in self.w.s.get("targets_extra", []):
                out.append(VectorSample(extra["labels"], float(extra.get("up", 1)), ts))
            return out
        if name == "host_availability" and p["host_id"] in cat.hosts:
            import re

            m = re.search(r"\[(\d+[smhdw])\]", str(promql))
            span = parse_duration(m.group(1)) if m else timedelta(hours=24)
            start = at - span
            down = 0.0
            for s, e in self.w.host_down_intervals(p["host_id"]):
                lo, hi = max(s, start), min(e or at, at)
                down += max(0.0, (hi - lo).total_seconds())
            return [VectorSample({}, 1 - down / span.total_seconds(), ts)]
        if name == "host_reboots":
            return [VectorSample({}, float(self.w.host(p["host_id"]).get("reboots", 0)), ts)]
        if name.startswith("host_") and p.get("host_id") in cat.hosts:
            if self.w.host_up_at(p["host_id"], at) == 0:
                return []  # no fresh samples from a down exporter
            res = {"cpu_busy_pct": 18.0, "mem_available_pct": 42.0, "disk_used_pct": {"/": 55.0},
                   "load1_per_core": 0.4, "uptime_s": 86400 * 12}
            res.update(self.w.host(p["host_id"]).get("resources", {}))
            key = name.removeprefix("host_").replace("load_per_core", "load1_per_core")
            val = res.get(key)
            if isinstance(val, dict):
                return [VectorSample({"mountpoint": m}, float(v), ts) for m, v in val.items()]
            return [VectorSample({}, float(val), ts)] if val is not None else []
        if name.startswith("k8s_"):
            return self._k8s(name, p, ts)
        return []

    def _k8s(self, name: str, p: dict, ts: float) -> list[VectorSample]:
        k8s = self.w.s.get("k8s") or {}
        if "workload" in p:
            wl = (k8s.get("workloads") or {}).get(f"{p['cluster_id']}/{p['namespace']}/{p['workload']}")
            if wl is None:
                wl = {"desired": 2, "available": 2}
            if name == "k8s_desired":
                return [VectorSample({}, float(wl.get("desired", 2)), ts)]
            if name == "k8s_available":
                return [VectorSample({}, float(wl.get("available", wl.get("desired", 2))), ts)]
            if name == "k8s_restarts":
                return [VectorSample({"pod": pod}, float(n), ts) for pod, n in (wl.get("restarts") or {}).items()]
            if name == "k8s_waiting":
                return [VectorSample({"pod": pod, "reason": reason}, 1.0, ts)
                        for pod, reason in (wl.get("waiting") or {}).items()]
            if name == "k8s_oom":
                return [VectorSample({"pod": pod}, 1.0, ts) for pod in wl.get("oom_killed", [])]
            return []
        cluster = (k8s.get("clusters") or {}).get(p.get("cluster_id"), {})
        if name == "k8s_nodes_not_ready":
            return [VectorSample({"node": n, "label_agentpool": pool}, 0.0, ts)
                    for n, pool in (cluster.get("nodes_not_ready") or {}).items()]
        if name == "k8s_crashloop_pods":
            return [VectorSample({"namespace": ns, "pod": pod}, 1.0, ts)
                    for ns_pod in cluster.get("crashlooping", []) for ns, pod in [ns_pod.split("/", 1)]
                    if not p.get("namespace") or ns == p["namespace"]]
        return []

    async def query_range(self, promql: str, start: datetime, end: datetime, step_s: int) -> list[Series]:
        name = getattr(promql, "name", "")
        p = getattr(promql, "params", {})
        if name == "host_up_range" and p["host_id"] in self.w.catalog.hosts:
            host = self.w.catalog.hosts[p["host_id"]]
            samples = []
            t = start
            while t <= end:
                samples.append((t.timestamp(), self.w.host_up_at(host.id, t)))
                t += timedelta(seconds=step_s)
            return [Series(host.prometheus, samples)]
        return []

    def explore_url(self, query: str, start: datetime, end: datetime) -> str | None:
        return None


class FakeLogs(LogsBackend):
    def __init__(self, scenario: str | dict | None = None, context: BuildContext | None = None, name: str = "fake-loki"):
        self.name = name
        self.w = _world(scenario, context)

    def _svc(self, logql) -> tuple[dict, str]:
        p = getattr(logql, "params", {})
        return self.w.service(p.get("service_id", "")), getattr(logql, "name", "")

    async def query_metric(self, logql: str, at: datetime) -> list[VectorSample]:
        svc, name = self._svc(logql)
        if not svc:
            return []
        range_s = _range_seconds(str(logql))
        previous = at < self.w.now() - timedelta(seconds=range_s / 2)
        hours = range_s / 3600
        total = float(svc.get("requests_per_hour", 1000)) * hours
        pct = float(svc.get("previous_error_pct" if previous else "error_pct", 0.0))
        errors = round(total * pct / 100)
        ts = at.timestamp()
        if name == "log_total":
            return [VectorSample({}, round(total), ts)]
        if name == "log_errors":
            return [VectorSample({}, errors, ts)] if errors else []
        if name == "log_errors_by_path":
            catalog_svc = self.w.catalog.services.get(getattr(logql, "params", {}).get("service_id", ""))
            label = (catalog_svc.logs.path_label if catalog_svc and catalog_svc.logs else None) or "path"
            paths = svc.get("error_paths") or {}
            weight = sum(paths.values()) or 1
            return [VectorSample({label: path}, round(errors * n / weight), ts) for path, n in paths.items()]
        return []

    async def query_logs(self, logql: str, start: datetime, end: datetime, limit: int = 20,
                         direction: Literal["backward", "forward"] = "backward") -> list[LogLine]:
        svc, _ = self._svc(logql)
        now = self.w.now()
        lines = svc.get("error_lines", []) if svc else []
        return [LogLine(now - timedelta(minutes=3 * i), {"service": "fake"}, line) for i, line in enumerate(lines[:limit])]


def _range_seconds(logql: str) -> float:
    import re

    m = re.findall(r"\[(\d+[smhdw])\]", logql)
    return parse_duration(m[-1]).total_seconds() if m else 3600.0


class FakeAlerts(AlertsBackend):
    def __init__(self, scenario: str | dict | None = None, context: BuildContext | None = None, name: str = "fake-alerts"):
        self.name = name
        self.w = _world(scenario, context)

    async def recent_alerts(self, since: datetime, until: datetime | None = None) -> list[Alert]:
        now = self.w.now()
        out = []
        for i, a in enumerate(self.w.s.get("alerts", [])):
            started = _ago(a.get("started_ago"), now)
            ended = _ago(a.get("ended_ago"), now)
            if ended and ended < since:
                continue
            out.append(Alert(a.get("source", "grafana"), str(a.get("id", i)), a["title"],
                             a.get("state", "firing"), a.get("severity"), started, ended,
                             a.get("labels", {}), a.get("summary", ""), a.get("url")))
        return out


class FakeInventory(InventoryBackend):
    def __init__(self, scenario: str | dict | None = None, context: BuildContext | None = None,
                 provider: str = "aws", name: str = "fake-inventory"):
        self.name = name
        self.provider = provider
        self.w = _world(scenario, context)

    async def get_instance(self, ref: CloudRef) -> InstanceState:
        ident = ref.instance_id or ref.vm_name or ref.instance_name
        for host_id, h in (self.w.s.get("hosts") or {}).items():
            host = self.w.catalog.hosts.get(host_id)
            if host and host.cloud_ref and host.cloud_ref == ref:
                state = h.get("cloud_state", "running")
                return InstanceState(ref.provider, ident, host_id, state, state, ref.region or ref.zone)
        return InstanceState(ref.provider, ident, None, "running", "running", ref.region)

    async def list_instances(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        now = self.w.now()
        out = []
        for vm in self.w.s.get("vibe_vms", []):
            tags = {"Name": vm["name"], "Owner": vm.get("owner", ""), **tag_filter, **vm.get("tags", {})}
            out.append(InstanceState(self.provider, vm["id"], vm["name"], vm.get("state", "running"),
                                     vm.get("state", "running"), vm.get("region", "ap-south-1"),
                                     private_ip=vm.get("private_ip", _fake_ip(vm["id"])),
                                     launched_at=_ago(vm.get("launched_ago", "2h"), now),
                                     instance_type=vm.get("instance_type", "t3.large"), tags=tags,
                                     status_checks=vm.get("status_checks", {"system": "ok", "instance": "ok"})))
        return out


class FakeProber(Prober):
    """Resolves probe URLs against scenario services (by check URL) and vibe VM apps (by IP:port)."""

    def __init__(self, scenario: str | dict | None = None, context: BuildContext | None = None, name: str = "fake-prober"):
        self.name = name
        self.w = _world(scenario, context)

    async def probe(self, check: HealthCheck) -> ProbeResult:
        now = self.w.now()
        spec: dict[str, Any] | None = None
        for svc in self.w.catalog.services.values():
            for hc in svc.health_checks:
                if hc.url == check.url:
                    spec = (self.w.service(svc.id).get("probes") or {}).get(hc.name, {})
        if spec is None:
            for vm in self.w.s.get("vibe_vms", []):
                ip = vm.get("private_ip", _fake_ip(vm["id"]))
                if f"//{ip}:" in check.url and self.w.catalog.vibe:
                    for app in self.w.catalog.vibe.apps:
                        if f":{app.port}" in check.url:
                            spec = (vm.get("apps") or {}).get(app.name, {})
        spec = spec or {}
        ok = bool(spec.get("ok", True))
        rng = random.Random(check.url)
        latency = float(spec.get("latency_ms", rng.uniform(20, 120))) if ok else None
        status = spec.get("status", 200 if ok else None)
        return ProbeResult(ok, status, latency, None if ok else spec.get("error", "connection refused"),
                           now, check.url)


def _fake_ip(seed: str) -> str:
    h = hashlib.sha1(seed.encode()).digest()
    return f"10.20.{h[0]}.{h[1] or 1}"
