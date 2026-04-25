"""Unit + integration tests for EBIJobRunner (spec §4.5, §4.13, §7.1).

Covers the shared submit → poll → fetch machinery for EBI Job Dispatcher
services. Each test uses respx to sequence per-endpoint HTTP responses;
the runner itself is exercised through its public `run()` entry point
and its individual primitives where we need to assert intermediate
behaviour (cancellation, jitter, semaphore sharing).

Integration test at the bottom is gated on RUN_INTEGRATION=1 and needs
EBI_EMAIL set — it runs a real tiny Clustal alignment against EBI.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.ebi import (
    EBI_BASE_URL,
    POLLING_STATES,
    EBIJobRunner,
)
from grounded_bio_mcp.utils.errors import JobFailed, JobTimeoutError
from grounded_bio_mcp.utils.rate_limit import RateLimitedClient

SERVICE = "clustalo"
BASE = f"{EBI_BASE_URL}/{SERVICE}"


@pytest.fixture
async def ebi_client():
    client = RateLimitedClient(max_concurrent=3, min_interval_s=0.0)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def runner(ebi_client: RateLimitedClient) -> EBIJobRunner:
    return EBIJobRunner(service=SERVICE, client=ebi_client)


# ---- happy path ----------------------------------------------------------


@respx.mock
async def test_run_happy_path(runner: EBIJobRunner) -> None:
    """Submit → RUNNING → FINISHED → fetch result — bytes flow through."""
    respx.post(f"{BASE}/run/").mock(
        return_value=httpx.Response(200, text="clustalo-R123")
    )
    respx.get(f"{BASE}/status/clustalo-R123").mock(
        side_effect=[
            httpx.Response(200, text="RUNNING"),
            httpx.Response(200, text="FINISHED"),
        ]
    )
    respx.get(f"{BASE}/result/clustalo-R123/aln-clustal_num").mock(
        return_value=httpx.Response(200, content=b"EXPECTED_ALIGNMENT")
    )

    result = await runner.run(
        params={"email": "test@example.org", "sequence": ">a\nAAAA"},
        result_type="aln-clustal_num",
        initial_interval=0.001,
        max_interval=0.001,
        timeout=5.0,
    )

    assert result == b"EXPECTED_ALIGNMENT"


@respx.mock
async def test_run_treats_queued_as_polling_state(runner: EBIJobRunner) -> None:
    """QUEUED must keep polling — EBI's reference client proves this. Prompt's
    original list missed it, so guard against regression."""
    assert "QUEUED" in POLLING_STATES

    respx.post(f"{BASE}/run/").mock(
        return_value=httpx.Response(200, text="J1")
    )
    respx.get(f"{BASE}/status/J1").mock(
        side_effect=[
            httpx.Response(200, text="QUEUED"),
            httpx.Response(200, text="RUNNING"),
            httpx.Response(200, text="FINISHED"),
        ]
    )
    respx.get(f"{BASE}/result/J1/aln-clustal_num").mock(
        return_value=httpx.Response(200, content=b"OK")
    )

    out = await runner.run(
        params={}, result_type="aln-clustal_num",
        initial_interval=0.001, max_interval=0.001, timeout=5.0,
    )
    assert out == b"OK"


# ---- timeout + cancellation --------------------------------------------


@respx.mock
async def test_timeout_triggers_cancel_and_raises(runner: EBIJobRunner) -> None:
    """Never-finishing status → JobTimeoutError, and DELETE /delete/{jobId} is called."""
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J-HANG"))
    respx.get(f"{BASE}/status/J-HANG").mock(
        return_value=httpx.Response(200, text="RUNNING")
    )
    delete_route = respx.delete(f"{BASE}/delete/J-HANG").mock(
        return_value=httpx.Response(200, text="ok")
    )

    with pytest.raises(JobTimeoutError) as exc:
        await runner.run(
            params={},
            result_type="aln-clustal_num",
            initial_interval=0.001,
            max_interval=0.001,
            timeout=0.01,
        )

    assert exc.value.job_id == "J-HANG"
    assert exc.value.cancelled is True
    assert delete_route.called


@respx.mock
async def test_timeout_absent_delete_endpoint_returns_false_not_warning(
    runner: EBIJobRunner,
) -> None:
    """404 on DELETE → cancelled=False, timeout still raises, no exception
    from cancel."""
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J"))
    respx.get(f"{BASE}/status/J").mock(
        return_value=httpx.Response(200, text="RUNNING")
    )
    respx.delete(f"{BASE}/delete/J").mock(
        return_value=httpx.Response(404, text="not found")
    )

    with pytest.raises(JobTimeoutError) as exc:
        await runner.run(
            params={},
            result_type="x",
            initial_interval=0.001,
            max_interval=0.001,
            timeout=0.01,
        )
    assert exc.value.cancelled is False


@respx.mock
async def test_timeout_cancel_5xx_still_raises_timeout(
    runner: EBIJobRunner,
) -> None:
    """5xx from DELETE must NOT swallow the timeout error. cancel=False."""
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J"))
    respx.get(f"{BASE}/status/J").mock(
        return_value=httpx.Response(200, text="RUNNING")
    )
    respx.delete(f"{BASE}/delete/J").mock(
        return_value=httpx.Response(500, text="internal error")
    )

    with pytest.raises(JobTimeoutError) as exc:
        await runner.run(
            params={},
            result_type="x",
            initial_interval=0.001,
            max_interval=0.001,
            timeout=0.01,
        )
    assert exc.value.cancelled is False


# ---- failure paths ------------------------------------------------------


@respx.mock
async def test_failed_status_raises_jobfailed(runner: EBIJobRunner) -> None:
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J-F"))
    respx.get(f"{BASE}/status/J-F").mock(
        return_value=httpx.Response(200, text="FAILED")
    )

    with pytest.raises(JobFailed) as exc:
        await runner.run(
            params={},
            result_type="x",
            initial_interval=0.001,
            max_interval=0.001,
            timeout=5.0,
        )
    assert exc.value.status == "FAILED"
    assert exc.value.job_id == "J-F"


@respx.mock
async def test_not_found_status_raises_jobfailed(runner: EBIJobRunner) -> None:
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J-NF"))
    respx.get(f"{BASE}/status/J-NF").mock(
        return_value=httpx.Response(200, text="NOT_FOUND")
    )

    with pytest.raises(JobFailed) as exc:
        await runner.run(
            params={},
            result_type="x",
            initial_interval=0.001,
            max_interval=0.001,
            timeout=5.0,
        )
    assert exc.value.status == "NOT_FOUND"


@respx.mock
async def test_error_status_raises_jobfailed(runner: EBIJobRunner) -> None:
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J-E"))
    respx.get(f"{BASE}/status/J-E").mock(
        return_value=httpx.Response(200, text="ERROR")
    )

    with pytest.raises(JobFailed):
        await runner.run(
            params={}, result_type="x",
            initial_interval=0.001, max_interval=0.001, timeout=5.0,
        )


@respx.mock
async def test_unknown_status_raises_jobfailed(runner: EBIJobRunner) -> None:
    """Unknown status values fail fast rather than polling forever."""
    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J-U"))
    respx.get(f"{BASE}/status/J-U").mock(
        return_value=httpx.Response(200, text="ZOMBIFIED")
    )

    with pytest.raises(JobFailed) as exc:
        await runner.run(
            params={}, result_type="x",
            initial_interval=0.001, max_interval=0.001, timeout=5.0,
        )
    assert "UNKNOWN" in exc.value.status


# ---- jitter --------------------------------------------------------------


@respx.mock
async def test_jitter_applied_with_expected_range(
    runner: EBIJobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every poll wait must be multiplied by random.uniform(0.8, 1.2).

    Without jitter, concurrent clients polling EBI stay lock-step.
    """
    calls: list[tuple[float, float]] = []

    def fake_uniform(a: float, b: float) -> float:
        calls.append((a, b))
        return 1.0  # deterministic 'no jitter' for test reproducibility

    monkeypatch.setattr("grounded_bio_mcp.clients.ebi.random.uniform", fake_uniform)

    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J"))
    respx.get(f"{BASE}/status/J").mock(
        side_effect=[
            httpx.Response(200, text="RUNNING"),
            httpx.Response(200, text="FINISHED"),
        ]
    )
    respx.get(f"{BASE}/result/J/x").mock(return_value=httpx.Response(200, content=b""))

    await runner.run(
        params={}, result_type="x",
        initial_interval=0.001, max_interval=0.001, timeout=5.0,
    )

    assert calls, "random.uniform was never called — jitter is missing"
    assert all(c == (0.8, 1.2) for c in calls), f"wrong jitter range: {calls}"


# ---- backoff -------------------------------------------------------------


@respx.mock
async def test_backoff_steps_up_after_several_polls(
    runner: EBIJobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After _BACKOFF_STEP_AFTER_POLLS consecutive polls, the wait grows
    toward max_interval. We observe via the asyncio.sleep args."""
    sleeps: list[float] = []

    real_sleep = asyncio.sleep

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)
        await real_sleep(0)  # still yield to event loop

    monkeypatch.setattr("grounded_bio_mcp.clients.ebi.asyncio.sleep", fake_sleep)
    # Freeze jitter to 1.0 so we can see raw intervals.
    monkeypatch.setattr(
        "grounded_bio_mcp.clients.ebi.random.uniform", lambda _a, _b: 1.0
    )

    respx.post(f"{BASE}/run/").mock(return_value=httpx.Response(200, text="J"))
    # 7 RUNNINGs then FINISHED — enough to trip the backoff.
    respx.get(f"{BASE}/status/J").mock(
        side_effect=[
            *([httpx.Response(200, text="RUNNING")] * 7),
            httpx.Response(200, text="FINISHED"),
        ]
    )
    respx.get(f"{BASE}/result/J/x").mock(return_value=httpx.Response(200, content=b""))

    await runner.run(
        params={}, result_type="x",
        initial_interval=1.0, max_interval=8.0, timeout=100.0,
    )

    # First few sleeps at ~1.0, later sleeps > 1.0 as backoff kicks in.
    assert sleeps[0] == 1.0
    assert max(sleeps) > 1.0, f"backoff never stepped up: {sleeps}"


# ---- semaphore sharing (critical for cross-service EBI cap) ------------


@pytest.mark.asyncio
async def test_shared_client_semaphore_caps_across_services() -> None:
    """Two runners (clustalo + iprscan5) sharing one RateLimitedClient must
    share the 3-concurrent cap — not get 3 each."""
    max_concurrent = 3
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, text="J")

    transport = httpx.MockTransport(handler)
    shared = RateLimitedClient(
        max_concurrent=max_concurrent, min_interval_s=0.0, transport=transport
    )
    try:
        r1 = EBIJobRunner("clustalo", shared)
        r2 = EBIJobRunner("iprscan5", shared)
        # Fire 12 submits across both services concurrently.
        await asyncio.gather(*(
            r.submit({"email": "x@y"}) for r in [r1, r2] * 6
        ))
    finally:
        await shared.aclose()

    assert peak <= max_concurrent, (
        f"cross-service EBI cap breached: peak={peak}, cap={max_concurrent}"
    )


# ---- list_result_types -------------------------------------------------


@respx.mock
async def test_list_result_types_returns_descriptors(runner: EBIJobRunner) -> None:
    respx.get(f"{BASE}/resulttypes/J-OK").mock(
        return_value=httpx.Response(
            200,
            json={
                "types": [
                    {"identifier": "aln-clustal_num", "label": "Clustal with nums"},
                    {"identifier": "fa", "label": "FASTA"},
                ]
            },
        )
    )
    types = await runner.list_result_types("J-OK")
    assert len(types) == 2
    assert types[0]["identifier"] == "aln-clustal_num"


# ---- integration (gated) -----------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("EBI_EMAIL"),
    reason="integration test needs EBI_EMAIL in environment",
)
async def test_integration_runner_real_clustal() -> None:
    """Real EBI Clustal Omega run — 3 tiny sequences; checks the whole loop."""
    import os as _os

    email = _os.environ["EBI_EMAIL"]
    client = RateLimitedClient(max_concurrent=3, min_interval_s=0.5, timeout=60.0)
    r = EBIJobRunner("clustalo", client)
    try:
        fasta = (
            ">a\nMKWVTFISLLFLFSSAYSRG\n"
            ">b\nMKWVTFLSLLFLFSSAYSRG\n"
            ">c\nMKWITFISLLFLFSSAYSRG\n"
        )
        result = await r.run(
            params={"email": email, "sequence": fasta, "stype": "protein"},
            result_type="aln-clustal_num",
            initial_interval=2.0,
            max_interval=10.0,
            timeout=180.0,
        )
    finally:
        await client.aclose()

    text = result.decode()
    assert "CLUSTAL" in text
    assert "a" in text and "b" in text and "c" in text
