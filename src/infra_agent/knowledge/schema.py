"""Catalog schema: the structured half of the knowledge base.

Every entity maps to *explicit* selectors so answers never depend on label conventions.
See docs/knowledge-base.md for field-by-field guidance.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def render_selector(labels: dict[str, str]) -> str:
    """{"job": "node", "instance": "10.0.0.1:9100"} -> {instance="10.0.0.1:9100",job="node"}.

    Values starting with "~" are regex matches: {"instance": "~dev-.*"} -> instance=~"dev-.*".
    """
    parts = []
    for key in sorted(labels):
        value = labels[key]
        op = "="
        if value.startswith("~"):
            op, value = "=~", value[1:]
        elif value.startswith("!~"):
            op, value = "!~", value[2:]
        elif value.startswith("!"):
            op, value = "!=", value[1:]
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{key}{op}"{escaped}"')
    return "{" + ",".join(parts) + "}"


class CloudRef(_Model):
    """Where to ask the cloud API about a VM. Fill the fields for the relevant cloud."""

    provider: Literal["aws", "azure", "gcp"]
    # aws
    instance_id: str | None = None
    region: str | None = None
    account: str | None = None  # name of the inventory backend instance, if several
    # azure
    subscription_id: str | None = None
    resource_group: str | None = None
    vm_name: str | None = None
    # gcp
    project: str | None = None
    zone: str | None = None
    instance_name: str | None = None

    @model_validator(mode="after")
    def _required(self) -> CloudRef:
        needed = {"aws": ["instance_id"], "azure": ["resource_group", "vm_name"],
                  "gcp": ["project", "zone", "instance_name"]}[self.provider]
        missing = [f for f in needed if not getattr(self, f)]
        if missing:
            raise ValueError(f"cloud_ref for {self.provider} needs {', '.join(missing)}")
        return self


class HealthCheck(_Model):
    name: str = "health"
    url: str
    method: Literal["GET", "HEAD"] = "GET"
    expect_status: list[int] = Field(default_factory=lambda: [200])
    expect_body_contains: str | None = None
    timeout_s: float = 5.0
    # Header name -> environment variable holding the value (tokens stay out of git).
    headers_env: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = True

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("health check url must be http(s)")
        return v


class LogSignal(_Model):
    """How to compute request/error rates for a service from Loki."""

    selector: str  # LogQL stream selector, e.g. {namespace="payments", app="payments-api"}
    # Parser stage(s) applied before filtering. Common: "| json", "| logfmt",
    # '| pattern `<ip> - - [<_>] "<method> <path> <_>" <status> <_>`'.
    parser: str = "| json"
    # Optional line filter selecting request log lines only (e.g. '|= "http_request"').
    request_filter: str = ""
    # Label filter expression marking a failed request (after parsing).
    error_filter: str = "| status >= 500"
    # Extracted label holding the request path/route, for "top failing endpoints".
    path_label: str | None = "path"
    # Services without status codes: set error_filter to a line filter, e.g. '|~ "(?i)error|exception"'.

    @field_validator("selector")
    @classmethod
    def _selector(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("{") and v.endswith("}")):
            raise ValueError("selector must be a LogQL stream selector like {app=\"x\"}")
        return v

    def base(self) -> str:
        return " ".join(p for p in (self.selector, self.request_filter, self.parser) if p)


class K8sWorkload(_Model):
    cluster: str  # cluster id from the catalog
    namespace: str
    kind: Literal["deployment", "statefulset", "daemonset"] = "deployment"
    name: str


class SLO(_Model):
    availability_pct: float | None = None  # e.g. 99.9
    error_rate_pct: float | None = None  # max acceptable, e.g. 1.0
    latency_p95_ms: float | None = None


class AlertMatch(_Model):
    """How to relate alerts (Grafana / OnCall) to this entity."""

    labels: dict[str, str] = Field(default_factory=dict)  # all must match
    title_contains: list[str] = Field(default_factory=list)  # any matches (case-insensitive)


class _Entity(_Model):
    id: str
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    environment: str | None = None
    owner: str | None = None
    team: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    alert_match: AlertMatch | None = None
    runbooks: list[str] = Field(default_factory=list)  # paths under docs dirs

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"id {v!r} must be lowercase letters, digits, '.', '_' or '-'")
        return v

    @property
    def display_name(self) -> str:
        return self.name or self.id


class Host(_Entity):
    kind: Literal["host"] = "host"
    cloud: Literal["aws", "azure", "gcp", "onprem", "other", "unknown"] = "unknown"
    region: str | None = None
    # Labels identifying this VM's node_exporter target in Prometheus.
    prometheus: dict[str, str]
    cloud_ref: CloudRef | None = None
    services: list[str] = Field(default_factory=list)


class Service(_Entity):
    kind: Literal["service"] = "service"
    criticality: Literal["critical", "high", "medium", "low"] = "medium"
    hosts: list[str] = Field(default_factory=list)
    kubernetes: list[K8sWorkload] = Field(default_factory=list)
    health_checks: list[HealthCheck] = Field(default_factory=list)
    logs: LogSignal | None = None
    # Extra named PromQL for this service; "{window}" is replaced with a range literal.
    metrics: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    slo: SLO | None = None


class Cluster(_Entity):
    kind: Literal["cluster"] = "cluster"
    cloud: Literal["aws", "azure", "gcp", "onprem", "other", "unknown"] = "unknown"
    region: str | None = None
    # Labels that scope kube-state-metrics / cadvisor series to this cluster, e.g. {"cluster": "prod-aks"}.
    prometheus: dict[str, str] = Field(default_factory=dict)
    # kube_node_labels label carrying the nodepool name (depends on the cloud / KSM allowlist).
    nodepool_label: str | None = None
    namespaces: list[str] = Field(default_factory=list)


class VibeApp(_Model):
    name: str
    port: int
    path: str = "/"
    scheme: Literal["http", "https"] = "http"
    expect_status: list[int] = Field(default_factory=lambda: [200, 301, 302, 401, 403])
    timeout_s: float = 4.0
    verify_tls: bool = False


class VibePlatform(_Model):
    """User-provisioned development VMs (created with boto3 + Ansible)."""

    description: str = ""
    provider: Literal["aws", "azure", "gcp"] = "aws"
    inventory_backend: str | None = None  # name of the inventory backend to use
    # Cloud tag filter selecting vibe VMs, e.g. {"Purpose": "vibe-coding"}.
    tag_filter: dict[str, str]
    owner_tag: str = "Owner"
    name_tag: str = "Name"
    # Which address the prober uses: private_ip (agent inside the VPC) or public_ip.
    address: Literal["private_ip", "public_ip"] = "private_ip"
    apps: list[VibeApp] = Field(default_factory=list)
    # Optional tag written by the provisioning scripts, e.g. provision_status=failed.
    provision_status_tag: str | None = None
    runbooks: list[str] = Field(default_factory=list)


class CatalogFile(_Model):
    hosts: list[Host] = Field(default_factory=list)
    services: list[Service] = Field(default_factory=list)
    clusters: list[Cluster] = Field(default_factory=list)
    vibe: VibePlatform | None = None
    # Company vocabulary: "PG" -> "payment gateway", "RGS" -> "report-generation service".
    glossary: dict[str, str] = Field(default_factory=dict)
