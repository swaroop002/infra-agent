import httpx
import respx
import yaml

from infra_agent.backends import Backends
from infra_agent.backends.loki import LokiLogs
from infra_agent.backends.prometheus import PrometheusMetrics
from infra_agent.knowledge.catalog import Catalog
from infra_agent.knowledge.discover import discover


def _vector(results):
    return {"status": "success", "data": {"resultType": "vector", "result": results}}


@respx.mock
async def test_discover_writes_valid_drafts(tmp_path):
    respx.get("http://prom/api/v1/targets").mock(return_value=httpx.Response(200, json={"status": "success", "data": {
        "activeTargets": [
            {"labels": {"job": "node", "instance": "10.0.0.1:9100", "env": "prod"}, "health": "up",
             "lastError": "", "scrapeUrl": "http://10.0.0.1:9100/metrics"},
            {"labels": {"job": "node", "instance": "10.0.0.2:9100"}, "health": "down",
             "lastError": "timeout", "scrapeUrl": "http://10.0.0.2:9100/metrics"},
            {"labels": {"job": "kube-state-metrics", "instance": "ksm:8080"}, "health": "up",
             "lastError": "", "scrapeUrl": "http://ksm:8080/metrics"},
        ]}}))

    def prom_query(request):
        q = request.url.params["query"]
        if q == "node_uname_info":
            return httpx.Response(200, json=_vector([{"metric": {"instance": "10.0.0.1:9100", "nodename": "Web-01"}, "value": [1, "1"]}]))
        if q.startswith("count by (cluster, namespace, deployment)"):
            return httpx.Response(200, json=_vector([
                {"metric": {"cluster": "prod-aks", "namespace": "payments", "deployment": "payments-api"}, "value": [1, "1"]},
                {"metric": {"cluster": "prod-aks", "namespace": "kube-system", "deployment": "coredns"}, "value": [1, "1"]}]))
        if q == "kube_node_labels":
            return httpx.Response(200, json=_vector([{"metric": {"node": "n1", "label_agentpool": "sys"}, "value": [1, "1"]}]))
        return httpx.Response(200, json=_vector([]))

    respx.get("http://prom/api/v1/query").mock(side_effect=prom_query)
    respx.get("http://loki/loki/api/v1/labels").mock(return_value=httpx.Response(200, json={"status": "success", "data": ["app", "namespace"]}))
    respx.get("http://loki/loki/api/v1/label/app/values").mock(return_value=httpx.Response(200, json={"status": "success", "data": ["payments-api"]}))
    respx.get("http://loki/loki/api/v1/query_range").mock(return_value=httpx.Response(200, json={"status": "success", "data": {
        "resultType": "streams", "result": [{"stream": {"app": "payments-api"}, "values": [
            ["1757678400000000000", '{"status": 200, "path": "/v1/charges"}'],
            ["1757678401000000000", '{"status": 502, "path": "/v1/charges"}']]}]}}))

    backends = Backends(metrics=PrometheusMetrics("http://prom"), logs=LokiLogs("http://loki"))
    out = await discover(backends, tmp_path)
    await backends.aclose()

    hosts = yaml.safe_load((out / "hosts.yaml").read_text())["hosts"]
    assert [h["id"] for h in hosts] == ["web-01", "10.0.0.2"]
    assert hosts[0]["environment"] == "prod"
    services = yaml.safe_load((out / "services.yaml").read_text())["services"]
    assert [s["id"] for s in services] == ["payments-api"]  # kube-system skipped, loki merged into k8s entry
    assert services[0]["logs"]["error_filter"] == "| status >= 500"
    clusters = yaml.safe_load((out / "clusters.yaml").read_text())["clusters"]
    assert clusters[0]["nodepool_label"] == "agentpool"
    report = (out / "REPORT.md").read_text()
    assert "Label recommendations" in report and "| node | 2 | 1 |" in report

    # Drafts must be valid catalog entries once promoted.
    promoted = tmp_path / "promoted"
    promoted.mkdir()
    for name in ("hosts.yaml", "services.yaml", "clusters.yaml"):
        (promoted / name).write_text((out / name).read_text())
    catalog = Catalog.load([promoted])
    assert catalog.services["payments-api"].kubernetes[0].cluster == "prod-aks"
