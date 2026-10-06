"""Catalog loading, validation and entity resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import yaml
from pydantic import ValidationError

from .schema import CatalogFile, Cluster, Host, Service, VibePlatform

Entity = Host | Service | Cluster

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "for", "of", "in", "on", "to", "with", "any",
    "there", "what", "which", "how", "our", "my", "past", "last", "hours", "hrs", "days", "rate",
    "down", "up", "issues", "issue", "problem", "problems", "status", "service", "server", "api",
    "vm", "it", "be", "do", "does", "did", "has", "have", "been", "and", "or", "me", "tell",
    "show", "check", "please", "right", "now", "currently", "today", "failure", "error", "errors",
    "uptime", "health", "healthy",
}


class CatalogError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class EntityMatch:
    kind: str
    id: str
    name: str
    score: float
    matched_on: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "name": self.name,
                "score": round(self.score, 2), "matched_on": self.matched_on}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tokens(text: str) -> list[str]:
    return _norm(text).split()


@dataclass
class Catalog:
    hosts: dict[str, Host] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    clusters: dict[str, Cluster] = field(default_factory=dict)
    vibe: VibePlatform | None = None
    glossary: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, dirs: Iterable[str | Path]) -> Catalog:
        catalog = cls()
        problems: list[str] = []
        for directory in dirs:
            directory = Path(directory)
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*.y*ml")):
                # Folders starting with "_" (e.g. _discovered/) hold drafts and are skipped.
                if any(part.startswith("_") for part in path.relative_to(directory).parts):
                    continue
                catalog._load_file(path, problems)
        problems.extend(catalog.check_references())
        if problems:
            raise CatalogError(problems)
        return catalog

    def _load_file(self, path: Path, problems: list[str]) -> None:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            parsed = CatalogFile.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            problems.append(f"{path}: {exc}")
            return
        self.sources.append(str(path))
        for bucket, items in (("hosts", parsed.hosts), ("services", parsed.services),
                              ("clusters", parsed.clusters)):
            target: dict = getattr(self, bucket)
            for item in items:
                if item.id in target:
                    problems.append(f"{path}: duplicate {bucket[:-1]} id {item.id!r}")
                target[item.id] = item
        if parsed.vibe:
            if self.vibe:
                problems.append(f"{path}: 'vibe' defined in more than one file")
            self.vibe = parsed.vibe
        self.glossary.update(parsed.glossary)

    def check_references(self) -> list[str]:
        problems = []
        for svc in self.services.values():
            for h in svc.hosts:
                if h not in self.hosts:
                    problems.append(f"service {svc.id}: unknown host {h!r}")
            for d in svc.depends_on:
                if d not in self.services:
                    problems.append(f"service {svc.id}: depends_on unknown service {d!r}")
            for w in svc.kubernetes:
                if w.cluster not in self.clusters:
                    problems.append(f"service {svc.id}: unknown cluster {w.cluster!r}")
        for host in self.hosts.values():
            for s in host.services:
                if s not in self.services:
                    problems.append(f"host {host.id}: unknown service {s!r}")
        seen: dict[str, str] = {}
        for entity in self.entities():
            for label in [entity.id, *entity.aliases]:
                key = _norm(label)
                owner = f"{entity.kind}:{entity.id}"
                if key in seen and seen[key] != owner:
                    problems.append(f"alias {label!r} used by both {seen[key]} and {owner}")
                seen[key] = owner
        return problems

    # ---------------------------------------------------------------- access
    def entities(self) -> list[Entity]:
        return [*self.hosts.values(), *self.services.values(), *self.clusters.values()]

    def get(self, kind: str, entity_id: str) -> Entity | None:
        return {"host": self.hosts, "service": self.services, "cluster": self.clusters}[kind].get(entity_id)

    def services_on_host(self, host_id: str) -> list[Service]:
        host = self.hosts.get(host_id)
        declared = set(host.services) if host else set()
        return [s for s in self.services.values() if host_id in s.hosts or s.id in declared]

    def dependents_of(self, service_id: str) -> list[Service]:
        return [s for s in self.services.values() if service_id in s.depends_on]

    def host_for_target(self, labels: dict[str, str]) -> Host | None:
        """Find the catalog host whose selector matches a Prometheus target's labels."""
        for host in self.hosts.values():
            if all(_label_matches(labels.get(k, ""), v) for k, v in host.prometheus.items()):
                return host
        return None

    def summary(self) -> str:
        envs = sorted({e.environment for e in self.entities() if e.environment})
        lines = [
            f"{len(self.hosts)} hosts, {len(self.services)} services, {len(self.clusters)} clusters"
            + (f"; environments: {', '.join(envs)}" if envs else ""),
        ]
        if self.vibe:
            lines.append(f"Vibe-coding VM platform on {self.vibe.provider} "
                         f"(apps: {', '.join(a.name for a in self.vibe.apps) or 'none'})")
        if self.glossary:
            lines.append("Glossary: " + "; ".join(f"{k} = {v}" for k, v in sorted(self.glossary.items())))
        return "\n".join(lines)

    # ------------------------------------------------------------- resolution
    def resolve(self, text: str, kinds: Iterable[str] | None = None, limit: int = 5,
                threshold: float = 0.72) -> list[EntityMatch]:
        """Find entities mentioned in free text ("is the dev server down?").

        Scores exact phrase containment highest, then fuzzy n-gram similarity, so typos and
        partial names ("report gen") still resolve. Glossary expansions are applied first.
        """
        kinds = set(kinds) if kinds else {"host", "service", "cluster"}
        expanded = self._expand_glossary(text)
        q_norm = f" {_norm(expanded)} "
        q_tokens = [t for t in _tokens(expanded) if t not in _STOPWORDS] or _tokens(expanded)
        best: dict[str, EntityMatch] = {}
        for entity in self.entities():
            if entity.kind not in kinds:
                continue
            for label in [entity.id, entity.display_name, *entity.aliases]:
                score = _score(label, q_norm, q_tokens)
                key = f"{entity.kind}:{entity.id}"
                if score >= threshold and (key not in best or score > best[key].score):
                    best[key] = EntityMatch(entity.kind, entity.id, entity.display_name, score, label)
        ranked = sorted(best.values(), key=lambda m: (-m.score, m.kind, m.id))
        return ranked[:limit]

    def resolve_one(self, name: str, kind: str) -> tuple[Entity | None, list[EntityMatch]]:
        """Resolve a name the model passed to a tool. Returns (entity, candidates)."""
        bucket = {"host": self.hosts, "service": self.services, "cluster": self.clusters}[kind]
        if name in bucket:
            return bucket[name], []
        matches = self.resolve(name, kinds=[kind], limit=5, threshold=0.6)
        if matches and (len(matches) == 1 or matches[0].score - matches[1].score >= 0.1):
            return bucket[matches[0].id], matches
        return None, matches

    def _expand_glossary(self, text: str) -> str:
        out = text
        for term, meaning in self.glossary.items():
            if re.search(rf"\b{re.escape(term)}\b", out, flags=re.IGNORECASE):
                out = f"{out} {meaning}"
        return out


def _label_matches(actual: str, expected: str) -> bool:
    if expected.startswith("!~"):
        return re.fullmatch(expected[2:], actual) is None
    if expected.startswith("~"):
        return re.fullmatch(expected[1:], actual) is not None
    if expected.startswith("!"):
        return actual != expected[1:]
    return actual == expected


def _score(label: str, q_norm: str, q_tokens: list[str]) -> float:
    l_norm = _norm(label)
    if not l_norm:
        return 0.0
    if f" {l_norm} " in q_norm:
        # Longer exact phrases are more specific.
        return min(1.0, 0.9 + 0.02 * len(l_norm.split()))
    l_tokens = l_norm.split()
    n = len(l_tokens)
    best = 0.0
    joined_label = "".join(l_tokens)
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(1, len(q_tokens) - size + 1)):
            gram = q_tokens[i:i + size]
            if not gram:
                continue
            ratio = SequenceMatcher(None, " ".join(gram), l_norm).ratio()
            ratio = max(ratio, SequenceMatcher(None, "".join(gram), joined_label).ratio())
            best = max(best, ratio)
    # Token prefix match: "report gen" vs "report generation".
    if len(q_tokens) >= 1 and n >= 2:
        hits = sum(any(lt.startswith(qt) and len(qt) >= 3 for qt in q_tokens) for lt in l_tokens)
        best = max(best, 0.6 + 0.35 * hits / n if hits >= 2 else best)
    return best * 0.88  # fuzzy never outranks an exact phrase
