"""Configuration: a YAML file with ${ENV_VAR} / ${ENV_VAR:-default} interpolation.

Secrets never live in the YAML itself; reference environment variables instead.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATH = "config/agent.yaml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelSpec(_Model):
    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class LLMSettings(_Model):
    # "<provider>:<model>", resolved by langchain's init_chat_model.
    model: str = "anthropic:claude-opus-5"
    # Passed verbatim to the provider class (max_tokens, base_url, api_version, ...).
    params: dict[str, Any] = Field(default_factory=dict)
    # Tried in order if the primary model errors. A plain string reuses no params.
    fallbacks: list[str | ModelSpec] = Field(default_factory=list)
    max_tool_rounds: int = 12
    max_history_turns: int = 10


class KnowledgeSettings(_Model):
    catalog_dirs: list[str] = Field(default_factory=lambda: ["knowledge/catalog"])
    docs_dirs: list[str] = Field(default_factory=lambda: ["knowledge/docs"])
    # "<provider>:<model>" for langchain init_embeddings; None means BM25 only.
    embeddings: str | None = None
    embeddings_cache: str = "data/embeddings-cache.json"


class BackendSpec(BaseModel):
    """One adapter instance. `type` is a registered name or "package.module:Class"."""

    model_config = ConfigDict(extra="allow")
    type: str

    def options(self) -> dict[str, Any]:
        return {k: v for k, v in (self.model_extra or {}).items()}


class BackendsSettings(_Model):
    metrics: BackendSpec | None = None
    logs: BackendSpec | None = None
    alerts: list[BackendSpec] = Field(default_factory=list)
    inventory: list[BackendSpec] = Field(default_factory=list)
    prober: BackendSpec = Field(default_factory=lambda: BackendSpec(type="http"))
    # How far back the metrics backend keeps data; longer windows are answered from the ledger.
    metrics_retention: str = "7d"
    # Regex on the `job` label for node_exporter targets (used by fleet views and the collector).
    node_job_regex: str | None = None


class LedgerSettings(_Model):
    url: str = "sqlite:///data/ledger.db"
    interval_s: int = 60
    failure_threshold: int = 2
    sample_retention_days: int = 90
    run_in_server: bool = True
    probe_concurrency: int = 20


class NotificationSettings(_Model):
    # Env var holding a Teams Workflows "When a webhook request is received" URL.
    teams_workflow_url_env: str | None = None
    notify_on: list[Literal["down", "up"]] = Field(default_factory=lambda: ["down", "up"])


class AuthSettings(_Model):
    mode: Literal["none", "api_key", "trusted_header"] = "none"
    api_keys_env: str = "INFRA_AGENT_API_KEYS"  # comma separated
    trusted_header: str = "X-Forwarded-Email"


class ServerSettings(_Model):
    host: str = "0.0.0.0"
    port: int = 8080
    auth: AuthSettings = Field(default_factory=AuthSettings)
    cors_origins: list[str] = Field(default_factory=list)


class MemorySettings(_Model):
    type: Literal["memory", "sqlite", "postgres"] = "memory"
    url: str | None = None


class TeamsSettings(_Model):
    enabled: bool = False
    # When true, the SDK converts incoming messages to proactive turns so long LLM runs
    # don't hit the channel's ~15s timeout. Requires CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID.
    long_running_messages: bool = True


class Settings(_Model):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    backends: BackendsSettings = Field(default_factory=BackendsSettings)
    ledger: LedgerSettings = Field(default_factory=LedgerSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    teams: TeamsSettings = Field(default_factory=TeamsSettings)
    # Directory relative paths in this file are resolved against (set by the loader).
    base_dir: str = "."

    def resolve_path(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else Path(self.base_dir) / p


def interpolate_env(value: Any, env: dict[str, str] | None = None) -> Any:
    env = os.environ if env is None else env
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            return ""
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: interpolate_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, env) for v in value]
    return value


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    path = Path(path or os.environ.get("INFRA_AGENT_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"Config file {path} not found. Copy config/agent.example.yaml to {path} "
            "or pass --config / set INFRA_AGENT_CONFIG."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    data = interpolate_env(raw)
    # Paths inside the config are relative to the project root (the config's parent dir's parent
    # when the file lives in config/), which keeps `infra-agent` runnable from any cwd.
    base = path.resolve().parent
    if base.name == "config":
        base = base.parent
    data.setdefault("base_dir", str(base))
    return Settings.model_validate(data)
