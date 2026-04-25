"""Europe PMC REST client — spec §4.14, §4.15, §7.1.

Thin async wrapper around the two Europe PMC endpoints the literature
tools need:

* ``/search?query=...&resultType=core&format=json`` — ranked papers with
  full metadata: ``abstractText``, ``isOpenAccess``, ``inPMC``, ``pmcid``,
  ``doi``, ``journalInfo``, ``meshHeadingList``, ``fullTextIdList``.
* ``/{PMC_ID}/fullTextXML`` — JATS 1.4 XML body; 404 when no PMC fulltext
  is available for that identifier.

``resultType`` is always ``core`` rather than ``lite`` because the tool
layer needs the availability flags (``isOpenAccess``, ``inPMC``, ``hasPDF``)
and the abstract text to decide whether to follow up with a fulltext
fetch; ``lite`` drops those fields to save bytes.

Rate limiter: spec §7.1 entry ``europepmc`` (10 concurrent, 0.1 s
interval). No live ``X-RateLimit-*`` response headers were observed
during pre-work probing on 2026-04-24, so the spec values stand.

Error normalisation follows the rest of the project: 404 on fulltext →
``AccessionNotFound`` (meaning "no fulltext for this identifier");
429 → ``RateLimitExceeded``; 5xx → ``ExternalServiceDown``.
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

EUROPEPMC_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"


class EuropePMCClient:
    """Minimal async Europe PMC REST client."""

    def __init__(self) -> None:
        params = RATE_LIMITS["europepmc"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            timeout=60.0,
            headers={
                "User-Agent": "grounded-bio-mcp/0.2 (+europepmc-client)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- search ---------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def search(
        self,
        query: str,
        *,
        max_results: int = 20,
        open_access_only: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> dict[str, Any]:
        """Run a Europe PMC search and return the parsed ``core`` response.

        Europe PMC has no dedicated query parameters for open-access or
        year filters; they are encoded directly into the Lucene-style
        query string (``OPEN_ACCESS:Y``, ``PUB_YEAR:[2015 TO 2020]``).
        The caller passes a plain-text query; this method composes the
        final filter expression.
        """
        full_query = _compose_query(
            query,
            open_access_only=open_access_only,
            year_from=year_from,
            year_to=year_to,
        )
        response = await self._client.request(
            "GET",
            f"{EUROPEPMC_BASE_URL}/search",
            params={
                "query": full_query,
                "format": "json",
                "resultType": "core",
                "pageSize": max_results,
            },
        )
        self._raise_for_status(response, identifier=query)
        return response.json()

    # ---- fulltext -------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def fetch_fulltext_xml(self, pmc_id: str) -> bytes:
        """Return the JATS fulltext XML bytes for ``pmc_id``.

        Accepts either ``PMC5059666`` or the bare numeric form
        ``5059666`` — the latter is normalised to the ``PMC`` prefix
        before the request is sent. 404 means "no fulltext XML is
        available for this identifier" and maps to
        :class:`AccessionNotFound`; the tool layer then falls back to
        the abstract-only path.
        """
        normalised = _normalise_pmc_id(pmc_id)
        # Per-request Accept override — Europe PMC returns 406 when the
        # default Accept: application/json is sent to /fullTextXML.
        response = await self._client.request(
            "GET",
            f"{EUROPEPMC_BASE_URL}/{normalised}/fullTextXML",
            headers={"Accept": "application/xml"},
        )
        self._raise_for_status(response, identifier=normalised)
        return response.content

    # ---- error normalisation -------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status == 404:
            raise AccessionNotFound(accession=identifier, database="Europe PMC")
        if status == 429:
            raise RateLimitExceeded(service="Europe PMC", env_var=None)
        if status in (500, 502, 503, 504):
            raise ExternalServiceDown(
                service="Europe PMC",
                reason=f"HTTP {status}",
                status_url="https://europepmc.org/",
            )
        if status >= 400:
            response.raise_for_status()


# ---- helpers -------------------------------------------------------------


def _normalise_pmc_id(pmc_id: str) -> str:
    """Return ``PMC<digits>`` for either ``PMC5059666`` or ``5059666`` input."""
    stripped = pmc_id.strip()
    if stripped.upper().startswith("PMC"):
        return "PMC" + stripped[3:]
    return "PMC" + stripped


def _compose_query(
    query: str,
    *,
    open_access_only: bool,
    year_from: int | None,
    year_to: int | None,
) -> str:
    """Compose the Lucene-style filter expression Europe PMC expects."""
    parts = [query]
    if open_access_only:
        parts.append("OPEN_ACCESS:Y")
    if year_from is not None or year_to is not None:
        lo = year_from if year_from is not None else 1800
        hi = year_to if year_to is not None else 2100
        parts.append(f"PUB_YEAR:[{lo} TO {hi}]")
    return " AND ".join(parts)
