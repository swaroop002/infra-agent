"""Alerts across Grafana / Prometheus / OnCall, and relating alerts to catalog entities."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from ..backends.base import Alert
from ..context import AgentContext
from ..knowledge.catalog import Entity
from ..knowledge.schema import Host
from ..timeutil import Window
from .common import Recorder

_ENTITY_LABELS = ("service", "service_name", "app", "app_kubernetes_io_name", "job", "instance", "host",
                  "hostname", "nodename", "deployment", "container", "vm", "server")
_STATE_ORDER = {"firing": 0, "pending": 1, "acknowledged": 2, "silenced": 3, "resolved": 4}


def alert_matches(alert: Alert, entity: Entity) -> bool:
    am = entity.alert_match
    if am and (am.labels or am.title_contains):
        if am.labels and not all(alert.labels.get(k) == v for k, v in am.labels.items()):
            return False
        if am.title_contains:
            title = alert.title.lower()
            return any(t.lower() in title for t in am.title_contains)
        return True
    names = {entity.id.lower(), entity.display_name.lower(), *(a.lower() for a in entity.aliases)}
    if isinstance(entity, Host):
        inst = entity.prometheus.get("instance")
        if inst:
            names.update({inst.lower(), inst.split(":")[0].lower()})
    for key in _ENTITY_LABELS:
        value = alert.labels.get(key, "").lower()
        if value and (value in names or value.split(":")[0] in names):
            return True
    text = f"{alert.title} {alert.summary}".lower()
    return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text) for n in names if len(n) >= 4)


async def fetch_alerts(ctx: AgentContext, window: Window, rec: Recorder) -> list[Alert]:
    sources = ctx.backends.alerts
    if not sources:
        rec.errors.append("no alert sources configured")
        return []
    results = await asyncio.gather(*(rec.call(f"alerts from {s.name}", s.recent_alerts(window.start, window.end), [])
                                     for s in sources))
    for s in sources:
        rec.add("alerts", s.name, start=window.start.isoformat(timespec="seconds"),
                end=window.end.isoformat(timespec="seconds"))
    alerts = [a for batch in results for a in batch]
    return sort_alerts(alerts)


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    def key(a: Alert):
        started = a.started_at.timestamp() if a.started_at else 0
        return (_STATE_ORDER.get(a.state, 5), -started)
    return sorted(alerts, key=key)


def in_window(alert: Alert, start: datetime) -> bool:
    return alert.state in ("firing", "pending", "acknowledged") or not alert.ended_at or alert.ended_at >= start


async def recent_alerts(ctx: AgentContext, window: Window, *, state: str | None = None,
                        text: str | None = None, entity: Entity | None = None, limit: int = 50):
    rec = Recorder()
    alerts = await fetch_alerts(ctx, window, rec)
    alerts = [a for a in alerts if in_window(a, window.start)]
    if state:
        wanted = {s.strip() for s in state.split(",")}
        alerts = [a for a in alerts if a.state in wanted]
    if entity:
        alerts = [a for a in alerts if alert_matches(a, entity)]
    if text:
        t = text.lower()
        alerts = [a for a in alerts if t in f"{a.title} {a.summary} {' '.join(a.labels.values())}".lower()]
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a.state] = counts.get(a.state, 0) + 1
    return rec.result({
        "window": window.to_dict(),
        "counts_by_state": counts,
        "alerts": [_compact(a) for a in alerts[:limit]],
        "truncated": len(alerts) > limit,
    })


def _compact(a: Alert) -> dict:
    d = a.to_dict()
    d["labels"] = {k: v for k, v in a.labels.items() if not k.startswith("__")}
    d["summary"] = (a.summary or "")[:300]
    return d
