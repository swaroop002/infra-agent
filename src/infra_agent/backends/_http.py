"""Shared HTTP client construction for HTTP-API backends."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx

from .base import BackendError


def build_client(url: str, *, token_env: str | None = None, token_scheme: str = "Bearer",
                 username_env: str | None = None, password_env: str | None = None,
                 headers: dict[str, str] | None = None, headers_env: dict[str, str] | None = None,
                 timeout_s: float = 30.0, verify_tls: bool = True) -> httpx.AsyncClient:
    if not url:
        raise BackendError("backend url is empty (check the ${ENV} reference in the config)")
    hdrs = dict(headers or {})
    for header, env in (headers_env or {}).items():
        if os.environ.get(env):
            hdrs[header] = os.environ[env]
    if token_env and os.environ.get(token_env):
        token = os.environ[token_env]
        hdrs["Authorization"] = f"{token_scheme} {token}".strip() if token_scheme else token
    auth = None
    if username_env and password_env and os.environ.get(username_env):
        auth = httpx.BasicAuth(os.environ[username_env], os.environ.get(password_env, ""))
    return httpx.AsyncClient(base_url=url.rstrip("/"), headers=hdrs, auth=auth,
                             timeout=timeout_s, verify=verify_tls)


async def get_json(client: httpx.AsyncClient, path: str, params: Any = None, what: str = "request") -> Any:
    try:
        resp = await client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise BackendError(f"{what} failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        body = resp.text[:500]
        raise BackendError(f"{what} failed: HTTP {resp.status_code}: {body}")
    try:
        return resp.json()
    except ValueError as exc:
        raise BackendError(f"{what} returned non-JSON response") from exc


def grafana_explore_url(grafana_url: str | None, datasource_uid: str | None, ds_type: str,
                        expr: str, start_ms: int, end_ms: int, org_id: int = 1) -> str | None:
    if not grafana_url or not datasource_uid:
        return None
    panes = {"a": {"datasource": datasource_uid,
                   "queries": [{"refId": "A", "expr": expr,
                                "datasource": {"type": ds_type, "uid": datasource_uid}}],
                   "range": {"from": str(start_ms), "to": str(end_ms)}}}
    return (f"{grafana_url.rstrip('/')}/explore?schemaVersion=1&orgId={org_id}"
            f"&panes={quote(json.dumps(panes, separators=(',', ':')))}")
