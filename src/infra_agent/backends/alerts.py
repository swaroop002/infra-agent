"""Alert sources: Grafana Alertmanager, Prometheus rules, Grafana OnCall / Cloud IRM."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from ._http import build_client, get_json
from .base import Alert, AlertsBackend, BackendError


def _dt(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GrafanaAlertmanagerAlerts(AlertsBackend):
    """Currently active alerts from Grafana's Alertmanager API.

    `alertmanager` is "grafana" for Grafana-managed alerts, or the UID of an external
    Alertmanager data source.
    """

    def __init__(self, url: str, *, alertmanager: str = "grafana", name: str = "grafana", **client_opts):
        self.name = name
        self._am = alertmanager
        self._base = url.rstrip("/")
        self._client = build_client(url, **client_opts)

    async def recent_alerts(self, since: datetime, until: datetime | None = None) -> list[Alert]:
        path = f"/api/alertmanager/{self._am}/api/v2/alerts"
        data = await get_json(self._client, path, {"active": "true", "silenced": "true", "inhibited": "true"},
                              "Grafana alerts")
        alerts = []
        for a in data or []:
            labels = a.get("labels", {})
            ann = a.get("annotations", {})
            status = (a.get("status") or {}).get("state", "active")
            alerts.append(Alert(
                source=self.name,
                id=a.get("fingerprint", ""),
                title=labels.get("alertname", "alert"),
                state="firing" if status == "active" else "silenced",
                severity=labels.get("severity"),
                started_at=_dt(a.get("startsAt")),
                ended_at=None,
                labels=labels,
                summary=ann.get("summary") or ann.get("description", ""),
                url=a.get("generatorURL") or None,
            ))
        return alerts

    async def aclose(self) -> None:
        await self._client.aclose()


class PrometheusRuleAlerts(AlertsBackend):
    """Firing/pending alerts evaluated by Prometheus itself (/api/v1/alerts)."""

    def __init__(self, url: str, *, name: str = "prometheus-rules", **client_opts):
        self.name = name
        self._client = build_client(url, **client_opts)

    async def recent_alerts(self, since: datetime, until: datetime | None = None) -> list[Alert]:
        payload = await get_json(self._client, "/api/v1/alerts", None, "Prometheus alerts")
        if payload.get("status") != "success":
            raise BackendError(f"Prometheus alerts error: {payload.get('error')}")
        out = []
        for a in payload["data"].get("alerts", []):
            labels = a.get("labels", {})
            ann = a.get("annotations", {})
            out.append(Alert(self.name, f"{labels.get('alertname')}:{sorted(labels.items())}",
                             labels.get("alertname", "alert"), a.get("state", "firing"),
                             labels.get("severity"), _dt(a.get("activeAt")), None, labels,
                             ann.get("summary") or ann.get("description", "")))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


class OnCallAlertGroups(AlertsBackend):
    """Grafana OnCall (OSS) / Grafana Cloud IRM alert groups via the public API.

    `url` is the OnCall API base, e.g. https://oncall.example.com or the IRM API URL shown in
    Grafana Cloud. Token is sent as a raw Authorization header (OnCall convention).
    """

    def __init__(self, url: str, *, token_env: str = "ONCALL_API_TOKEN", max_pages: int = 10,
                 name: str = "oncall", **client_opts):
        self.name = name
        self._max_pages = max_pages
        client_opts.setdefault("token_scheme", "")
        self._client = build_client(url, token_env=token_env, **client_opts)

    async def recent_alerts(self, since: datetime, until: datetime | None = None) -> list[Alert]:
        params: dict[str, str] | None = {"started_at": _range(since, until)}
        path = "/api/v1/alert_groups/"
        out: list[Alert] = []
        for _ in range(self._max_pages):
            data = await get_json(self._client, path, params, "OnCall alert groups")
            for g in data.get("results", []):
                created = _dt(g.get("created_at"))
                resolved = _dt(g.get("resolved_at"))
                if created and created < since and (resolved and resolved < since):
                    continue
                labels = _oncall_labels(g.get("labels"))
                state = g.get("state", "new")
                out.append(Alert(
                    source=self.name,
                    id=str(g.get("id")),
                    title=g.get("title") or "alert group",
                    state={"new": "firing"}.get(state, state),
                    severity=labels.get("severity"),
                    started_at=created,
                    ended_at=resolved,
                    labels=labels,
                    summary=f"{g.get('alerts_count', 0)} alerts",
                    url=(g.get("permalinks") or {}).get("web"),
                ))
            nxt = data.get("next")
            if not nxt:
                break
            # `next` is absolute; httpx does not merge base_url into absolute URLs.
            if not urlparse(nxt).scheme:
                break
            path, params = nxt, None
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


def _range(since: datetime, until: datetime | None) -> str:
    fmt = "%Y-%m-%dT%H:%M:%S"
    end = until or datetime.now(since.tzinfo)
    return f"{since.strftime(fmt)}_{end.strftime(fmt)}"


def _oncall_labels(raw) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    labels = {}
    for item in raw or []:
        key = (item.get("key") or {}).get("name")
        value = (item.get("value") or {}).get("name")
        if key:
            labels[key] = value or ""
    return labels
