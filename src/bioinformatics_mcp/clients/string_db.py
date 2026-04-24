"""STRING REST client — spec §4.18, §7.1.

Thin async wrapper around two STRING endpoints:

* ``/api/json/interaction_partners`` — top-N interaction partners of
  the query protein. This is the endpoint spec §4.18 actually needs.
  ``/api/json/network`` returns the *induced subgraph* of the queried
  protein plus its neighbours (so a single-protein query produces
  edges between unrelated neighbours rather than edges-to-the-query),
  which is the wrong semantics for "what does X interact with".
* ``/api/json/get_string_ids`` — resolves free-text identifiers
  (gene symbols, UniProt accessions) to STRING's native ENSP IDs.

**Score scale — load-bearing documentation:**

* Input ``required_score`` is on the **0-1000 scale** (so ``700``
  requests a 0.7 threshold). The client forwards this verbatim.
* Output ``score`` and sub-scores are on the **0-1 scale**. The
  client does not convert — callers are told the scale on both sides.

A caller asking for high-confidence edges at threshold 0.9 who passes
that as ``required_score`` instead of ``900`` would accidentally get
threshold 0.0009 — effectively every edge STRING knows. This is the
single scale-confusion bug the tool layer must document out of
existence; see the tool's field descriptions for belt-and-braces
documentation redundancy.

**Server-side filter reliability:** verified 2026-04-24 that 30
partners at ``required_score=900`` all had ``score ≥ 0.998`` (zero
leakage). Unlike ChEMBL's ``confidence_score__gte`` — which leaked
76% of rows — STRING's ``required_score`` is honestly enforced.
We trust it and do not add a client-side counter; the
``test_interaction_partners_filter_is_not_leaky`` unit test is the
canary for future drift.

**User-Agent email:** STRING's docs ask for a contact email so they
can reach out if a user causes problems. Pattern: if
``STRING_USER_EMAIL`` is set (threaded in via the tool layer when
constructing the client), the client appends ``(+mailto:<email>)``
to its User-Agent. Missing email logs a warning at init; unlike EBI
Job Dispatcher, STRING does not enforce it.

Rate limiter: spec §7.1 entry ``string`` (3 concurrent, 1.0 s
interval). STRING requests ``<1 req/sec`` in their guidance.

Error normalisation: 404 → ``AccessionNotFound`` (protein not found
in the supplied taxon — STRING's 404 body is an error-array JSON);
429 → ``RateLimitExceeded``; 5xx → ``ExternalServiceDown``.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

STRING_BASE_URL = "https://string-db.org"


class StringDBClient:
    """Minimal async STRING REST client."""

    def __init__(self, *, user_email: str | None) -> None:
        self.user_email = user_email
        if user_email:
            ua = (
                f"bioinformatics-mcp/0.2 (+string-client) (+mailto:{user_email})"
            )
        else:
            logger.warning(
                "STRING_USER_EMAIL not set; STRING requests a contact email "
                "for courtesy. Set STRING_USER_EMAIL in the server env."
            )
            ua = "bioinformatics-mcp/0.2 (+string-client)"
        params = RATE_LIMITS["string"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            timeout=30.0,
            headers={
                "User-Agent": ua,
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- interaction_partners ------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def interaction_partners(
        self,
        *,
        identifier: str,
        species_taxon: int,
        required_score: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return top-N interaction partners of ``identifier``.

        ``required_score`` is on the 0-1000 input scale; the endpoint's
        output ``score`` is on the 0-1 scale — see module docstring.
        """
        response = await self._client.request(
            "GET",
            f"{STRING_BASE_URL}/api/json/interaction_partners",
            params={
                "identifiers": identifier,
                "species": species_taxon,
                "required_score": required_score,
                "limit": limit,
            },
        )
        self._raise_for_status(response, identifier=identifier)
        return list(response.json())

    # ---- get_string_ids ------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get_string_ids(
        self,
        *,
        identifier: str,
        species_taxon: int,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Resolve a free-text identifier to STRING's native ENSP ID."""
        response = await self._client.request(
            "GET",
            f"{STRING_BASE_URL}/api/json/get_string_ids",
            params={
                "identifiers": identifier,
                "species": species_taxon,
                "limit": limit,
            },
        )
        self._raise_for_status(response, identifier=identifier)
        return list(response.json())

    # ---- error normalisation -------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status == 404:
            raise AccessionNotFound(accession=identifier, database="STRING")
        if status == 429:
            raise RateLimitExceeded(service="STRING", env_var=None)
        if status in (500, 502, 503, 504):
            raise ExternalServiceDown(
                service="STRING",
                reason=f"HTTP {status}",
                status_url="https://string-db.org/",
            )
        if status >= 400:
            response.raise_for_status()
