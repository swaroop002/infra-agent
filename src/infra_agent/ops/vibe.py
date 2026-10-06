"""Vibe-coding VM platform: inventory from cloud tags, app health on each VM."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from ..backends.base import InstanceState
from ..context import AgentContext
from ..knowledge.schema import HealthCheck, VibeApp, VibePlatform
from ..timeutil import human_duration
from .common import OpResult, Recorder, iso


def app_check(platform: VibePlatform, vm: InstanceState, app: VibeApp) -> HealthCheck | None:
    address = vm.private_ip if platform.address == "private_ip" else vm.public_ip
    if not address:
        return None
    return HealthCheck(name=app.name, url=f"{app.scheme}://{address}:{app.port}{app.path}",
                       expect_status=app.expect_status, timeout_s=app.timeout_s, verify_tls=app.verify_tls)


def vm_summary(platform: VibePlatform, vm: InstanceState, now) -> dict:
    out = {
        "id": vm.id, "name": vm.name, "owner": vm.tags.get(platform.owner_tag), "state": vm.state,
        "instance_type": vm.instance_type, "region": vm.region, "private_ip": vm.private_ip,
        "launched_at": iso(vm.launched_at),
        "running_for": human_duration(now - vm.launched_at) if vm.launched_at and vm.state == "running" else None,
    }
    if vm.status_checks:
        out["cloud_status_checks"] = vm.status_checks
    if platform.provision_status_tag and platform.provision_status_tag in vm.tags:
        out["provision_status"] = vm.tags[platform.provision_status_tag]
    return {k: v for k, v in out.items() if v is not None}


async def _inventory(ctx: AgentContext, rec: Recorder) -> tuple[VibePlatform | None, list[InstanceState]]:
    platform = ctx.catalog.vibe
    if platform is None:
        rec.errors.append("vibe-coding platform is not described in the catalog (`vibe:` section)")
        return None, []
    inv = ctx.backends.inventory_for(platform.provider, platform.inventory_backend)
    if inv is None:
        rec.errors.append(f"no inventory backend for {platform.provider}")
        return platform, []
    vms = await rec.call(f"{inv.name} list instances", inv.list_instances(platform.tag_filter), [])
    rec.add("cloud", inv.name, note=f"instances tagged {platform.tag_filter}")
    return platform, vms


async def list_vibe_vms(ctx: AgentContext, *, owner: str | None = None, state: str | None = None) -> OpResult:
    rec = Recorder()
    now = ctx.now()
    platform, vms = await _inventory(ctx, rec)
    if platform is None:
        return rec.result({})
    if owner:
        o = owner.lower()
        vms = [v for v in vms if o in (v.tags.get(platform.owner_tag) or "").lower()]
    if state:
        vms = [v for v in vms if v.state == state]
    counts: dict[str, int] = {}
    for v in vms:
        counts[v.state] = counts.get(v.state, 0) + 1
    return rec.result({"count": len(vms), "by_state": counts,
                       "vms": [vm_summary(platform, v, now) for v in vms[:100]], "truncated": len(vms) > 100})


async def vibe_vm_status(ctx: AgentContext, query: str) -> OpResult:
    """Status of VMs matching an instance id, Name tag or owner, including app health probes."""
    rec = Recorder()
    now = ctx.now()
    platform, vms = await _inventory(ctx, rec)
    if platform is None:
        return rec.result({})
    q = query.strip().lower()
    exact = [v for v in vms if v.id.lower() == q or (v.name or "").lower() == q]
    matches = exact or [v for v in vms if q in (v.name or "").lower()
                        or q in (v.tags.get(platform.owner_tag) or "").lower()]
    if not matches:
        return rec.result({"query": query, "matches": 0,
                           "note": "No vibe VM matches that id, name or owner (it may be terminated)."})
    out = []
    for vm in matches[:10]:
        entry = vm_summary(platform, vm, now)
        if vm.state == "running" and ctx.backends.prober is not None:
            checks = [(app, app_check(platform, vm, app)) for app in platform.apps]
            results = await asyncio.gather(*(ctx.backends.prober.probe(c) for _, c in checks if c))
            apps = []
            it = iter(results)
            for app, check in checks:
                if check is None:
                    apps.append({"app": app.name, "ok": None, "error": f"VM has no {platform.address}"})
                    continue
                r = next(it)
                apps.append({"app": app.name, "ok": r.ok, "status_code": r.status_code,
                             "latency_ms": r.to_dict().get("latency_ms"), "error": r.error})
                rec.add("probe", ctx.backends.prober.name, query=f"GET {check.url}")
            entry["apps"] = apps
            impaired = {k: v for k, v in vm.status_checks.items() if v not in ("ok", "initializing", "not-applicable")}
            failing = [a["app"] for a in apps if a["ok"] is False]
            entry["verdict"] = ("DEGRADED" if failing or impaired else "HEALTHY")
            entry["reasons"] = ([f"app not responding: {', '.join(failing)}"] if failing else []) + \
                               ([f"cloud status checks: {impaired}"] if impaired else [])
        else:
            entry["verdict"] = vm.state.upper()
        if ctx.ledger:
            outages = await asyncio.to_thread(ctx.ledger.outages_in, now - timedelta(days=7), now,
                                              keys=[f"vibe:{vm.id}", *(f"vibe_app:{vm.id}/{a.name}" for a in platform.apps)],
                                              limit=10)
            if outages:
                entry["recent_outages_7d"] = [{"entity": o["key"], "started_at": iso(o["started_at"]),
                                               "ended_at": iso(o["ended_at"])} for o in outages]
        out.append(entry)
    return rec.result({"query": query, "matches": len(matches), "vms": out,
                       "runbooks": platform.runbooks})
