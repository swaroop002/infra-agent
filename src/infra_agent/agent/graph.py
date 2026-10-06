"""The LangGraph agent: ground → agent ⇄ tools → finalize."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from ..context import AgentContext
from ..timeutil import human_duration
from .llm import bind_model
from .prompts import BUDGET_EXHAUSTED, render_system_prompt
from .tools import TOOL_LABELS, build_tools

_VIBE_HINT = ("vibe", "vscode", "vs code", "code-server", "code server", "workspace", "deployments manager", "devbox")


class AgentState(MessagesState):
    grounding: str
    tool_rounds: int
    evidence: list[dict]


@dataclass
class AgentAnswer:
    text: str
    evidence: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"answer": self.text, "evidence": self.evidence, "tools_used": self.tools_used,
                "duration_s": round(self.duration_s, 2)}


def _last_human_index(messages: list[AnyMessage]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i
    return 0


def trim_history(messages: list[AnyMessage], max_turns: int) -> list[AnyMessage]:
    """Keep the last `max_turns` user turns, always cutting at a HumanMessage boundary."""
    humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(humans) <= max_turns:
        return list(messages)
    return list(messages[humans[-max_turns]:])


def build_grounding(ctx: AgentContext, question: str) -> str:
    cat = ctx.catalog
    lines: list[str] = []
    for m in cat.resolve(question, limit=6):
        e = cat.get(m.kind, m.id)
        extra = []
        if e.environment:
            extra.append(f"env={e.environment}")
        if m.kind == "host":
            extra.append(f"cloud={e.cloud}")
            if e.services:
                extra.append(f"runs={','.join(e.services)}")
        elif m.kind == "service":
            signals = []
            if e.health_checks:
                signals.append("health-checks")
            if e.logs:
                signals.append("loki-logs")
            if e.kubernetes:
                signals.append("k8s:" + ",".join(f"{w.cluster}/{w.namespace}/{w.name}" for w in e.kubernetes))
            if e.hosts:
                signals.append("hosts:" + ",".join(e.hosts))
            extra.append("signals=" + (" ".join(signals) or "none"))
        desc = f" — {e.description[:120]}" if e.description else ""
        lines.append(f"- {m.kind} `{m.id}` ({e.display_name}; matched '{m.matched_on}'; {'; '.join(extra)}){desc}")
    if cat.vibe and any(h in question.lower() for h in _VIBE_HINT):
        lines.append(f"- vibe-coding platform on {cat.vibe.provider}; apps: {', '.join(a.name for a in cat.vibe.apps)} "
                     "(use list_vibe_vms / get_vibe_vm_status)")
    hits = [h for h in ctx.docs.search(question, limit=3) if h.score > 1.0]
    if hits:
        lines.append("- Possibly relevant docs: " + "; ".join(f"{h.source} › {h.heading}" for h in hits))
    return "\n".join(lines)


class InfraAgent:
    def __init__(self, ctx: AgentContext, *, model: BaseChatModel | None = None, checkpointer=None):
        self.ctx = ctx
        self.tools = build_tools(ctx)
        self.model = bind_model(ctx.settings.llm, self.tools, model=model)
        if checkpointer is None:
            from langgraph.checkpoint.memory import InMemorySaver

            checkpointer = InMemorySaver()
        self.graph = self._compile(checkpointer)

    def _compile(self, checkpointer):
        settings = self.ctx.settings.llm
        ctx = self.ctx

        def ground(state: AgentState) -> dict:
            messages = state["messages"]
            question = messages[_last_human_index(messages)].text if messages else ""
            return {"grounding": build_grounding(ctx, question), "tool_rounds": 0, "evidence": []}

        async def agent(state: AgentState, config) -> dict:
            rounds = state.get("tool_rounds", 0)
            system = SystemMessage(render_system_prompt(
                now=ctx.now(), catalog_summary=ctx.catalog.summary().replace("\n", "; "),
                grounding=state.get("grounding", ""),
                metrics_retention=human_duration(ctx.metrics_retention)))
            prompt: list[AnyMessage] = [system, *trim_history(state["messages"], settings.max_history_turns)]
            exhausted = rounds >= settings.max_tool_rounds
            if exhausted:
                prompt.append(HumanMessage(BUDGET_EXHAUSTED))
            response = await self.model.ainvoke(prompt, config)
            if exhausted and getattr(response, "tool_calls", None):
                response = AIMessage(content=(response.text or
                                              "I reached the investigation step limit before I could finish, "
                                              "so I can't give a verified answer. Please narrow the question "
                                              "(one service or host, a shorter window)."))
            return {"messages": [response], "tool_rounds": rounds + (1 if getattr(response, "tool_calls", None) else 0)}

        def route(state: AgentState) -> str:
            last = state["messages"][-1]
            return "tools" if isinstance(last, AIMessage) and last.tool_calls else "finalize"

        def finalize(state: AgentState) -> dict:
            messages = state["messages"]
            seen, evidence = set(), []
            for m in messages[_last_human_index(messages):]:
                if isinstance(m, ToolMessage) and isinstance(m.artifact, list):
                    for ev in m.artifact:
                        key = (ev.get("kind"), ev.get("source"), ev.get("query"), ev.get("note"))
                        if key not in seen:
                            seen.add(key)
                            evidence.append({**ev, "tool": m.name})
            return {"evidence": evidence}

        g = StateGraph(AgentState)
        g.add_node("ground", ground)
        g.add_node("agent", agent)
        g.add_node("tools", ToolNode(self.tools, handle_tool_errors=True))
        g.add_node("finalize", finalize)
        g.add_edge(START, "ground")
        g.add_edge("ground", "agent")
        g.add_conditional_edges("agent", route, {"tools": "tools", "finalize": "finalize"})
        g.add_edge("tools", "agent")
        g.add_edge("finalize", END)
        return g.compile(checkpointer=checkpointer)

    def _config(self, thread_id: str, user: str | None) -> dict:
        # Each tool round is agent+tools; leave headroom for ground/finalize and the forced answer.
        limit = 2 * self.ctx.settings.llm.max_tool_rounds + 10
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": limit,
                "metadata": {"user": user or "anonymous"}}

    async def ask(self, question: str, thread_id: str, user: str | None = None) -> AgentAnswer:
        started = time.perf_counter()
        state = await self.graph.ainvoke({"messages": [HumanMessage(question)]}, self._config(thread_id, user))
        return self._answer(state, started)

    async def stream(self, question: str, thread_id: str, user: str | None = None) -> AsyncIterator[dict]:
        """Yields {"type": "tool_start"|"tool_end"|"token"|"final"|"error", ...} events."""
        started = time.perf_counter()
        config = self._config(thread_id, user)
        async for mode, chunk in self.graph.astream({"messages": [HumanMessage(question)]}, config,
                                                    stream_mode=["updates", "messages"]):
            if mode == "messages":
                msg, meta = chunk
                if meta.get("langgraph_node") == "agent" and hasattr(msg, "text"):
                    text = msg.text
                    if text:
                        yield {"type": "token", "text": text}
                continue
            for node, update in chunk.items():
                if not update:
                    continue
                if node == "agent":
                    for m in update.get("messages", []):
                        for call in getattr(m, "tool_calls", None) or []:
                            yield {"type": "tool_start", "id": call["id"], "name": call["name"],
                                   "label": TOOL_LABELS.get(call["name"], call["name"]), "args": call["args"]}
                elif node == "tools":
                    for m in update.get("messages", []):
                        if isinstance(m, ToolMessage):
                            yield {"type": "tool_end", "id": m.tool_call_id, "name": m.name,
                                   "status": getattr(m, "status", "success")}
        state = (await self.graph.aget_state(config)).values
        yield {"type": "final", **self._answer(state, started).to_dict()}

    @staticmethod
    def _answer(state: dict, started: float) -> AgentAnswer:
        messages = state["messages"]
        start = _last_human_index(messages)
        final = messages[-1]
        tools_used = [m.name for m in messages[start:] if isinstance(m, ToolMessage)]
        return AgentAnswer(text=final.text if isinstance(final, AIMessage) else "", evidence=state.get("evidence", []),
                           tools_used=tools_used, duration_s=time.perf_counter() - started)
