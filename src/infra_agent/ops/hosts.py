"""VM status, downtime history and fleet overview."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from .. import queries
from ..context import AgentContext
from ..knowledge.schema import Host
from ..timeutil import Window, down_intervals, human_duration, pick_step, prom_duration
from .alerts import alert_matches, fetch_alerts, in_window
from .common import OpResult, Recorder, iso, round_or_none

DISK_PCT_DEGRADED = 90.0
MEM_AVAILABLE_PCT_DEGRADED = 5.0
LOAD_PER_CORE_DEGRADED = 2.0


def host_verdict(up: float | None, cloud_state: str | None, resources: dict) -> tuple[str, list[str]]:
    """Combine Prometheus liveness, cloud power state and resource pressure into one verdict."""
    reasons: list[str] = []
    if up is None:
        if cloud_state in ("terminated", "not_found"):
            return "GONE", [f"No Prometheus target and the cloud reports the VM as {cloud_state}"]
        if cloud_state in ("stopped", "stopping"):
            return "DOWN", [f"VM is {cloud_state} in the cloud console and not scraped"]
        if cloud_state == "running":
            return "NOT_MONITORED", ["VM is running but no Prometheus target matches the catalog selector "
                                     "(dropped from service discovery or selector is wrong)"]
        return "UNKNOWN", ["No Prometheus series matched the catalog selector; cloud state unavailable"]
    if up < 1:
        if cloud_state in ("stopped", "stopping"):
            return "DOWN", [f"VM is {cloud_state} (cloud API)"]
        if cloud_state in ("terminated", "not_found"):
            return "GONE", [f"VM is {cloud_state} (cloud API)"]
        if cloud_state == "starting":
            return "DOWN", ["VM is starting (cloud API); exporter not reachable yet"]
        if cloud_state == "running":
            return "UNREACHABLE", ["VM is running per cloud API but Prometheus cannot scrape node_exporter: "
                                   "exporter stopped, firewall/network issue, or the OS is hung"]
        return "DOWN", ["Prometheus scrape of node_exporter is failing (up == 0)"]
    disk = resources.get("disk_used_pct") or {}
    for mount, pct in disk.items():
        if pct is not None and pct >= DISK_PCT_DEGRADED:
            reasons.append(f"disk {mount} at {pct:.0f}% used")
    mem = resources.get("mem_available_pct")
    if mem is not None and mem < MEM_AVAILABLE_PCT_DEGRADED:
        reasons.append(f"only {mem:.1f}% memory available")
    load = resources.get("load1_per_core")
    if load is not None and load > LOAD_PER_CORE_DEGRADED:
        reasons.append(f"load average {load:.1f}x CPU cores")
    return ("DEGRADED", reasons) if reasons else ("UP", ["node_exporter scrape healthy (up == 1)"])


async def host_status(ctx: AgentContext, host: Host) -> OpResult:
    rec = Recorder()
    now = ctx.now()
    m = ctx.backends.metrics
    data: dict = {"host": _host_info(host), "checked_at": iso(now)}

    up_val: float | None = None
    resources: dict = {}
    if m is None:
        rec.errors.append("no metrics backend configured")
    else:
        q_up = queries.host_up(host)
        res_q = queries.host_resources(host)
        rec.promql(m, q_up, now - timedelta(hours=1), now)
        calls = {"up": (f"query {q_up}", m.query(q_up), [])}
        for key, q in res_q.items():
            calls[key] = (f"query {key}", m.query(q), [])
        results = await rec.gather(**calls)
        ups = results.pop("up")
        if ups:
            up_val = min(s.value for s in ups)
            data["prometheus_targets"] = [{"labels": s.labels, "up": s.value} for s in ups]
        for key, samples in results.items():
            if not samples:
                continue
            if key == "disk_used_pct":
                resources[key] = {s.labels.get("mountpoint", "?"): round(s.value, 1) for s in samples}
            else:
                resources[key] = round(samples[0].value, 2)
        if "uptime_s" in resources:
            resources["os_uptime"] = human_duration(resources.pop("uptime_s"))

    cloud_state = None
    inv = ctx.backends.inventory_for(host.cloud_ref.provider, host.cloud_ref.account) if host.cloud_ref else None
    if host.cloud_ref and inv:
        inst = await rec.call(f"{inv.name} instance lookup", inv.get_instance(host.cloud_ref), None)
        if inst:
            cloud_state = inst.state
            data["cloud"] = {k: v for k, v in inst.to_dict().items() if k not in ("tags",) and v}
            rec.add("cloud", inv.name, note=f"instance {inst.id}: {inst.raw_state}")
    elif host.cloud_ref:
        rec.errors.append(f"no inventory backend for {host.cloud_ref.provider}")

    verdict, reasons = host_verdict(up_val, cloud_state, resources)
    data.update(verdict=verdict, reasons=reasons, resources=resources or None)

    if verdict in ("DOWN", "UNREACHABLE") and m is not None:
        data["down_since"] = await _down_since(ctx, host, rec)

    if ctx.ledger:
        state = await asyncio.to_thread(ctx.ledger.state, f"host:{host.id}")
        if state:
            data["ledger"] = {"status": state["status"], "since": iso(state["since"]),
                              "last_checked": iso(state["last_checked"])}
            rec.add("ledger", "ledger", note=f"host:{host.id}")

    window = Window(now - timedelta(hours=24), now)
    alerts = await fetch_alerts(ctx, window, rec) if ctx.backends.alerts else []
    related = [a for a in alerts if in_window(a, window.start) and alert_matches(a, host)]
    data["related_alerts"] = [{"title": a.title, "state": a.state, "severity": a.severity,
                               "started_at": iso(a.started_at), "source": a.source} for a in related[:10]]
    data["services_on_host"] = [s.id for s in ctx.catalog.services_on_host(host.id)]
    return rec.result(data)


async def _down_since(ctx: AgentContext, host: Host, rec: Recorder) -> dict:
    now = ctx.now()
    lookback = min(ctx.metrics_retention, timedelta(hours=24))
    window = Window(now - lookback, now)
    step = pick_step(window.duration, max_points=1440, min_step=30)
    q = queries.host_up_range(host, step)
    series = await rec.call("up range query", ctx.backends.metrics.query_range(q, window.start, window.end, step), [])
    if not series:
        return {"note": "no samples in lookback"}
    samples = _merge_min(series)
    intervals, _, _ = down_intervals(samples, step, window)
    ongoing = [i for i in intervals if i.end is None]
    if not ongoing:
        return {"note": "latest samples show recovery; status may be flapping"}
    start = ongoing[-1].start
    out = {"since": iso(start), "for": human_duration(now - start)}
    if start - window.start <= timedelta(seconds=2 * step):
        out["note"] = f"down for the entire {human_duration(lookback)} lookback (possibly longer)"
    return out


def _merge_min(series) -> list[tuple[float, float]]:
    merged: dict[float, float] = {}
    for s in series:
        for ts, v in s.samples:
            merged[ts] = min(v, merged.get(ts, v))
    return sorted(merged.items())


async def host_downtime(ctx: AgentContext, host: Host, window: Window) -> OpResult:
    rec = Recorder()
    now = ctx.now()
    data: dict = {"host": _host_info(host), "window": window.to_dict()}
    retention = ctx.metrics_retention
    prom_start = max(window.start, now - retention)
    m = ctx.backends.metrics

    if m is not None and prom_start < window.end:
        prom_window = Window(prom_start, window.end)
        step = pick_step(prom_window.duration, max_points=1440, min_step=30)
        q_range = queries.host_up_range(host, step)
        q_avail = queries.host_availability(host, prom_duration(prom_window.duration))
        q_boot = queries.host_reboots(host, prom_duration(prom_window.duration))
        results = await rec.gather(
            series=("up range query", m.query_range(q_range, prom_window.start, prom_window.end, step), []),
            avail=("availability query", m.query(q_avail, prom_window.end), []),
            boots=("reboot query", m.query(q_boot, prom_window.end), []),
        )
        rec.promql(m, q_range, prom_window.start, prom_window.end, note=f"step {step}s")
        rec.promql(m, q_avail, prom_window.start, prom_window.end)
        rec.promql(m, q_boot, prom_window.start, prom_window.end)
        if results["series"]:
            intervals, down_s, observed_s = down_intervals(_merge_min(results["series"]), step, prom_window)
            intervals.sort(key=lambda i: i.start, reverse=True)
            data["prometheus"] = {
                "window": prom_window.to_dict(),
                "resolution": f"{step}s",
                "availability_pct": round_or_none(100 * results["avail"][0].value, 3) if results["avail"] else None,
                "downtime_total": human_duration(down_s),
                "outage_count": len(intervals),
                "longest_outage": human_duration(max((i.duration(now) for i in intervals), default=timedelta())),
                "currently_down": any(i.end is None for i in intervals),
                "outages": [i.to_dict(now) for i in intervals[:30]],
                "data_coverage_pct": round(100 * observed_s / prom_window.duration.total_seconds(), 1),
            }
            if results["boots"]:
                data["prometheus"]["reboots"] = int(results["boots"][0].value)
        else:
            data["prometheus"] = {"note": "no samples for this host's selector in the window"}
        if window.start < prom_start:
            data["prometheus"]["note"] = (f"Prometheus retention is {human_duration(retention)}; "
                                          f"older history comes from the ledger")

    if ctx.ledger:
        key = f"host:{host.id}"
        uptime = await asyncio.to_thread(ctx.ledger.uptime, key, window.start, window.end, ctx.settings.ledger.interval_s)
        items = await asyncio.to_thread(ctx.ledger.outages_in, window.start, window.end, keys=[key], limit=50)
        data["ledger"] = {**uptime, "outages": [_outage(o, now) for o in items]}
        rec.add("ledger", "ledger", note=f"outages for {key}", start=iso(window.start), end=iso(window.end))
    return rec.result(data)


async def fleet_overview(ctx: AgentContext) -> OpResult:
    rec = Recorder()
    now = ctx.now()
    m = ctx.backends.metrics
    data: dict = {"checked_at": iso(now)}
    if m is not None:
        q = queries.fleet_up(ctx.settings.backends.node_job_regex)
        samples = await rec.call("fleet up query", m.query(q), [])
        rec.promql(m, q, now - timedelta(hours=1), now)
        down, seen_hosts = [], set()
        for s in samples:
            host = ctx.catalog.host_for_target(s.labels)
            if host:
                seen_hosts.add(host.id)
            if s.value < 1:
                down.append({"host_id": host.id if host else None,
                             "name": host.display_name if host else s.labels.get("instance"),
                             "environment": host.environment if host else None,
                             "job": s.labels.get("job"), "instance": s.labels.get("instance")})
        missing = [h.id for h in ctx.catalog.hosts.values() if samples and h.id not in seen_hosts]
        data["targets"] = {"total": len(samples), "down": len(down)}
        data["down_targets"] = down
        if missing:
            data["catalog_hosts_without_target"] = missing
    if ctx.ledger:
        open_items = await asyncio.to_thread(ctx.ledger.open_outages)
        # Individual health checks roll up into their service entry; listing both is noise.
        data["open_outages"] = [_outage(o, now) for o in open_items if o["kind"] != "check"]
        rec.add("ledger", "ledger", note="open outages")
    return rec.result(data)


def _outage(o: dict, now) -> dict:
    end = o["ended_at"]
    return {"entity": o["key"], "name": o["name"], "started_at": iso(o["started_at"]),
            "ended_at": iso(end), "duration": human_duration((end or now) - o["started_at"]),
            "ongoing": end is None, "detail": (o.get("detail") or "")[:200]}


def _host_info(host: Host) -> dict:
    return {k: v for k, v in {
        "id": host.id, "name": host.display_name, "environment": host.environment, "cloud": host.cloud,
        "region": host.region, "owner": host.owner or host.team, "description": host.description or None,
        "selector": host.prometheus,
    }.items() if v}


outage_dict = _outage
