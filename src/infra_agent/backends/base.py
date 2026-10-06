"""Backend interfaces. Implement these to plug in a different metrics/logs/alerts/cloud stack.

All methods are read-only by contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..knowledge.schema import CloudRef, HealthCheck


class BackendError(Exception):
    """A backend call failed (network, auth, bad query). Message is safe to show the model."""


class Query(str):
    """A query string that also carries the builder name and parameters.

    Real backends treat it as a plain string. The fake backend dispatches on `.name`, which lets
    demo/eval scenarios answer the exact queries the tools build without a PromQL engine.
    """

    name: str
    params: dict[str, Any]

    def __new__(cls, text: str, name: str = "", **params: Any) -> Query:
        obj = super().__new__(cls, text)
        obj.name = name
        obj.params = params
        return obj


# ------------------------------------------------------------------ metrics
@dataclass
class VectorSample:
    labels: dict[str, str]
    value: float
    ts: float


@dataclass
class Series:
    labels: dict[str, str]
    samples: list[tuple[float, float]]


@dataclass
class Target:
    labels: dict[str, str]
    health: str  # up | down | unknown
    last_error: str
    last_scrape: str | None
    scrape_url: str


class MetricsBackend(ABC):
    name: str = "metrics"

    @abstractmethod
    async def query(self, promql: str, at: datetime | None = None) -> list[VectorSample]: ...

    @abstractmethod
    async def query_range(self, promql: str, start: datetime, end: datetime, step_s: int) -> list[Series]: ...

    async def targets(self) -> list[Target]:
        return []

    async def label_values(self, label: str, match: str | None = None) -> list[str]:
        return []

    def explore_url(self, query: str, start: datetime, end: datetime) -> str | None:
        return None

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------- logs
@dataclass
class LogLine:
    ts: datetime
    labels: dict[str, str]
    line: str


class LogsBackend(ABC):
    name: str = "logs"

    @abstractmethod
    async def query_metric(self, logql: str, at: datetime) -> list[VectorSample]:
        """Instant evaluation of a LogQL metric query (count_over_time, rate...)."""

    @abstractmethod
    async def query_logs(self, logql: str, start: datetime, end: datetime, limit: int = 20,
                         direction: Literal["backward", "forward"] = "backward") -> list[LogLine]: ...

    async def query_range_metric(self, logql: str, start: datetime, end: datetime, step_s: int) -> list[Series]:
        return []

    async def label_names(self, start: datetime | None = None, end: datetime | None = None) -> list[str]:
        return []

    async def label_values(self, label: str, start: datetime | None = None, end: datetime | None = None) -> list[str]:
        return []

    def explore_url(self, query: str, start: datetime, end: datetime) -> str | None:
        return None

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------- alerts
@dataclass
class Alert:
    source: str
    id: str
    title: str
    state: str  # firing | resolved | acknowledged | silenced | pending
    severity: str | None
    started_at: datetime | None
    ended_at: datetime | None = None
    labels: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("started_at", "ended_at"):
            d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
        return d


class AlertsBackend(ABC):
    name: str = "alerts"

    @abstractmethod
    async def recent_alerts(self, since: datetime, until: datetime | None = None) -> list[Alert]:
        """Alerts active in, or started during, the window."""

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------- inventory
InstanceStateName = Literal["running", "stopped", "starting", "stopping", "terminated", "not_found", "unknown"]


@dataclass
class InstanceState:
    provider: str
    id: str
    name: str | None
    state: InstanceStateName
    raw_state: str
    region: str | None = None
    private_ip: str | None = None
    public_ip: str | None = None
    launched_at: datetime | None = None
    instance_type: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    # Provider health checks, e.g. AWS {"system": "ok", "instance": "impaired"}.
    status_checks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["launched_at"] = self.launched_at.isoformat(timespec="seconds") if self.launched_at else None
        return d


class InventoryBackend(ABC):
    name: str = "inventory"
    provider: str = ""

    @abstractmethod
    async def get_instance(self, ref: CloudRef) -> InstanceState: ...

    @abstractmethod
    async def list_instances(self, tag_filter: dict[str, str]) -> list[InstanceState]: ...

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------- prober
@dataclass
class ProbeResult:
    ok: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None
    checked_at: datetime
    url: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checked_at"] = self.checked_at.isoformat(timespec="seconds")
        if self.latency_ms is not None:
            d["latency_ms"] = round(self.latency_ms, 1)
        return d


class Prober(ABC):
    name: str = "prober"

    @abstractmethod
    async def probe(self, check: HealthCheck) -> ProbeResult: ...

    async def aclose(self) -> None:
        return None
