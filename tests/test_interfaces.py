import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from infra_agent.agent import InfraAgent
from infra_agent.context import build_context
from infra_agent.interfaces.api import create_app
from infra_agent.interfaces.teams import format_alerts, parse_command, run_command
from tests.conftest import ScriptedChatModel, tool_call


@pytest.fixture
def client_factory(demo_settings):
    clients = []

    def make(responses=None, auth_mode="none"):
        demo_settings.server.auth.mode = auth_mode
        ctx = build_context(demo_settings)
        agent = InfraAgent(ctx, model=ScriptedChatModel(responses=responses or [AIMessage(content="hi")]))
        client = TestClient(create_app(demo_settings, ctx=ctx, agent=agent, start_collector=False))
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.close()


def test_meta_alerts_status(client_factory):
    with client_factory() as client:
        assert client.get("/healthz").json() == {"ok": True}
        meta = client.get("/api/meta").json()
        assert meta["agent_ready"] and meta["vibe"]
        alerts = client.get("/api/alerts", params={"window": "24h", "state": "firing"}).json()
        assert alerts["alerts"] and all(a["state"] == "firing" for a in alerts["alerts"])
        status = client.get("/api/status").json()
        assert status["fleet"]["targets"]["down"] == 3
        assert status["vibe"]["count"] == 4
        assert client.get("/api/alerts", params={"window": "bogus"}).status_code == 400
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200


def test_chat_and_stream(client_factory):
    responses = [AIMessage(content="plain answer"),
                 tool_call("get_host_status", {"host": "dev-app-01"}), AIMessage(content="it is down")]
    with client_factory(responses) as client:
        r = client.post("/api/chat", json={"message": "hello"})
        assert r.status_code == 200 and r.json()["answer"] == "plain answer"
        with client.stream("POST", "/api/chat/stream", json={"message": "is dev down?", "thread_id": "abc"}) as s:
            events = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
        assert [e["type"] for e in events][0] == "tool_start"
        assert events[-1]["type"] == "final" and events[-1]["answer"] == "it is down"
        assert events[-1]["evidence"]


def test_api_key_auth(client_factory, monkeypatch):
    monkeypatch.setenv("INFRA_AGENT_API_KEYS", "k1,k2")
    with client_factory(auth_mode="api_key") as client:
        assert client.get("/api/meta").status_code == 401
        assert client.get("/api/meta", headers={"X-API-Key": "k2"}).status_code == 200


def test_trusted_header_auth(client_factory):
    with client_factory(auth_mode="trusted_header") as client:
        assert client.get("/api/meta").status_code == 401
        assert client.get("/api/meta", headers={"X-Forwarded-Email": "sre@example.com"}).json()["user"] == "sre@example.com"


def test_parse_command():
    assert parse_command("hello there") is None
    assert parse_command("/alerts 6h") == ("alerts", ["6h"])
    assert parse_command('/health "report generation" 2h') == ("health", ["report generation", "2h"])
    assert parse_command("help") == ("help", [])


async def test_teams_commands(ctx):
    epochs = {}
    alerts = await run_command(ctx, ctx.settings, "alerts", ["24h"], epochs, "c1")
    assert "ReportGeneratorPodsCrashLooping" in alerts
    health = await run_command(ctx, ctx.settings, "health", ["report", "generation", "1h"], epochs, "c1")
    assert "DOWN" in health and "CrashLoopBackOff" in health
    downtime = await run_command(ctx, ctx.settings, "downtime", ["dev-app-01", "7d"], epochs, "c1")
    assert "Availability" in downtime and "ongoing" in downtime
    assert "new conversation" in (await run_command(ctx, ctx.settings, "reset", [], epochs, "c1")).lower()
    assert epochs["c1"] == 1
    with pytest.raises(ValueError, match="Did you mean"):
        await run_command(ctx, ctx.settings, "downtime", ["report worker"], epochs, "c1")


def test_format_alerts_handles_links():
    text = format_alerts({"window": {"duration": "1d"}, "counts_by_state": {"firing": 1},
                          "alerts": [{"title": "X", "url": "https://a", "state": "firing", "severity": "critical",
                                      "started_at": "2026-09-12T10:00:00", "source": "grafana"}]})
    assert "[X](https://a)" in text and "🔥" in text


def test_teams_mount_without_credentials_returns_503(demo_settings, monkeypatch):
    monkeypatch.delenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", raising=False)
    demo_settings.teams.enabled = True
    ctx = build_context(demo_settings)
    app = create_app(demo_settings, ctx=ctx, agent=object(), start_collector=False)
    with TestClient(app) as client:
        assert client.post("/api/messages", json={}).status_code == 503


def test_teams_mount_with_credentials_requires_jwt(demo_settings, monkeypatch):
    monkeypatch.setenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET", "not-a-real-secret")
    monkeypatch.setenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID", "00000000-0000-0000-0000-000000000002")
    demo_settings.teams.enabled = True
    ctx = build_context(demo_settings)
    app = create_app(demo_settings, ctx=ctx, agent=object(), start_collector=False)
    with TestClient(app) as client:
        # No Bot Framework JWT -> rejected before reaching the agent.
        assert client.post("/api/messages", json={"type": "message", "text": "hi"}).status_code == 401
