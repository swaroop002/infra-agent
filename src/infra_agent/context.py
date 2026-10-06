"""Runtime context shared by tools, collector and interfaces."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .backends import Backends, build_backends
from .config import Settings
from .knowledge.catalog import Catalog
from .knowledge.docs import DocsIndex
from .ledger import LedgerStore
from .timeutil import parse_duration, utcnow

log = logging.getLogger(__name__)


@dataclass
class AgentContext:
    settings: Settings
    catalog: Catalog
    docs: DocsIndex
    backends: Backends
    ledger: LedgerStore | None
    clock: Callable[[], datetime] = field(default=utcnow)

    def now(self) -> datetime:
        return self.clock()

    @property
    def metrics_retention(self) -> timedelta:
        return parse_duration(self.settings.backends.metrics_retention)

    async def aclose(self) -> None:
        await self.backends.aclose()


def build_context(settings: Settings, *, with_ledger: bool = True) -> AgentContext:
    catalog = Catalog.load(settings.resolve_path(d) for d in settings.knowledge.catalog_dirs)
    embeddings = None
    if settings.knowledge.embeddings:
        from langchain.embeddings import init_embeddings

        embeddings = init_embeddings(settings.knowledge.embeddings)
    docs = DocsIndex.load([settings.resolve_path(d) for d in settings.knowledge.docs_dirs],
                          embeddings=embeddings,
                          cache_path=settings.resolve_path(settings.knowledge.embeddings_cache))
    backends = build_backends(settings, catalog)
    ledger = LedgerStore(settings.ledger.url, settings.base_dir) if with_ledger and settings.ledger.url else None
    log.info("context ready: %s; %d doc chunks", catalog.summary().splitlines()[0], len(docs.chunks))
    return AgentContext(settings=settings, catalog=catalog, docs=docs, backends=backends, ledger=ledger)
