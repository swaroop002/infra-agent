# Extending: models, backends, tools

## Use a different LLM

Set `llm.model` to any LangChain `init_chat_model` string and install its integration:

| Provider | `llm.model` | Extra | Credentials |
|---|---|---|---|
| Anthropic | `anthropic:claude-opus-5` | `infra-agent[anthropic]` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:gpt-5` | `infra-agent[openai]` | `OPENAI_API_KEY` |
| Azure OpenAI | `azure_openai:<deployment>` | `infra-agent[openai]` | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `params.api_version` |
| Google Gemini | `google_genai:gemini-2.5-pro` | `infra-agent[google]` | `GOOGLE_API_KEY` |
| AWS Bedrock | `bedrock_converse:<model-id>` | `infra-agent[bedrock]` | AWS credential chain |

`llm.params` goes to the provider class unchanged (e.g. `max_tokens`, `base_url`, `timeout`).
Don't add sampling parameters by default: some current models reject `temperature`.

Before switching the production model, run:

```bash
infra-agent eval run --model openai:gpt-5
```

The evals use the deterministic demo world, so differences come from the model, not the data.

What keeps behaviour consistent across models:
- Entity resolution runs in code (the `ground` node), not in the model.
- Tools are coarse and return verdicts computed in code, so the model explains rather than
  calculates.
- Evidence is attached from tool artifacts, never written by the model.
- Prompts use no provider-specific features (no prefill, XML tags or forced tool choice).

## Add a backend (e.g. Datadog, VictoriaLogs, Zabbix)

1. Implement the interface from `infra_agent/backends/base.py`:

```python
# mycorp_agent/datadog.py
from infra_agent.backends.base import MetricsBackend, VectorSample, Series

class DatadogMetrics(MetricsBackend):
    def __init__(self, *, site: str, api_key_env: str, app_key_env: str, name: str = "datadog"):
        ...
    async def query(self, promql, at=None) -> list[VectorSample]: ...
    async def query_range(self, promql, start, end, step_s) -> list[Series]: ...
```

2. Reference it from config, by dotted path or entry point:

```yaml
backends:
  metrics:
    type: "mycorp_agent.datadog:DatadogMetrics"
    site: datadoghq.eu
    api_key_env: DD_API_KEY
    app_key_env: DD_APP_KEY
```

```toml
# or in your package's pyproject.toml
[project.entry-points."infra_agent.backends"]
"metrics.datadog" = "mycorp_agent.datadog:DatadogMetrics"
```

The query language is fixed by `infra_agent/queries.py` (PromQL/LogQL). A non-Prometheus
backend can either translate the `Query` objects it receives (each carries `.name` and
`.params`, as `backends/fake.py` does), or you can fork `queries.py`.

Adapters must be **read-only** and should raise `BackendError` with a short message. Operations
turn those errors into `data_errors` the model reports, so one broken backend never breaks the
answer.

## Add a tool

1. Write the investigation as an operation in `infra_agent/ops/` returning an `OpResult`
   (data + evidence + errors). Operations are reusable by the web UI and Teams commands.
2. Wrap it in `agent/tools.py`: an args schema with field descriptions, a description that
   says **when** to use it, and an entry in `TOOL_LABELS`.
3. Add a test with the fake backends (`tests/test_ops.py`) and an eval case.

Keep tools composite and verdict-producing. Five good tools beat twenty thin ones, especially
on smaller models.

## Swap the knowledge base

Point `knowledge.catalog_dirs` and `knowledge.docs_dirs` at another repository (a git submodule
or a mounted volume). Several directories are merged in order, so a shared base catalog plus a
team-specific overlay works.

## Conversation memory

`memory.type`: `memory` (single process), `sqlite` (single node, survives restarts) or
`postgres` (multiple replicas; install `infra-agent[postgres]`).
