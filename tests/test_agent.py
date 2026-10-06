import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from infra_agent.agent import InfraAgent
from infra_agent.agent.graph import build_grounding, trim_history
from infra_agent.agent.tools import build_tools
from tests.conftest import ScriptedChatModel, tool_call


async def test_tool_loop_answer_and_evidence(ctx):
    def final(messages):
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        payload = json.loads(tool_msgs[-1].content)
        return AIMessage(content=f"Yes, dev-app-01 is {payload['verdict']}.")

    model = ScriptedChatModel(responses=[tool_call("get_host_status", {"host": "development server"}), final])
    agent = InfraAgent(ctx, model=model)
    answer = await agent.ask("Is the development server down?", "t1")
    assert answer.text == "Yes, dev-app-01 is UNREACHABLE."
    assert answer.tools_used == ["get_host_status"]
    kinds = {e["kind"] for e in answer.evidence}
    assert {"promql", "cloud", "alerts"} <= kinds
    assert all(e["tool"] == "get_host_status" for e in answer.evidence)

    system = model.seen[0][0]
    assert isinstance(system, SystemMessage)
    assert "`dev-app-01`" in system.content and "Current time" in system.content


async def test_parallel_tool_calls(ctx):
    both = AIMessage(content="", tool_calls=[
        {"name": "get_api_error_rate", "args": {"service": "payments"}, "id": "a", "type": "tool_call"},
        {"name": "get_recent_alerts", "args": {"entity": "payments-api"}, "id": "b", "type": "tool_call"},
    ])
    model = ScriptedChatModel(responses=[both, AIMessage(content="done")])
    answer = await InfraAgent(ctx, model=model).ask("payments errors and alerts?", "t2")
    assert sorted(answer.tools_used) == ["get_api_error_rate", "get_recent_alerts"]


async def test_unresolved_entity_returns_candidates(ctx):
    model = ScriptedChatModel(responses=[tool_call("get_host_status", {"host": "report worker"}), AIMessage(content="which?")])
    agent = InfraAgent(ctx, model=model)
    await agent.ask("is the report worker ok?", "t3")
    tool_msg = [m for m in model.seen[1] if isinstance(m, ToolMessage)][0]
    payload = json.loads(tool_msg.content)
    assert "candidates" in payload and len(payload["candidates"]) >= 2


async def test_tool_budget_forces_answer(ctx):
    ctx.settings.llm.max_tool_rounds = 2
    loop = [tool_call("list_down_hosts", {}, f"c{i}") for i in range(5)]
    model = ScriptedChatModel(responses=loop)
    answer = await InfraAgent(ctx, model=model).ask("loop forever", "t4")
    assert answer.tools_used == ["list_down_hosts", "list_down_hosts"]
    assert "step limit" in answer.text
    last_prompt = model.seen[-1]
    assert "budget is used up" in last_prompt[-1].content


async def test_conversation_memory_across_turns(ctx):
    model = ScriptedChatModel(responses=[AIMessage(content="first"), AIMessage(content="second")])
    agent = InfraAgent(ctx, model=model)
    await agent.ask("hello", "same-thread")
    await agent.ask("and again", "same-thread")
    humans = [m.content for m in model.seen[1] if isinstance(m, HumanMessage)]
    assert humans == ["hello", "and again"]


async def test_stream_events(ctx):
    model = ScriptedChatModel(responses=[tool_call("check_service_health", {"service": "reports"}), AIMessage(content="It is down.")])
    agent = InfraAgent(ctx, model=model)
    events = [e async for e in agent.stream("any issues with reports?", "t5")]
    types = [e["type"] for e in events]
    assert types[0] == "tool_start" and "tool_end" in types and types[-1] == "final"
    assert events[-1]["answer"] == "It is down."


def test_trim_history_cuts_at_human_boundary():
    msgs = []
    for i in range(5):
        msgs += [HumanMessage(f"q{i}"), tool_call("x", {}, f"id{i}"), ToolMessage("r", tool_call_id=f"id{i}"), AIMessage("a")]
    trimmed = trim_history(msgs, 2)
    assert isinstance(trimmed[0], HumanMessage) and trimmed[0].content == "q3"


def test_grounding_mentions_entities_and_docs(ctx):
    g = build_grounding(ctx, "is vscode broken on alice's vibe VM? node_exporter unreachable runbook")
    assert "vibe-coding platform" in g and "runbooks/" in g


async def test_every_tool_runs_against_demo(ctx):
    """Smoke: each tool returns JSON content and a list artifact with demo backends."""
    args = {
        "find_entities": {"query": "payments"}, "get_host_status": {"host": "dev-app-01"},
        "get_host_downtime": {"host": "dev-app-01", "window": "24h"}, "list_down_hosts": {},
        "get_api_error_rate": {"service": "payments-api"}, "check_service_health": {"service": "auth-service"},
        "get_uptime_report": {"kind": "service", "window": "7d"}, "get_recent_alerts": {"window": "24h"},
        "get_kubernetes_status": {"cluster": "prod-gke", "workload": "report-generator", "namespace": "reports"},
        "list_vibe_vms": {}, "get_vibe_vm_status": {"vm": "dan"},
        "search_knowledge_base": {"query": "OOMKilled report exports"},
        "query_prometheus": {"promql": "up", "window": "1h"}, "query_loki": {"logql": '{app="x"}'},
    }
    tools = {t.name: t for t in build_tools(ctx)}
    assert set(tools) == set(args)
    for name, a in args.items():
        msg = await tools[name].ainvoke({"type": "tool_call", "id": "x", "name": name, "args": a})
        assert isinstance(msg, ToolMessage), name
        body = json.loads(msg.content.split('…"[truncated')[0]) if "truncated" not in msg.content else {}
        assert "error" not in body or name == "query_loki", (name, body)
        assert isinstance(msg.artifact, list)
