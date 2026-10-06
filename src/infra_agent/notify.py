"""Optional outbound notifications for ledger transitions (off by default).

Grafana OnCall stays the paging system; this is an FYI feed for a Teams channel via a
Power Automate Workflows "When a Teams webhook request is received" trigger.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from .ledger.store import Transition

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def transition(self, t: Transition) -> None: ...


class TeamsWorkflowNotifier:
    def __init__(self, url: str):
        self._url = url
        self._client = httpx.AsyncClient(timeout=10)

    async def transition(self, t: Transition) -> None:
        down = t.new_status == "down"
        title = f"{'🔴 DOWN' if down else '🟢 RECOVERED' if t.old_status == 'down' else t.new_status.upper()}: {t.name}"
        facts = [{"title": "Entity", "value": t.key}, {"title": "At", "value": t.at.isoformat(timespec="seconds")}]
        if t.detail:
            facts.append({"title": "Detail", "value": t.detail[:400]})
        card = {
            "type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True,
                      "color": "Attention" if down else "Good"},
                     {"type": "FactSet", "facts": facts}],
        }
        payload = {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
        try:
            resp = await self._client.post(self._url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Teams notification failed: %s", exc)


def build_notifier(settings) -> Notifier | None:
    env = settings.notifications.teams_workflow_url_env
    if env and os.environ.get(env):
        return TeamsWorkflowNotifier(os.environ[env])
    return None

