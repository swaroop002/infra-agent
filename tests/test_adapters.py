from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from infra_agent.backends.alerts import GrafanaAlertmanagerAlerts, OnCallAlertGroups
from infra_agent.backends.base import BackendError
from infra_agent.backends.loki import LokiLogs
from infra_agent.backends.prober import HttpProber
from infra_agent.backends.prometheus import PrometheusMetrics
from infra_agent.knowledge.schema import HealthCheck

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


@respx.mock
async def test_prometheus_query_and_range(monkeypatch):
    monkeypatch.setenv("PROM_TOKEN", "t0ken")
    route = respx.get("http://prom:9090/api/v1/query").mock(return_value=httpx.Response(200, json={
        "status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"instance": "a:9100"}, "value": [1757678400.0, "0"]}]}}))
    respx.get("http://prom:9090/api/v1/query_range").mock(return_value=httpx.Response(200, json={
        "status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[1, "1"], [2, "0"]]}]}}))
    m = PrometheusMetrics("http://prom:9090", token_env="PROM_TOKEN", grafana_url="https://g", grafana_datasource_uid="uid1")
    res = await m.query("up", NOW)
    assert res[0].value == 0.0 and res[0].labels == {"instance": "a:9100"}
    assert route.calls[0].request.headers["Authorization"] == "Bearer t0ken"
    series = await m.query_range("up", NOW - timedelta(hours=1), NOW, 60)
    assert series[0].samples == [(1.0, 1.0), (2.0, 0.0)]
    assert m.explore_url("up", NOW - timedelta(hours=1), NOW).startswith("https://g/explore?schemaVersion=1")
    await m.aclose()


@respx.mock
async def test_prometheus_error_is_backend_error():
    respx.get("http://prom:9090/api/v1/query").mock(return_value=httpx.Response(400, json={
        "status": "error", "errorType": "bad_data", "error": "parse error"}))
    m = PrometheusMetrics("http://prom:9090")
    with pytest.raises(BackendError, match="HTTP 400"):
        await m.query("up{")
    await m.aclose()


@respx.mock
async def test_loki_metric_and_logs():
    route = respx.get("http://loki:3100/loki/api/v1/query").mock(return_value=httpx.Response(200, json={
        "status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "42"]}]}}))
    respx.get("http://loki:3100/loki/api/v1/query_range").mock(return_value=httpx.Response(200, json={
        "status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"app": "x"}, "values": [["1757678400000000000", "line a"], ["1757678460000000000", "line b"]]}]}}))
    lg = LokiLogs("http://loki:3100", tenant_id="t1")
    assert (await lg.query_metric("sum(count_over_time({app=\"x\"}[1h]))", NOW))[0].value == 42
    assert route.calls[0].request.headers["X-Scope-OrgID"] == "t1"
    lines = await lg.query_logs('{app="x"}', NOW - timedelta(hours=1), NOW, limit=5)
    assert [ln.line for ln in lines] == ["line b", "line a"]
    await lg.aclose()


@respx.mock
async def test_oncall_pagination_and_mapping(monkeypatch):
    monkeypatch.setenv("ONCALL_API_TOKEN", "abc")
    page2 = "https://oncall.example.com/api/v1/alert_groups/?page=2"
    respx.get("https://oncall.example.com/api/v1/alert_groups/", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"next": None, "results": [
            {"id": "I2", "title": "Old", "state": "resolved", "created_at": "2026-09-12T10:00:00Z",
             "resolved_at": "2026-09-12T10:30:00Z"}]}))
    first = respx.get("https://oncall.example.com/api/v1/alert_groups/")
    first.mock(return_value=httpx.Response(200, json={"next": page2, "results": [
        {"id": "I1", "title": "DB down", "state": "new", "created_at": "2026-09-12T11:00:00Z",
         "labels": [{"key": {"name": "severity"}, "value": {"name": "critical"}}], "permalinks": {"web": "https://x/I1"}}]}))
    oc = OnCallAlertGroups("https://oncall.example.com")
    alerts = await oc.recent_alerts(NOW - timedelta(hours=6), NOW)
    assert [(a.id, a.state, a.severity) for a in alerts] == [("I1", "firing", "critical"), ("I2", "resolved", None)]
    assert first.calls[0].request.headers["Authorization"] == "abc"
    assert "_" in first.calls[0].request.url.params["started_at"]
    await oc.aclose()


@respx.mock
async def test_grafana_alertmanager_mapping():
    respx.get("https://grafana/api/alertmanager/grafana/api/v2/alerts").mock(return_value=httpx.Response(200, json=[
        {"labels": {"alertname": "InstanceDown", "severity": "warning"}, "annotations": {"summary": "down"},
         "startsAt": "2026-09-12T11:00:00Z", "status": {"state": "active"}, "fingerprint": "f1"},
        {"labels": {"alertname": "Muted"}, "annotations": {}, "startsAt": "2026-09-12T11:00:00Z",
         "status": {"state": "suppressed"}, "fingerprint": "f2"}]))
    g = GrafanaAlertmanagerAlerts("https://grafana")
    alerts = await g.recent_alerts(NOW - timedelta(hours=1))
    assert [(a.title, a.state) for a in alerts] == [("InstanceDown", "firing"), ("Muted", "silenced")]
    await g.aclose()


@respx.mock
async def test_http_prober():
    respx.get("https://svc/health").mock(return_value=httpx.Response(503))
    respx.get("https://svc/ok").mock(return_value=httpx.Response(200, text="status: green"))
    respx.get("https://svc/slow").mock(side_effect=httpx.ConnectTimeout("boom"))
    p = HttpProber()
    bad = await p.probe(HealthCheck(url="https://svc/health"))
    good = await p.probe(HealthCheck(url="https://svc/ok", expect_body_contains="green"))
    slow = await p.probe(HealthCheck(url="https://svc/slow", timeout_s=1))
    assert (bad.ok, bad.status_code) == (False, 503)
    assert good.ok and good.latency_ms is not None
    assert not slow.ok and "timeout" in slow.error
    await p.aclose()


def test_registry_unknown_type(demo_settings):
    from infra_agent.backends import resolve_class

    with pytest.raises(BackendError, match="unknown metrics backend"):
        resolve_class("metrics", "datadog")
    assert resolve_class("metrics", "infra_agent.backends.prometheus:PrometheusMetrics") is PrometheusMetrics
