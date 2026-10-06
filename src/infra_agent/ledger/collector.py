"""Periodic collector: turns live signals into ledger observations.

One cycle = Prometheus `up` for all targets + catalog health checks + vibe VM state/app probes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import queries
from ..context import AgentContext
from ..notify import Notifier
from ..ops.vibe import app_check
from ..timeutil import Window, down_intervals, pick_step, utcnow
from .store import Transition

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    started_at: datetime
    finished_at: datetime | None = None
    checked: int = 0
    errors: list[str] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)


class Collector:
    def __init__(self, ctx: AgentContext, notifier: Notifier | None = None):
        if ctx.ledger is None:
            raise ValueError("collector requires a ledger")
        self.ctx = ctx
        self.ledger = ctx.ledger
        self.notifier = notifier
        self.cfg = ctx.settings.ledger
        self._cycles = 0

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        log.info("collector started (interval %ss)", self.cfg.interval_s)
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # keep collecting
                log.exception("collector cycle crashed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.interval_s)
            except TimeoutError:
                pass

    async def run_once(self) -> CycleReport:
        report = CycleReport(started_at=self.ctx.now())
        await asyncio.gather(self._hosts(report), self._health_checks(report), self._vibe(report))
        report.finished_at = self.ctx.now()
        await asyncio.to_thread(self.ledger.record_run, report.started_at, report.finished_at,
                                report.checked, report.errors)
        self._cycles += 1
        if self._cycles % 60 == 1:
            await asyncio.to_thread(self.ledger.prune, self.cfg.sample_retention_days)
        if self.notifier:
            notify_on = self.ctx.settings.notifications.notify_on
            for t in report.transitions:
                went_down = t.new_status == "down"
                recovered = t.old_status == "down" and not went_down
                if (went_down and "down" in notify_on) or (recovered and "up" in notify_on):
                    await self.notifier.transition(t)
        if report.errors:
            log.warning("collector cycle errors: %s", "; ".join(report.errors[:5]))
        return report

    async def _observe(self, report: CycleReport, **kw) -> None:
        kw.setdefault("at", self.ctx.now())
        kw.setdefault("failure_threshold", self.cfg.failure_threshold)
        t = await asyncio.to_thread(self.ledger.observe, **kw)
        report.checked += 1
        if t:
            report.transitions.append(t)
            log.info("transition %s %s -> %s", t.key, t.old_status, t.new_status)

    # ---------------------------------------------------------------- hosts
    async def _hosts(self, report: CycleReport) -> None:
        m = self.ctx.backends.metrics
        if m is None:
            return
        try:
            samples = await m.query(queries.fleet_up(self.ctx.settings.backends.node_job_regex))
        except Exception as exc:
            report.errors.append(f"fleet up: {exc}")
            return
        seen = set()
        for s in samples:
            host = self.ctx.catalog.host_for_target(s.labels)
            if host:
                if host.id in seen:
                    continue
                seen.add(host.id)
                key, kind, eid, name = f"host:{host.id}", "host", host.id, host.display_name
            else:
                job, inst = s.labels.get("job", "?"), s.labels.get("instance", "?")
                key, kind, eid, name = f"target:{job}/{inst}", "target", f"{job}/{inst}", inst
            detail = "" if s.value >= 1 else "Prometheus scrape failing (up == 0)"
            if host and s.value < 1 and host.cloud_ref:
                detail = await self._cloud_detail(host, detail)
            await self._observe(report, key=key, kind=kind, entity_id=eid, name=name,
                                status="up" if s.value >= 1 else "down", source="prometheus_up", detail=detail)
        if not samples:
            return
        for host in self.ctx.catalog.hosts.values():
            if host.id not in seen:
                detail = await self._cloud_detail(host, "no Prometheus target matches the catalog selector")
                await self._observe(report, key=f"host:{host.id}", kind="host", entity_id=host.id,
                                    name=host.display_name, status="down", source="prometheus_up", detail=detail)

    async def _cloud_detail(self, host, detail: str) -> str:
        inv = self.ctx.backends.inventory_for(host.cloud_ref.provider, host.cloud_ref.account) if host.cloud_ref else None
        if not inv:
            return detail
        try:
            inst = await inv.get_instance(host.cloud_ref)
            return f"{detail}; cloud state: {inst.state}"
        except Exception as exc:
            return f"{detail}; cloud lookup failed: {exc}"

    # ------------------------------------------------------- health checks
    async def _health_checks(self, report: CycleReport) -> None:
        prober = self.ctx.backends.prober
        if prober is None:
            return
        sem = asyncio.Semaphore(self.cfg.probe_concurrency)

        async def one(svc, check):
            async with sem:
                return svc, check, await prober.probe(check)

        jobs = [one(svc, c) for svc in self.ctx.catalog.services.values() for c in svc.health_checks]
        by_service: dict[str, list] = {}
        for svc, check, r in await asyncio.gather(*jobs):
            by_service.setdefault(svc.id, []).append((check, r))
            await self._observe(report, key=f"check:{svc.id}/{check.name}", kind="check",
                                entity_id=f"{svc.id}/{check.name}", name=f"{svc.display_name} {check.name}",
                                status="up" if r.ok else "down", source="http_probe", detail=r.error or "",
                                sample={"ok": r.ok, "latency_ms": r.latency_ms, "status_code": r.status_code,
                                        "error": r.error})
        for svc_id, results in by_service.items():
            svc = self.ctx.catalog.services[svc_id]
            failing = [f"{c.name}: {r.error}" for c, r in results if not r.ok]
            await self._observe(report, key=f"service:{svc_id}", kind="service", entity_id=svc_id,
                                name=svc.display_name, status="down" if failing else "up", source="http_probe",
                                detail="; ".join(failing))

    # ----------------------------------------------------------- vibe VMs
    async def _vibe(self, report: CycleReport) -> None:
        platform = self.ctx.catalog.vibe
        if platform is None:
            return
        inv = self.ctx.backends.inventory_for(platform.provider, platform.inventory_backend)
        if inv is None:
            return
        try:
            vms = await inv.list_instances(platform.tag_filter)
        except Exception as exc:
            report.errors.append(f"vibe inventory: {exc}")
            return
        seen = set()
        prober = self.ctx.backends.prober
        for vm in vms:
            seen.add(f"vibe:{vm.id}")
            owner = vm.tags.get(platform.owner_tag, "")
            name = f"{vm.name or vm.id} ({owner})" if owner else (vm.name or vm.id)
            impaired = [f"{k}={v}" for k, v in vm.status_checks.items() if v == "impaired"]
            if vm.state == "running":
                status, detail = ("down", "cloud status checks: " + ", ".join(impaired)) if impaired else ("up", "")
            elif vm.state in ("stopped", "stopping"):
                status, detail = "stopped", vm.raw_state
            else:
                status, detail = "unknown", vm.raw_state
            await self._observe(report, key=f"vibe:{vm.id}", kind="vibe", entity_id=vm.id, name=name,
                                status=status, source="cloud_state", detail=detail)
            if vm.state != "running" or prober is None:
                for app in platform.apps:
                    if await asyncio.to_thread(self.ledger.state, f"vibe_app:{vm.id}/{app.name}"):
                        await self._observe(report, key=f"vibe_app:{vm.id}/{app.name}", kind="vibe_app",
                                            entity_id=f"{vm.id}/{app.name}", name=f"{name} {app.name}",
                                            status="stopped", source="http_probe", detail="VM not running")
                continue
            for app in platform.apps:
                check = app_check(platform, vm, app)
                if check is None:
                    continue
                r = await prober.probe(check)
                await self._observe(report, key=f"vibe_app:{vm.id}/{app.name}", kind="vibe_app",
                                    entity_id=f"{vm.id}/{app.name}", name=f"{name} {app.name}",
                                    status="up" if r.ok else "down", source="http_probe", detail=r.error or "",
                                    sample={"ok": r.ok, "latency_ms": r.latency_ms, "status_code": r.status_code,
                                            "error": r.error})
        # VMs that disappeared (terminated) close any open outage.
        for st in await asyncio.to_thread(self.ledger.states, "vibe"):
            if st["key"] not in seen and st["status"] != "missing":
                await self._observe(report, key=st["key"], kind="vibe", entity_id=st["entity_id"],
                                    name=st["name"], status="missing", source="cloud_state",
                                    detail="no longer in inventory (terminated?)")


async def backfill_hosts(ctx: AgentContext, hours: int) -> dict[str, int]:
    """Reconstruct host outages from Prometheus history into the ledger."""
    assert ctx.ledger is not None and ctx.backends.metrics is not None
    now = utcnow()
    span = min(timedelta(hours=hours), ctx.metrics_retention)
    window = Window(now - span, now)
    step = pick_step(window.duration, max_points=5000, min_step=60)
    added = {}
    for host in ctx.catalog.hosts.values():
        series = await ctx.backends.metrics.query_range(queries.host_up_range(host, step), window.start, window.end, step)
        key = f"host:{host.id}"
        await asyncio.to_thread(ctx.ledger.ensure_entity, key=key, kind="host", entity_id=host.id,
                                name=host.display_name, first_seen=window.start)
        count = 0
        for s in series:
            intervals, _, _ = down_intervals(s.samples, step, window)
            for i in intervals:
                if i.end is None:
                    continue  # ongoing outages are opened by the live collector
                if await asyncio.to_thread(ctx.ledger.add_historical_outage, key=key, kind="host", entity_id=host.id,
                                           name=host.display_name, started_at=i.start, ended_at=i.end,
                                           source="backfill", detail="reconstructed from Prometheus up"):
                    count += 1
        added[host.id] = count
    await asyncio.to_thread(ctx.ledger.record_run, window.start, now, len(added), [])
    return added
