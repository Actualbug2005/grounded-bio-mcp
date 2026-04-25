"""Reactome Content Service client — spec §4.17, §7.1.

Thin async wrapper around the three Reactome REST endpoints the pathway
tool needs:

* ``/data/query/{stId}`` — full pathway record. **The spec's
  implementation note at §4.17 says ``/data/``; the sub-path is
  ``/query``, not ``/pathway``.** The obvious-looking
  ``/data/pathway/{stId}`` returns 404 against live Reactome (verified
  2026-04-24). Captured as spec errata in memory.
* ``/data/mapping/{resource}/{identifier}/pathways?species={taxon}`` —
  all pathways containing the given entity for a given species.
  ``resource`` is ``UniProt`` (for protein accessions) or ``Ensembl``.
* ``/search/query?query=...&types=Pathway`` — gene-symbol / free-text
  pathway search. Results are envelope-wrapped as
  ``{results: [{typeName, entries: [...]}]}`` with each entry stamped
  with a ``species: [...]`` list so the tool layer can surface
  cross-species candidates for disambiguation.

Rate limiter: spec §7.1 entry ``reactome`` (5 concurrent, 0.2 s
interval). No authentication required.

Error normalisation: 404 → ``AccessionNotFound`` (Reactome's 404 body
is a clean JSON ``{code, reason, messages[]}`` with the submitted ID in
the URL — we use the caller's identifier, not the Reactome message,
for consistency with other clients); 429 → ``RateLimitExceeded``; 5xx
→ ``ExternalServiceDown``.
"""

from __future__ import annotations

from typing import Any, Literal

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

REACTOME_BASE_URL = "https://reactome.org/ContentService"


class ReactomeClient:
    """Minimal async Reactome Content Service client."""

    def __init__(self) -> None:
        params = RATE_LIMITS["reactome"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            timeout=30.0,
            headers={
                "User-Agent": "grounded-bio-mcp/0.2 (+reactome-client)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- /data/query/{stId} --------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def query_pathway(self, stable_id: str) -> dict[str, Any]:
        """Fetch the full pathway record for a Reactome stable ID.

        Returns Reactome's JSON representation verbatim; the tool layer
        trims it to the spec §4.17 output shape.
        """
        response = await self._client.request(
            "GET", f"{REACTOME_BASE_URL}/data/query/{stable_id}"
        )
        self._raise_for_status(response, identifier=stable_id)
        return response.json()

    # ---- /data/mapping/{resource}/{identifier}/pathways -----------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def mapping_to_pathways(
        self,
        *,
        resource: Literal["UniProt", "Ensembl"],
        identifier: str,
        species_taxon: int | None = None,
    ) -> list[dict[str, Any]]:
        """List pathways containing the given protein / entity."""
        params: dict[str, str] = {}
        if species_taxon is not None:
            params["species"] = str(species_taxon)
        response = await self._client.request(
            "GET",
            f"{REACTOME_BASE_URL}/data/mapping/{resource}/{identifier}/pathways",
            params=params,
        )
        self._raise_for_status(response, identifier=identifier)
        return list(response.json())

    # ---- /search/query --------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def search_pathways(
        self,
        *,
        query: str,
        species: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search pathways by free-text / gene-symbol.

        Returns the ``results`` envelope — a list of groups, each with
        ``typeName`` and ``entries: [...]``. Empty results surface as
        an empty list, not an error (a query with no hits is a valid
        outcome, same as the gene-tool pattern).
        """
        params: dict[str, str] = {"query": query, "types": "Pathway"}
        if species:
            params["species"] = species
        response = await self._client.request(
            "GET", f"{REACTOME_BASE_URL}/search/query", params=params
        )
        # The search endpoint returns 404 when there are zero hits;
        # that is a valid empty-result, not an error.
        if response.status_code == 404:
            return []
        self._raise_for_status(response, identifier=query)
        payload = response.json()
        return list(payload.get("results") or [])

    # ---- error normalisation -------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status == 404:
            raise AccessionNotFound(accession=identifier, database="Reactome")
        if status == 429:
            raise RateLimitExceeded(service="Reactome", env_var=None)
        if status in (500, 502, 503, 504):
            raise ExternalServiceDown(
                service="Reactome",
                reason=f"HTTP {status}",
                status_url="https://reactome.org/",
            )
        if status >= 400:
            response.raise_for_status()
