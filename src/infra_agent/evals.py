"""Model/prompt regression evals over the deterministic demo world.

Each case checks which tools the agent used and which facts appear in the answer, so swapping
the LLM (or editing prompts/tools) can be measured instead of eyeballed.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .agent import InfraAgent


@dataclass
class CaseResult:
    id: str
    question: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    answer: str = ""


def _contains(answer: str, expected: str | list[str]) -> bool:
    text = answer.lower()
    options = expected if isinstance(expected, list) else [expected]
    return any(o.lower() in text for o in options)


def load_cases(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())["cases"]


async def run_case(agent: InfraAgent, case: dict) -> CaseResult:
    started = time.perf_counter()
    failures: list[str] = []
    answer_text, tools = "", []
    thread = f"eval:{case['id']}:{uuid.uuid4().hex[:8]}"
    try:
        for turn in case.get("setup_turns", []):
            await agent.ask(turn, thread)
        answer = await agent.ask(case["question"], thread)
        answer_text, tools = answer.text, answer.tools_used
    except Exception as exc:
        failures.append(f"agent error: {type(exc).__name__}: {exc}")
    if not failures:
        any_of = case.get("expect_tools_any") or []
        if any_of and not any(t in tools for t in any_of):
            failures.append(f"expected one of tools {any_of}, used {tools}")
        for t in case.get("expect_tools_all") or []:
            if t not in tools:
                failures.append(f"expected tool {t}")
        for t in case.get("forbid_tools") or []:
            if t in tools:
                failures.append(f"must not use tool {t}")
        for exp in case.get("expect_in_answer") or []:
            if not _contains(answer_text, exp):
                failures.append(f"answer missing {exp!r}")
        for bad in case.get("forbid_in_answer") or []:
            if _contains(answer_text, bad):
                failures.append(f"answer must not contain {bad!r}")
        max_tools = case.get("max_tool_calls")
        if max_tools is not None and len(tools) > max_tools:
            failures.append(f"used {len(tools)} tool calls (max {max_tools})")
    return CaseResult(case["id"], case["question"], not failures, failures, tools,
                      round(time.perf_counter() - started, 2), answer_text)


async def run_evals(agent: InfraAgent, cases: list[dict], out_dir: Path | None = None,
                    only: list[str] | None = None) -> dict:
    selected = [c for c in cases if not only or c["id"] in only]
    results = [await run_case(agent, c) for c in selected]
    summary = {
        "model": agent.ctx.settings.llm.model,
        "passed": sum(r.passed for r in results),
        "total": len(results),
        "avg_duration_s": round(sum(r.duration_s for r in results) / max(1, len(results)), 2),
        "results": [asdict(r) for r in results],
    }
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{summary['model'].replace(':', '_').replace('/', '_')}.json"
        (out_dir / name).write_text(json.dumps(summary, indent=2))
        summary["report_path"] = str(out_dir / name)
    return summary
