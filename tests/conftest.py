from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from infra_agent.backends import fake as fake_module
from infra_agent.config import load_settings
from infra_agent.context import build_context

ROOT = Path(__file__).resolve().parents[1]


class ScriptedChatModel(BaseChatModel):
    """Returns queued responses; each may be an AIMessage or a callable(messages) -> AIMessage."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    responses: list[Any] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if not self.responses:
            reply: AIMessage | Callable = AIMessage(content="(script exhausted)")
        else:
            reply = self.responses.pop(0)
        if callable(reply):
            reply = reply(messages)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


@pytest.fixture(autouse=True)
def _fresh_fake_worlds():
    fake_module._WORLDS.clear()
    yield
    fake_module._WORLDS.clear()


@pytest.fixture
def demo_settings():
    settings = load_settings(ROOT / "config" / "demo.yaml")
    settings.ledger.url = "sqlite://"
    return settings


@pytest.fixture
async def ctx(demo_settings):
    context = build_context(demo_settings)
    yield context
    await context.aclose()
