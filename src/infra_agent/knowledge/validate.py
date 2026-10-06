"""Live validation: does every catalog selector actually match data?"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .. import queries
from ..context import AgentContext
from ..timeutil import utcnow


@dataclass
class Check:
    entity: str
    check: str
    ok: bool
    detail: str


async def validate_live(ctx: AgentContext) -> list[Check]:
    results: list[Check] = []
    m, logs, prober = ctx.backends.metrics, ctx.backends.logs, ctx.backends.prober
    now = utcnow()

    async def run(entity: str, name: str, coro, judge):
        try:
            value = await coro
            ok, detail = judge(value)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(Check(entity, name, ok, detail))

    jobs = []
    for host in ctx.catalog.hosts.values():
        if m:
            jobs.append(run(f"host:{host.id}", "prometheus selector", m.query(queries.host_up(host)),
                            lambda v: (bool(v), f"{len(v)} series" if v else "no series match the selector")))
        if host.cloud_ref:
            inv = ctx.backends.inventory_for(host.cloud_ref.provider, host.cloud_ref.account)
            if inv:
                jobs.append(run(f"host:{host.id}", "cloud_ref", inv.get_instance(host.cloud_ref),
                                lambda v: (v.state != "not_found", v.state)))
    for svc in ctx.catalog.services.values():
        if svc.logs and logs:
            q = f"sum(count_over_time({svc.logs.selector} [1h]))"
            jobs.append(run(f"service:{svc.id}", "loki selector", logs.query_metric(q, now),
                            lambda v: (bool(v) and v[0].value > 0, f"{int(v[0].value)} lines/1h" if v else "no lines in 1h")))
            q_req = queries.log_total(svc, "1h")
            jobs.append(run(f"service:{svc.id}", "loki parser", logs.query_metric(q_req, now),
                            lambda v: (bool(v), "parses" if v else "0 parsed request lines (parser/request_filter?)")))
        for check in svc.health_checks:
            if prober:
                jobs.append(run(f"service:{svc.id}", f"health {check.name}", prober.probe(check),
                                lambda r: (r.ok, r.error or f"HTTP {r.status_code} in {r.latency_ms:.0f}ms")))
        for w in svc.kubernetes:
            cluster = ctx.catalog.clusters.get(w.cluster)
            if cluster and m:
                q = queries.k8s_workload(cluster, w)["desired"]
                jobs.append(run(f"service:{svc.id}", f"k8s {w.namespace}/{w.name}", m.query(q),
                                lambda v: (bool(v), f"desired={int(v[0].value)}" if v else "no kube-state-metrics series")))
    await asyncio.gather(*jobs)
    return sorted(results, key=lambda c: (c.ok, c.entity))

