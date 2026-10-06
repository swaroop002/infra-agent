import textwrap
from pathlib import Path

import pytest

from infra_agent.knowledge.catalog import Catalog, CatalogError
from infra_agent.knowledge.discover import detect_log_format, guess_cloud
from infra_agent.knowledge.docs import DocsIndex, chunk_markdown
from infra_agent.knowledge.schema import render_selector

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load([ROOT / "examples/demo/catalog"])


@pytest.mark.parametrize("question,expected", [
    ("is the development server down?", ("host", "dev-app-01")),
    ("what was the api failure rate for payments api in the past 24 hrs", ("service", "payments-api")),
    ("are there any issues with the report generation service", ("service", "report-generation")),
    ("is RGS ok", ("service", "report-generation")),          # glossary
    ("report gen issues?", ("service", "report-generation")),  # partial
    ("status of the staging server", ("host", "stg-app-01")),
    ("anything wrong on prod-gke", ("cluster", "prod-gke")),
])
def test_resolve(catalog, question, expected):
    matches = catalog.resolve(question)
    assert matches, question
    assert (matches[0].kind, matches[0].id) == expected


def test_resolve_one_ambiguous_returns_candidates(catalog):
    entity, candidates = catalog.resolve_one("report worker", "host")
    assert entity is None
    assert {c.id for c in candidates} >= {"prod-report-worker-01", "prod-report-worker-02"}


def test_resolve_nothing(catalog):
    assert catalog.resolve("how do I bake bread") == []


def test_host_for_target(catalog):
    assert catalog.host_for_target({"job": "node", "instance": "10.10.1.15:9100"}).id == "dev-app-01"
    assert catalog.host_for_target({"job": "node", "instance": "10.99.9.9:9100"}) is None


def test_catalog_reference_and_duplicate_errors(tmp_path):
    (tmp_path / "a.yaml").write_text(textwrap.dedent("""
        hosts:
          - {id: h1, aliases: [box], prometheus: {instance: "1:9100"}}
          - {id: h2, aliases: [box], prometheus: {instance: "2:9100"}}
        services:
          - {id: s1, hosts: [nope], depends_on: [ghost]}
    """))
    with pytest.raises(CatalogError) as exc:
        Catalog.load([tmp_path])
    text = "\n".join(exc.value.problems)
    assert "unknown host 'nope'" in text and "unknown service 'ghost'" in text and "alias 'box'" in text


def test_discovered_drafts_are_ignored(tmp_path):
    (tmp_path / "_discovered").mkdir()
    (tmp_path / "_discovered" / "hosts.yaml").write_text("hosts: [{id: BAD ID}]")
    assert Catalog.load([tmp_path]).hosts == {}


def test_render_selector_operators():
    assert render_selector({"job": "node", "instance": "~dev-.*", "env": "!prod"}) == \
        '{env!="prod",instance=~"dev-.*",job="node"}'


def test_docs_search_and_chunking():
    index = DocsIndex.load([ROOT / "examples/demo/docs"])
    hits = index.search("VM unreachable node_exporter scrape failing")
    assert hits and hits[0].source == "runbooks/host-down.md"
    assert index.get_document("runbooks/vibe-vms.md")
    chunks = chunk_markdown("x.md", "# A\ntext\n```\n# not a heading\n```\n## B\nmore")
    assert [c.heading for c in chunks] == ["A", "A > B"]


def test_detect_log_format():
    json_lines = ['{"status": 200, "path": "/a"}', '{"status": 502, "path": "/b"}']
    assert detect_log_format(json_lines) == {"parser": "| json", "fields_seen": ["status", "path"],
                                             "error_filter": "| status >= 500", "path_label": "path"}
    logfmt = ['level=info endpoint=/x took=3ms', 'level=error endpoint=/y took=9ms']
    fmt = detect_log_format(logfmt)
    assert fmt["parser"] == "| logfmt" and fmt["error_filter"].startswith("| level=~") and fmt["path_label"] == "endpoint"
    assert detect_log_format(["plain text", "more text"])["parser"] == ""


def test_guess_cloud():
    assert guess_cloud({"instance_id": "i-0abc12345678"}) == "aws"
    assert guess_cloud({"cloud": "Azure"}) == "azure"
    assert guess_cloud({"x": "y"}) == "unknown"
