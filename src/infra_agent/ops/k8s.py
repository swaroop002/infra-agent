"""Kubernetes workload and cluster health from kube-state-metrics in Prometheus."""

from __future__ import annotations

from datetime import timedelta

from .. import queries
from ..context import AgentContext
from ..knowledge.schema import Cluster, K8sWorkload
from .common import OpResult, Recorder


async def workload_status(ctx: AgentContext, workload: K8sWorkload, rec: Recorder, range_literal: str = "1h") -> dict:
    cluster = ctx.catalog.clusters.get(workload.cluster)
    m = ctx.backends.metrics
    ident = f"{workload.cluster}/{workload.namespace}/{workload.kind}/{workload.name}"
    if cluster is None or m is None:
        return {"workload": ident, "error": "cluster not in catalog or no metrics backend"}
    now = ctx.now()
    qs = queries.k8s_workload(cluster, workload, range_literal)
    res = await rec.gather(**{k: (f"k8s {k} query", m.query(q), []) for k, q in qs.items()})
    for q in qs.values():
        rec.promql(m, q, now - timedelta(hours=1), now)
    desired = res["desired"][0].value if res["desired"] else None
    available = res["available"][0].value if res["available"] else None
    out = {
        "workload": ident,
        "desired_replicas": int(desired) if desired is not None else None,
        "available_replicas": int(available) if available is not None else None,
        f"restarts_last_{range_literal}": {s.labels.get("pod", "?"): int(s.value) for s in res["restarts"]},
        "waiting_pods": [{"pod": s.labels.get("pod"), "reason": s.labels.get("reason")} for s in res["waiting"]],
        "oom_killed_pods": [s.labels.get("pod") for s in res["oom_killed"]],
    }
    issues = []
    if desired is None:
        issues.append("no kube-state-metrics series found for this workload (name/namespace/cluster labels?)")
    elif available is not None and available < desired:
        issues.append(f"{int(available)}/{int(desired)} replicas available")
    if out["waiting_pods"]:
        reasons = sorted({w["reason"] for w in out["waiting_pods"] if w["reason"]})
        issues.append(f"pods waiting: {', '.join(reasons)}")
    if out[f"restarts_last_{range_literal}"]:
        issues.append(f"{sum(out[f'restarts_last_{range_literal}'].values())} container restarts in {range_literal}")
    if out["oom_killed_pods"]:
        issues.append("containers OOMKilled")
    out["issues"] = issues
    out["state"] = ("DOWN" if desired and available == 0 else "DEGRADED" if issues and desired is not None
                    else "UNKNOWN" if desired is None else "HEALTHY")
    return out


async def cluster_status(ctx: AgentContext, cluster: Cluster, namespace: str | None = None) -> OpResult:
    rec = Recorder()
    m = ctx.backends.metrics
    now = ctx.now()
    data: dict = {"cluster": {"id": cluster.id, "name": cluster.display_name, "cloud": cluster.cloud,
                              "environment": cluster.environment}, "namespace": namespace}
    if m is None:
        rec.errors.append("no metrics backend configured")
        return rec.result(data)
    qs = queries.k8s_cluster(cluster, namespace)
    res = await rec.gather(**{k: (f"k8s {k} query", m.query(q), []) for k, q in qs.items()})
    for q in qs.values():
        rec.promql(m, q, now - timedelta(hours=1), now)
    pool_label = f"label_{cluster.nodepool_label}" if cluster.nodepool_label else None
    data["nodes_not_ready"] = [{"node": s.labels.get("node"), "nodepool": s.labels.get(pool_label) if pool_label else None}
                               for s in res["nodes_not_ready"]]
    data["unavailable_deployments"] = [{"namespace": s.labels.get("namespace"), "deployment": s.labels.get("deployment"),
                                        "unavailable": int(s.value)} for s in res["unavailable_workloads"]]
    data["crashlooping_pods"] = [f"{s.labels.get('namespace')}/{s.labels.get('pod')}" for s in res["crashlooping_pods"]]
    data["pods_not_running"] = [{"namespace": s.labels.get("namespace"), "phase": s.labels.get("phase"),
                                 "count": int(s.value)} for s in res["pods_not_running"]]
    problems = sum(len(data[k]) for k in ("nodes_not_ready", "unavailable_deployments", "crashlooping_pods",
                                          "pods_not_running"))
    data["verdict"] = "HEALTHY" if problems == 0 and not rec.errors else "DEGRADED" if problems else "UNKNOWN"
    return rec.result(data)
