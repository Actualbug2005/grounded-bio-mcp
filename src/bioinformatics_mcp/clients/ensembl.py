"""Ensembl REST client — spec §4.11, §4.12, §7.1.

Thin async wrapper around the subset of Ensembl REST the variant tools
need:

* ``/variation/{species}/{id}`` — variant details (rsID lookup).
* ``/vep/{species}/hgvs/{hgvs}`` — VEP by HGVS notation (accepts
  HGVS.c / HGVS.p / HGVS.g).
* ``/vep/{species}/region/{chr}:{start}-{end}:{strand}/{allele}`` — VEP
  by genomic region + alternate allele (REF is taken from the assembly).

Assembly routing is silent — a caller passing ``assembly="GRCh37"``
transparently gets results from ``grch37.rest.ensembl.org``; any other
value (or ``None``) routes to the GRCh38 default at
``rest.ensembl.org``. Each tool surfaces the actual assembly used in
its output (design decision approved 2026-04-24). See memory
``project_ensembl_client_pattern.md`` for rationale.

Rate limiter: spec §7.1 entry ``ensembl`` (15 concurrent, 0.07 s
interval → ~14 req/s). Verified against live response headers
``x-ratelimit-limit: 55000`` per hour (≈ 15.3 req/s) — no update
needed. A single ``RateLimitedClient`` is used for both GRCh38 and
GRCh37 traffic since they belong to the same Ensembl service and share
the published per-IP cap.

Error normalisation mirrors the rest of the project: 400 →
``AccessionNotFound`` (Ensembl's /variation returns 400 "not found" for
unknown rsIDs, and /vep returns 400 "Unable to parse HGVS notation"
for malformed HGVS), 429 → ``RateLimitExceeded``, 5xx →
``ExternalServiceDown``.
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

ENSEMBL_REST_BASE_URL = "https://rest.ensembl.org"
ENSEMBL_GRCH37_BASE_URL = "https://grch37.rest.ensembl.org"


class EnsemblClient:
    """Minimal async Ensembl REST client with GRCh38/GRCh37 routing."""

    def __init__(self) -> None:
        params = RATE_LIMITS["ensembl"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            timeout=30.0,
            headers={
                "User-Agent": "bioinformatics-mcp/0.2 (+ensembl-client)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- variation ------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def lookup_variation(
        self,
        species: str,
        variant_id: str,
        *,
        assembly: str | None = None,
        include_populations: bool = True,
    ) -> dict[str, Any]:
        """Fetch a variation record by rsID or synonym.

        ``include_populations=True`` adds ``?pops=1`` so population-level
        frequencies (gnomAD, 1000 Genomes, ESP, TOPMed) come back in the
        same round-trip. The spec-level MAF field on GRCh38 is often
        ``null`` because 1000 Genomes is GRCh37-aligned; the tool layer
        derives MAF from the populations array instead.
        """
        base = _resolve_base(assembly)
        params: dict[str, int] = {}
        if include_populations:
            params["pops"] = 1
        response = await self._client.request(
            "GET",
            f"{base}/variation/{species}/{variant_id}",
            params=params,
        )
        self._raise_for_status(response, identifier=variant_id)
        return response.json()

    # ---- VEP ------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def vep_hgvs(
        self,
        species: str,
        hgvs: str,
        *,
        assembly: str | None = None,
    ) -> list[dict[str, Any]]:
        """Predict consequences for a variant in HGVS notation.

        Accepts HGVS.c (transcript), HGVS.p (protein), HGVS.g (genomic).
        Ensembl returns a list even for single-variant input; callers
        treat it as a list of per-input results.
        """
        base = _resolve_base(assembly)
        response = await self._client.request(
            "GET",
            f"{base}/vep/{species}/hgvs/{hgvs}",
        )
        self._raise_for_status(response, identifier=hgvs)
        return list(response.json())

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def vep_region(
        self,
        species: str,
        *,
        region: str,
        strand: int,
        allele: str,
        assembly: str | None = None,
    ) -> list[dict[str, Any]]:
        """Predict consequences for a variant in region notation.

        ``region`` is ``chr:start-end`` (REF is read from the assembly
        at those coordinates; mismatched REF returns empty consequences
        rather than an explicit error). ``strand`` is ``1`` or ``-1``;
        ``allele`` is the alternate allele.
        """
        base = _resolve_base(assembly)
        response = await self._client.request(
            "GET",
            f"{base}/vep/{species}/region/{region}:{strand}/{allele}",
        )
        self._raise_for_status(response, identifier=f"{region}/{allele}")
        return list(response.json())

    # ---- error normalisation -------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status == 400:
            # Ensembl returns 400 for both "rs… not found" and
            # "Unable to parse HGVS notation". We map both to
            # AccessionNotFound so the tool layer surfaces a consistent
            # "unknown identifier" error.
            raise AccessionNotFound(accession=identifier, database="Ensembl")
        if status == 404:
            raise AccessionNotFound(accession=identifier, database="Ensembl")
        if status == 429:
            raise RateLimitExceeded(service="Ensembl", env_var=None)
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="Ensembl",
                reason=f"HTTP {status}",
                status_url="https://www.ensembl.org/Help/Contact",
            )
        if status >= 400:
            response.raise_for_status()


def _resolve_base(assembly: str | None) -> str:
    """Return the base URL for the requested assembly.

    ``"GRCh37"`` routes to the legacy server; everything else
    (including ``None`` and non-human assemblies) routes to the main
    GRCh38 server. Case-insensitive.
    """
    if assembly and assembly.upper() == "GRCH37":
        return ENSEMBL_GRCH37_BASE_URL
    return ENSEMBL_REST_BASE_URL
