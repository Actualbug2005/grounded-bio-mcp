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

import asyncio
import logging
import random
import re
import time
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
    JobFailed,
    JobTimeoutError,
    RateLimitExceeded,
)

logger = logging.getLogger(__name__)

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
        # BLAST lives on a different host. Same rate-limit profile (NCBI's
        # documented per-IP cap), but the slow polling cadence is what
        # actually controls BLAST traffic — see ``blast_run`` for the
        # 15→60 s ramp.
        self._blast_client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=BLAST_BASE_URL,
            timeout=60.0,
            headers={"User-Agent": "bioinformatics-mcp/0.2 (+ncbi-blast)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._blast_client.aclose()

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

    # ---- BLAST URL API --------------------------------------------------

    async def blast_submit(
        self,
        *,
        program: str,
        database: str,
        query: str,
        max_hits: int,
        e_value: float,
        organism_filter: str | None = None,
    ) -> tuple[str, int]:
        """POST CMD=Put to /Blast.cgi; return ``(RID, RTOE_seconds)``.

        ``organism_filter`` is passed through as ``ENTREZ_QUERY`` (NCBI's
        Entrez query syntax — e.g. ``"Mammalia[ORGN]"``). Raises
        :class:`JobFailed` if the response 200s without a QBlastInfo
        block (NCBI maintenance pages, unexpected error pages).
        """
        form: dict[str, Any] = {
            "CMD": "Put",
            "PROGRAM": program,
            "DATABASE": database,
            "QUERY": query,
            "HITLIST_SIZE": str(max_hits),
            "EXPECT": str(e_value),
            "FORMAT_TYPE": "JSON2_S",
        }
        if organism_filter:
            form["ENTREZ_QUERY"] = organism_filter
        if self._api_key:
            form["api_key"] = self._api_key
        if self._email:
            form["email"] = self._email
        response = await self._blast_client.request(
            "POST", "/Blast.cgi", data=form
        )
        self._raise_blast_status(response, context="submit")
        info = _parse_qblast_info(response.text)
        rid = info.get("RID")
        rtoe = info.get("RTOE")
        if not rid:
            raise JobFailed(
                service="BLAST",
                job_id="<unsubmitted>",
                status="MISSING_QBLAST_INFO",
            )
        try:
            rtoe_s = int(rtoe) if rtoe is not None else 0
        except ValueError:
            rtoe_s = 0
        logger.info("BLAST submitted rid=%s rtoe=%ds", rid, rtoe_s)
        return rid, rtoe_s

    async def blast_status(self, rid: str) -> str:
        """GET CMD=Get + FORMAT_OBJECT=SearchInfo; return the parsed Status
        token (WAITING / READY / UNKNOWN / FAILED). Empty string if the
        QBlastInfo block is absent — caller decides if that's fatal."""
        params = {"CMD": "Get", "RID": rid, "FORMAT_OBJECT": "SearchInfo"}
        response = await self._blast_client.request(
            "GET", "/Blast.cgi", params=params
        )
        self._raise_blast_status(response, context="status", rid=rid)
        return _parse_qblast_info(response.text).get("Status", "")

    async def blast_fetch_result(self, rid: str) -> dict[str, Any]:
        """GET CMD=Get + FORMAT_TYPE=JSON2_S; return the parsed
        ``BlastOutput2`` dict. Caller drills to
        ``BlastOutput2[0].report.results.search.hits`` for hit data."""
        params = {"CMD": "Get", "RID": rid, "FORMAT_TYPE": "JSON2_S"}
        response = await self._blast_client.request(
            "GET", "/Blast.cgi", params=params
        )
        self._raise_blast_status(response, context="result", rid=rid)
        return response.json()

    async def blast_run(
        self,
        *,
        program: str,
        database: str,
        query: str,
        max_hits: int,
        e_value: float,
        organism_filter: str | None = None,
        initial_interval: float = 15.0,
        max_interval: float = 60.0,
        max_wait_seconds: float = 600.0,
        ramp_after_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Submit, poll until READY, fetch result.

        Polling cadence: ``initial_interval`` (default 15 s) until
        ``ramp_after_seconds`` of wall time has elapsed (default 300 s),
        then ``max_interval`` (default 60 s). Each wait is jittered
        0.8-1.2× to desynchronise concurrent callers — do not remove.
        Wall-clock timeout raises :class:`JobTimeoutError`; FAILED /
        UNKNOWN / unrecognised statuses raise :class:`JobFailed`.
        """
        rid, _ = await self.blast_submit(
            program=program,
            database=database,
            query=query,
            max_hits=max_hits,
            e_value=e_value,
            organism_filter=organism_filter,
        )
        await self._poll_blast(
            rid,
            initial_interval=initial_interval,
            max_interval=max_interval,
            max_wait_seconds=max_wait_seconds,
            ramp_after_seconds=ramp_after_seconds,
        )
        return await self.blast_fetch_result(rid)

    async def _poll_blast(
        self,
        rid: str,
        *,
        initial_interval: float,
        max_interval: float,
        max_wait_seconds: float,
        ramp_after_seconds: float,
    ) -> None:
        start = time.monotonic()
        while True:
            status = await self.blast_status(rid)
            if status == "READY":
                logger.info(
                    "BLAST rid=%s READY in %.1fs", rid, time.monotonic() - start
                )
                return
            if status in ("FAILED", "UNKNOWN", ""):
                raise JobFailed(
                    service="BLAST",
                    job_id=rid,
                    status=status or "EMPTY_QBLAST_INFO",
                )
            if status != "WAITING":
                raise JobFailed(
                    service="BLAST", job_id=rid, status=f"UNEXPECTED:{status}"
                )

            elapsed = time.monotonic() - start
            interval = initial_interval if elapsed < ramp_after_seconds else max_interval
            jittered = interval * random.uniform(0.8, 1.2)
            if elapsed + jittered > max_wait_seconds:
                raise JobTimeoutError(
                    service="BLAST",
                    job_id=rid,
                    timeout_s=max_wait_seconds,
                    status_url=f"{BLAST_BASE_URL}/Blast.cgi?CMD=Get&RID={rid}",
                    cancelled=False,
                )
            await asyncio.sleep(jittered)

    @staticmethod
    def _raise_blast_status(
        response: httpx.Response, *, context: str, rid: str | None = None
    ) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 429:
            raise RateLimitExceeded(service="NCBI-BLAST", env_var="NCBI_API_KEY")
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="NCBI-BLAST",
                reason=f"HTTP {status} on {context}",
                status_url="https://blast.ncbi.nlm.nih.gov/",
            )
        raise JobFailed(
            service="BLAST",
            job_id=rid or "<unsubmitted>",
            status=f"HTTP_{status}",
        )

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


# ---------------------------------------------------------------------------
# BLAST URL API (separate host: blast.ncbi.nlm.nih.gov)
#
# Conceptually similar to EBI's Job Dispatcher (submit → poll → fetch) but
# with three NCBI-specific quirks:
#
#  1. Submit + poll responses are HTML; the machine-readable payload sits
#     inside an HTML comment block called QBlastInfo with one ``key=value``
#     per line. Only the result fetch returns clean JSON (FORMAT_TYPE=
#     JSON2_S).
#  2. The polling interval has to be much longer than EBI — NCBI's BLAST
#     etiquette caps batch-job polling at one request per ~60 s for
#     queries running longer than five minutes. We start at 15 s and
#     ramp toward 60 s, with the same 0.8-1.2× jitter the EBI runner uses.
#  3. ``Status=UNKNOWN`` is genuinely ambiguous — it means either the RID
#     expired (NCBI keeps results for ~24 h) or the RID was never valid.
#     Callers cannot distinguish, so we surface both possibilities.
# ---------------------------------------------------------------------------

BLAST_BASE_URL = "https://blast.ncbi.nlm.nih.gov"
BLAST_WEB_RESULT_URL_FMT = (
    "https://blast.ncbi.nlm.nih.gov/Blast.cgi?CMD=Get&RID={rid}"
)


_QBLAST_KEY_RE = re.compile(r"\b(RID|RTOE|Status)\s*=\s*(\S+)")


def _parse_qblast_info(html: str) -> dict[str, str]:
    """Extract the ``key = value`` pairs from any QBlastInfo block in
    ``html``. Returns a dict keyed by RID / RTOE / Status; missing keys
    are simply absent (no exceptions raised). The regex is whitespace-
    tolerant so it works on both submit responses (where the block is
    indented inside an HTML comment) and status responses (where the
    block sometimes lacks the surrounding ``<!-- -->`` markers).
    """
    return dict(_QBLAST_KEY_RE.findall(html))


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
