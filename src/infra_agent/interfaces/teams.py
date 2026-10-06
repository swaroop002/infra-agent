"""Microsoft Teams bot via the Microsoft 365 Agents SDK.

Environment (Azure Bot registration, single-tenant app):
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<app id>
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<secret>
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<tenant id>
Messaging endpoint: https://<host>/api/messages
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Callable
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..ops import alerts as alert_ops
from ..ops import hosts as host_ops
from ..ops import services as service_ops
from ..ops import uptime as uptime_ops
from ..timeutil import resolve_window

log = logging.getLogger(__name__)
MAX_TEAMS_CHARS = 20000

HELP = """**Infra Agent**: ask anything in plain English, e.g. *is the development server down?*

Commands (no AI involved, instant):
- `/alerts [window]`: active and recent alerts (default 24h)
- `/status`: down servers, open outages, service status
- `/downtime <host> [window]`: VM outage history (default 24h)
- `/health <service> [window]`: composite service health (default 1h)
- `/uptime [window]`: service uptime league table (default 7d)
- `/reset`: start a new conversation
"""

_SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "warning": "🟡", "info": "🔵"}
_STATE_ICON = {"firing": "🔥", "acknowledged": "👀", "resolved": "✅", "silenced": "🔕", "pending": "⏳"}


def parse_command(text: str) -> tuple[str, list[str]] | None:
    text = text.strip()
    if text.lower() in ("help", "?"):
        return "help", []
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text[1:])
    except ValueError:
        parts = text[1:].split()
    if not parts:
        return "help", []
    return parts[0].lower(), parts[1:]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEAMS_CHARS else text[:MAX_TEAMS_CHARS] + "\n\n…(truncated)"


def format_alerts(payload: dict) -> str:
    alerts = payload.get("alerts", [])
    counts = ", ".join(f"{n} {s}" for s, n in payload.get("counts_by_state", {}).items()) or "none"
    lines = [f"**Alerts, last {payload['window']['duration']}**: {counts}"]
    for a in alerts[:25]:
        icon = _STATE_ICON.get(a["state"], "•")
        sev = _SEVERITY_ICON.get((a.get("severity") or "").lower(), "")
        title = f"[{a['title']}]({a['url']})" if a.get("url") else a["title"]
        started = (a.get("started_at") or "")[:16].replace("T", " ")
        lines.append(f"- {icon} {sev} **{title}** · {a['state']} · since {started} UTC · _{a['source']}_")
    if payload.get("data_errors"):
        lines.append(f"\n⚠️ {'; '.join(payload['data_errors'])}")
    return "\n".join(lines)


def format_status(fleet: dict, services: list[dict]) -> str:
    lines = []
    t = fleet.get("targets")
    if t:
        lines.append(f"**Servers**: {t['total'] - t['down']}/{t['total']} targets up")
        for d in fleet.get("down_targets", []):
            env = f" ({d['environment']})" if d.get("environment") else ""
            lines.append(f"- 🔴 {d['name']}{env}")
    if fleet.get("open_outages"):
        lines.append("\n**Open outages**")
        for o in fleet["open_outages"][:20]:
            lines.append(f"- {o['name']}: {o['duration']} · {o['detail']}")
    if services:
        lines.append("\n**Services**")
        icon = {"up": "🟢", "down": "🔴", "unknown": "⚪"}
        for s in services:
            up = f" · 24h uptime {s['uptime_24h_pct']}%" if s.get("uptime_24h_pct") is not None else ""
            lines.append(f"- {icon.get(s['status'], '⚪')} {s['name']}{up}")
    return "\n".join(lines) or "No status data available."


def format_answer(answer: dict) -> str:
    text = answer["answer"] or "_(no answer)_"
    links = [e for e in answer.get("evidence", []) if e.get("url")][:5]
    if links:
        text += "\n\n**Sources**: " + " · ".join(f"[{e['kind']}: {e.get('tool', '')}]({e['url']})" for e in links)
    return _clip(text)


def mount_teams(app: FastAPI, settings: Settings, get_agent: Callable, get_ctx: Callable) -> None:
    try:
        from microsoft_agents.activity import ActivityTypes, load_configuration_from_env
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.hosting.core import (
            AgentApplication,
            Authorization,
            MemoryStorage,
            MessageFactory,
            TurnContext,
            TurnState,
        )
        from microsoft_agents.hosting.fastapi import CloudAdapter, jwt_authorization_decorator, start_agent_process
    except ImportError:
        log.error("Teams enabled but the Agents SDK is missing: pip install 'infra-agent[teams]'")
        _mount_unavailable(app, "Teams SDK not installed")
        return

    client_id = os.environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID")
    if not client_id:
        log.error("Teams enabled but CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID is not set")
        _mount_unavailable(app, "Teams bot credentials not configured")
        return

    sdk_config = load_configuration_from_env(os.environ)
    storage = MemoryStorage()
    connections = MsalConnectionManager(**sdk_config)
    adapter = CloudAdapter(connection_manager=connections)
    authorization = Authorization(storage, connections, **sdk_config)
    bot = AgentApplication[TurnState](
        storage=storage, adapter=adapter, authorization=authorization, bot_app_id=client_id,
        long_running_messages=settings.teams.long_running_messages, **sdk_config)
    app.state.agent_configuration = connections.get_default_connection_configuration()
    thread_epochs: dict[str, int] = {}

    async def reply(context: TurnContext, markdown: str) -> None:
        activity = MessageFactory.text(_clip(markdown))
        activity.text_format = "markdown"
        await context.send_activity(activity)

    @bot.activity(ActivityTypes.conversation_update)
    async def on_members_added(context: TurnContext, _state: TurnState):
        for member in context.activity.members_added or []:
            if member.id != context.activity.recipient.id:
                await reply(context, HELP)

    @bot.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, _state: TurnState):
        text = re.sub(r"<at>.*?</at>", "", context.activity.text or "").strip()
        conv = context.activity.conversation.id
        sender = context.activity.from_property
        user = (sender.aad_object_id or sender.id or "teams-user") if sender else "teams-user"
        ctx = get_ctx()
        try:
            cmd = parse_command(text)
            if cmd is None:
                agent = get_agent()
                if agent is None:
                    await reply(context, "The AI agent is unavailable right now; commands still work. Try `/help`.")
                    return
                thread = f"teams:{conv}:{thread_epochs.get(conv, 0)}"
                answer = await agent.ask(text, thread, user=user)
                await reply(context, format_answer(answer.to_dict()))
                return
            await reply(context, await run_command(ctx, settings, cmd[0], cmd[1], thread_epochs, conv))
        except ValueError as exc:
            await reply(context, f"⚠️ {exc}")
        except Exception as exc:
            log.exception("Teams message failed")
            await reply(context, f"⚠️ Something went wrong: {type(exc).__name__}. The error has been logged.")

    @app.post("/api/messages", include_in_schema=False)
    @jwt_authorization_decorator
    async def messages(request: Request):
        return await start_agent_process(request, bot, adapter)


async def run_command(ctx, settings: Settings, name: str, args: list[str], thread_epochs: dict, conv: str) -> str:
    now = ctx.now()
    if name == "help":
        return HELP
    if name == "reset":
        thread_epochs[conv] = thread_epochs.get(conv, 0) + 1
        return "Started a new conversation."
    if name == "alerts":
        w = resolve_window(args[0] if args else "24h", now=now, max_duration=timedelta(days=30))
        return format_alerts((await alert_ops.recent_alerts(ctx, w)).payload())
    if name == "status":
        fleet = (await host_ops.fleet_overview(ctx)).payload()
        services = []
        if ctx.ledger:
            for st in ctx.ledger.states("service"):
                svc = ctx.catalog.services.get(st["entity_id"])
                u = ctx.ledger.uptime(st["key"], now - timedelta(hours=24), now, settings.ledger.interval_s)
                services.append({"name": svc.display_name if svc else st["name"], "status": st["status"],
                                 "uptime_24h_pct": u.get("uptime_pct")})
        return format_status(fleet, services)
    if name == "downtime":
        target, window = _split_window(args)
        if not target:
            raise ValueError("usage: /downtime <host> [window]")
        host, candidates = ctx.catalog.resolve_one(target, "host")
        if host is None:
            raise ValueError(f"unknown host {target!r}. Did you mean: {', '.join(c.id for c in candidates) or 'n/a'}")
        w = resolve_window(window or "24h", now=now)
        data = (await host_ops.host_downtime(ctx, host, w)).payload()
        p = data.get("prometheus") or {}
        lines = [f"**{host.display_name}**: downtime over {w.to_dict()['duration']}"]
        if p.get("availability_pct") is not None:
            lines.append(f"Availability {p['availability_pct']}% · {p['outage_count']} outage(s) · "
                         f"total {p['downtime_total']} · reboots {p.get('reboots', 'n/a')}")
        for o in p.get("outages", [])[:10]:
            end = "ongoing" if o["ongoing"] else o["end"][11:16]
            lines.append(f"- {o['start'][:16].replace('T', ' ')} → {end} ({o['duration']})")
        led = data.get("ledger") or {}
        if led.get("uptime_pct") is not None:
            lines.append(f"Ledger uptime {led['uptime_pct']}% (coverage {led['coverage_pct']}%)")
        return "\n".join(lines)
    if name == "health":
        target, window = _split_window(args)
        if not target:
            raise ValueError("usage: /health <service> [window]")
        svc, candidates = ctx.catalog.resolve_one(target, "service")
        if svc is None:
            raise ValueError(f"unknown service {target!r}. Did you mean: {', '.join(c.id for c in candidates) or 'n/a'}")
        w = resolve_window(window or "1h", now=now)
        data = (await service_ops.service_health(ctx, svc, w)).payload()
        icon = {"HEALTHY": "🟢", "DEGRADED": "🟠", "DOWN": "🔴"}.get(data["verdict"], "⚪")
        lines = [f"{icon} **{svc.display_name}**: {data['verdict']}"]
        lines += [f"- {r}" for r in data["reasons"]]
        if data.get("runbooks"):
            lines.append("Runbooks: " + ", ".join(data["runbooks"]))
        return "\n".join(lines)
    if name == "uptime":
        w = resolve_window(args[0] if args else "7d", now=now, max_duration=timedelta(days=366))
        data = (await uptime_ops.uptime_report(ctx, w, kind="service")).payload()
        lines = [f"**Service uptime, last {w.to_dict()['duration']}**"]
        for r in data.get("entities", []):
            lines.append(f"- {r['name']}: {r['uptime_pct']}% · {r['outages']} outage(s) · down {r['downtime']}")
        if data.get("note"):
            lines.append(data["note"])
        return "\n".join(lines)
    return f"Unknown command `/{name}`.\n\n{HELP}"


def _split_window(args: list[str]) -> tuple[str, str | None]:
    """"report generation 6h" -> ("report generation", "6h")."""
    args = list(args)
    window = args.pop() if len(args) > 1 and re.fullmatch(r"\d+[smhdw]", args[-1]) else None
    return " ".join(args), window


def _mount_unavailable(app: FastAPI, reason: str) -> None:
    @app.post("/api/messages", include_in_schema=False)
    async def messages_unavailable():
        return JSONResponse({"error": reason}, status_code=503)
