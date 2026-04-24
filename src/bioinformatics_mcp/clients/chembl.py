"""ChEMBL REST client — spec §4.9, §4.10, §7.1.

Thin async wrapper around the five ChEMBL endpoints the compound and
bioactivity tools need:

* ``/molecule/{id}.json`` — structured compound record (SMILES, InChI,
  properties, synonyms, max_phase, ATC classifications, cross-refs).
* ``/molecule/search?q=…`` — relevance-ranked free-text search.
* ``/activity.json`` — measured bioactivity records, filterable by
  ``molecule_chembl_id``, ``target_chembl_id``, ``standard_type__in``
  and ``confidence_score__gte`` (the confidence filter joins to the
  related assay row — the returned activity record does NOT itself
  include ``confidence_score``).
* ``/assay.json?assay_chembl_id__in=…`` — batch lookup for confidence
  score + description, which the bioactivity tool merges onto each
  activity row (since activities don't carry it).
* ``/target.json?target_chembl_id__in=…`` — batch lookup for
  UniProt accession(s) under ``target_components``.

Rate limiter: spec §7.1 entry ``chembl`` (3 concurrent, 0.34 s interval
≈ 3 req/s). No authentication.

Error normalisation mirrors ``UniProtClient``: 404 →
``AccessionNotFound``, 429 → ``RateLimitExceeded``, 5xx →
``ExternalServiceDown``.
"""

from __future__ import annotations

from collections.abc import Sequence
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

CHEMBL_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"


class ChEMBLClient:
    """Minimal async ChEMBL REST client."""

    def __init__(self) -> None:
        params = RATE_LIMITS["chembl"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=CHEMBL_BASE_URL,
            timeout=30.0,
            headers={
                "User-Agent": "bioinformatics-mcp/0.2 (+chembl-client)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- molecule ---------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get_molecule(self, chembl_id: str) -> dict[str, Any]:
        response = await self._client.request("GET", f"/molecule/{chembl_id}.json")
        self._raise_for_status(response, identifier=chembl_id)
        return response.json()

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def search_molecules(
        self, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        response = await self._client.request(
            "GET",
            "/molecule/search",
            params={"q": query, "format": "json", "limit": limit},
        )
        self._raise_for_status(response, identifier=query)
        payload = response.json()
        return list(payload.get("molecules", []))

    # ---- activity ---------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def list_activities(
        self,
        *,
        molecule_chembl_id: str | None,
        target_chembl_id: str | None,
        activity_types: Sequence[str],
        min_confidence: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if molecule_chembl_id is None and target_chembl_id is None:
            raise ValueError(
                "list_activities requires at least one of molecule_chembl_id "
                "or target_chembl_id"
            )
        params: dict[str, str | int] = {
            "confidence_score__gte": int(min_confidence),
            "standard_type__in": ",".join(activity_types),
            "limit": int(limit),
            "offset": int(offset),
            "format": "json",
        }
        if molecule_chembl_id is not None:
            params["molecule_chembl_id"] = molecule_chembl_id
        if target_chembl_id is not None:
            params["target_chembl_id"] = target_chembl_id
        response = await self._client.request("GET", "/activity.json", params=params)
        self._raise_for_status(response, identifier="activity")
        return response.json()

    # ---- batch enrichment -------------------------------------------------

    async def list_assays_by_ids(
        self, assay_chembl_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not assay_chembl_ids:
            return []
        return await self._batch_fetch(
            "/assay.json",
            param_name="assay_chembl_id__in",
            ids=assay_chembl_ids,
            collection_key="assays",
        )

    async def list_targets_by_ids(
        self, target_chembl_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not target_chembl_ids:
            return []
        return await self._batch_fetch(
            "/target.json",
            param_name="target_chembl_id__in",
            ids=target_chembl_ids,
            collection_key="targets",
        )

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _batch_fetch(
        self,
        path: str,
        *,
        param_name: str,
        ids: Sequence[str],
        collection_key: str,
    ) -> list[dict[str, Any]]:
        response = await self._client.request(
            "GET",
            path,
            params={param_name: ",".join(ids), "limit": len(ids), "format": "json"},
        )
        self._raise_for_status(response, identifier=",".join(ids))
        return list(response.json().get(collection_key, []))

    # ---- error normalisation ---------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response, identifier: str) -> None:
        status = response.status_code
        if status == 404:
            raise AccessionNotFound(accession=identifier, database="ChEMBL")
        if status == 429:
            raise RateLimitExceeded(service="ChEMBL", env_var=None)
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="ChEMBL",
                reason=f"HTTP {status}",
                status_url="https://www.ebi.ac.uk/chembl/",
            )
        if status >= 400:
            response.raise_for_status()
