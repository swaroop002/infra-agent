"""Result and evidence types shared by operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from ..backends.base import BackendError

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class Evidence:
    kind: str  # promql | logql | alerts | cloud | probe | ledger | catalog | docs
    source: str
    query: str | None = None
    start: str | None = None
    end: str | None = None
    url: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class OpResult:
    data: dict[str, Any]
    evidence: list[Evidence] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        out = dict(self.data)
        if self.errors:
            out["data_errors"] = self.errors
        return out


class Recorder:
    """Collects evidence and backend errors while an operation runs."""

    def __init__(self) -> None:
        self.evidence: list[Evidence] = []
        self.errors: list[str] = []

    def promql(self, backend, query: str, start: datetime | None = None, end: datetime | None = None,
               note: str | None = None) -> None:
        url = backend.explore_url(query, start, end) if start and end else None
        self.evidence.append(Evidence("promql", backend.name, str(query), _iso(start), _iso(end), url, note))

    def logql(self, backend, query: str, start: datetime, end: datetime, note: str | None = None) -> None:
        self.evidence.append(Evidence("logql", backend.name, str(query), _iso(start), _iso(end),
                                      backend.explore_url(query, start, end), note))

    def add(self, kind: str, source: str, **kw: Any) -> None:
        self.evidence.append(Evidence(kind, source, **kw))

    async def call(self, what: str, aw: Awaitable[T], default: T) -> T:
        try:
            return await aw
        except BackendError as exc:
            self.errors.append(f"{what}: {exc}")
        except Exception as exc:  # adapters must not take the agent down
            log.exception("%s failed", what)
            self.errors.append(f"{what}: unexpected {type(exc).__name__}: {exc}")
        return default

    async def gather(self, **calls: tuple[str, Awaitable[Any], Any]) -> dict[str, Any]:
        keys = list(calls)
        results = await asyncio.gather(*(self.call(w, aw, d) for w, aw, d in calls.values()))
        return dict(zip(keys, results, strict=True))

    def result(self, data: dict[str, Any]) -> OpResult:
        return OpResult(data, self.evidence, self.errors)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def iso(dt: datetime | None) -> str | None:
    return _iso(dt)


def round_or_none(v: float | None, digits: int = 2) -> float | None:
    return None if v is None else round(v, digits)


class NotResolved(Exception):
    """Entity name did not resolve; carries candidate suggestions for the model."""

    def __init__(self, kind: str, name: str, candidates: list[dict]):
        self.kind, self.name, self.candidates = kind, name, candidates
        super().__init__(f"No unique {kind} matches {name!r}")

    def payload(self) -> dict:
        return {"error": str(self), "candidates": self.candidates,
                "hint": "Call again with one of the candidate ids, or ask the user which one they mean."}
