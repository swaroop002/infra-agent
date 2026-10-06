"""Service-level investigations: API error rates from Loki and composite health."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from .. import queries
from ..context import AgentContext
from ..knowledge.schema import Service
from ..timeutil import Window, human_duration, prom_duration
from .alerts import alert_matches, fetch_alerts, in_window
from .common import OpResult, Recorder, iso
from .hosts import host_verdict
from .k8s import workload_status

DEFAULT_ERROR_RATE_THRESHOLD_PCT = 5.0
MAX_LINE_CHARS = 400


async def api_errors(ctx: AgentContext, svc: Service, window: Window, *, compare: bool = True,
                     sample_lines: int = 5, rec: Recorder | None = None) -> OpResult:
    own = rec is None
    rec = rec or Recorder()
    data: dict = {"service": svc.id, "window": window.to_dict()}
    logs = ctx.backends.logs
    if svc.logs is None:
        data["error"] = (f"Service {svc.id} has no `logs` signal in the catalog, so request/error rates "
                         "cannot be computed. Add one (see docs/knowledge-base.md).")
        return rec.result(data)
    if logs is None:
        rec.errors.append("no logs backend configured")
        return rec.result(data)

    rng = prom_duration(window.duration)
    q_total, q_err = queries.log_total(svc, rng), queries.log_errors(svc, rng)
    q_paths = queries.log_errors_by_path(svc, rng)
    calls = {
        "total": ("Loki total requests", logs.query_metric(q_total, window.end), []),
        "errors": ("Loki error count", logs.query_metric(q_err, window.end), []),
    }
    if q_paths is not None:
        calls["paths"] = ("Loki errors by path", logs.query_metric(q_paths, window.end), [])
    if compare:
        calls["prev_total"] = ("Loki previous-window total", logs.query_metric(q_total, window.start), [])
        calls["prev_errors"] = ("Loki previous-window errors", logs.query_metric(q_err, window.start), [])
    if sample_lines:
        line_start = max(window.start, window.end - timedelta(hours=6))
        calls["lines"] = ("Loki error lines",
                          logs.query_logs(queries.log_error_lines(svc), line_start, window.end, limit=sample_lines), [])
    res = await rec.gather(**calls)
    for q in (q_total, q_err, q_paths):
        if q is not None:
            rec.logql(logs, q, window.start, window.end)

    total = _sum(res["total"])
    errors = _sum(res["errors"])
    data["requests"] = int(total)
    data["errors"] = int(errors)
    data["error_rate_pct"] = round(100 * errors / total, 3) if total else None
    if total == 0 and not rec.errors:
        data["note"] = "No request log lines matched the selector in this window (no traffic or wrong selector)."
    if compare:
        pt, pe = _sum(res["prev_total"]), _sum(res["prev_errors"])
        data["previous_window"] = {"requests": int(pt), "errors": int(pe),
                                   "error_rate_pct": round(100 * pe / pt, 3) if pt else None}
    if res.get("paths"):
        label = svc.logs.path_label
        data["top_failing_paths"] = sorted(({"path": s.labels.get(label, "?"), "errors": int(s.value)}
                                            for s in res["paths"]), key=lambda d: -d["errors"])
    if res.get("lines"):
        data["sample_error_lines_untrusted"] = [
            {"ts": iso(line.ts), "line": line.line[:MAX_LINE_CHARS]} for line in res["lines"]]
    threshold = svc.slo.error_rate_pct if svc.slo and svc.slo.error_rate_pct is not None else None
    if data["error_rate_pct"] is not None:
        if threshold is not None:
            data["slo_error_rate_pct"] = threshold
            data["within_slo"] = data["error_rate_pct"] <= threshold
    return rec.result(data) if own else OpResult(data)


async def service_health(ctx: AgentContext, svc: Service, window: Window) -> OpResult:
    rec = Recorder()
    now = ctx.now()
    data: dict = {
        "service": {"id": svc.id, "name": svc.display_name, "environment": svc.environment,
                    "criticality": svc.criticality, "owner": svc.owner or svc.team,
                    "description": svc.description or None},
        "window": window.to_dict(),
    }
    signals = 0
    problems: list[str] = []
    down_signals: list[str] = []

    async def probes():
        if not svc.health_checks or ctx.backends.prober is None:
            return []
        results = await asyncio.gather(*(ctx.backends.prober.probe(c) for c in svc.health_checks))
        for c in svc.health_checks:
            rec.add("probe", ctx.backends.prober.name, query=f"{c.method} {c.url}")
        return [{"check": c.name, **r.to_dict()} for c, r in zip(svc.health_checks, results, strict=True)]

    async def k8s():
        return [await workload_status(ctx, w, rec) for w in svc.kubernetes]

    async def hosts():
        out = []
        m = ctx.backends.metrics
        if m is None:
            return out
        for host_id in svc.hosts:
            host = ctx.catalog.hosts[host_id]
            q = queries.host_up(host)
            samples = await rec.call(f"up for {host_id}", m.query(q), [])
            up = min((s.value for s in samples), default=None)
            verdict, _ = host_verdict(up, None, {})
            out.append({"host": host_id, "verdict": verdict})
            rec.promql(m, q, now - timedelta(hours=1), now)
        return out

    async def errors():
        if svc.logs is None or ctx.backends.logs is None:
            return None
        return (await api_errors(ctx, svc, window, compare=True, sample_lines=3, rec=rec)).data

    async def alerts():
        if not ctx.backends.alerts:
            return []
        items = await fetch_alerts(ctx, Window(min(window.start, now - timedelta(hours=24)), now), rec)
        return [a for a in items if in_window(a, window.start) and alert_matches(a, svc)]

    probe_res, k8s_res, host_res, err_res, alert_res = await asyncio.gather(
        rec.call("health probes", probes(), []), k8s(), hosts(), errors(), alerts())

    if probe_res:
        signals += 1
        data["health_checks"] = probe_res
        failing = [p for p in probe_res if not p["ok"]]
        if failing and len(failing) == len(probe_res):
            down_signals.append("all health checks failing: " + "; ".join(f"{p['check']}: {p['error']}" for p in failing))
        elif failing:
            problems.append("health checks failing: " + ", ".join(p["check"] for p in failing))
    if k8s_res:
        signals += 1
        data["kubernetes"] = k8s_res
        for w in k8s_res:
            if w.get("state") == "DOWN":
                down_signals.append(f"{w['workload']}: no replicas available")
            elif w.get("issues"):
                problems.append(f"{w['workload']}: {'; '.join(w['issues'])}")
    if host_res:
        signals += 1
        data["hosts"] = host_res
        bad = [h for h in host_res if h["verdict"] not in ("UP", "DEGRADED")]
        if bad and len(bad) == len(host_res):
            down_signals.append("all hosts down: " + ", ".join(h["host"] for h in bad))
        elif bad:
            problems.append("hosts down: " + ", ".join(h["host"] for h in bad))
    if err_res is not None:
        data["api_errors"] = err_res
        rate = err_res.get("error_rate_pct")
        if rate is not None:
            signals += 1
            limit = (svc.slo.error_rate_pct if svc.slo and svc.slo.error_rate_pct is not None
                     else DEFAULT_ERROR_RATE_THRESHOLD_PCT)
            if rate > limit:
                problems.append(f"error rate {rate:.2f}% over {human_duration(window.duration)} (threshold {limit}%)")
    if alert_res:
        data["related_alerts"] = [{"title": a.title, "state": a.state, "severity": a.severity,
                                   "started_at": iso(a.started_at), "source": a.source, "url": a.url}
                                  for a in alert_res[:10]]
        firing = [a for a in alert_res if a.state in ("firing", "acknowledged")]
        if firing:
            problems.append(f"{len(firing)} active alert(s): " + ", ".join(a.title for a in firing[:3]))

    if ctx.ledger:
        key = f"service:{svc.id}"
        interval = ctx.settings.ledger.interval_s
        state = await asyncio.to_thread(ctx.ledger.state, key)
        if state:
            signals += 1
            u24 = await asyncio.to_thread(ctx.ledger.uptime, key, now - timedelta(hours=24), now, interval)
            u7 = await asyncio.to_thread(ctx.ledger.uptime, key, now - timedelta(days=7), now, interval)
            stats = await asyncio.to_thread(ctx.ledger.sample_stats, f"check:{svc.id}/{svc.health_checks[0].name}",
                                            now - timedelta(hours=24), now) if svc.health_checks else {}
            outages = await asyncio.to_thread(ctx.ledger.outages_in, now - timedelta(days=7), now, keys=[key], limit=10)
            data["history"] = {
                "status": state["status"], "since": iso(state["since"]),
                "uptime_24h_pct": u24.get("uptime_pct"), "uptime_7d_pct": u7.get("uptime_pct"),
                "coverage_7d_pct": u7.get("coverage_pct"), "probe_stats_24h": stats or None,
                "recent_outages": [{"started_at": iso(o["started_at"]), "ended_at": iso(o["ended_at"]),
                                    "detail": o["detail"][:200]} for o in outages],
            }
            rec.add("ledger", "ledger", note=key)

    deps = []
    for dep_id in svc.depends_on:
        st = await asyncio.to_thread(ctx.ledger.state, f"service:{dep_id}") if ctx.ledger else None
        deps.append({"service": dep_id, "status": st["status"] if st else "unknown"})
        if st and st["status"] == "down":
            problems.append(f"dependency {dep_id} is down")
    if deps:
        data["dependencies"] = deps
    dependents = [s.id for s in ctx.catalog.dependents_of(svc.id)]
    if dependents:
        data["dependents"] = dependents
    data["runbooks"] = svc.runbooks

    if down_signals:
        verdict = "DOWN"
    elif problems:
        verdict = "DEGRADED"
    elif signals == 0:
        verdict = "UNKNOWN"
        problems.append("no health checks, logs signal, workloads or hosts configured for this service in the catalog")
    else:
        verdict = "HEALTHY"
    data["verdict"] = verdict
    data["reasons"] = down_signals + problems
    return rec.result(data)


def _sum(samples) -> float:
    return float(sum(s.value for s in samples)) if samples else 0.0
