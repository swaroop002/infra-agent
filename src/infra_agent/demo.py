"""Seed the demo ledger with plausible history so uptime views have something to show."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta

from .backends.fake import FakeWorld
from .context import AgentContext
from .ledger.collector import Collector
from .timeutil import parse_duration, utcnow


async def seed_demo_ledger(ctx: AgentContext, days: int = 14) -> dict[str, int]:
    assert ctx.ledger is not None
    ledger = ctx.ledger
    now = utcnow()
    start = now - timedelta(days=days)
    metrics = ctx.backends.metrics
    world: FakeWorld | None = getattr(metrics, "w", None)
    counts: dict[str, int] = {}

    def add(key: str, kind: str, eid: str, name: str, s, e, detail: str) -> None:
        if ledger.add_historical_outage(key=key, kind=kind, entity_id=eid, name=name, started_at=s, ended_at=e,
                                        source="demo-seed", detail=detail):
            counts[key] = counts.get(key, 0) + 1

    for host in ctx.catalog.hosts.values():
        key = f"host:{host.id}"
        ledger.ensure_entity(key=key, kind="host", entity_id=host.id, name=host.display_name, first_seen=start)
        if world:
            for s, e in world.host_down_intervals(host.id):
                if e is not None:
                    add(key, "host", host.id, host.display_name, s, e, "Prometheus scrape failing (up == 0)")
        rng = random.Random(host.id)
        for _ in range(rng.randint(0, 2)):
            s = start + timedelta(minutes=rng.randint(0, (days - 1) * 1440))
            add(key, "host", host.id, host.display_name, s, s + timedelta(minutes=rng.randint(3, 35)),
                "Prometheus scrape failing (up == 0)")

    flaky = {"report-generation": 5, "payments-api": 2, "dev-portal": 3}
    for svc in ctx.catalog.services.values():
        key = f"service:{svc.id}"
        ledger.ensure_entity(key=key, kind="service", entity_id=svc.id, name=svc.display_name, first_seen=start)
        for check in svc.health_checks:
            ledger.ensure_entity(key=f"check:{svc.id}/{check.name}", kind="check", entity_id=f"{svc.id}/{check.name}",
                                 name=f"{svc.display_name} {check.name}", first_seen=start)
        rng = random.Random(svc.id)
        for _ in range(flaky.get(svc.id, rng.randint(0, 1))):
            s = start + timedelta(minutes=rng.randint(0, (days - 1) * 1440))
            add(key, "service", svc.id, svc.display_name, s, s + timedelta(minutes=rng.randint(2, 50)),
                "health: unexpected status 503")

    ledger.record_run(start, now - timedelta(minutes=2), 0, [])

    # Backdate the start of current failures so "down since" looks realistic, then run live cycles.
    if world:
        for host in ctx.catalog.hosts.values():
            h = world.host(host.id)
            if int(h.get("up", 1)) == 0:
                since = now - parse_duration(h.get("down_for", "10m"))
                for offset in (0, 1):
                    ledger.observe(key=f"host:{host.id}", kind="host", entity_id=host.id, name=host.display_name,
                                   status="down", at=since + timedelta(minutes=offset), source="prometheus_up",
                                   detail="Prometheus scrape failing (up == 0)", failure_threshold=2)
    collector = Collector(ctx)
    for _ in range(2):
        await collector.run_once()
        await asyncio.sleep(0)
    return counts
