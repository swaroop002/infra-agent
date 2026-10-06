"""Backend registry: config `type` -> adapter class.

Resolution order for a spec's `type`:
  1. built-in names below,
  2. entry points in the `infra_agent.backends` group named "<category>.<type>",
  3. a dotted path "package.module:ClassName".
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

from ..config import BackendSpec, Settings
from ..knowledge.catalog import Catalog
from .base import AlertsBackend, BackendError, InventoryBackend, LogsBackend, MetricsBackend, Prober
from .fake import BuildContext

BUILTIN: dict[str, dict[str, str]] = {
    "metrics": {
        "prometheus": "infra_agent.backends.prometheus:PrometheusMetrics",
        "fake": "infra_agent.backends.fake:FakeMetrics",
    },
    "logs": {
        "loki": "infra_agent.backends.loki:LokiLogs",
        "fake": "infra_agent.backends.fake:FakeLogs",
    },
    "alerts": {
        "grafana": "infra_agent.backends.alerts:GrafanaAlertmanagerAlerts",
        "prometheus_rules": "infra_agent.backends.alerts:PrometheusRuleAlerts",
        "oncall": "infra_agent.backends.alerts:OnCallAlertGroups",
        "fake": "infra_agent.backends.fake:FakeAlerts",
    },
    "inventory": {
        "aws": "infra_agent.backends.inventory:AwsInventory",
        "azure": "infra_agent.backends.inventory:AzureInventory",
        "gcp": "infra_agent.backends.inventory:GcpInventory",
        "fake": "infra_agent.backends.fake:FakeInventory",
    },
    "prober": {
        "http": "infra_agent.backends.prober:HttpProber",
        "fake": "infra_agent.backends.fake:FakeProber",
    },
}


def _load(target: str):
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def resolve_class(category: str, type_name: str):
    if type_name in BUILTIN.get(category, {}):
        return _load(BUILTIN[category][type_name])
    for ep in entry_points(group="infra_agent.backends"):
        if ep.name == f"{category}.{type_name}":
            return ep.load()
    if ":" in type_name:
        return _load(type_name)
    known = ", ".join(sorted(BUILTIN.get(category, {})))
    raise BackendError(f"unknown {category} backend type {type_name!r} (built-in: {known})")


def build_one(category: str, spec: BackendSpec, context: BuildContext):
    cls = resolve_class(category, spec.type)
    opts = spec.options()
    if "context" in inspect.signature(cls.__init__).parameters:
        opts["context"] = context
    try:
        return cls(**opts)
    except TypeError as exc:
        raise BackendError(f"{category} backend {spec.type!r}: bad options: {exc}") from exc


@dataclass
class Backends:
    metrics: MetricsBackend | None = None
    logs: LogsBackend | None = None
    alerts: list[AlertsBackend] = field(default_factory=list)
    inventory: list[InventoryBackend] = field(default_factory=list)
    prober: Prober | None = None

    def inventory_for(self, provider: str, name: str | None = None) -> InventoryBackend | None:
        for inv in self.inventory:
            if name and inv.name == name:
                return inv
        for inv in self.inventory:
            if not name and inv.provider == provider:
                return inv
        return None

    async def aclose(self) -> None:
        for b in [self.metrics, self.logs, self.prober, *self.alerts, *self.inventory]:
            if b is not None:
                await b.aclose()


def build_backends(settings: Settings, catalog: Catalog | None) -> Backends:
    ctx = BuildContext(base_dir=Path(settings.base_dir), catalog=catalog)
    b = settings.backends
    return Backends(
        metrics=build_one("metrics", b.metrics, ctx) if b.metrics else None,
        logs=build_one("logs", b.logs, ctx) if b.logs else None,
        alerts=[build_one("alerts", s, ctx) for s in b.alerts],
        inventory=[build_one("inventory", s, ctx) for s in b.inventory],
        prober=build_one("prober", b.prober, ctx),
    )
