from datetime import UTC, datetime, timedelta

from infra_agent.ledger import LedgerStore
from infra_agent.ledger.collector import Collector
from infra_agent.ops import uptime

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _obs(store, status, minutes, threshold=2, key="service:x"):
    return store.observe(key=key, kind="service", entity_id="x", name="X", status=status,
                         at=T0 + timedelta(minutes=minutes), source="test", failure_threshold=threshold)


def test_debounced_outage_lifecycle():
    store = LedgerStore("sqlite://")
    assert _obs(store, "up", 0) is None
    assert _obs(store, "down", 1) is None            # first failure: not yet an outage
    t = _obs(store, "down", 2)
    assert t and t.new_status == "down" and t.at == T0 + timedelta(minutes=1)  # backdated to first failure
    assert _obs(store, "down", 3) is None
    t = _obs(store, "up", 10)
    assert t and t.old_status == "down" and t.new_status == "up"
    outages = store.outages_in(T0, T0 + timedelta(hours=1))
    assert len(outages) == 1 and outages[0]["ended_at"] == T0 + timedelta(minutes=10)


def test_flap_below_threshold_is_not_outage():
    store = LedgerStore("sqlite://")
    _obs(store, "up", 0)
    _obs(store, "down", 1)
    _obs(store, "up", 2)
    assert store.outages_in(T0, T0 + timedelta(hours=1)) == []


def test_neutral_status_closes_outage():
    store = LedgerStore("sqlite://")
    _obs(store, "down", 0, threshold=1, key="vibe_app:i-1/vscode")
    _obs(store, "stopped", 5, threshold=1, key="vibe_app:i-1/vscode")
    assert store.open_outages() == []


def test_uptime_accounts_for_collector_gaps():
    store = LedgerStore("sqlite://")
    interval = 60
    _obs(store, "up", 0, threshold=1)
    # collector ran for the first 2 hours, then stopped for 2 hours, then ran again
    store.record_run(T0, T0 + timedelta(hours=2), 1, [])
    store.record_run(T0 + timedelta(hours=4), T0 + timedelta(hours=4, seconds=5), 1, [])
    _obs(store, "down", 60, threshold=1)
    _obs(store, "up", 90, threshold=1)
    u = store.uptime("service:x", T0, T0 + timedelta(hours=4), interval)
    assert u["downtime_s"] == 1800
    assert u["coverage_pct"] == 50.0
    assert u["uptime_pct"] == 75.0  # 30 min down out of 2 h observed


def test_backfill_overlap_is_skipped():
    store = LedgerStore("sqlite://")
    store.ensure_entity(key="host:h", kind="host", entity_id="h", name="h", first_seen=T0)
    kw = dict(key="host:h", kind="host", entity_id="h", name="h", source="backfill")
    assert store.add_historical_outage(started_at=T0, ended_at=T0 + timedelta(minutes=10), **kw)
    assert not store.add_historical_outage(started_at=T0 + timedelta(minutes=5), ended_at=T0 + timedelta(minutes=20), **kw)


async def test_collector_cycle_on_demo_world(ctx):
    ctx.settings.ledger.failure_threshold = 1
    collector = Collector(ctx)
    report = await collector.run_once()
    assert not report.errors
    down = {t.key for t in report.transitions if t.new_status == "down"}
    assert {"host:dev-app-01", "host:prod-report-worker-02", "target:node/10.40.0.9:9100",
            "service:report-generation", "check:dev-portal/health", "vibe:i-0vibe00000000c33",
            "vibe_app:i-0vibe00000000a11/deployments-manager"} <= down
    assert "vibe:i-0vibe00000000b22" not in down  # user-stopped VM is not an outage
    worker = ctx.ledger.state("host:prod-report-worker-02")
    assert "cloud state: stopped" in worker["detail"]

    # second cycle: no new transitions, uptime report works
    report2 = await collector.run_once()
    assert report2.transitions == []
    now = ctx.now()
    rep = (await uptime.uptime_report(ctx, resolve(now), kind="service")).payload()
    names = [r["entity"] for r in rep["entities"]]
    assert names[0] in ("service:report-generation", "service:dev-portal")


def resolve(now):
    from infra_agent.timeutil import Window

    return Window(now - timedelta(minutes=5), now + timedelta(seconds=1))


async def test_demo_seed(ctx):
    from infra_agent.demo import seed_demo_ledger

    counts = await seed_demo_ledger(ctx, days=14)
    assert counts.get("service:report-generation", 0) >= 3
    open_keys = {o["key"] for o in ctx.ledger.open_outages()}
    assert "host:dev-app-01" in open_keys
    st = ctx.ledger.state("host:dev-app-01")
    assert (ctx.now() - st["since"]) > timedelta(minutes=30)
