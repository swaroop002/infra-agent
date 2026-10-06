"""FastAPI app: web UI, JSON/SSE API, Teams endpoint and the in-process collector."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Settings
from ..context import AgentContext, build_context
from ..ops import alerts as alert_ops
from ..ops import hosts as host_ops
from ..ops import uptime as uptime_ops
from ..ops import vibe as vibe_ops
from ..ops.common import iso
from ..timeutil import resolve_window

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = Field(None, max_length=200)


class AppState:
    def __init__(self) -> None:
        self.ctx: AgentContext | None = None
        self.agent = None
        self.agent_error: str | None = None
        self.collector_task: asyncio.Task | None = None
        self.stop = asyncio.Event()


def create_app(settings: Settings, *, ctx: AgentContext | None = None, agent=None,
               start_collector: bool | None = None) -> FastAPI:
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from ..agent.memory import open_checkpointer

        state.ctx = ctx or build_context(settings)
        async with open_checkpointer(settings) as checkpointer:
            if agent is not None:
                state.agent = agent
            else:
                try:
                    from ..agent import InfraAgent

                    state.agent = InfraAgent(state.ctx, checkpointer=checkpointer)
                except Exception as exc:  # e.g. missing API key: keep dashboards usable
                    state.agent_error = f"{type(exc).__name__}: {exc}"
                    log.error("agent unavailable: %s", state.agent_error)
            run_collector = settings.ledger.run_in_server if start_collector is None else start_collector
            if run_collector and state.ctx.ledger is not None:
                from ..ledger.collector import Collector
                from ..notify import build_notifier

                collector = Collector(state.ctx, build_notifier(settings))
                state.collector_task = asyncio.create_task(collector.run_forever(state.stop))
            yield
            state.stop.set()
            if state.collector_task:
                await asyncio.wait([state.collector_task], timeout=10)
            if ctx is None:
                await state.ctx.aclose()

    app = FastAPI(title="Infra Agent", version="0.1.0", lifespan=lifespan)
    app.state.infra = state
    if settings.server.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.server.cors_origins,
                           allow_methods=["GET", "POST"], allow_headers=["*"])

    auth = settings.server.auth
    if auth.mode == "none":
        log.warning("server auth mode is 'none' — do not expose this instance beyond localhost")

    def current_user(request: Request) -> str:
        if auth.mode == "api_key":
            keys = [k.strip() for k in os.environ.get(auth.api_keys_env, "").split(",") if k.strip()]
            supplied = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not supplied or not any(secrets.compare_digest(supplied, k) for k in keys):
                raise HTTPException(401, "invalid API key")
            return "api-key"
        if auth.mode == "trusted_header":
            user = request.headers.get(auth.trusted_header)
            if not user:
                raise HTTPException(401, f"missing {auth.trusted_header} header (is the auth proxy in front?)")
            return user
        return "local"

    def need_ctx() -> AgentContext:
        if state.ctx is None:
            raise HTTPException(503, "starting up")
        return state.ctx

    # ------------------------------------------------------------------ web
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/meta")
    async def meta(user: str = Depends(current_user)):
        c = need_ctx()
        return {"user": user, "model": settings.llm.model, "agent_ready": state.agent is not None,
                "agent_error": state.agent_error, "catalog": c.catalog.summary(),
                "ledger": c.ledger is not None, "collector": state.collector_task is not None,
                "teams": settings.teams.enabled, "vibe": c.catalog.vibe is not None}

    # ----------------------------------------------------------------- chat
    def need_agent():
        if state.agent is None:
            raise HTTPException(503, f"agent unavailable: {state.agent_error or 'starting up'}")
        return state.agent

    def thread_for(user: str, thread_id: str | None) -> str:
        # Namespace threads by user so one user can't read another's conversation by id.
        return f"web:{user}:{thread_id or uuid.uuid4().hex}"

    @app.post("/api/chat")
    async def chat(req: ChatRequest, user: str = Depends(current_user)):
        try:
            answer = await need_agent().ask(req.message, thread_for(user, req.thread_id), user=user)
        except HTTPException:
            raise
        except Exception as exc:  # model/provider failures: surface the reason, not a bare 500
            log.exception("chat failed")
            raise HTTPException(502, f"agent failed: {type(exc).__name__}: {exc}") from exc
        return answer.to_dict()

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, user: str = Depends(current_user)):
        agent_obj = need_agent()
        thread = thread_for(user, req.thread_id)

        async def events():
            try:
                async for event in agent_obj.stream(req.message, thread, user=user):
                    yield f"data: {json.dumps(event, default=str)}\n\n"
            except Exception as exc:
                log.exception("chat stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ----------------------------------------------------------- dashboards
    @app.get("/api/alerts")
    async def get_alerts(window: str = Query("24h"), state_filter: str | None = Query(None, alias="state"),
                         search: str | None = None, user: str = Depends(current_user)):
        c = need_ctx()
        try:
            w = resolve_window(window, now=c.now(), max_duration=timedelta(days=30))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        result = await alert_ops.recent_alerts(c, w, state=state_filter, text=search, limit=200)
        return {**result.payload(), "sources": [b.name for b in c.backends.alerts]}

    @app.get("/api/status")
    async def get_status(user: str = Depends(current_user)):
        c = need_ctx()
        fleet = await host_ops.fleet_overview(c)
        services = []
        if c.ledger:
            states = {s["key"]: s for s in await asyncio.to_thread(c.ledger.states, "service")}
            now = c.now()
            for svc in c.catalog.services.values():
                st = states.get(f"service:{svc.id}")
                u = await asyncio.to_thread(c.ledger.uptime, f"service:{svc.id}", now - timedelta(hours=24), now,
                                            settings.ledger.interval_s) if st else {}
                services.append({"id": svc.id, "name": svc.display_name, "environment": svc.environment,
                                 "criticality": svc.criticality, "status": st["status"] if st else "unknown",
                                 "since": iso(st["since"]) if st else None, "detail": (st or {}).get("detail"),
                                 "uptime_24h_pct": u.get("uptime_pct")})
        vibe = None
        if c.catalog.vibe:
            vibe = (await vibe_ops.list_vibe_vms(c)).payload()
        return {"fleet": fleet.payload(), "services": services, "vibe": vibe}

    @app.get("/api/uptime")
    async def get_uptime(kind: str = "service", window: str = "7d", user: str = Depends(current_user)):
        c = need_ctx()
        if kind not in ("service", "host", "check", "vibe", "vibe_app", "target"):
            raise HTTPException(400, "unknown kind")
        try:
            w = resolve_window(window, now=c.now(), max_duration=timedelta(days=366))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return (await uptime_ops.uptime_report(c, w, kind=kind, limit=500)).payload()

    @app.get("/api/outages")
    async def get_outages(window: str = "7d", kind: str | None = None, user: str = Depends(current_user)):
        c = need_ctx()
        if c.ledger is None:
            return {"outages": [], "note": "ledger not configured"}
        try:
            w = resolve_window(window, now=c.now(), max_duration=timedelta(days=366))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        rows = await asyncio.to_thread(c.ledger.outages_in, w.start, w.end, kind=kind, limit=500)
        return {"window": w.to_dict(), "outages": [host_ops.outage_dict(o, c.now()) | {"kind": o["kind"]} for o in rows]}

    if settings.teams.enabled:
        from .teams import mount_teams

        mount_teams(app, settings, lambda: state.agent, lambda: state.ctx)
    return app


def app_from_env() -> FastAPI:
    """ASGI entry point for `uvicorn infra_agent.interfaces.api:app_from_env --factory`."""
    from ..config import load_settings

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    return create_app(load_settings())
