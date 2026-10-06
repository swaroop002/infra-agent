"""Conversation memory (LangGraph checkpointers)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from ..config import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings):
    mem = settings.memory
    if mem.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
    elif mem.type == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = settings.resolve_path(mem.url or "data/conversations.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            yield saver
    elif mem.type == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(mem.url) as saver:
            await saver.setup()
            yield saver
    else:  # pragma: no cover
        raise ValueError(f"unknown memory type {mem.type}")
