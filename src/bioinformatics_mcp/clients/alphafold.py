"""AlphaFold DB client — spec §4.4, §7.1.

Two endpoint families, both on ``alphafold.ebi.ac.uk``:

- ``/api/prediction/{accession}`` — JSON metadata (one entry per available
  AlphaFold model). Contains ``pdbUrl`` / ``cifUrl`` / ``paeDocUrl`` /
  ``globalMetricValue`` / ``latestVersion``.
- ``/files/…`` — structure files and PAE JSON. The URL is **read from
  metadata** rather than constructed locally; AlphaFold increments the
  version string (``_v4``, ``_v5`` …) over time.

Both share one :class:`RateLimitedClient` — the metadata call and the
subsequent file download are the same host with the same published limit
(spec §7.1: 5 concurrent, 0.2 s apart). Passing an absolute URL to
``request`` overrides ``base_url``, so a single client services both.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bioinformatics_mcp.clients.base import RATE_LIMITS, RateLimitedClient
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
)

ALPHAFOLD_BASE_URL = "https://alphafold.ebi.ac.uk"


class AlphaFoldClient:
    """Async client for the EBI AlphaFold Database."""

    def __init__(self) -> None:
        params = RATE_LIMITS["alphafold"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=ALPHAFOLD_BASE_URL,
            timeout=60.0,
            headers={"User-Agent": "bioinformatics-mcp/0.2 (+alphafold-client)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_prediction(self, accession: str) -> list[dict[str, Any]]:
        """Return the prediction metadata list for `accession` (often length 1)."""
        response = await self._client.request(
            "GET", f"/api/prediction/{accession}"
        )
        self._raise_for_status(response, accession)
        data = response.json()
        # The API sometimes returns `{}` for "no prediction"; normalise to [].
        return data if isinstance(data, list) else []

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_structure_file(self, url: str, *, identifier: str) -> str:
        """Fetch a PDB/CIF file whose URL came from a prediction record.

        ``url`` is expected absolute (that is what the metadata response
        returns); ``httpx`` ignores ``base_url`` for absolute requests, so
        the rate-limited client still gates the call.
        """
        response = await self._client.request("GET", url)
        self._raise_for_status(response, identifier)
        return response.text

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status in (400, 404):
            raise AccessionNotFound(accession=identifier, database="AlphaFold DB")
        if status == 429:
            raise RateLimitExceeded(service="AlphaFold", env_var=None)
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="AlphaFold",
                reason=f"HTTP {status}",
                status_url="https://alphafold.ebi.ac.uk/",
            )
        if status >= 400:
            response.raise_for_status()
