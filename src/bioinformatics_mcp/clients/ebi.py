"""EBI Job Dispatcher async-job runner — spec §4.5, §4.13, §7.1.

Shared submit → poll → fetch machinery for EBI REST services (Clustal
Omega, InterProScan, …). Every service reachable through the Job
Dispatcher exposes the same endpoint shape, so one runner class serves
all of them — each instance is pinned to a single service name and a
single `RateLimitedClient` (the `ebi` semaphore/interval pair from
`clients.base.RATE_LIMITS`).

**Cancellation is best-effort.** EBI's public docs and reference Python
client (github.com/ebi-wp/webservice-clients) do not advertise a
`DELETE /delete/{jobId}` endpoint. We try the call anyway on timeout so
we're a good citizen *if* the endpoint exists; a 404/405 is treated as
"endpoint unavailable" and logged at DEBUG, not WARNING. EBI auto-expires
jobs after 7 days regardless.

**Polling states.** `{PENDING, RUNNING, QUEUED}` are continuation states
(source: EBI's own reference client treats RUNNING *and* QUEUED as
"keep polling"). `FINISHED` is success. `FAILED`, `ERROR`, `NOT_FOUND`
raise `JobFailed`.

**Jitter.** Every poll wait is multiplied by `random.uniform(0.8, 1.2)`
so concurrent clients submitting at the same wall-clock second
desynchronise rather than polling in lockstep. Do not "simplify" to
deterministic — the anti-lock-step behaviour is the point.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from bioinformatics_mcp.utils.errors import (
    ExternalServiceDown,
    JobFailed,
    JobTimeoutError,
    RateLimitExceeded,
)
from bioinformatics_mcp.utils.rate_limit import RateLimitedClient

EBI_BASE_URL = "https://www.ebi.ac.uk/Tools/services/rest"

POLLING_STATES: frozenset[str] = frozenset({"PENDING", "RUNNING", "QUEUED"})
TERMINAL_FAILURE_STATES: frozenset[str] = frozenset({"FAILED", "ERROR", "NOT_FOUND"})
TERMINAL_SUCCESS_STATE = "FINISHED"

# Backoff knobs: after this many consecutive polls, step the wait up
# toward `max_interval`. 2 → 5 → 10 s is the spec §4.5 guidance shape.
_BACKOFF_STEP_AFTER_POLLS = 5

logger = logging.getLogger(__name__)


class EBIJobRunner:
    """Submit → poll → fetch pattern for one EBI Job Dispatcher service.

    Instantiate one per service (e.g. ``clustalo``, ``iprscan5``). Both
    runners for the same server must share the *same* `RateLimitedClient`
    so the EBI per-IP cap (3 concurrent / 500 ms) is enforced across
    services, not reset per-instance.

    Cancellation on timeout is best-effort and silent on 404/405.
    """

    def __init__(self, service: str, client: RateLimitedClient) -> None:
        self.service = service
        self._client = client
        self.base_url = f"{EBI_BASE_URL}/{service}"

    # ---- endpoint URL helpers ------------------------------------------------

    def _status_url(self, job_id: str) -> str:
        return f"{self.base_url}/status/{job_id}"

    def _result_url(self, job_id: str, result_type: str) -> str:
        return f"{self.base_url}/result/{job_id}/{result_type}"

    def _resulttypes_url(self, job_id: str) -> str:
        return f"{self.base_url}/resulttypes/{job_id}"

    def _delete_url(self, job_id: str) -> str:
        return f"{self.base_url}/delete/{job_id}"

    # ---- primitives ----------------------------------------------------------

    async def submit(self, params: dict[str, Any]) -> str:
        """POST form-encoded params to /run/, return the plain-text job ID."""
        response = await self._client.request(
            "POST", f"{self.base_url}/run/", data=params
        )
        self._raise_for_status(response, context="submit")
        job_id = response.text.strip()
        logger.info("%s submitted job_id=%s", self.service, job_id)
        return job_id

    async def get_status(self, job_id: str) -> str:
        response = await self._client.request("GET", self._status_url(job_id))
        self._raise_for_status(response, context="status", job_id=job_id)
        return response.text.strip()

    async def fetch_result(self, job_id: str, result_type: str) -> bytes:
        response = await self._client.request(
            "GET", self._result_url(job_id, result_type)
        )
        self._raise_for_status(response, context="result", job_id=job_id)
        return response.content

    async def list_result_types(self, job_id: str) -> list[dict[str, Any]]:
        """Return available result-type descriptors — used to probe mapping."""
        response = await self._client.request(
            "GET",
            self._resulttypes_url(job_id),
            headers={"Accept": "application/json"},
        )
        self._raise_for_status(response, context="resulttypes", job_id=job_id)
        payload = response.json()
        # EBI wraps as {"types": [...]} for JSON Accept; tolerate a bare list too.
        if isinstance(payload, dict):
            return payload.get("types") or []
        return payload or []

    async def cancel(self, job_id: str) -> bool:
        """Best-effort DELETE; returns True if EBI accepted the cancel.

        404/405 → endpoint absent or job already gone → log DEBUG, return False.
        Network / 5xx errors during cancel log WARNING but do not raise.
        """
        try:
            response = await self._client.request("DELETE", self._delete_url(job_id))
        except (httpx.RequestError, httpx.HTTPError) as exc:
            logger.warning(
                "%s cancel failed for job_id=%s: %s", self.service, job_id, exc
            )
            return False

        if 200 <= response.status_code < 300:
            logger.info("%s cancelled job_id=%s", self.service, job_id)
            return True
        if response.status_code in (404, 405):
            logger.debug(
                "%s cancel not supported / job already gone (status=%d) for job_id=%s",
                self.service,
                response.status_code,
                job_id,
            )
            return False
        logger.warning(
            "%s cancel unexpected status=%d for job_id=%s",
            self.service,
            response.status_code,
            job_id,
        )
        return False

    # ---- composite ----------------------------------------------------------

    async def run(
        self,
        params: dict[str, Any],
        result_type: str,
        *,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
    ) -> bytes:
        """Submit, poll until complete, fetch the result.

        On wall-clock timeout, best-effort-cancels the job and raises
        `JobTimeoutError`. On terminal failure status, raises `JobFailed`.
        """
        job_id = await self.submit(params)
        await self.poll_until_complete(
            job_id,
            initial_interval=initial_interval,
            max_interval=max_interval,
            timeout=timeout,
        )
        return await self.fetch_result(job_id, result_type)

    async def poll_until_complete(
        self,
        job_id: str,
        *,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
    ) -> str:
        """Poll /status until FINISHED; backoff + jitter between polls."""
        start = time.monotonic()
        poll_count = 0
        interval = initial_interval

        while True:
            status = await self.get_status(job_id)
            logger.debug("%s job_id=%s status=%s", self.service, job_id, status)

            if status == TERMINAL_SUCCESS_STATE:
                elapsed = time.monotonic() - start
                logger.info(
                    "%s job_id=%s FINISHED in %.1fs", self.service, job_id, elapsed
                )
                return status

            if status in TERMINAL_FAILURE_STATES:
                logger.info(
                    "%s job_id=%s terminated status=%s", self.service, job_id, status
                )
                raise JobFailed(service=self.service, job_id=job_id, status=status)

            if status not in POLLING_STATES:
                # Unknown status values: treat as failure rather than polling
                # forever. If EBI adds a new "keep polling" state, bump
                # `POLLING_STATES` rather than relaxing this check.
                raise JobFailed(
                    service=self.service, job_id=job_id, status=f"UNKNOWN:{status}"
                )

            poll_count += 1
            # Step the interval up after a burst of consecutive polls.
            if poll_count >= _BACKOFF_STEP_AFTER_POLLS and interval < max_interval:
                interval = min(interval * 2.5, max_interval)

            # Jitter desynchronises concurrent callers — do not remove.
            jittered = interval * random.uniform(0.8, 1.2)

            if time.monotonic() - start + jittered > timeout:
                cancelled = await self.cancel(job_id)
                raise JobTimeoutError(
                    service=self.service,
                    job_id=job_id,
                    timeout_s=timeout,
                    status_url=self._status_url(job_id),
                    cancelled=cancelled,
                )

            await asyncio.sleep(jittered)

    # ---- status mapping ------------------------------------------------------

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        *,
        context: str,
        job_id: str | None = None,
    ) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 429:
            raise RateLimitExceeded(service="EBI")
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="EBI",
                reason=f"HTTP {status} during {context}"
                + (f" for job {job_id}" if job_id else ""),
                status_url="https://www.ebi.ac.uk/Tools/common/status",
            )
        # Surface the body in the exception because EBI embeds the actual
        # reason ("Invalid email address", "Sequence too short", etc.) there.
        response.raise_for_status()
