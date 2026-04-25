"""`bio_fetch_pdb` — experimentally-determined protein structure fetch from RCSB.

Phase 1, MVP. See spec §4.3.

Annotations: ``readOnlyHint=True``, ``destructiveHint=False``,
``openWorldHint=True``, ``idempotentHint=True``, ``title="Fetch PDB Structure"``.

Returns metadata by default; ``include_coordinates=True`` additionally
inlines the mmCIF file. Inline coordinates are **soft-capped at 2 MB**
(~95 % of PDB entries fit, ribosomes and large viral capsids do not); on
overflow, the tool returns the metadata and an actionable error pointing
the caller at the direct ``files.rcsb.org`` URL so they can fetch it out
of band.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from grounded_bio_mcp.clients.rcsb import RCSBClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)
from grounded_bio_mcp.utils.formatting import soft_cap_with_url_fallback

# Spec approval: 2 MB inline cap. Covers the vast majority of PDB entries
# while protecting against ribosome-scale payloads (25 MB+). Counted in
# UTF-8 encoded bytes of the CIF text.
COORDINATES_SOFT_CAP_BYTES = 2 * 1024 * 1024


class FetchPDBInput(BaseModel):
    """Input schema for ``bio_fetch_pdb`` (spec §4.3)."""

    pdb_id: str = Field(
        ...,
        pattern=r"^[0-9][A-Za-z0-9]{3}$",
        description="4-character PDB ID (e.g., '7XKB', '1CRN').",
    )
    include_coordinates: bool = Field(
        default=False,
        description=(
            "If True, also inline the mmCIF structure file. Capped at ~2 MB; "
            "larger structures return metadata plus a URL to fetch directly."
        ),
    )
    chain_filter: str | None = Field(
        default=None,
        description="If set, restrict chain information to this auth chain ID (e.g. 'A').",
    )


def _normalise_resolution(entry: dict[str, Any]) -> float | None:
    info = entry.get("rcsb_entry_info") or {}
    values = info.get("resolution_combined") or []
    return float(values[0]) if values else None


def _experimental_method(entry: dict[str, Any]) -> str | None:
    # Prefer the mmCIF-canonical `exptl[0].method` ("X-RAY DIFFRACTION",
    # "ELECTRON MICROSCOPY", "SOLUTION NMR") over RCSB's shorthand
    # `rcsb_entry_info.experimental_method` ("X-ray"). The canonical term
    # is stable across RCSB relabelling and matches what the model is
    # most likely to recognise from the literature.
    exptl = entry.get("exptl") or []
    if exptl:
        method = exptl[0].get("method")
        if method:
            return method
    info = entry.get("rcsb_entry_info") or {}
    return info.get("experimental_method")


def _r_factors(entry: dict[str, Any]) -> dict[str, float | None]:
    refine = entry.get("refine") or []
    if not refine:
        return {"r_work": None, "r_free": None}
    first = refine[0]
    return {
        "r_work": first.get("ls_R_factor_R_work"),
        "r_free": first.get("ls_R_factor_R_free"),
    }


def _chain_from_polymer_entity(entity: dict[str, Any]) -> dict[str, Any]:
    poly = entity.get("entity_poly") or {}
    ids = entity.get("rcsb_polymer_entity_container_identifiers") or {}
    desc = entity.get("rcsb_polymer_entity") or {}
    strand = poly.get("pdbx_strand_id") or ""
    auth_chains: list[str] = ids.get("auth_asym_ids") or (
        [c.strip() for c in strand.split(",") if c.strip()]
    )
    return {
        "entity_id": ids.get("entity_id"),
        "auth_chains": auth_chains,
        "polymer_type": poly.get("type"),
        "sequence": poly.get("pdbx_seq_one_letter_code_can", ""),
        "length": poly.get("rcsb_sample_sequence_length"),
        "description": desc.get("pdbx_description"),
    }


def _filter_chains(
    chains: list[dict[str, Any]], chain_filter: str | None
) -> list[dict[str, Any]]:
    if not chain_filter:
        return chains
    return [c for c in chains if chain_filter in (c.get("auth_chains") or [])]


async def _collect_chains(
    client: RCSBClient, pdb_id: str, entity_ids: list[str]
) -> list[dict[str, Any]]:
    if not entity_ids:
        return []
    entities = await asyncio.gather(
        *(client.fetch_polymer_entity(pdb_id, eid) for eid in entity_ids)
    )
    return [_chain_from_polymer_entity(e) for e in entities]


def _build_metadata(entry: dict[str, Any], chains: list[dict[str, Any]]) -> dict[str, Any]:
    access = entry.get("rcsb_accession_info") or {}
    symmetry = entry.get("symmetry") or {}
    struct = entry.get("struct") or {}
    container = entry.get("rcsb_entry_container_identifiers") or {}
    return {
        "pdb_id": entry.get("rcsb_id"),
        "title": struct.get("title"),
        "experimental_method": _experimental_method(entry),
        "resolution": _normalise_resolution(entry),
        "deposit_date": access.get("deposit_date"),
        "release_date": access.get("initial_release_date"),
        "space_group": symmetry.get("space_group_name_H_M"),
        "r_factors": _r_factors(entry),
        "chains": chains,
        "non_polymer_entity_ids": container.get("non_polymer_entity_ids") or [],
        "assembly_ids": container.get("assembly_ids") or [],
    }


async def fetch_pdb(
    pdb_id: str,
    include_coordinates: bool = False,
    chain_filter: str | None = None,
    *,
    client: RCSBClient,
) -> dict[str, Any]:
    """Fetch metadata (and optionally coordinates) for a PDB entry."""
    params = FetchPDBInput(
        pdb_id=pdb_id,
        include_coordinates=include_coordinates,
        chain_filter=chain_filter,
    )
    try:
        entry = await client.fetch_entry(params.pdb_id)
        entity_ids = (
            (entry.get("rcsb_entry_container_identifiers") or {}).get(
                "polymer_entity_ids"
            )
            or []
        )
        chains = await _collect_chains(client, params.pdb_id, entity_ids)
    except AccessionNotFound as exc:
        return error_response(
            f"PDB ID '{exc.accession}' not found in RCSB.",
            suggestions=[
                "Check the PDB ID — it must be a 4-character code starting with a digit (e.g., '1CRN').",
                "If you only know a protein name, try bio_fetch_uniprot and look at cross_references.PDB.",
            ],
        )
    except RateLimitExceeded:
        return error_response(
            "RCSB rate limit exceeded. Retry in a moment.",
            suggestions=["Spec §7.1 caps RCSB at 10 concurrent, 0.1 s apart."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"RCSB API is unreachable: {exc.reason}.",
            suggestions=["Transient upstream error. Retry in a few minutes."],
        )

    filtered_chains = _filter_chains(chains, params.chain_filter)
    result = _build_metadata(entry, filtered_chains)

    if params.include_coordinates:
        try:
            cif_text = await client.fetch_cif(params.pdb_id)
        except AccessionNotFound:
            result["coordinates_error"] = (
                f"CIF file for '{params.pdb_id}' not available at files.rcsb.org."
            )
        else:
            result.update(
                soft_cap_with_url_fallback(
                    cif_text,
                    cap_bytes=COORDINATES_SOFT_CAP_BYTES,
                    fallback_url=(
                        f"https://files.rcsb.org/download/{params.pdb_id.lower()}.cif"
                    ),
                    key_prefix="coordinates",
                    format_label="mmCIF",
                    overage_noun="Structure",
                )
            )

    return result
