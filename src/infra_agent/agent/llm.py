"""Provider-agnostic chat model construction.

`llm.model` is "<provider>:<model>" understood by langchain's init_chat_model, e.g.
  anthropic:claude-opus-5 · openai:gpt-5 · azure_openai:<deployment> · google_genai:gemini-2.5-pro
  bedrock_converse:anthropic.claude-opus-5 · google_vertexai:...
The provider's integration package must be installed (see pyproject extras).
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from ..config import LLMSettings, ModelSpec


def build_chat_model(model: str, params: dict | None = None) -> BaseChatModel:
    from langchain.chat_models import init_chat_model

    try:
        return init_chat_model(model, **(params or {}))
    except ImportError as exc:
        raise RuntimeError(
            f"Model {model!r} needs its LangChain integration package: {exc}. "
            "e.g. pip install 'infra-agent[anthropic]' or 'infra-agent[openai]'"
        ) from exc


def bind_model(settings: LLMSettings, tools: Sequence[BaseTool], model: BaseChatModel | None = None) -> Runnable:
    primary = (model or build_chat_model(settings.model, settings.params)).bind_tools(list(tools))
    fallbacks = []
    for fb in settings.fallbacks:
        spec = fb if isinstance(fb, ModelSpec) else ModelSpec(model=fb)
        fallbacks.append(build_chat_model(spec.model, spec.params).bind_tools(list(tools)))
    return primary.with_fallbacks(fallbacks) if fallbacks else primary
