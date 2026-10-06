# Infra Agent — Design

Status: v1 (read-only) · Last updated: 2026-09-12

## 1. Goal

A conversational SRE assistant that answers operational questions about our estate from
**live telemetry plus a curated knowledge base**, e.g.

- "Is the development server down?"
- "What was the failure rate of the payments API in the last 24 hours?"
- "Are there any issues with the report generation service?"
- "What's the uptime of our services this month?"
- "Which vibe-coding VMs are running for alice, and is VS Code up on them?"

Hard requirements:

| Requirement | How it is met |
|---|---|
| Web UI **and** Microsoft Teams | One FastAPI service hosts the web UI, a JSON/SSE API and the Teams bot endpoint. |
| Recent incidents visible without asking the LLM | Web "Alerts" tab; Teams `/alerts`, `/status`, `/downtime` commands. |
| Strictly read-only (v1) | No tool mutates anything. Backends only issue GET/query calls. Cloud credentials documented as reader-only. |
| Works with any hosted LLM | Model is a config string resolved via LangChain `init_chat_model` (`anthropic:…`, `openai:…`, `azure_openai:…`, `google_genai:…`, `bedrock_converse:…`). |
| Works on someone else's infra / knowledge | Every data source is an adapter behind an interface, selected in config; the catalog is plain YAML with a documented schema. |

## 2. Constraints that shaped the design

| Fact about our environment | Consequence |
|---|---|
| Prometheus retention is **7 days** | "Uptime this month" cannot come from Prometheus. The agent runs a **collector** that records state transitions into its own **downtime ledger** (SQLite/Postgres). Prometheus is used for ≤ 7 d detail, the ledger for long windows. |
| 40–50 VMs scraped by node_exporter; **Prometheus SD is the only inventory** | Host liveness = `up` for the target. Cloud APIs (read-only) are used to explain *why* a target is down (stopped vs. running-but-unreachable). |
| **Label conventions are unclear** | The catalog maps each entity to an *explicit* Prometheus selector / LogQL selector, so correctness never depends on conventions. A `kb discover` command inspects what really exists and generates draft catalog files plus a label report. |
| API request data lives **only in Loki** | Error rate = LogQL `count_over_time` of error lines ÷ all request lines. Parser, status field and error expression are configured per service. |
| Need "health checks kind of" for APIs | Catalog declares HTTP health checks. The collector probes them on an interval (GET only, allow-listed URLs) and records results — this gives uptime history beyond 7 days. |
| No knowledge base yet | Bootstrap tooling (`kb discover`, `kb validate`), a schema, templates and a docs folder for runbooks/postmortems. |
| Vibe-coding VMs are created with **boto3 + Ansible** (AWS) | Discovered live via EC2 tags; app health (VS Code, deployments manager…) is probed on the instance's configured ports. |
| Hosted models are fine | No local model serving. Embeddings are optional (lexical search works without them). |

## 3. Architecture

```
            ┌──────────── Interfaces ─────────────┐
            │ Web UI (Ask · Alerts · Status)       │
            │ REST + SSE  /api/*                   │
            │ Teams bot   /api/messages            │
            │ CLI         infra-agent ask|chat     │
            └───────────────┬──────────────────────┘
                            │
                ┌───────────▼────────────┐
                │  LangGraph agent        │
                │  ground → agent ⇄ tools │
                │        → finalize       │
                └───┬──────────────┬──────┘
                    │              │
      ┌─────────────▼───┐    ┌─────▼──────────────────────────────┐
      │ Knowledge layer  │    │ Tool layer (read-only, composite)   │
      │ • Catalog (YAML) │    │ host status/downtime · fleet down   │
      │ • Docs index     │    │ API errors · service health         │
      │   (BM25 ± embed) │    │ uptime report · alerts · k8s        │
      │ • Ledger (SQL)   │    │ vibe VMs · KB search · raw PromQL/  │
      └────────▲─────────┘    │ LogQL escape hatches                │
               │              └─────┬──────────────────────────────┘
               │                    │ adapter interfaces
      ┌────────┴─────────┐   ┌──────▼─────────────────────────────────────┐
      │ Collector        │──▶│ Metrics: Prometheus (any Prom-compatible)   │
      │ (every 60 s)     │   │ Logs:    Loki                               │
      │ up · probes ·    │   │ Alerts:  Grafana Alertmanager · OnCall      │
      │ cloud · vibe VMs │   │ Inventory: AWS · Azure · GCP (reader)       │
      └──────────────────┘   │ Prober:  HTTP                               │
                             │ Fake:    scenario-driven (demo, tests, eval)│
                             └─────────────────────────────────────────────┘
```

### 3.1 The agent graph

```
START → ground → agent ──(tool calls & budget left)──▶ tools ─┐
                   ▲                                           │
                   └───────────────────────────────────────────┘
                 agent ──(final answer)──▶ finalize → END
```

- **ground** (deterministic, no LLM): fuzzy-matches the question against catalog names and
  aliases ("development server" → host `dev-app-01`), and pulls the top runbook snippets.
  The result is injected into the system prompt. This is the single biggest lever for
  accuracy across *different* models: the model never has to guess hostnames or labels.
- **agent**: the chat model with tools bound. Receives the static instructions, current time,
  a catalog summary and the grounding block.
- **tools**: LangGraph `ToolNode`; tool errors become tool results, not crashes.
- **finalize**: collects *evidence* (the exact PromQL/LogQL/API call, time range, Grafana link)
  from tool artifacts. Evidence is attached by code, not written by the model, so citations
  can't be hallucinated.
- A **step budget** (default 12 tool rounds) bounds cost and latency.
- Conversation memory uses a LangGraph checkpointer keyed by thread (web session / Teams
  conversation): in-memory, SQLite or Postgres.

### 3.2 Tool design principles

1. **Composite, deterministic tools.** `check_service_health` gathers probes, ledger uptime, Loki
   error rate, K8s workload state, related alerts and dependencies in one call and computes a
   verdict in code. Weaker models do well because they orchestrate and explain rather than
   write PromQL.
2. **Catalog-first, escape hatch second.** Queries come from the catalog. `query_prometheus` /
   `query_loki` exist for questions the catalog doesn't cover, with range and size caps.
3. **Everything returns evidence** (`content_and_artifact`): the model sees compact JSON; the UI
   sees queries and links.
4. **Untrusted data stays data.** Log lines and alert text are truncated and labelled as
   untrusted in tool output; the system prompt instructs the model never to follow
   instructions found in them. Health probes can only hit URLs defined in the catalog
   (no SSRF via prompt injection).

### 3.3 Answer semantics for common questions

**"Is X down?" (host)**

| `up` | Cloud state | Verdict |
|---|---|---|
| 1 | any | **UP** (DEGRADED if disk > 90 % / memory < 5 % / load ≫ cores) |
| 0 | running | **UNREACHABLE** — VM running but not scrapeable: node_exporter down, firewall/network, or host hung |
| 0 | stopped / deallocated | **DOWN (stopped)** — include who/when if the cloud API says |
| 0 or absent | terminated / not found | **GONE** |
| 0 | unknown (no cloud ref) | **DOWN** — scrape failing since *t* |
| absent | running | **NOT MONITORED** — dropped out of service discovery |

Downtime windows ≤ 7 d come from `up` range queries (exact intervals + availability %);
longer windows from the ledger. Reboots come from `changes(node_boot_time_seconds[w])`.

**"API failure rate for payments (24 h)"** — from the service's `logs` signal:

```
errors = sum(count_over_time({selector} | <parser> | <error_expr> [24h]))
total  = sum(count_over_time({selector} | <parser> [24h]))
```

Also: top failing paths, previous-window comparison, SLO comparison, sample error lines.

**"Any issues with report generation?"** — `check_service_health`: live probes, open outages,
24 h / 7 d uptime, error rate, K8s workload health (kube-state-metrics), host status, firing
alerts, dependency status, linked runbooks → HEALTHY / DEGRADED / DOWN / UNKNOWN with reasons.

## 4. Knowledge base

Three parts, all in version control except the ledger:

| Part | Content | Where |
|---|---|---|
| **Catalog** (structured) | hosts, services, clusters, vibe platform, glossary; aliases; explicit selectors; health checks; log signals; dependencies; SLOs; owners; runbook links | `knowledge/catalog/*.yaml` |
| **Docs** (unstructured) | runbooks, architecture notes, postmortems, FAQs (Markdown) | `knowledge/docs/**.md` |
| **Ledger** (operational history) | outages, check samples, collector runs | SQL database |

Building it is a loop, described in [knowledge-base.md](knowledge-base.md):
`kb discover` → review drafts → promote to `knowledge/catalog/` → `kb validate --live` →
write runbooks → run evals.

Docs search is BM25 by default (no external calls). If an embeddings model is configured,
search becomes hybrid (BM25 + vectors, reciprocal-rank fusion). Embeddings are cached on
disk by chunk hash.

## 5. Collector & downtime ledger

Runs inside the server (single replica) or as its own process (`infra-agent collector run`).

Each cycle (default 60 s):

1. `up` for **all** targets → entity per target (catalog host id if a selector matches,
   otherwise `target:<job>/<instance>`), so un-catalogued SD targets are still tracked.
2. HTTP health checks from the catalog (concurrency-limited, 2 consecutive failures to open an
   outage, 1 success to close).
3. Cloud state for catalog hosts that declare a `cloud_ref`.
4. Vibe VMs: EC2 instances matching the configured tags; app probes on running instances.
   User-stopped VMs are *not* outages.

Transitions open/close rows in `outages`; every probe stores a sample (latency, status code)
with retention pruning. Collector gaps are recorded, so uptime reports state their coverage
instead of silently counting gaps as "up". `infra-agent ledger backfill --hours 168`
reconstructs host outages from Prometheus history on first install.

Optional: transitions can post to a Teams channel through a Workflows webhook. Off by
default — Grafana OnCall remains the paging system; the agent explains, it does not page.

## 6. Portability

**Model.** `llm.model: "<provider>:<model>"` plus free-form `llm.params`; optional
`llm.fallbacks`. Nothing in prompts or tools depends on a provider feature. Swapping a model
is validated with `infra-agent eval run`, which replays scripted scenarios through the fake
backends and checks tool usage and answer facts.

**Infrastructure.** Adapters implement small interfaces (`MetricsBackend`, `LogsBackend`,
`AlertsBackend`, `InventoryBackend`, `Prober`). Config selects them by type name; third-party
adapters load by dotted path (`type: "mycorp.datadog:DatadogMetrics"`) or the
`infra_agent.backends` entry-point group. Mimir, Thanos, VictoriaMetrics and Cortex work with the
Prometheus adapter unchanged.

**Knowledge.** Point `knowledge.catalog_dirs` / `knowledge.docs_dirs` at another repository.

## 7. Security

- Read-only by construction: no write tools; cloud permissions documented as reader roles
  (`ec2:Describe*`, Azure *Reader*, GCP *Compute Viewer*); Grafana *Viewer* service account.
- Secrets only via environment variables referenced from config (`${VAR}`).
- Web/API auth: `api_key` or `trusted_header` (behind oauth2-proxy / Entra ID app proxy).
  `none` is for local use only and logs a warning.
- Teams: Bot Framework JWT validation via the Microsoft 365 Agents SDK.
- Prompt-injection hygiene for logs/alerts (see 3.2) and probe URL allow-listing.
- Telemetry sent to the LLM provider is limited to what tools return (truncated JSON);
  no bulk log export.

## 8. Rollout plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | Demo mode (fake backends) + evals | `pytest` green; eval pass rate ≥ 90 % on chosen model |
| 1 | Read-only against real Prometheus/Loki/Grafana/OnCall; catalog for top 10 services + all VMs | `kb validate --live` clean; team dog-foods in web UI |
| 2 | Collector + ledger in production; Teams bot for SRE channel | 2 weeks of ledger data; answers match OnCall history |
| 3 | Vibe-coding platform, runbooks, postmortems; all services catalogued | Eval set grown from real questions |
| Later | Guarded actions (restart, scale) behind approval; K8s API adapter; pgvector | Separate design review |

## 9. Known limitations / open items

- Label conventions: recommend adding `env`, `service`, `team`, `cloud` via relabeling
  (see knowledge-base.md). Not required, but it shrinks the catalog.
- Grafana OnCall OSS is in maintenance / archival; the OnCall adapter uses the public
  `api/v1/alert_groups` API which Grafana Cloud IRM also serves. Confirm which one we run.
- Collector runs single-instance (no leader election) in v1.
- Azure and GCP inventory adapters are implemented against the official SDKs but have not
  been exercised against our subscriptions yet.
- Kubernetes insight relies on kube-state-metrics in Prometheus; a direct K8s API adapter is
  a later phase.
