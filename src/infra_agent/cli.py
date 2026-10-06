"""`infra-agent` command line."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import Settings, load_settings

app = typer.Typer(no_args_is_help=True, help="Read-only infrastructure agent.")
kb_app = typer.Typer(no_args_is_help=True, help="Knowledge base: validate, discover, search.")
collector_app = typer.Typer(no_args_is_help=True, help="Downtime collector.")
ledger_app = typer.Typer(no_args_is_help=True, help="Downtime ledger maintenance.")
demo_app = typer.Typer(no_args_is_help=True, help="Demo mode helpers.")
eval_app = typer.Typer(no_args_is_help=True, help="Model / prompt evals.")
app.add_typer(kb_app, name="kb")
app.add_typer(collector_app, name="collector")
app.add_typer(ledger_app, name="ledger")
app.add_typer(demo_app, name="demo")
app.add_typer(eval_app, name="eval")

console = Console()
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Config YAML (default: $INFRA_AGENT_CONFIG or config/agent.yaml)")]


def _settings(config: Path | None) -> Settings:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return load_settings(config)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


def _context(settings: Settings, with_ledger: bool = True):
    from .context import build_context
    from .knowledge.catalog import CatalogError

    try:
        return build_context(settings, with_ledger=with_ledger)
    except CatalogError as exc:
        console.print("[red]Catalog errors:[/red]")
        for p in exc.problems:
            console.print(f"  • {p}")
        raise typer.Exit(2) from exc


def _render_answer(answer, show_evidence: bool) -> None:
    console.print(Markdown(answer.text or "_(no answer)_"))
    footer = f"[dim]{answer.duration_s:.1f}s · tools: {', '.join(answer.tools_used) or 'none'}[/dim]"
    console.print(footer)
    if show_evidence and answer.evidence:
        table = Table("kind", "source", "query / note", "link", show_lines=False, header_style="dim")
        for e in answer.evidence:
            table.add_row(e.get("kind", ""), e.get("source", ""), e.get("query") or e.get("note") or "",
                          "Grafana" if e.get("url") else "")
        console.print(table)


# ---------------------------------------------------------------------- chat
@app.command()
def ask(question: str, config: ConfigOpt = None, thread: str | None = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
        evidence: Annotated[bool, typer.Option("--evidence/--no-evidence")] = True):
    """Ask one question."""
    settings = _settings(config)

    async def run():
        from .agent import InfraAgent

        ctx = _context(settings)
        try:
            agent = InfraAgent(ctx)
            answer = await agent.ask(question, thread or f"cli:{uuid.uuid4().hex}")
        finally:
            await ctx.aclose()
        if as_json:
            print(json.dumps(answer.to_dict(), indent=2, default=str))
        else:
            _render_answer(answer, evidence)

    asyncio.run(run())


@app.command()
def chat(config: ConfigOpt = None):
    """Interactive conversation in the terminal."""
    settings = _settings(config)

    async def run():
        from .agent import InfraAgent

        ctx = _context(settings)
        agent = InfraAgent(ctx)
        thread = f"cli:{uuid.uuid4().hex}"
        console.print(f"[bold]Infra Agent[/bold] · {settings.llm.model} · read-only · /new resets · Ctrl-D exits\n")
        try:
            while True:
                try:
                    q = await asyncio.to_thread(console.input, "[bold cyan]› [/bold cyan]")
                except EOFError:
                    break
                if not q.strip():
                    continue
                if q.strip() == "/new":
                    thread = f"cli:{uuid.uuid4().hex}"
                    console.print("[dim]new conversation[/dim]")
                    continue
                with console.status("investigating…"):
                    answer = await agent.ask(q, thread)
                _render_answer(answer, show_evidence=False)
                console.print()
        finally:
            await ctx.aclose()

    asyncio.run(run())


@app.command()
def serve(config: ConfigOpt = None, host: str | None = None, port: int | None = None,
          collector: Annotated[bool | None, typer.Option("--collector/--no-collector")] = None):
    """Run the web UI, API and (optionally) Teams bot and collector."""
    import uvicorn

    from .interfaces.api import create_app

    settings = _settings(config)
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    api = create_app(settings, start_collector=collector)
    uvicorn.run(api, host=host or settings.server.host, port=port or settings.server.port,
                log_level=os.environ.get("LOG_LEVEL", "info").lower())


# ------------------------------------------------------------------ collector
@collector_app.command("run")
def collector_run(config: ConfigOpt = None, once: bool = False):
    """Run the downtime collector (standalone process)."""
    from .ledger.collector import Collector
    from .notify import build_notifier

    settings = _settings(config)
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))

    async def run():
        ctx = _context(settings)
        c = Collector(ctx, build_notifier(settings))
        try:
            if once:
                report = await c.run_once()
                console.print(f"checked {report.checked} · transitions {len(report.transitions)} · errors {len(report.errors)}")
                for e in report.errors:
                    console.print(f"  [yellow]{e}[/yellow]")
            else:
                await c.run_forever()
        finally:
            await ctx.aclose()

    asyncio.run(run())


@ledger_app.command("backfill")
def ledger_backfill(config: ConfigOpt = None, hours: int = 168):
    """Reconstruct host outages from Prometheus history (bounded by retention)."""
    from .ledger.collector import backfill_hosts

    settings = _settings(config)

    async def run():
        ctx = _context(settings)
        try:
            added = await backfill_hosts(ctx, hours)
        finally:
            await ctx.aclose()
        console.print(f"backfilled {sum(added.values())} outages across {len(added)} hosts")

    asyncio.run(run())


@demo_app.command("seed")
def demo_seed(config: Annotated[Path, typer.Option("--config", "-c")] = Path("config/demo.yaml"), days: int = 14):
    """Fill the demo ledger with synthetic history."""
    from .demo import seed_demo_ledger

    settings = _settings(config)

    async def run():
        ctx = _context(settings)
        try:
            counts = await seed_demo_ledger(ctx, days)
        finally:
            await ctx.aclose()
        console.print(f"seeded {sum(counts.values())} historical outages; ledger: {settings.ledger.url}")

    asyncio.run(run())


# ------------------------------------------------------------------------ kb
@kb_app.command("validate")
def kb_validate(config: ConfigOpt = None, live: bool = typer.Option(False, help="Check selectors against live backends")):
    """Validate the catalog schema, references and (optionally) live selectors."""
    settings = _settings(config)
    ctx = _context(settings, with_ledger=False)
    console.print(f"[green]catalog OK[/green]: {ctx.catalog.summary()}")
    console.print(f"docs: {len(ctx.docs.sources)} documents, {len(ctx.docs.chunks)} chunks")
    missing = []
    for e in ctx.catalog.entities():
        for rb in e.runbooks:
            if ctx.docs.get_document(rb) is None:
                missing.append(f"{e.kind}:{e.id} → {rb}")
    for m in missing:
        console.print(f"[yellow]runbook not found[/yellow]: {m}")
    for svc in ctx.catalog.services.values():
        if not (svc.health_checks or svc.logs or svc.kubernetes or svc.hosts):
            console.print(f"[yellow]no signals[/yellow]: service {svc.id} has no health checks, logs, workloads or hosts")
    if not live:
        asyncio.run(ctx.aclose())
        return

    async def run():
        from .knowledge.validate import validate_live

        try:
            checks = await validate_live(ctx)
        finally:
            await ctx.aclose()
        table = Table("entity", "check", "ok", "detail")
        for c in checks:
            table.add_row(c.entity, c.check, "[green]✓[/green]" if c.ok else "[red]✗[/red]", c.detail)
        console.print(table)
        failed = sum(not c.ok for c in checks)
        if failed:
            console.print(f"[red]{failed} live checks failed[/red]")
            raise typer.Exit(1)

    asyncio.run(run())


@kb_app.command("discover")
def kb_discover(config: ConfigOpt = None,
                vibe_tag: Annotated[list[str] | None, typer.Option(help="Tag filter Key=Value for vibe VMs")] = None):
    """Generate draft catalog files from live Prometheus, Loki and cloud inventory."""
    from .backends import build_backends
    from .knowledge.discover import discover

    settings = _settings(config)
    tags = dict(t.split("=", 1) for t in (vibe_tag or []))

    async def run():
        backends = build_backends(settings, None)
        try:
            out = await discover(backends, settings.resolve_path(settings.knowledge.catalog_dirs[0]),
                                 node_job_regex=settings.backends.node_job_regex, vibe_tags=tags or None)
        finally:
            await backends.aclose()
        console.print(f"drafts written to [bold]{out}[/bold]; start with {out / 'REPORT.md'}")

    asyncio.run(run())


@kb_app.command("search")
def kb_search(query: str, config: ConfigOpt = None):
    """Search runbooks/docs and resolve catalog entities for a query."""
    settings = _settings(config)
    ctx = _context(settings, with_ledger=False)
    for m in ctx.catalog.resolve(query):
        console.print(f"[cyan]{m.kind}[/cyan] {m.id} (score {m.score:.2f}, matched '{m.matched_on}')")
    for h in ctx.docs.search(query):
        console.print(f"[green]{h.source}[/green] › {h.heading} ({h.score:.2f})\n  [dim]{h.snippet[:200]}[/dim]")
    asyncio.run(ctx.aclose())


# ---------------------------------------------------------------------- evals
@eval_app.command("run")
def eval_run(config: Annotated[Path, typer.Option("--config", "-c")] = Path("config/demo.yaml"),
             cases: Path = Path("evals/cases.yaml"), only: Annotated[list[str] | None, typer.Option()] = None,
             model: Annotated[str | None, typer.Option(help="Override llm.model, e.g. openai:gpt-5")] = None):
    """Run eval cases against the demo world with the configured (or overridden) model."""
    from .agent import InfraAgent
    from .evals import load_cases, run_evals

    settings = _settings(config)
    if model:
        settings.llm.model = model

    async def run():
        ctx = _context(settings, with_ledger=False)
        try:
            agent = InfraAgent(ctx)
            summary = await run_evals(agent, load_cases(cases), Path("evals/results"), only)
        finally:
            await ctx.aclose()
        table = Table("case", "result", "tools", "time", "failures")
        for r in summary["results"]:
            table.add_row(r["id"], "[green]pass[/green]" if r["passed"] else "[red]fail[/red]",
                          ", ".join(r["tools_used"]), f"{r['duration_s']}s", "\n".join(r["failures"]))
        console.print(table)
        console.print(f"[bold]{summary['passed']}/{summary['total']} passed[/bold] · {summary['model']} · "
                      f"report {summary.get('report_path')}")

    asyncio.run(run())


if __name__ == "__main__":
    app()
