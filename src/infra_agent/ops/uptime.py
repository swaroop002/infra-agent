"""Uptime reports from the downtime ledger (any window, independent of Prometheus retention)."""

from __future__ import annotations

import asyncio

from ..context import AgentContext
from ..timeutil import Window, human_duration
from .common import OpResult, Recorder, iso
from .hosts import outage_dict


async def uptime_report(ctx: AgentContext, window: Window, *, kind: str = "service",
                        entity_id: str | None = None, limit: int = 50) -> OpResult:
    rec = Recorder()
    if ctx.ledger is None:
        rec.errors.append("downtime ledger is not configured; uptime history is unavailable")
        return rec.result({"window": window.to_dict()})
    interval = ctx.settings.ledger.interval_s
    if entity_id:
        keys = [f"{kind}:{entity_id}"]
    else:
        states = await asyncio.to_thread(ctx.ledger.states, kind)
        keys = [s["key"] for s in states]
    rows = []
    for key in keys:
        u = await asyncio.to_thread(ctx.ledger.uptime, key, window.start, window.end, interval)
        if not u.get("known"):
            continue
        rows.append({
            "entity": key, "name": u.get("name"), "uptime_pct": u.get("uptime_pct"),
            "downtime": human_duration(u.get("downtime_s", 0)), "outages": u.get("outages", 0),
            "coverage_pct": u.get("coverage_pct"), "current_status": u.get("current_status"),
        })
    rows.sort(key=lambda r: (r["uptime_pct"] if r["uptime_pct"] is not None else 101, r["entity"]))
    rec.add("ledger", "ledger", note=f"uptime for {len(rows)} {kind} entities", start=iso(window.start), end=iso(window.end))
    data = {"window": window.to_dict(), "kind": kind, "entities": rows[:limit], "truncated": len(rows) > limit}
    if entity_id and rows:
        outages = await asyncio.to_thread(ctx.ledger.outages_in, window.start, window.end, keys=keys, limit=30)
        data["outages"] = [outage_dict(o, ctx.now()) for o in outages]
    if not rows:
        data["note"] = ("No ledger history for this selection yet. The collector records history from the moment "
                        "it starts; run `infra-agent ledger backfill` to import recent Prometheus history.")
    low_coverage = [r["entity"] for r in rows if (r["coverage_pct"] or 0) < 90]
    if low_coverage:
        data["coverage_warning"] = (f"{len(low_coverage)} entities have <90% collector coverage in this window; "
                                    "uptime is computed over observed time only")
    return rec.result(data)
