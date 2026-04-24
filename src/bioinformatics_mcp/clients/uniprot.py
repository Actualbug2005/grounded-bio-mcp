"""UniProt REST client — spec §4.2, §7.1.

We hit the public UniProtKB endpoint (no authentication, no key). The JSON
schema is stable enough that tools parse it into narrow Pydantic models;
see ``tools/fetch_uniprot.py`` for that layer. This module's job is only
the HTTP + error-normalisation.

Endpoint: ``https://rest.uniprot.org/uniprotkb/{accession}.json``.
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

UNIPROT_BASE_URL = "https://rest.uniprot.org"


class UniProtClient:
    """Minimal async UniProtKB client."""

    def __init__(self) -> None:
        params = RATE_LIMITS["uniprot"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=UNIPROT_BASE_URL,
            timeout=30.0,
            headers={
                "User-Agent": "bioinformatics-mcp/0.2 (+uniprot-client)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_entry(self, accession: str) -> dict[str, Any]:
        """Return the full UniProtKB JSON entry for `accession`."""
        response = await self._client.request(
            "GET", f"/uniprotkb/{accession}.json"
        )
        self._raise_for_status(response, accession)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response, accession: str) -> None:
        status = response.status_code
        if status == 404:
            raise AccessionNotFound(accession=accession, database="UniProtKB")
        if status == 400:
            # UniProt returns 400 for malformed accessions as well as 404.
            raise AccessionNotFound(accession=accession, database="UniProtKB")
        if status == 429:
            raise RateLimitExceeded(service="UniProt", env_var=None)
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="UniProt",
                reason=f"HTTP {status}",
                status_url="https://status.uniprot.org/",
            )
        if status >= 400:
            response.raise_for_status()
