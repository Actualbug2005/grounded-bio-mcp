"""NCBI E-utilities client — spec §4.1 + §7.1.

**Architectural pattern for all NCBI-backed tools** (`bio_fetch_sequence`,
`bio_fetch_gene`, `bio_blast_search`, any future `bio_fetch_pubmed` etc.):
we issue raw HTTPS requests to `eutils.ncbi.nlm.nih.gov` through the shared
:class:`~bioinformatics_mcp.utils.rate_limit.RateLimitedClient` and use
Biopython *only for parsing* the returned text. We deliberately do **not**
call ``Bio.Entrez.efetch`` directly. Rationale:

1. **Rate-limit uniformity.** Spec §7.1 lists per-service concurrency caps
   and minimum inter-request gaps. Those limits are only real if every
   upstream call goes through ``RateLimitedClient``. ``Bio.Entrez`` would
   route around it.
2. **Async end-to-end.** ``Bio.Entrez`` is synchronous and would block the
   asyncio event loop; wrapping it in ``asyncio.to_thread`` is strictly
   worse than doing the HTTP ourselves (extra thread pool, no shared
   client, no composable retry).
3. **Biopython stays in the role it's good at.** ``SeqIO.read`` on a string
   is pure-CPU parsing — synchronous is correct. The parse-only usage also
   isolates us from ``Bio.Entrez`` parameter quirks.

Endpoint base: ``https://eutils.ncbi.nlm.nih.gov/entrez/eutils``. We send
``email`` (from ``EBI_EMAIL``; NCBI accepts and encourages it) and
``api_key`` (from ``NCBI_API_KEY``) when available so the 10 req/s tier
applies.
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
from bioinformatics_mcp.config import Settings
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
)

EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class NCBIClient:
    """Thin async wrapper over NCBI E-utilities.

    Exactly one instance should be long-lived across the server's lifetime;
    the embedded :class:`RateLimitedClient` holds an ``httpx.AsyncClient``
    with a connection pool.
    """

    def __init__(self, settings: Settings) -> None:
        limit_key = "ncbi_with_key" if settings.ncbi_api_key else "ncbi_no_key"
        params = RATE_LIMITS[limit_key]
        self._api_key = settings.ncbi_api_key
        self._email = settings.ebi_email  # NCBI accepts the EBI contact email too.
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=EUTILS_BASE_URL,
            timeout=30.0,
            headers={"User-Agent": "bioinformatics-mcp/0.2 (+ncbi-client)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def efetch(
        self,
        *,
        db: str,
        accession: str,
        rettype: str,
        retmode: str = "text",
    ) -> str:
        """Fetch a record as text from NCBI's ``efetch.fcgi``.

        Raises :class:`AccessionNotFound` on 400/404 or on the textual
        error messages NCBI sometimes returns with a 200 status, and
        :class:`RateLimitExceeded` / :class:`ExternalServiceDown` on
        transient failures (both are retried by the decorator).
        """
        query: dict[str, Any] = {
            "db": db,
            "id": accession,
            "rettype": rettype,
            "retmode": retmode,
        }
        if self._api_key:
            query["api_key"] = self._api_key
        if self._email:
            query["email"] = self._email

        response = await self._client.request("GET", "/efetch.fcgi", params=query)
        self._raise_for_status(response, db=db, accession=accession)
        return response.text

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def esearch(
        self,
        *,
        db: str,
        term: str,
        retmax: int = 20,
    ) -> list[str]:
        """Resolve a query term to a list of NCBI IDs via ``esearch.fcgi``.

        Used by :mod:`bio_fetch_gene` to translate a gene symbol +
        organism into a Gene ID list. The JSON ``esearchresult.idlist``
        is returned directly; callers decide whether a single hit is a
        unique resolution or multiple hits require disambiguation.
        """
        query: dict[str, Any] = {
            "db": db,
            "term": term,
            "retmax": int(retmax),
            "retmode": "json",
        }
        if self._api_key:
            query["api_key"] = self._api_key
        if self._email:
            query["email"] = self._email

        response = await self._client.request("GET", "/esearch.fcgi", params=query)
        self._raise_for_status(response, db=db, accession=term)
        payload = response.json()
        return list(payload.get("esearchresult", {}).get("idlist", []))

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def esummary(
        self,
        *,
        db: str,
        ids: list[str],
    ) -> dict[str, Any]:
        """Fetch structured summary records for a batch of IDs.

        Returns the raw ``result`` dict from the JSON esummary response:
        keyed by UID with a ``uids`` list. Callers iterate the UIDs to
        build disambiguation candidates or drill into a unique record.
        """
        if not ids:
            return {"uids": []}
        query: dict[str, Any] = {
            "db": db,
            "id": ",".join(ids),
            "retmode": "json",
        }
        if self._api_key:
            query["api_key"] = self._api_key
        if self._email:
            query["email"] = self._email

        response = await self._client.request("GET", "/esummary.fcgi", params=query)
        self._raise_for_status(response, db=db, accession=",".join(ids))
        payload = response.json()
        return dict(payload.get("result", {}))

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def efetch_xml(
        self,
        *,
        db: str,
        uid: str,
    ) -> str:
        """Fetch a record as Entrezgene XML (or db-appropriate XML).

        Used by :mod:`bio_fetch_gene` to retrieve the full Entrezgene
        record — RefSeq transcripts, GO annotations, UniProt/Ensembl
        cross-references. Parsers live in the tool layer, not here.
        """
        query: dict[str, Any] = {
            "db": db,
            "id": uid,
            "retmode": "xml",
        }
        if self._api_key:
            query["api_key"] = self._api_key
        if self._email:
            query["email"] = self._email

        response = await self._client.request("GET", "/efetch.fcgi", params=query)
        self._raise_for_status(response, db=db, accession=uid)
        return response.text

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, db: str, accession: str) -> None:
        status = response.status_code
        if status in (400, 404):
            raise AccessionNotFound(accession=accession, database=db)
        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise RateLimitExceeded(
                service="NCBI",
                retry_after=retry_after,
                env_var="NCBI_API_KEY",
            )
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="NCBI",
                reason=f"HTTP {status}",
                status_url="https://www.ncbi.nlm.nih.gov/home/about/outage/",
            )
        if status >= 400:
            response.raise_for_status()

        # NCBI sometimes returns 200 with the body "Error: …" when the id
        # is bogus. Only check the head of the payload so we don't drag a
        # 30 MB GenBank record through a substring search.
        head = response.text[:256].lstrip().lower()
        if head.startswith(("error", "<error", "id list is empty")):
            raise AccessionNotFound(accession=accession, database=db)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
