"""LangChain tools over the read-only operations.

Each tool returns (content_for_model, evidence_artifact). The model sees compact JSON; the
interfaces render the evidence (queries, time ranges, Grafana links) separately.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..backends.base import BackendError
from ..context import AgentContext
from ..knowledge.schema import K8sWorkload
from ..ops import alerts as alert_ops
from ..ops import hosts as host_ops
from ..ops import k8s as k8s_ops
from ..ops import services as service_ops
from ..ops import uptime as uptime_ops
from ..ops import vibe as vibe_ops
from ..ops.common import Evidence, NotResolved, OpResult, Recorder
from ..timeutil import Window, pick_step, resolve_window

MAX_TOOL_CHARS = 14000

TOOL_LABELS = {
    "find_entities": "Looking up the catalog",
    "get_host_status": "Checking VM status",
    "get_host_downtime": "Reading VM downtime history",
    "list_down_hosts": "Scanning the fleet for down targets",
    "get_api_error_rate": "Computing API error rate from Loki",
    "check_service_health": "Checking service health",
    "get_uptime_report": "Building uptime report",
    "get_recent_alerts": "Fetching recent alerts",
    "get_kubernetes_status": "Checking Kubernetes workloads",
    "list_vibe_vms": "Listing vibe-coding VMs",
    "get_vibe_vm_status": "Checking vibe-coding VM",
    "search_knowledge_base": "Searching runbooks and docs",
    "query_prometheus": "Running PromQL",
    "query_loki": "Running LogQL",
}

_WINDOW_DESC = "Lookback such as 30m, 6h, 24h, 7d, 30d. Ignored when start is given."
_TIME_DESC = "Optional ISO-8601 timestamp (UTC if no offset)."


def _dump(payload: Any) -> str:
    text = json.dumps(payload, default=str, separators=(",", ":"), ensure_ascii=False)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + '…"[truncated: narrow the query or window]"'
    return text


def _out(result: OpResult) -> tuple[str, list[dict]]:
    return _dump(result.payload()), [e.to_dict() for e in result.evidence]


def _err(message: str, **extra: Any) -> tuple[str, list[dict]]:
    return _dump({"error": message, **extra}), []


class _Host(BaseModel):
    host: str = Field(description="Host id, name or alias from the catalog (e.g. 'dev-app-01' or 'development server').")


class _HostWindow(_Host):
    window: str = Field("24h", description=_WINDOW_DESC)
    start: str | None = Field(None, description=_TIME_DESC)
    end: str | None = Field(None, description=_TIME_DESC)


class _Service(BaseModel):
    service: str = Field(description="Service id, name or alias from the catalog (e.g. 'payments-api', 'report generation').")


class _ServiceWindow(_Service):
    window: str = Field("24h", description=_WINDOW_DESC)
    start: str | None = Field(None, description=_TIME_DESC)
    end: str | None = Field(None, description=_TIME_DESC)


class _Health(_Service):
    window: str = Field("1h", description="Window for error rate and alerts, e.g. 15m, 1h, 24h.")


class _Find(BaseModel):
    query: str = Field(description="Name, alias, keyword or partial name.")
    kind: Literal["any", "host", "service", "cluster"] = "any"


class _Uptime(BaseModel):
    kind: Literal["service", "host", "check", "vibe", "vibe_app", "target"] = Field(
        "service", description="Entity type to report on. 'check' = individual health checks.")
    entity: str | None = Field(None, description="Optional single entity id/name; omit for all entities of that kind.")
    window: str = Field("7d", description=_WINDOW_DESC)
    start: str | None = Field(None, description=_TIME_DESC)
    end: str | None = Field(None, description=_TIME_DESC)


class _Alerts(BaseModel):
    window: str = Field("24h", description=_WINDOW_DESC)
    state: str | None = Field(None, description="Comma-separated states: firing,acknowledged,resolved,silenced,pending.")
    search: str | None = Field(None, description="Free-text filter on title, summary and labels.")
    entity: str | None = Field(None, description="Host or service (id/name/alias) to relate alerts to.")


class _K8s(BaseModel):
    cluster: str = Field(description="Cluster id, name or alias from the catalog.")
    namespace: str | None = Field(None, description="Optional namespace filter.")
    workload: str | None = Field(None, description="Optional deployment/statefulset name (requires namespace if not in catalog).")


class _VibeList(BaseModel):
    owner: str | None = Field(None, description="Owner (user name/email fragment) to filter by.")
    state: Literal["running", "stopped", "starting", "stopping"] | None = None


class _VibeVM(BaseModel):
    vm: str = Field(description="Instance id, Name tag, or owner of the vibe-coding VM.")


class _KB(BaseModel):
    query: str = Field(description="What to look for: symptom, service, error message, procedure.")
    source: str | None = Field(None, description="Pass a source path from a previous search result to read the whole document.")


class _PromQL(BaseModel):
    promql: str = Field(description="PromQL expression. Read-only.")
    window: str | None = Field(None, description="If set, evaluate as a range query over this lookback; else instant.")


class _LogQL(BaseModel):
    logql: str = Field(description="LogQL. Stream selector queries return lines; metric queries return values.")
    window: str = Field("1h", description="Lookback window (logs capped at 24h).")
    limit: int = Field(30, ge=1, le=200)


def build_tools(ctx: AgentContext) -> list[BaseTool]:
    cat = ctx.catalog

    def resolve(kind: str, name: str):
        entity, candidates = cat.resolve_one(name, kind)
        if entity is None:
            raise NotResolved(kind, name, [c.to_dict() for c in candidates])
        return entity

    def window_of(window: str | None, start: str | None, end: str | None, default: str) -> Window:
        return resolve_window(window, start, end, default=default, now=ctx.now())

    async def guarded(fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except NotResolved as exc:
            return _dump(exc.payload()), []
        except (ValueError, BackendError) as exc:
            return _err(str(exc))

    # ---------------------------------------------------------------- catalog
    async def find_entities(query: str, kind: str = "any"):
        kinds = None if kind == "any" else [kind]
        matches = cat.resolve(query, kinds=kinds, limit=8, threshold=0.55)
        items = []
        for m in matches:
            e = cat.get(m.kind, m.id)
            item = {**m.to_dict(), "environment": e.environment, "aliases": e.aliases,
                    "description": e.description[:200] or None}
            if m.kind == "service":
                item.update(health_checks=len(e.health_checks), has_logs_signal=e.logs is not None,
                            kubernetes=[f"{w.cluster}/{w.namespace}/{w.name}" for w in e.kubernetes], hosts=e.hosts)
            if m.kind == "host":
                item.update(cloud=e.cloud, selector=e.prometheus)
            items.append(item)
        payload: dict[str, Any] = {"matches": items}
        if not items:
            payload["note"] = "Nothing in the catalog matches. It may be an un-catalogued Prometheus target; " \
                              "try list_down_hosts or query_prometheus with up{instance=~\".*<name>.*\"}."
        if cat.vibe and re.search(r"vibe|vs ?code|code-server|workspace|dev ?box", query, re.I):
            payload["vibe_platform"] = {"apps": [a.name for a in cat.vibe.apps], "tag_filter": cat.vibe.tag_filter}
        return _dump(payload), [Evidence("catalog", "catalog", note=f"resolve {query!r}").to_dict()]

    # ------------------------------------------------------------------ hosts
    async def get_host_status(host: str):
        return _out(await host_ops.host_status(ctx, resolve("host", host)))

    async def get_host_downtime(host: str, window: str = "24h", start: str | None = None, end: str | None = None):
        return _out(await host_ops.host_downtime(ctx, resolve("host", host), window_of(window, start, end, "24h")))

    async def list_down_hosts():
        return _out(await host_ops.fleet_overview(ctx))

    # --------------------------------------------------------------- services
    async def get_api_error_rate(service: str, window: str = "24h", start: str | None = None, end: str | None = None):
        return _out(await service_ops.api_errors(ctx, resolve("service", service), window_of(window, start, end, "24h")))

    async def check_service_health(service: str, window: str = "1h"):
        return _out(await service_ops.service_health(ctx, resolve("service", service), window_of(window, None, None, "1h")))

    async def get_uptime_report(kind: str = "service", entity: str | None = None, window: str = "7d",
                                start: str | None = None, end: str | None = None):
        entity_id = None
        if entity:
            entity_id = resolve(kind, entity).id if kind in ("service", "host") else entity
        return _out(await uptime_ops.uptime_report(ctx, window_of(window, start, end, "7d"), kind=kind,
                                                   entity_id=entity_id))

    async def get_recent_alerts(window: str = "24h", state: str | None = None, search: str | None = None,
                                entity: str | None = None):
        ent = None
        if entity:
            ent, _ = cat.resolve_one(entity, "service")
            if ent is None:
                ent, _ = cat.resolve_one(entity, "host")
            if ent is None:
                search = search or entity
        return _out(await alert_ops.recent_alerts(ctx, window_of(window, None, None, "24h"), state=state,
                                                  text=search, entity=ent))

    # ------------------------------------------------------------- kubernetes
    async def get_kubernetes_status(cluster: str, namespace: str | None = None, workload: str | None = None):
        cl = resolve("cluster", cluster)
        if not workload:
            return _out(await k8s_ops.cluster_status(ctx, cl, namespace))
        rec = Recorder()
        known = [w for s in cat.services.values() for w in s.kubernetes
                 if w.cluster == cl.id and w.name == workload and (not namespace or w.namespace == namespace)]
        if known:
            target = known[0]
        elif namespace:
            target = K8sWorkload(cluster=cl.id, namespace=namespace, name=workload)
        else:
            return _err("Workload is not in the catalog; pass its namespace too.")
        data = await k8s_ops.workload_status(ctx, target, rec)
        return _out(rec.result(data))

    # ------------------------------------------------------------------- vibe
    async def list_vibe_vms(owner: str | None = None, state: str | None = None):
        return _out(await vibe_ops.list_vibe_vms(ctx, owner=owner, state=state))

    async def get_vibe_vm_status(vm: str):
        return _out(await vibe_ops.vibe_vm_status(ctx, vm))

    # ------------------------------------------------------------------- docs
    async def search_knowledge_base(query: str, source: str | None = None):
        if source:
            doc = ctx.docs.get_document(source)
            if doc is None:
                return _err(f"No document {source!r}", available=ctx.docs.sources[:50])
            return _dump({"source": source, "content_untrusted": doc[:MAX_TOOL_CHARS - 500]}), \
                [Evidence("docs", "knowledge-base", note=source).to_dict()]
        hits = ctx.docs.search(query, limit=4)
        return _dump({"results": [h.to_dict() for h in hits]} if hits else {"results": [], "note": "no matching docs"}), \
            [Evidence("docs", "knowledge-base", note=h.source).to_dict() for h in hits]

    # ---------------------------------------------------------- escape hatches
    async def query_prometheus(promql: str, window: str | None = None):
        m = ctx.backends.metrics
        if m is None:
            return _err("no metrics backend configured")
        now = ctx.now()
        if window:
            w = resolve_window(window, now=now, max_duration=ctx.metrics_retention)
            step = pick_step(w.duration, max_points=300)
            series = await m.query_range(promql, w.start, w.end, step)
            out = []
            for s in series[:30]:
                vals = [v for _, v in s.samples]
                out.append({"labels": s.labels, "points": len(vals),
                            "min": min(vals, default=None), "max": max(vals, default=None),
                            "avg": round(sum(vals) / len(vals), 4) if vals else None,
                            "last": vals[-1] if vals else None,
                            "sampled": [[int(t), round(v, 4)] for t, v in s.samples[:: max(1, len(s.samples) // 20)]]})
            payload = {"window": w.to_dict(), "step_s": step, "series_count": len(series), "series": out}
            ev = Evidence("promql", m.name, promql, w.to_dict()["start"], w.to_dict()["end"], m.explore_url(promql, w.start, w.end))
        else:
            samples = await m.query(promql, now)
            payload = {"at": now.isoformat(timespec="seconds"), "result_count": len(samples),
                       "result": [{"labels": s.labels, "value": s.value} for s in samples[:100]]}
            ev = Evidence("promql", m.name, promql, url=m.explore_url(promql, now - timedelta(hours=1), now))
        return _dump(payload), [ev.to_dict()]

    async def query_loki(logql: str, window: str = "1h", limit: int = 30):
        lg = ctx.backends.logs
        if lg is None:
            return _err("no logs backend configured")
        now = ctx.now()
        if logql.strip().startswith("{"):
            w = resolve_window(window, now=now, max_duration=timedelta(hours=24))
            lines = await lg.query_logs(logql, w.start, w.end, limit=limit)
            payload = {"window": w.to_dict(), "lines_untrusted": [
                {"ts": ln.ts.isoformat(timespec="seconds"), "labels": ln.labels, "line": ln.line[:500]} for ln in lines]}
        else:
            w = resolve_window(window, now=now, max_duration=timedelta(days=7))
            samples = await lg.query_metric(logql, w.end)
            payload = {"at": w.end.isoformat(timespec="seconds"),
                       "result": [{"labels": s.labels, "value": s.value} for s in samples[:100]]}
        ev = Evidence("logql", lg.name, logql, w.to_dict()["start"], w.to_dict()["end"], lg.explore_url(logql, w.start, w.end))
        return _dump(payload), [ev.to_dict()]

    specs = [
        (find_entities, _Find,
         "Search the infrastructure catalog for hosts, services and clusters by name, alias or keyword. "
         "Use when a name in the question is ambiguous or was not resolved in the context."),
        (get_host_status, _Host,
         "Current status of a VM: Prometheus liveness, cloud power state, CPU/memory/disk, down-since, related "
         "alerts and a verdict (UP, DEGRADED, UNREACHABLE, DOWN, GONE, NOT_MONITORED, UNKNOWN). "
         "Use for 'is <server> down/up/ok?'."),
        (get_host_downtime, _HostWindow,
         "Downtime history of a VM over a window: outage intervals, availability %, reboots. Uses Prometheus "
         "for recent data and the ledger for older history."),
        (list_down_hosts, None,
         "Fleet overview: every Prometheus target currently down (catalogued or not) plus open outages. "
         "Use for 'which servers are down?' or when a host is not in the catalog."),
        (get_api_error_rate, _ServiceWindow,
         "Request count, error count and error rate % for a service's API from Loki logs over a window, with "
         "previous-window comparison, top failing paths and sample error lines."),
        (check_service_health, _Health,
         "Composite health of a service: live health checks, Kubernetes workloads, hosts, error rate, related "
         "alerts, uptime history, dependencies and runbooks, with a verdict. Use for 'any issues with <service>?'."),
        (get_uptime_report, _Uptime,
         "Uptime % and outage counts from the downtime ledger for services, hosts, health checks or vibe VMs over "
         "any window (including longer than Prometheus retention)."),
        (get_recent_alerts, _Alerts,
         "Recent and active alerts/incidents from Grafana alerting and Grafana OnCall, optionally filtered by "
         "state, text or related entity."),
        (get_kubernetes_status, _K8s,
         "Kubernetes health from kube-state-metrics: for a cluster/namespace (not-ready nodes, unavailable "
         "deployments, crashlooping pods) or a single workload (replicas, restarts, waiting reasons, OOMKills)."),
        (list_vibe_vms, _VibeList,
         "List user vibe-coding VMs (instance, owner, state, uptime), optionally by owner or state."),
        (get_vibe_vm_status, _VibeVM,
         "Status of a vibe-coding VM by instance id, name or owner: cloud state and status checks, health of the "
         "preinstalled apps (VS Code, deployments manager, ...), recent outages."),
        (search_knowledge_base, _KB,
         "Search runbooks, architecture notes and postmortems. Pass `source` to read a whole document."),
        (query_prometheus, _PromQL,
         "Run an arbitrary read-only PromQL query (instant, or range when window is set). Only use when no "
         "dedicated tool covers the question."),
        (query_loki, _LogQL,
         "Run an arbitrary read-only LogQL query. Only use when no dedicated tool covers the question."),
    ]

    tools: list[BaseTool] = []
    for fn, schema, description in specs:
        tools.append(StructuredTool.from_function(
            coroutine=_bind(guarded, fn), name=fn.__name__, description=description,
            args_schema=schema or _NoArgs, response_format="content_and_artifact"))
    return tools


class _NoArgs(BaseModel):
    pass


def _bind(guarded, fn):
    async def call(**kwargs):
        return await guarded(fn, **kwargs)

    call.__name__ = fn.__name__
    return call
