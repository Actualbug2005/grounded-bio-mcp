"""`bio_fetch_compound` — small-molecule / compound data from ChEMBL and/or PubChem.

Phase 1, MVP. See spec §4.9.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch Compound Data".

Design decisions (approved 2026-04-24):

* **Parallel queries** (``asyncio.gather``) when ``source="both"`` and
  the identifier resolves natively on both sides (name / smiles / inchi).
  Doubles instantaneous concurrent request count by one against a
  *different* service; each service's ``RateLimitedClient`` bounds its
  own steady-state rate.
* **One-side miss returns what was found**, with explicit
  ``sources_queried`` and ``sources_found`` arrays so the model can see
  provenance. Error only when *both* sides miss.
* **Dual-source merge: ChEMBL wins** on structural and drug-curation
  fields (SMILES, InChI/InChIKey, formula, MW, LogP, HBD/HBA/rotatable,
  clinical phase, ATC classifications). PubChem contributes the IUPAC
  name and the synonym list (broader coverage). Per-field provenance
  lives under ``sources`` so a reader can see which source supplied
  each value.

Identifier-resolution matrix:

=================  ===========================  =========================================
identifier_type    ChEMBL resolution            PubChem resolution
=================  ===========================  =========================================
chembl_id          direct /molecule/{id}        via InChIKey from ChEMBL → /cids
pubchem_cid        via InChIKey from PubChem    direct /cid/{cid}/...
                   → /molecule/{inchikey}
name               /molecule/search?q=…         /compound/name/{name}/cids (may return ≥2)
smiles             /molecule/search?q=…         /compound/smiles/{smiles}/cids
inchi              /molecule/search?q=…         /compound/inchi/{inchi}/cids
=================  ===========================  =========================================

When PubChem's ``/cids`` endpoint returns multiple CIDs (common for
names that refer to stereoisomer/salt families), all CIDs surface as
``candidate_pubchem_cids`` with a ``disambiguation_hint`` so the model
can re-query with a more specific identifier.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from bioinformatics_mcp.clients.chembl import ChEMBLClient
from bioinformatics_mcp.clients.pubchem import (
    PubChemClient,
    PubChemCompoundNotFound,
)
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    BioMCPError,
    error_response,
)

logger = logging.getLogger(__name__)

IdentifierType = Literal[
    "name", "smiles", "inchi", "chembl_id", "pubchem_cid"
]
Source = Literal["chembl", "pubchem", "both"]

_DISAMBIGUATION_HINT = (
    "PubChem returned multiple CIDs for this identifier. This usually "
    "means the name refers to a family of related structures (salts, "
    "hydrates, stereoisomers). The first CID is PubChem's top-ranked "
    "match. To select a different member, re-query with a specific CID, "
    "SMILES, or InChI."
)

_CANDIDATE_SEARCH_LIMIT = 5


class FetchCompoundInput(BaseModel):
    """Input schema — spec §4.9."""

    identifier: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "Compound identifier: name, SMILES, InChI, ChEMBL ID, or PubChem CID."
        ),
    )
    identifier_type: IdentifierType
    source: Source = Field(
        default="both",
        description="Which database(s) to query.",
    )


# ---- ChEMBL side ---------------------------------------------------------


async def _resolve_chembl(
    client: ChEMBLClient,
    identifier: str,
    identifier_type: IdentifierType,
    bridge_inchikey: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return ``(chembl_record_or_none, candidate_chembl_ids)``."""
    try:
        if identifier_type == "chembl_id":
            record = await client.get_molecule(identifier)
            return record, []
        if identifier_type == "pubchem_cid":
            if not bridge_inchikey:
                return None, []
            record = await client.get_molecule(bridge_inchikey)
            return record, []
        # name / smiles / inchi: free-text search, take top hit.
        hits = await client.search_molecules(
            identifier, limit=_CANDIDATE_SEARCH_LIMIT
        )
        if not hits:
            return None, []
        top_id = hits[0].get("molecule_chembl_id")
        if not top_id:
            return None, []
        record = await client.get_molecule(top_id)
        return record, [h.get("molecule_chembl_id", "") for h in hits if h.get("molecule_chembl_id")]
    except AccessionNotFound:
        return None, []


# ---- PubChem side --------------------------------------------------------


async def _resolve_pubchem(
    client: PubChemClient,
    identifier: str,
    identifier_type: IdentifierType,
    bridge_inchikey: str | None,
) -> tuple[dict[str, Any] | None, list[int], list[str]]:
    """Return ``(pubchem_record_or_none, candidate_cids, synonyms)``.

    The synonyms list is fetched alongside the properties since the
    compound tool's output always wants both.
    """
    try:
        cid: int | None = None
        candidates: list[int] = []
        if identifier_type == "pubchem_cid":
            try:
                cid = int(identifier)
            except (TypeError, ValueError):
                return None, [], []
        elif identifier_type == "chembl_id":
            if not bridge_inchikey:
                return None, [], []
            cids = await client.resolve_to_cids("inchikey", bridge_inchikey)
            if not cids:
                return None, [], []
            cid = cids[0]
            candidates = cids
        else:
            # name / smiles / inchi
            cids = await client.resolve_to_cids(identifier_type, identifier)
            if not cids:
                return None, [], []
            cid = cids[0]
            candidates = cids

        if cid is None:
            return None, [], []
        properties = await client.get_properties(cid)
        synonyms = await client.get_synonyms(cid, limit=10)
        return properties, candidates, synonyms
    except PubChemCompoundNotFound:
        return None, [], []


# ---- merge ---------------------------------------------------------------


def _coerce_number(v: Any) -> Any:
    """Coerce string-encoded numbers to float/int where safe.

    ChEMBL serialises ``max_phase``, ``full_mwt``, etc. as strings;
    PubChem serialises ``MolecularWeight`` as a string. Keeping the
    raw types in the output surfaces cross-source inconsistency to
    the caller; we coerce here so numeric comparisons work.
    """
    if isinstance(v, str):
        try:
            if "." in v:
                return float(v)
            return int(v)
        except (TypeError, ValueError):
            return v
    return v


def _chembl_to_partial(record: dict[str, Any]) -> dict[str, Any]:
    structures = record.get("molecule_structures") or {}
    properties = record.get("molecule_properties") or {}
    return {
        "chembl_id": record.get("molecule_chembl_id"),
        "pref_name": record.get("pref_name"),
        "smiles": structures.get("canonical_smiles"),
        "inchi": structures.get("standard_inchi"),
        "inchi_key": structures.get("standard_inchi_key"),
        "molecular_formula": properties.get("full_molformula"),
        "molecular_weight": _coerce_number(properties.get("full_mwt")),
        "logp": _coerce_number(properties.get("alogp")),
        "h_bond_donors": _coerce_number(properties.get("hbd")),
        "h_bond_acceptors": _coerce_number(properties.get("hba")),
        "rotatable_bonds": _coerce_number(properties.get("rtb")),
        "clinical_phase": _coerce_number(record.get("max_phase")),
        "atc_classifications": list(record.get("atc_classifications") or []),
    }


def _pubchem_to_partial(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "pubchem_cid": record.get("CID"),
        "iupac_name": record.get("IUPACName"),
        "smiles": record.get("SMILES"),
        "inchi": record.get("InChI"),
        "inchi_key": record.get("InChIKey"),
        "molecular_formula": record.get("MolecularFormula"),
        "molecular_weight": _coerce_number(record.get("MolecularWeight")),
        "logp": _coerce_number(record.get("XLogP")),
        "h_bond_donors": _coerce_number(record.get("HBondDonorCount")),
        "h_bond_acceptors": _coerce_number(record.get("HBondAcceptorCount")),
        "rotatable_bonds": _coerce_number(record.get("RotatableBondCount")),
    }


_MERGE_FIELDS: tuple[str, ...] = (
    "smiles",
    "inchi",
    "inchi_key",
    "molecular_formula",
    "molecular_weight",
    "logp",
    "h_bond_donors",
    "h_bond_acceptors",
    "rotatable_bonds",
)


def _merge_with_provenance(
    chembl_partial: dict[str, Any] | None,
    pubchem_partial: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge partials with ChEMBL-wins policy; record provenance per field."""
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for field in _MERGE_FIELDS:
        c_val = chembl_partial.get(field) if chembl_partial else None
        p_val = pubchem_partial.get(field) if pubchem_partial else None
        if c_val not in (None, ""):
            merged[field] = c_val
            provenance[field] = "chembl"
        elif p_val not in (None, ""):
            merged[field] = p_val
            provenance[field] = "pubchem"
        else:
            merged[field] = None
    return merged, provenance


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ---- tool entry ----------------------------------------------------------


async def bio_fetch_compound(
    *,
    identifier: str,
    identifier_type: str,
    source: str,
    chembl: ChEMBLClient,
    pubchem: PubChemClient,
) -> dict[str, Any]:
    """Fetch structured compound data from ChEMBL and/or PubChem — spec §4.9."""
    try:
        params = FetchCompoundInput.model_validate(
            {
                "identifier": identifier,
                "identifier_type": identifier_type,
                "source": source,
            }
        )
    except ValidationError as exc:
        return error_response(
            f"Invalid input: {exc.errors()[0]['msg']}",
            suggestions=[
                "identifier_type must be one of: name, smiles, inchi, chembl_id, pubchem_cid.",
                "source must be one of: chembl, pubchem, both.",
            ],
        )

    want_chembl = params.source in ("chembl", "both")
    want_pubchem = params.source in ("pubchem", "both")
    sources_queried: list[str] = []
    if want_chembl:
        sources_queried.append("chembl")
    if want_pubchem:
        sources_queried.append("pubchem")

    chembl_record: dict[str, Any] | None = None
    pubchem_record: dict[str, Any] | None = None
    candidate_chembl_ids: list[str] = []
    candidate_pubchem_cids: list[int] = []
    pubchem_synonyms: list[str] = []

    # ---- resolve ----------------------------------------------------------
    # chembl_id / pubchem_cid with cross-source requires an InChIKey bridge:
    # the native source must be fetched first, then its InChIKey feeds the
    # other source. All other cases can resolve both sides in parallel.
    needs_chembl_first = (
        params.identifier_type == "chembl_id" and want_pubchem
    )
    needs_pubchem_first = (
        params.identifier_type == "pubchem_cid" and want_chembl
    )

    try:
        if needs_chembl_first:
            if want_chembl:
                chembl_record, candidate_chembl_ids = await _resolve_chembl(
                    chembl, params.identifier, params.identifier_type, None
                )
            bridge = (
                (chembl_record or {}).get("molecule_structures") or {}
            ).get("standard_inchi_key")
            if want_pubchem:
                (
                    pubchem_record,
                    candidate_pubchem_cids,
                    pubchem_synonyms,
                ) = await _resolve_pubchem(
                    pubchem, params.identifier, params.identifier_type, bridge
                )
        elif needs_pubchem_first:
            if want_pubchem:
                (
                    pubchem_record,
                    candidate_pubchem_cids,
                    pubchem_synonyms,
                ) = await _resolve_pubchem(
                    pubchem, params.identifier, params.identifier_type, None
                )
            bridge = (pubchem_record or {}).get("InChIKey")
            if want_chembl:
                chembl_record, candidate_chembl_ids = await _resolve_chembl(
                    chembl, params.identifier, params.identifier_type, bridge
                )
        else:
            # Parallel dual-source resolution for name / smiles / inchi,
            # or single-source for chembl and pubchem cases.
            tasks: list[asyncio.Task[Any]] = []
            chembl_task: asyncio.Task[Any] | None = None
            pubchem_task: asyncio.Task[Any] | None = None
            if want_chembl:
                chembl_task = asyncio.create_task(
                    _resolve_chembl(
                        chembl,
                        params.identifier,
                        params.identifier_type,
                        None,
                    )
                )
                tasks.append(chembl_task)
            if want_pubchem:
                pubchem_task = asyncio.create_task(
                    _resolve_pubchem(
                        pubchem,
                        params.identifier,
                        params.identifier_type,
                        None,
                    )
                )
                tasks.append(pubchem_task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)
            if chembl_task is not None:
                chembl_record, candidate_chembl_ids = chembl_task.result()
            if pubchem_task is not None:
                (
                    pubchem_record,
                    candidate_pubchem_cids,
                    pubchem_synonyms,
                ) = pubchem_task.result()
    except BioMCPError as exc:
        return error_response(str(exc))

    sources_found: list[str] = []
    if chembl_record:
        sources_found.append("chembl")
    if pubchem_record:
        sources_found.append("pubchem")

    if not sources_found:
        return error_response(
            f"Compound not found in any queried source for "
            f"{params.identifier_type}={params.identifier!r}.",
            suggestions=[
                "Try a different identifier_type (ChEMBL's free-text search "
                "is less reliable than PubChem's name index for common names).",
                "If using a SMILES, canonicalise it first — ChEMBL and "
                "PubChem both accept canonical or isomeric SMILES but not "
                "inconsistent whitespace.",
                "Broaden source to 'both' in case one database happens to "
                "be missing this compound.",
            ],
        )

    chembl_partial = _chembl_to_partial(chembl_record) if chembl_record else None
    pubchem_partial = (
        _pubchem_to_partial(pubchem_record) if pubchem_record else None
    )
    merged, provenance = _merge_with_provenance(chembl_partial, pubchem_partial)

    # Pull synonyms. ChEMBL has molecule_synonyms (dicts with syn_type);
    # we combine them with PubChem's ranked list and dedupe.
    chembl_synonyms_raw: list[str] = []
    if chembl_record:
        for syn in chembl_record.get("molecule_synonyms") or []:
            name = syn.get("synonyms") or syn.get("molecule_synonym")
            if isinstance(name, str):
                chembl_synonyms_raw.append(name)

    combined_synonyms = _dedupe_preserve_order(
        pubchem_synonyms + chembl_synonyms_raw
    )[:25]

    output: dict[str, Any] = {
        "identifier": params.identifier,
        "identifier_type": params.identifier_type,
        "sources_queried": sources_queried,
        "sources_found": sources_found,
        "chembl_id": (chembl_partial or {}).get("chembl_id"),
        "pubchem_cid": (pubchem_partial or {}).get("pubchem_cid"),
        "pref_name": (chembl_partial or {}).get("pref_name"),
        "iupac_name": (pubchem_partial or {}).get("iupac_name"),
        "structure": {
            "smiles": merged["smiles"],
            "inchi": merged["inchi"],
            "inchi_key": merged["inchi_key"],
        },
        "properties": {
            "molecular_formula": merged["molecular_formula"],
            "molecular_weight": merged["molecular_weight"],
            "logp": merged["logp"],
            "h_bond_donors": merged["h_bond_donors"],
            "h_bond_acceptors": merged["h_bond_acceptors"],
            "rotatable_bonds": merged["rotatable_bonds"],
        },
        "clinical_phase": (
            (chembl_partial or {}).get("clinical_phase")
            if chembl_partial
            else None
        ),
        "atc_classifications": (
            (chembl_partial or {}).get("atc_classifications") or []
        ),
        "synonyms": combined_synonyms,
        "sources": provenance,
        "see_also": (
            "For measured bioactivity data (target binding affinities, IC50, "
            "Ki, etc.) on this compound, use bio_fetch_bioactivity with "
            "query_type='compound'."
        ),
    }

    if len(candidate_chembl_ids) > 1:
        output["candidate_chembl_ids"] = candidate_chembl_ids
    if len(candidate_pubchem_cids) > 1:
        output["candidate_pubchem_cids"] = candidate_pubchem_cids
    if (
        len(candidate_chembl_ids) > 1
        or len(candidate_pubchem_cids) > 1
    ):
        output["disambiguation_hint"] = _DISAMBIGUATION_HINT

    return output
