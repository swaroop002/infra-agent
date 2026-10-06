"""Every PromQL / LogQL query the tools issue, in one reviewable place.

Queries assume node_exporter (hosts) and kube-state-metrics (Kubernetes). If your exporters
use different metric names, change them here — tools don't embed query text.
"""

from __future__ import annotations

from .backends.base import Query
from .knowledge.schema import Cluster, Host, K8sWorkload, LogSignal, Service, render_selector


def _sel(base: dict[str, str], **extra: str) -> str:
    return render_selector({**base, **extra})


# ------------------------------------------------------------------ hosts
def host_up(host: Host) -> Query:
    return Query(f"up{_sel(host.prometheus)}", "host_up", host_id=host.id)


def host_up_range(host: Host, step_s: int) -> Query:
    # min_over_time per step: a single failed scrape inside a step marks that step as down,
    # so short outages are not skipped by coarse steps on long windows.
    return Query(f"min_over_time(up{_sel(host.prometheus)}[{step_s}s])", "host_up_range", host_id=host.id)


def host_availability(host: Host, range_literal: str) -> Query:
    return Query(f"avg_over_time(up{_sel(host.prometheus)}[{range_literal}])", "host_availability",
                 host_id=host.id)


def fleet_up(job_regex: str | None = None) -> Query:
    text = f'up{{job=~"{job_regex}"}}' if job_regex else "up"
    return Query(text, "fleet_up", job_regex=job_regex)


def target_up_range(labels: dict[str, str]) -> Query:
    return Query(f"up{_sel(labels)}", "target_up_range", labels=labels)


def host_reboots(host: Host, range_literal: str) -> Query:
    return Query(f"changes(node_boot_time_seconds{_sel(host.prometheus)}[{range_literal}])",
                 "host_reboots", host_id=host.id)


def host_resources(host: Host) -> dict[str, Query]:
    s = host.prometheus
    fs = {"fstype": "!~tmpfs|overlay|squashfs|devtmpfs|ramfs|nsfs", "mountpoint": "!~/(run|boot|snap).*"}
    return {
        "uptime_s": Query(f"time() - node_boot_time_seconds{_sel(s)}", "host_uptime_s", host_id=host.id),
        "cpu_busy_pct": Query(
            f"100 * (1 - avg(rate(node_cpu_seconds_total{_sel(s, mode='idle')}[5m])))",
            "host_cpu_busy_pct", host_id=host.id),
        "mem_available_pct": Query(
            f"100 * node_memory_MemAvailable_bytes{_sel(s)} / node_memory_MemTotal_bytes{_sel(s)}",
            "host_mem_available_pct", host_id=host.id),
        "disk_used_pct": Query(
            f"max by (mountpoint) (100 * (1 - node_filesystem_avail_bytes{_sel(s, **fs)}"
            f" / node_filesystem_size_bytes{_sel(s, **fs)}))",
            "host_disk_used_pct", host_id=host.id),
        "load1_per_core": Query(
            f"node_load1{_sel(s)} / scalar(count(node_cpu_seconds_total{_sel(s, mode='idle')}))",
            "host_load_per_core", host_id=host.id),
    }


# ------------------------------------------------------------- kubernetes
_POD_PATTERN = {"deployment": "{name}-[a-z0-9]+-[a-z0-9]+", "statefulset": "{name}-[0-9]+",
                "daemonset": "{name}-[a-z0-9]+"}


def k8s_workload(cluster: Cluster, w: K8sWorkload, range_literal: str = "1h") -> dict[str, Query]:
    c = cluster.prometheus
    ids = {"cluster_id": cluster.id, "namespace": w.namespace, "kind": w.kind, "workload": w.name}
    pods = _sel(c, namespace=w.namespace, pod="~" + _POD_PATTERN[w.kind].format(name=w.name))
    obj = {w.kind: w.name, "namespace": w.namespace}
    desired, available = {
        "deployment": ("kube_deployment_spec_replicas", "kube_deployment_status_replicas_available"),
        "statefulset": ("kube_statefulset_replicas", "kube_statefulset_status_replicas_ready"),
        "daemonset": ("kube_daemonset_status_desired_number_scheduled", "kube_daemonset_status_number_ready"),
    }[w.kind]
    return {
        "desired": Query(f"sum({desired}{_sel(c, **obj)})", "k8s_desired", **ids),
        "available": Query(f"sum({available}{_sel(c, **obj)})", "k8s_available", **ids),
        "restarts": Query(f"sum by (pod) (increase(kube_pod_container_status_restarts_total{pods}[{range_literal}])) > 0",
                          "k8s_restarts", **ids),
        "waiting": Query(f"sum by (pod, reason) (kube_pod_container_status_waiting_reason{pods}) > 0",
                         "k8s_waiting", **ids),
        "oom_killed": Query(
            f"sum by (pod) (kube_pod_container_status_last_terminated_reason{_sel(c, namespace=w.namespace, reason='OOMKilled', pod='~' + _POD_PATTERN[w.kind].format(name=w.name))}) > 0",
            "k8s_oom", **ids),
    }


def k8s_cluster(cluster: Cluster, namespace: str | None = None) -> dict[str, Query]:
    c = cluster.prometheus
    ns = {"namespace": namespace} if namespace else {}
    ids = {"cluster_id": cluster.id, "namespace": namespace}
    nodes_not_ready = f"(kube_node_status_condition{_sel(c, condition='Ready', status='true')} == 0)"
    if cluster.nodepool_label:
        nodes_not_ready = (f"{nodes_not_ready} * on (node) group_left(label_{cluster.nodepool_label}) "
                           f"kube_node_labels{_sel(c)}")
    return {
        "nodes_not_ready": Query(nodes_not_ready, "k8s_nodes_not_ready", **ids),
        "unavailable_workloads": Query(
            f"kube_deployment_status_replicas_unavailable{_sel(c, **ns)} > 0", "k8s_unavailable_deployments", **ids),
        "crashlooping_pods": Query(
            f"sum by (namespace, pod) (kube_pod_container_status_waiting_reason{_sel(c, reason='CrashLoopBackOff', **ns)}) > 0",
            "k8s_crashloop_pods", **ids),
        "pods_not_running": Query(
            f"sum by (namespace, phase) (kube_pod_status_phase{_sel(c, phase='~Pending|Failed|Unknown', **ns)}) > 0",
            "k8s_pods_bad_phase", **ids),
    }


# ------------------------------------------------------------------- logs
def _pipeline(sig: LogSignal) -> str:
    base = sig.base()
    # Metric queries fail on unparsable lines unless parse errors are filtered out.
    if sig.parser.strip():
        base += ' | __error__=""'
    return base


def log_total(svc: Service, range_literal: str) -> Query:
    sig = svc.logs
    return Query(f"sum(count_over_time({_pipeline(sig)} [{range_literal}]))", "log_total", service_id=svc.id)


def log_errors(svc: Service, range_literal: str) -> Query:
    sig = svc.logs
    return Query(f"sum(count_over_time({_pipeline(sig)} {sig.error_filter} [{range_literal}]))",
                 "log_errors", service_id=svc.id)


def log_errors_by_path(svc: Service, range_literal: str, top: int = 5) -> Query | None:
    sig = svc.logs
    if not sig.path_label:
        return None
    return Query(f"topk({top}, sum by ({sig.path_label}) (count_over_time({_pipeline(sig)} {sig.error_filter} "
                 f"[{range_literal}])))", "log_errors_by_path", service_id=svc.id)


def log_error_lines(svc: Service) -> Query:
    sig = svc.logs
    return Query(f"{sig.base()} {sig.error_filter}".strip(), "log_error_lines", service_id=svc.id)


def log_error_rate_range(svc: Service, step_literal: str) -> Query:
    sig = svc.logs
    return Query(f"sum(count_over_time({_pipeline(sig)} {sig.error_filter} [{step_literal}]))",
                 "log_errors_range", service_id=svc.id)
