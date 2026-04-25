"""Shared rate-limited HTTP client — spec §7.1.

Every upstream service (NCBI, UniProt, EBI, Ensembl, AlphaFold, RCSB, ChEMBL,
PubChem, Europe PMC, Reactome, STRING) caps how aggressively we're allowed
to hit it. `RateLimitedClient` wraps an `httpx.AsyncClient` with two
independent controls:

- `max_concurrent` — at most this many requests in flight at once. A
  bounded asyncio semaphore enforces this.
- `min_interval_s` — successive request *starts* (by wall-clock) must be
  at least this far apart. A short asyncio lock + monotonic-clock bookkeeping
  enforces this.

The two controls compose: a request waits on both gates before its HTTP
dispatch. Retries on transient failures (HTTP 429/503) are layered on top
by `clients/base.py` using `tenacity`, so they stay outside this module's
concern.

Per-service parameter table: see spec §7.1. `clients/base.py` is the right
place to instantiate per-service clients from that table.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Any

import httpx


class RateLimitedClient:
    """An `httpx.AsyncClient` façade that enforces per-service rate limits.

    Independent instances are expected per upstream service — the limits are
    not global. Thread-safe within a single asyncio event loop; not safe to
    share across loops.
    """

    def __init__(
        self,
        *,
        max_concurrent: int,
        min_interval_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | float | None = 30.0,
        headers: dict[str, str] | None = None,
        base_url: str = "",
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be ≥ 1, got {max_concurrent}")
        if min_interval_s < 0:
            raise ValueError(f"min_interval_s must be ≥ 0, got {min_interval_s}")

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_interval_s = min_interval_s
        self._interval_lock = asyncio.Lock()
        self._next_allowed_start: float = 0.0
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers=headers,
            base_url=base_url,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Dispatch an HTTP request after acquiring both rate-limit gates."""
        async with self._semaphore:
            await self._await_next_slot()
            return await self._client.request(method, url, **kwargs)

    async def _await_next_slot(self) -> None:
        """Block until the minimum-interval floor has elapsed, then reserve the next slot.

        The reservation is made under `_interval_lock` so that two requests
        arriving simultaneously can't both compute the same wait and start
        in lock-step. Each caller reserves the *next* future start-time
        before releasing the lock.
        """
        if self._min_interval_s <= 0:
            return
        async with self._interval_lock:
            now = time.monotonic()
            wait = self._next_allowed_start - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            # Reserve the next slot from whichever is later: our actual
            # start-time, or the previously reserved floor. Using `now`
            # here means back-to-back requests that finish waiting at the
            # same time still end up interval_s apart.
            self._next_allowed_start = now + self._min_interval_s

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RateLimitedClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
