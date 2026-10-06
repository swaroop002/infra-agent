import pytest

from infra_agent.ops import alerts as alert_ops
from infra_agent.ops import hosts, services, vibe
from infra_agent.ops.hosts import host_verdict
from infra_agent.timeutil import resolve_window


@pytest.mark.parametrize("up,cloud,resources,verdict", [
    (1.0, None, {}, "UP"),
    (1.0, "running", {"disk_used_pct": {"/": 95.0}}, "DEGRADED"),
    (0.0, "running", {}, "UNREACHABLE"),
    (0.0, "stopped", {}, "DOWN"),
    (0.0, "terminated", {}, "GONE"),
    (0.0, None, {}, "DOWN"),
    (None, "running", {}, "NOT_MONITORED"),
    (None, None, {}, "UNKNOWN"),
])
def test_host_verdict_matrix(up, cloud, resources, verdict):
    assert host_verdict(up, cloud, resources)[0] == verdict


async def test_dev_server_unreachable(ctx):
    r = await hosts.host_status(ctx, ctx.catalog.hosts["dev-app-01"])
    d = r.payload()
    assert d["verdict"] == "UNREACHABLE"
    assert d["down_since"]["since"]
    assert any(a["title"] == "InstanceDown" for a in d["related_alerts"])
    assert any(e.kind == "promql" for e in r.evidence)


async def test_stopped_worker_and_degraded_staging(ctx):
    worker = (await hosts.host_status(ctx, ctx.catalog.hosts["prod-report-worker-02"])).payload()
    assert worker["verdict"] == "DOWN" and "stopped" in worker["reasons"][0]
    staging = (await hosts.host_status(ctx, ctx.catalog.hosts["stg-app-01"])).payload()
    assert staging["verdict"] == "DEGRADED" and "disk / at 93%" in staging["reasons"][0]


async def test_host_downtime_week(ctx):
    d = (await hosts.host_downtime(ctx, ctx.catalog.hosts["dev-app-01"], resolve_window("7d"))).payload()
    p = d["prometheus"]
    assert p["currently_down"] and p["outage_count"] == 3
    assert 98 < p["availability_pct"] < 99.5


async def test_payments_error_rate(ctx):
    d = (await services.api_errors(ctx, ctx.catalog.services["payments-api"], resolve_window("24h"))).payload()
    assert d["error_rate_pct"] == pytest.approx(2.7, abs=0.01)
    assert d["previous_window"]["error_rate_pct"] == pytest.approx(0.4, abs=0.01)
    assert d["top_failing_paths"][0]["path"] == "/v1/charges"
    assert d["within_slo"] is False


async def test_report_generation_down(ctx):
    d = (await services.service_health(ctx, ctx.catalog.services["report-generation"], resolve_window("1h"))).payload()
    assert d["verdict"] == "DOWN"
    joined = " ".join(d["reasons"])
    assert "CrashLoopBackOff" in joined and "OOMKilled" in joined and "prod-report-worker-02" in joined
    assert d["api_errors"]["top_failing_paths"][0]["path"] == "/v2/reports/export"


async def test_service_without_signals_is_unknown(ctx):
    from infra_agent.knowledge.schema import Service

    d = (await services.service_health(ctx, Service(id="lonely"), resolve_window("1h"))).payload()
    assert d["verdict"] == "UNKNOWN"


async def test_fleet_overview_includes_uncatalogued(ctx):
    d = (await hosts.fleet_overview(ctx)).payload()
    names = {t["name"] for t in d["down_targets"]}
    assert names == {"dev-app-01", "prod-report-worker-02", "10.40.0.9:9100"}


async def test_vibe_status(ctx):
    alice = (await vibe.vibe_vm_status(ctx, "alice")).payload()["vms"][0]
    assert alice["verdict"] == "DEGRADED"
    assert {a["app"]: a["ok"] for a in alice["apps"]} == {"vscode": True, "deployments-manager": False}
    carol = (await vibe.vibe_vm_status(ctx, "vibe-carol-02")).payload()["vms"][0]
    assert "impaired" in " ".join(carol["reasons"])
    listing = (await vibe.list_vibe_vms(ctx, state="stopped")).payload()
    assert listing["count"] == 1 and listing["vms"][0]["owner"] == "bob@example.com"


async def test_alert_filters(ctx):
    w = resolve_window("24h")
    active = (await alert_ops.recent_alerts(ctx, w, state="firing,acknowledged")).payload()
    assert all(a["state"] in ("firing", "acknowledged") for a in active["alerts"])
    related = (await alert_ops.recent_alerts(ctx, w, entity=ctx.catalog.services["payments-api"])).payload()
    assert [a["title"] for a in related["alerts"]] == ["[prod] payments-api 5xx rate above 2%"]


async def test_backend_failure_is_reported_not_raised(ctx):
    from infra_agent.backends.base import BackendError

    async def boom(*a, **k):
        raise BackendError("prometheus unreachable")

    ctx.backends.metrics.query = boom
    d = (await hosts.host_status(ctx, ctx.catalog.hosts["dev-app-01"])).payload()
    assert d["verdict"] == "NOT_MONITORED"  # cloud says running, no Prometheus data
    assert any("prometheus unreachable" in e for e in d["data_errors"])
