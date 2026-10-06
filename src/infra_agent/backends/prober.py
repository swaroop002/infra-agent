"""HTTP health prober. Only ever called with URLs that come from the catalog."""

from __future__ import annotations

import os
import time

import httpx

from ..knowledge.schema import HealthCheck
from ..timeutil import utcnow
from .base import Prober, ProbeResult


class HttpProber(Prober):
    def __init__(self, *, user_agent: str = "infra-agent-healthcheck/1.0", name: str = "http"):
        self.name = name
        self._ua = user_agent
        self._clients: dict[bool, httpx.AsyncClient] = {}

    def _client(self, verify: bool) -> httpx.AsyncClient:
        if verify not in self._clients:
            self._clients[verify] = httpx.AsyncClient(verify=verify, follow_redirects=False,
                                                      headers={"User-Agent": self._ua})
        return self._clients[verify]

    async def probe(self, check: HealthCheck) -> ProbeResult:
        headers = {h: os.environ[e] for h, e in check.headers_env.items() if e in os.environ}
        started = time.perf_counter()
        at = utcnow()
        try:
            resp = await self._client(check.verify_tls).request(check.method, check.url, headers=headers,
                                                                timeout=check.timeout_s)
        except httpx.TimeoutException:
            return ProbeResult(False, None, None, f"timeout after {check.timeout_s}s", at, check.url)
        except httpx.HTTPError as exc:
            return ProbeResult(False, None, None, f"{type(exc).__name__}: {exc}"[:300], at, check.url)
        latency = (time.perf_counter() - started) * 1000
        ok = resp.status_code in check.expect_status
        error = None if ok else f"unexpected status {resp.status_code}"
        if ok and check.expect_body_contains and check.method == "GET":
            if check.expect_body_contains not in resp.text[:65536]:
                ok, error = False, f"body missing {check.expect_body_contains!r}"
        return ProbeResult(ok, resp.status_code, latency, error, at, check.url)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
