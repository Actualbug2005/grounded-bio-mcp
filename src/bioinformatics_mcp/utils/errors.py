"""Actionable error responses for tools — spec §8.

Tools return structured errors the model can *recover from*, not raw stack
traces. Every error carries a short human-readable reason plus zero-or-more
suggestions telling the model what to try next. Internal details (API keys,
filesystem paths, tracebacks) must never make it into the returned text —
log those server-side only.

Usage inside a tool handler:

    try:
        result = await fetch(...)
    except AccessionNotFound as e:
        return error_response(
            f"Accession '{e.accession}' not found in {e.database}.",
            suggestions=[
                "Check the accession format.",
                "Try bio_blast_search if the exact accession is unknown.",
            ],
        )
"""

from __future__ import annotations

from typing import Any


class BioMCPError(Exception):
    """Base for all domain-specific errors raised by service clients."""


class AccessionNotFound(BioMCPError):
    def __init__(self, accession: str, database: str) -> None:
        super().__init__(f"Accession '{accession}' not found in {database}.")
        self.accession = accession
        self.database = database


class RateLimitExceeded(BioMCPError):
    def __init__(
        self,
        service: str,
        retry_after: float | None = None,
        env_var: str | None = None,
    ) -> None:
        super().__init__(f"{service} rate limit exceeded.")
        self.service = service
        self.retry_after = retry_after
        self.env_var = env_var


class ExternalServiceDown(BioMCPError):
    def __init__(
        self,
        service: str,
        reason: str,
        status_url: str | None = None,
    ) -> None:
        super().__init__(f"{service} API is unreachable: {reason}.")
        self.service = service
        self.reason = reason
        self.status_url = status_url


class JobTimeoutError(BioMCPError):
    """Raised when an async job (EBI Clustal/InterProScan/etc.) exceeds its wall-clock budget.

    Carries the job ID so callers can surface it in their error response —
    users can poll the EBI status URL manually if they need the eventual
    result. The runner best-effort-cancels the job before raising; whether
    that succeeded is in `cancelled`.
    """

    def __init__(
        self,
        service: str,
        job_id: str,
        timeout_s: float,
        status_url: str,
        cancelled: bool,
    ) -> None:
        cancel_note = " Cancelled." if cancelled else ""
        super().__init__(
            f"{service} job {job_id} did not complete in {timeout_s:.0f}s.{cancel_note} "
            f"Check status at {status_url}"
        )
        self.service = service
        self.job_id = job_id
        self.timeout_s = timeout_s
        self.status_url = status_url
        self.cancelled = cancelled


class JobFailed(BioMCPError):
    """Raised when an EBI job returns a terminal FAILED / ERROR / NOT_FOUND status."""

    def __init__(self, service: str, job_id: str, status: str) -> None:
        super().__init__(f"{service} job {job_id} ended with status {status}.")
        self.service = service
        self.job_id = job_id
        self.status = status


def error_response(
    message: str,
    suggestions: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the standard error payload a tool returns on failure.

    The shape is a plain `dict` so callers can include it directly in their
    returned structured content, or JSON-dump it for a text response.
    `suggestions` is filtered to drop empty strings so callers can supply
    conditional entries without cluttering output.
    """
    filtered_suggestions = [s for s in (suggestions or []) if s]
    payload: dict[str, Any] = {
        "error": True,
        "message": message,
    }
    if filtered_suggestions:
        payload["suggestions"] = filtered_suggestions
    if extra:
        payload.update(extra)
    return payload
