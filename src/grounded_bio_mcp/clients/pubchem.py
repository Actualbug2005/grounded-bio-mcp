"""PubChem PUG REST client — spec §4.9, §7.1.

Thin async wrapper around the three PubChem endpoints the compound
tool needs:

* ``/compound/cid/{cid}/property/{fields}/JSON`` — structured
  chemical data (SMILES, InChI, InChIKey, formula, MW, LogP,
  HBD/HBA, rotatable bonds, IUPAC name).
* ``/compound/cid/{cid}/synonyms/JSON`` — ranked synonym list
  (PubChem returns hundreds — we cap to the caller's ``limit``).
* ``/compound/{namespace}/{identifier}/cids/JSON`` — resolve
  names, SMILES, InChI, and InChIKeys to one or more CIDs (names
  may legitimately map to multiple CIDs for stereoisomers/salts).

PubChem quirks verified 2026-04-24:

* Current SMILES property is simply ``SMILES`` — the old
  ``CanonicalSMILES`` / ``IsomericSMILES`` names were retired.
* ``MolecularWeight`` is a string-encoded decimal.
* Valid-format unknown CID returns HTTP 200 with Properties
  containing ONLY the CID field. Treated as ``PubChemCompoundNotFound``
  by the client — a partial record is worse than a clean error
  because downstream tools can't distinguish "PubChem says no"
  from "PubChem says yes but with empty fields".
* Malformed identifier returns HTTP 400 with a ``Fault`` body;
  also treated as not-found (the distinction between malformed
  and absent isn't useful to callers).
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
    BioMCPError,
    ExternalServiceDown,
    RateLimitExceeded,
)

PUBCHEM_BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

PubChemNamespace = Literal["name", "smiles", "inchi", "inchikey", "cid"]

# Fields we always request. ChEMBL covers some of the same ground
# (SMILES, InChI, formula, MW), but PubChem's values aren't identical
# — e.g. PubChem's XLogP vs ChEMBL's alogp — and the compound tool
# surfaces both sources' values where they diverge.
_PROPERTY_FIELDS: tuple[str, ...] = (
    "SMILES",
    "ConnectivitySMILES",
    "InChI",
    "InChIKey",
    "MolecularFormula",
    "MolecularWeight",
    "XLogP",
    "HBondDonorCount",
    "HBondAcceptorCount",
    "RotatableBondCount",
    "IUPACName",
)


class PubChemCompoundNotFound(BioMCPError):
    """PubChem has no compound matching the given identifier."""

    def __init__(self, identifier: str, namespace: str) -> None:
        super().__init__(
            f"PubChem has no compound for {namespace}={identifier!r}."
        )
        self.identifier = identifier
        self.namespace = namespace


class PubChemClient:
    """Minimal async PubChem PUG REST client."""

    def __init__(self) -> None:
        params = RATE_LIMITS["pubchem"]
        self._client = RateLimitedClient(
            max_concurrent=params.max_concurrent,
            min_interval_s=params.min_interval_s,
            base_url=PUBCHEM_BASE_URL,
            timeout=30.0,
            headers={
                "User-Agent": "grounded-bio-mcp/0.2 (+pubchem-client)",
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
    async def get_properties(self, cid: int) -> dict[str, Any]:
        fields = ",".join(_PROPERTY_FIELDS)
        response = await self._client.request(
            "GET", f"/compound/cid/{cid}/property/{fields}/JSON"
        )
        self._raise_for_status(
            response, identifier=str(cid), namespace="cid"
        )
        payload = response.json()
        entries = (payload.get("PropertyTable") or {}).get("Properties") or []
        if not entries:
            raise PubChemCompoundNotFound(identifier=str(cid), namespace="cid")
        record = entries[0]
        # "Ghost" records: only CID present, no molecular data.
        if set(record.keys()) == {"CID"}:
            raise PubChemCompoundNotFound(identifier=str(cid), namespace="cid")
        return record

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get_synonyms(self, cid: int, limit: int) -> list[str]:
        response = await self._client.request(
            "GET", f"/compound/cid/{cid}/synonyms/JSON"
        )
        self._raise_for_status(
            response, identifier=str(cid), namespace="cid"
        )
        info_list = response.json().get("InformationList") or {}
        information = info_list.get("Information") or []
        if not information:
            return []
        return list(information[0].get("Synonym") or [])[:limit]

    @retry(
        retry=retry_if_exception_type((RateLimitExceeded, ExternalServiceDown)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def resolve_to_cids(
        self, namespace: PubChemNamespace, identifier: str
    ) -> list[int]:
        # PubChem accepts SMILES in the URL path; httpx percent-encodes it
        # automatically, so no manual quoting is needed.
        response = await self._client.request(
            "GET", f"/compound/{namespace}/{identifier}/cids/JSON"
        )
        self._raise_for_status(response, identifier=identifier, namespace=namespace)
        cid_list = (response.json().get("IdentifierList") or {}).get("CID") or []
        return [int(c) for c in cid_list]

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, identifier: str, namespace: str
    ) -> None:
        status = response.status_code
        if status == 404:
            raise PubChemCompoundNotFound(identifier=identifier, namespace=namespace)
        if status == 400:
            # PubChem's PUG REST uses 400 for malformed identifiers.
            # We treat malformed-vs-missing as the same for callers.
            raise PubChemCompoundNotFound(identifier=identifier, namespace=namespace)
        if status == 429:
            raise RateLimitExceeded(service="PubChem", env_var=None)
        if status in (502, 503, 504):
            raise ExternalServiceDown(
                service="PubChem",
                reason=f"HTTP {status}",
                status_url="https://pubchem.ncbi.nlm.nih.gov/",
            )
        if status >= 400:
            response.raise_for_status()
