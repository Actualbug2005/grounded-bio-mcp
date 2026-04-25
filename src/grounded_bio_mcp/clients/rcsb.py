"""RCSB PDB client — spec §4.3, §7.1.

Two base URLs are used:

- ``data.rcsb.org`` — REST metadata (entry, polymer entity, assembly, …)
- ``files.rcsb.org`` — raw structure files (``.cif`` / ``.pdb``)

They share the same rate-limit budget from ``RATE_LIMITS["rcsb"]``. A
single :class:`RateLimitedClient` wouldn't suffice because ``base_url``
is pinned at construction, so we keep two clients with the same limiter
parameters — total concurrency matches the spec for the service as a
whole.
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

from grounded_bio_mcp.clients.base import RATE_LIMITS, RateLimitedClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
)

RCSB_DATA_BASE_URL = "https://data.rcsb.org"
RCSB_FILES_BASE_URL = "https://files.rcsb.org"


class RCSBClient:
    """Async client for the RCSB Data API and file distribution host."""

    def __init__(self) -> None:
        params = RATE_LIMITS["rcsb"]
        headers = {"User-Agent": "grounded-bio-mcp/0.2 (+rcsb-client)"}
        self._data = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=RCSB_DATA_BASE_URL,
            timeout=30.0,
            headers={**headers, "Accept": "application/json"},
        )
        self._files = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=RCSB_FILES_BASE_URL,
            timeout=60.0,
            headers=headers,
        )

    async def aclose(self) -> None:
        await self._data.aclose()
        await self._files.aclose()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_entry(self, pdb_id: str) -> dict[str, Any]:
        response = await self._data.request(
            "GET", f"/rest/v1/core/entry/{pdb_id.lower()}"
        )
        self._raise_for_status(response, pdb_id)
        return response.json()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_polymer_entity(self, pdb_id: str, entity_id: str) -> dict[str, Any]:
        response = await self._data.request(
            "GET", f"/rest/v1/core/polymer_entity/{pdb_id.lower()}/{entity_id}"
        )
        self._raise_for_status(response, f"{pdb_id}/{entity_id}")
        return response.json()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_cif(self, pdb_id: str) -> str:
        response = await self._files.request(
            "GET", f"/download/{pdb_id.lower()}.cif"
        )
        self._raise_for_status(response, pdb_id)
        return response.text

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status in (400, 404):
            raise AccessionNotFound(accession=identifier, database="RCSB PDB")
        if status == 429:
            raise RateLimitExceeded(service="RCSB", env_var=None)
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="RCSB",
                reason=f"HTTP {status}",
                status_url="https://www.rcsb.org/",
            )
        if status >= 400:
            response.raise_for_status()
