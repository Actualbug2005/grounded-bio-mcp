"""Tests for the shared RateLimitedClient — spec §7.1.

Verifies the two invariants the whole server's rate-management relies on:

1. `max_concurrent` caps the number of in-flight upstream requests at any
   instant. This is what stops us from firing 20 parallel NCBI calls when
   NCBI only tolerates 10.
2. `min_interval_s` enforces a floor on the gap between successive request
   starts to the same service. This is the pacing that keeps us under
   each provider's req/s policy even when individual calls are very fast.

Any rate-limit regression here silently breaks every downstream tool, so
these tests are the foundation the rest of the server builds on.
"""

from __future__ import annotations

import asyncio
import time
from itertools import pairwise

import httpx
import pytest

from bioinformatics_mcp.utils.rate_limit import RateLimitedClient


@pytest.mark.asyncio
async def test_rate_limited_client_respects_max_concurrent() -> None:
    """At most `max_concurrent` requests may be in flight at once."""
    max_concurrent = 3
    total_requests = 12

    in_flight = 0
    peak_in_flight = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak_in_flight
        async with lock:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
        # Hold the "connection" long enough that a naive implementation
        # would let many requests overlap. 50 ms is comfortably longer
        # than the asyncio scheduler's tick granularity.
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    client = RateLimitedClient(
        max_concurrent=max_concurrent,
        min_interval_s=0.0,
        transport=transport,
    )
    try:
        await asyncio.gather(
            *(client.request("GET", "https://example.test/x") for _ in range(total_requests))
        )
    finally:
        await client.aclose()

    assert peak_in_flight <= max_concurrent, (
        f"concurrent_count exceeded max_concurrent={max_concurrent} (got {peak_in_flight})"
    )


@pytest.mark.asyncio
async def test_rate_limited_client_respects_min_interval() -> None:
    """Successive request starts must be at least `min_interval_s` apart."""
    min_interval_s = 0.1
    start_times: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        start_times.append(time.monotonic())
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    # max_concurrent is wide so it can't mask interval violations.
    client = RateLimitedClient(
        max_concurrent=10,
        min_interval_s=min_interval_s,
        transport=transport,
    )
    try:
        for _ in range(4):
            await client.request("GET", "https://example.test/x")
    finally:
        await client.aclose()

    assert len(start_times) == 4, "handler did not run for every request"

    # 5 ms of slack to absorb scheduler jitter without letting real violations through.
    tolerance = 0.005
    floor = min_interval_s - tolerance
    gaps = [t2 - t1 for t1, t2 in pairwise(start_times)]
    for i, gap in enumerate(gaps):
        assert gap >= floor, (
            f"min_interval violated between request {i} and {i + 1}: "
            f"gap={gap:.4f}s, floor={floor:.4f}s (min_interval_s={min_interval_s})"
        )
