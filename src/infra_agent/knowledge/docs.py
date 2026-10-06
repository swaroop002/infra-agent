"""Docs index: runbooks, architecture notes, postmortems (Markdown).

BM25 works with zero external calls. When an embeddings model is configured the search is
hybrid: BM25 and vector rankings are merged with reciprocal-rank fusion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are", "be", "with",
         "this", "that", "it", "as", "at", "by", "if", "from", "we", "you", "our", "not"}


@dataclass
class Chunk:
    source: str
    heading: str
    text: str

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.heading}|{self.text}".encode()).hexdigest()


@dataclass
class DocHit:
    source: str
    heading: str
    snippet: str
    score: float

    def to_dict(self) -> dict:
        return {"source": self.source, "heading": self.heading, "snippet": self.snippet,
                "score": round(self.score, 3)}


def tokenize(text: str) -> list[str]:
    return [t.strip(".-_") for t in _WORD.findall(text.lower()) if t not in _STOP and len(t) > 1]


def chunk_markdown(source: str, text: str, max_chars: int = 1500) -> list[Chunk]:
    """Split on headings, then on paragraphs when a section is too long."""
    chunks: list[Chunk] = []
    title = source
    heading_stack: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        heading = " > ".join(heading_stack) or title
        paragraphs = re.split(r"\n\s*\n", body)
        current = ""
        for para in paragraphs:
            if current and len(current) + len(para) > max_chars:
                chunks.append(Chunk(source, heading, current.strip()))
                current = ""
            current += para + "\n\n"
        if current.strip():
            chunks.append(Chunk(source, heading, current.strip()))

    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            heading_stack[:] = heading_stack[: level - 1] + [m.group(2).strip()]
            continue
        buf.append(line)
    flush()
    return chunks


class DocsIndex:
    def __init__(self, chunks: list[Chunk], embeddings=None, cache_path: Path | None = None):
        self.chunks = chunks
        self._tf = [Counter(tokenize(f"{c.heading} {c.heading} {c.text}")) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._embeddings = embeddings
        self._vectors: list[list[float]] | None = None
        if embeddings is not None and chunks:
            self._vectors = self._embed_chunks(cache_path)

    @classmethod
    def load(cls, dirs: list[Path], embeddings=None, cache_path: Path | None = None) -> DocsIndex:
        chunks: list[Chunk] = []
        for directory in dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*.md")):
                rel = str(path.relative_to(directory))
                chunks.extend(chunk_markdown(rel, path.read_text(errors="replace")))
        return cls(chunks, embeddings=embeddings, cache_path=cache_path)

    @property
    def sources(self) -> list[str]:
        return sorted({c.source for c in self.chunks})

    def search(self, query: str, limit: int = 5, source_filter: list[str] | None = None) -> list[DocHit]:
        candidates = [i for i, c in enumerate(self.chunks)
                      if not source_filter or any(c.source == s or c.source.endswith(s) for s in source_filter)]
        bm25 = self._bm25(query, candidates)
        if self._vectors is not None:
            try:
                vec = self._vector_rank(query, candidates)
                fused: dict[int, float] = {}
                for rank_list in (bm25, vec):
                    for rank, (idx, _) in enumerate(rank_list):
                        fused[idx] = fused.get(idx, 0.0) + 1.0 / (60 + rank)
                ranked = sorted(fused.items(), key=lambda kv: -kv[1])
            except Exception as exc:  # embeddings provider down: degrade to BM25
                log.warning("vector search failed, using BM25 only: %s", exc)
                ranked = bm25
        else:
            ranked = bm25
        hits = []
        for idx, score in ranked[:limit]:
            c = self.chunks[idx]
            hits.append(DocHit(c.source, c.heading, _snippet(c.text, query), score))
        return hits

    def get_document(self, source: str) -> str | None:
        parts = [c for c in self.chunks if c.source == source]
        if not parts:
            return None
        return "\n\n".join(f"## {c.heading}\n{c.text}" for c in parts)

    def _bm25(self, query: str, candidates: list[int], k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        q = tokenize(query)
        scores = []
        for i in candidates:
            tf = self._tf[i]
            s = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + k1 * (1 - b + b * self._len[i] / (self._avg or 1))
                s += self._idf.get(term, 0.0) * f * (k1 + 1) / denom
            if s > 0:
                scores.append((i, s))
        return sorted(scores, key=lambda kv: -kv[1])

    def _vector_rank(self, query: str, candidates: list[int]) -> list[tuple[int, float]]:
        qv = self._embeddings.embed_query(query)
        qn = math.sqrt(sum(x * x for x in qv)) or 1.0
        out = []
        for i in candidates:
            v = self._vectors[i]
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append((i, sum(a * b for a, b in zip(qv, v, strict=False)) / (qn * vn)))
        return sorted(out, key=lambda kv: -kv[1])[:20]

    def _embed_chunks(self, cache_path: Path | None) -> list[list[float]]:
        cache: dict[str, list[float]] = {}
        if cache_path and cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
            except json.JSONDecodeError:
                cache = {}
        missing = [c for c in self.chunks if c.id not in cache]
        if missing:
            vectors = self._embeddings.embed_documents([f"{c.heading}\n{c.text}" for c in missing])
            for c, v in zip(missing, vectors, strict=True):
                cache[c.id] = v
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                live = {c.id for c in self.chunks}
                cache_path.write_text(json.dumps({k: v for k, v in cache.items() if k in live}))
        return [cache[c.id] for c in self.chunks]


def _snippet(text: str, query: str, width: int = 600) -> str:
    if len(text) <= width:
        return text
    terms = set(tokenize(query))
    lower = text.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) >= 0]
    start = max(0, min(positions) - 120) if positions else 0
    return ("…" if start else "") + text[start:start + width].strip() + "…"
