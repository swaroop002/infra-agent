"""Downtime ledger: entity state, outages, probe samples and collector runs.

SQLite for single-node installs, Postgres (postgresql+psycopg://) for production.
Methods are synchronous; async callers use `asyncio.to_thread`.
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine

from ..timeutil import overlap_seconds

metadata = MetaData()

entity_state = Table(
    "entity_state", metadata,
    Column("key", String(300), primary_key=True),
    Column("kind", String(30), nullable=False, index=True),
    Column("entity_id", String(250), nullable=False),
    Column("name", String(250)),
    Column("status", String(20), nullable=False),  # up | down | stopped | missing | unknown
    Column("since", DateTime(timezone=True), nullable=False),
    Column("last_checked", DateTime(timezone=True), nullable=False),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("consecutive_failures", Integer, nullable=False, default=0),
    Column("first_failure_at", DateTime(timezone=True)),
    Column("detail", Text, default=""),
)

outages = Table(
    "outages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String(300), nullable=False, index=True),
    Column("kind", String(30), nullable=False, index=True),
    Column("entity_id", String(250), nullable=False),
    Column("name", String(250)),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("ended_at", DateTime(timezone=True), index=True),
    Column("source", String(50), nullable=False),
    Column("detail", Text, default=""),
)

check_samples = Table(
    "check_samples", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String(300), nullable=False, index=True),
    Column("ts", DateTime(timezone=True), nullable=False, index=True),
    Column("ok", Boolean, nullable=False),
    Column("latency_ms", Float),
    Column("status_code", Integer),
    Column("error", Text),
)

collector_runs = Table(
    "collector_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("checked", Integer, nullable=False, default=0),
    Column("errors", Integer, nullable=False, default=0),
    Column("error_detail", Text, default=""),
)

NEUTRAL = {"stopped", "missing", "unknown"}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Transition:
    key: str
    kind: str
    entity_id: str
    name: str
    old_status: str | None
    new_status: str
    at: datetime
    detail: str


class LedgerStore:
    def __init__(self, url: str, base_dir: str | Path = "."):
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
            rel = url.removeprefix("sqlite:///")
            if rel and rel != ":memory:":
                path = Path(base_dir) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                url = f"sqlite:///{path}"
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        # SQLite connections don't tolerate concurrent use from worker threads; serialize access.
        self._lock = threading.RLock() if url.startswith("sqlite") else nullcontext()
        metadata.create_all(self.engine)

    # ---------------------------------------------------------------- writes
    def observe(self, *, key: str, kind: str, entity_id: str, name: str, status: str, at: datetime,
                source: str, detail: str = "", failure_threshold: int = 1,
                sample: dict | None = None) -> Transition | None:
        """Record one observation; open/close outages on debounced transitions."""
        transition: Transition | None = None
        with self._lock, self.engine.begin() as conn:
            row = conn.execute(select(entity_state).where(entity_state.c.key == key)).mappings().first()
            if sample is not None:
                conn.execute(insert(check_samples).values(key=key, ts=at, **sample))
            if row is None:
                effective = status
                failures = 1 if status == "down" else 0
                if status == "down" and failure_threshold > 1:
                    effective = "unknown"
                conn.execute(insert(entity_state).values(
                    key=key, kind=kind, entity_id=entity_id, name=name, status=effective, since=at,
                    last_checked=at, first_seen=at, consecutive_failures=failures,
                    first_failure_at=at if status == "down" else None, detail=detail))
                if effective == "down":
                    conn.execute(insert(outages).values(key=key, kind=kind, entity_id=entity_id, name=name,
                                                        started_at=at, source=source, detail=detail))
                    transition = Transition(key, kind, entity_id, name, None, "down", at, detail)
                return transition

            old = row["status"]
            values: dict = {"last_checked": at, "detail": detail, "name": name}
            if status == "down":
                failures = row["consecutive_failures"] + 1
                first_fail = _aware(row["first_failure_at"]) or at
                values.update(consecutive_failures=failures, first_failure_at=first_fail)
                if old != "down" and failures >= failure_threshold:
                    values.update(status="down", since=first_fail)
                    conn.execute(insert(outages).values(key=key, kind=kind, entity_id=entity_id, name=name,
                                                        started_at=first_fail, source=source, detail=detail))
                    transition = Transition(key, kind, entity_id, name, old, "down", first_fail, detail)
                elif old == "down":
                    conn.execute(update(outages).where(and_(outages.c.key == key, outages.c.ended_at.is_(None)))
                                 .values(detail=detail))
            else:
                values.update(consecutive_failures=0, first_failure_at=None)
                if old == "down":
                    conn.execute(update(outages).where(and_(outages.c.key == key, outages.c.ended_at.is_(None)))
                                 .values(ended_at=at))
                    transition = Transition(key, kind, entity_id, name, old, status, at, detail)
                if old != status:
                    values.update(status=status, since=at)
            conn.execute(update(entity_state).where(entity_state.c.key == key).values(**values))
        return transition

    def record_run(self, started_at: datetime, finished_at: datetime, checked: int, errors: list[str]) -> None:
        with self._lock, self.engine.begin() as conn:
            conn.execute(insert(collector_runs).values(started_at=started_at, finished_at=finished_at,
                                                       checked=checked, errors=len(errors),
                                                       error_detail="\n".join(errors)[:4000]))

    def ensure_entity(self, *, key: str, kind: str, entity_id: str, name: str, first_seen: datetime,
                      status: str = "unknown") -> None:
        """Create or backdate an entity's first_seen (used by backfill)."""
        with self._lock, self.engine.begin() as conn:
            row = conn.execute(select(entity_state.c.first_seen).where(entity_state.c.key == key)).first()
            if row is None:
                conn.execute(insert(entity_state).values(
                    key=key, kind=kind, entity_id=entity_id, name=name, status=status, since=first_seen,
                    last_checked=first_seen, first_seen=first_seen, consecutive_failures=0, detail=""))
            elif _aware(row[0]) > first_seen:
                conn.execute(update(entity_state).where(entity_state.c.key == key).values(first_seen=first_seen))

    def add_historical_outage(self, *, key: str, kind: str, entity_id: str, name: str, started_at: datetime,
                              ended_at: datetime | None, source: str, detail: str = "") -> bool:
        """Insert a backfilled outage unless one already overlaps it."""
        with self._lock, self.engine.begin() as conn:
            end = ended_at or datetime.now(UTC)
            existing = conn.execute(select(outages.c.id).where(and_(
                outages.c.key == key, outages.c.started_at < end,
                or_(outages.c.ended_at.is_(None), outages.c.ended_at > started_at)))).first()
            if existing:
                return False
            conn.execute(insert(outages).values(key=key, kind=kind, entity_id=entity_id, name=name,
                                                started_at=started_at, ended_at=ended_at, source=source,
                                                detail=detail))
            return True

    def prune(self, retention_days: int, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        with self._lock, self.engine.begin() as conn:
            conn.execute(delete(check_samples).where(check_samples.c.ts < cutoff))
            conn.execute(delete(collector_runs).where(collector_runs.c.started_at < cutoff))

    # ----------------------------------------------------------------- reads
    def state(self, key: str) -> dict | None:
        with self._lock, self.engine.connect() as conn:
            row = conn.execute(select(entity_state).where(entity_state.c.key == key)).mappings().first()
        return _row(row) if row else None

    def states(self, kind: str | None = None, status: str | None = None) -> list[dict]:
        q = select(entity_state)
        if kind:
            q = q.where(entity_state.c.kind == kind)
        if status:
            q = q.where(entity_state.c.status == status)
        with self._lock, self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(q.order_by(entity_state.c.key)).mappings()]

    def outages_in(self, start: datetime, end: datetime, *, keys: list[str] | None = None,
                   kind: str | None = None, limit: int = 500) -> list[dict]:
        q = select(outages).where(and_(outages.c.started_at < end,
                                       or_(outages.c.ended_at.is_(None), outages.c.ended_at > start)))
        if keys is not None:
            q = q.where(outages.c.key.in_(keys))
        if kind:
            q = q.where(outages.c.kind == kind)
        with self._lock, self.engine.connect() as conn:
            rows = conn.execute(q.order_by(outages.c.started_at.desc()).limit(limit)).mappings()
            return [_row(r) for r in rows]

    def open_outages(self, kind: str | None = None) -> list[dict]:
        q = select(outages).where(outages.c.ended_at.is_(None))
        if kind:
            q = q.where(outages.c.kind == kind)
        with self._lock, self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(q.order_by(outages.c.started_at)).mappings()]

    def collector_gaps(self, start: datetime, end: datetime, interval_s: int) -> list[tuple[datetime, datetime]]:
        """Periods inside [start, end] not covered by collector runs (> 3 intervals apart).

        A run covers [started_at, finished_at]; backfill records one long run for the period it
        reconstructed from Prometheus.
        """
        with self._lock, self.engine.connect() as conn:
            runs = [(_aware(r[0]), _aware(r[1])) for r in conn.execute(
                select(collector_runs.c.started_at, collector_runs.c.finished_at)
                .where(and_(collector_runs.c.finished_at >= start - timedelta(seconds=3 * interval_s),
                            collector_runs.c.started_at <= end))
                .order_by(collector_runs.c.started_at))]
        limit = timedelta(seconds=3 * interval_s)
        gaps = []
        prev = start
        for run_start, run_end in runs:
            if run_start - prev > limit:
                gaps.append((prev, run_start))
            prev = max(prev, run_end)
        if end - prev > limit:
            gaps.append((prev, end))
        return gaps

    def uptime(self, key: str, start: datetime, end: datetime, interval_s: int) -> dict:
        st = self.state(key)
        if st is None:
            return {"key": key, "known": False}
        observed_start = max(start, st["first_seen"])
        if observed_start >= end:
            return {"key": key, "known": True, "coverage_pct": 0.0, "note": "entity first seen after window end"}
        gaps = self.collector_gaps(observed_start, end, interval_s)
        gap_s = sum((b - a).total_seconds() for a, b in gaps)
        observed_s = max(0.0, (end - observed_start).total_seconds() - gap_s)
        items = self.outages_in(observed_start, end, keys=[key])
        down_s = 0.0
        for o in items:
            o_end = o["ended_at"] or end
            d = overlap_seconds(o["started_at"], o_end, observed_start, end)
            d -= sum(overlap_seconds(o["started_at"], o_end, a, b) for a, b in gaps)
            down_s += max(0.0, d)
        window_s = (end - start).total_seconds()
        uptime_pct = 100.0 * (1 - down_s / observed_s) if observed_s else None
        return {
            "key": key, "known": True, "name": st["name"], "current_status": st["status"],
            "uptime_pct": round(uptime_pct, 3) if uptime_pct is not None else None,
            "downtime_s": round(down_s), "observed_s": round(observed_s),
            "coverage_pct": round(100.0 * observed_s / window_s, 1) if window_s else 0.0,
            "outages": len(items), "first_seen": st["first_seen"].isoformat(timespec="seconds"),
        }

    def sample_stats(self, key: str, start: datetime, end: datetime, max_rows: int = 20000) -> dict:
        with self._lock, self.engine.connect() as conn:
            rows = conn.execute(select(check_samples.c.ok, check_samples.c.latency_ms)
                                .where(and_(check_samples.c.key == key, check_samples.c.ts >= start,
                                            check_samples.c.ts <= end))
                                .order_by(check_samples.c.ts.desc()).limit(max_rows)).all()
        if not rows:
            return {"samples": 0}
        latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)

        def pct(p: float) -> float | None:
            return round(latencies[min(len(latencies) - 1, int(p * len(latencies)))], 1) if latencies else None

        ok = sum(1 for r in rows if r.ok)
        return {"samples": len(rows), "success_pct": round(100.0 * ok / len(rows), 2),
                "latency_p50_ms": pct(0.5), "latency_p95_ms": pct(0.95)}


def _row(row) -> dict:
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = _aware(v)
    return out
