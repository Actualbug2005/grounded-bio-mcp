"""`bio_fetch_uniprot` — curated UniProt protein record fetch by accession.

Phase 1, MVP. See spec §4.2.

Annotations: ``readOnlyHint=True``, ``destructiveHint=False``,
``openWorldHint=True``, ``idempotentHint=True``, ``title="Fetch UniProt Record"``.

**Output modelling policy:** narrow, not a full mirror of UniProt's JSON.
Reasons: (1) UniProt's schema occasionally shifts — a narrow model breaks
loudly on fields we actually depend on, rather than silently passing
through corrupted nested data; (2) it keeps MCP-response tokens tight;
(3) it documents what this tool *promises*, so future tool authors know
what's available without reading UniProt's spec. We surface the subset
spec §4.2 requires, plus ``entry_version`` and
``last_sequence_update_date`` because they help the model reason about
whether a cited sequence has changed since a cited paper was written.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from bioinformatics_mcp.clients.uniprot import UniProtClient
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)
from bioinformatics_mcp.utils.formatting import ResponseFormat, format_response


class FetchUniProtInput(BaseModel):
    """Input schema for ``bio_fetch_uniprot`` (spec §4.2)."""

    accession: str = Field(
        ...,
        pattern=r"^[A-Z0-9]{6,10}$",
        description="UniProt accession (e.g., 'A0A1E1GEY0', 'O43866', 'Q9QWK4').",
    )
    include_features: bool = Field(
        default=True,
        description="Include domain/region/disulfide/variant annotations in the response.",
    )
    response_format: ResponseFormat = Field(
        default="json",
        description="Output rendering.",
    )


# Cross-reference databases the model is most likely to need the bridge for.
# Others are still returned but grouped under 'other' so the important ones
# stand out at the top of the response.
_PRIORITY_XREFS: frozenset[str] = frozenset(
    {"PDB", "AlphaFoldDB", "RefSeq", "EMBL", "Ensembl", "InterPro", "Pfam", "SMART"}
)


def _extract_organism(entry: dict[str, Any]) -> dict[str, Any]:
    organism = entry.get("organism") or {}
    return {
        "scientific_name": organism.get("scientificName"),
        "common_name": organism.get("commonName"),
        "taxon_id": organism.get("taxonId"),
    }


def _extract_protein_name(entry: dict[str, Any]) -> str | None:
    desc = (entry.get("proteinDescription") or {}).get("recommendedName") or {}
    return ((desc.get("fullName") or {}).get("value")) or None


def _extract_sequence_block(entry: dict[str, Any]) -> dict[str, Any]:
    seq = entry.get("sequence") or {}
    return {
        "sequence": seq.get("value", ""),
        "length": seq.get("length", 0),
        "molecular_weight": seq.get("molWeight"),
    }


def _extract_features(entry: dict[str, Any]) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for feat in entry.get("features") or []:
        location = feat.get("location") or {}
        start = ((location.get("start") or {}).get("value"))
        end = ((location.get("end") or {}).get("value"))
        features.append(
            {
                "type": feat.get("type"),
                "start": start,
                "end": end,
                "description": feat.get("description") or "",
                "feature_id": feat.get("featureId"),
            }
        )
    return features


def _extract_cross_references(entry: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for xref in entry.get("uniProtKBCrossReferences") or []:
        db = xref.get("database") or "other"
        bucket = db if db in _PRIORITY_XREFS else "other"
        grouped.setdefault(bucket, []).append(
            {
                "database": db,
                "id": xref.get("id"),
                "properties": {
                    (p.get("key") or ""): p.get("value")
                    for p in (xref.get("properties") or [])
                    if p.get("key")
                },
            }
        )
    return grouped


def _build_output(
    entry: dict[str, Any],
    include_features: bool,
) -> dict[str, Any]:
    audit = entry.get("entryAudit") or {}
    output: dict[str, Any] = {
        "accession": entry.get("primaryAccession"),
        "entry_name": entry.get("uniProtkbId"),
        "entry_type": entry.get("entryType"),
        "protein_name": _extract_protein_name(entry),
        "organism": _extract_organism(entry),
        **_extract_sequence_block(entry),
        "entry_version": audit.get("entryVersion"),
        "last_sequence_update_date": audit.get("lastSequenceUpdateDate"),
        "cross_references": _extract_cross_references(entry),
    }
    if include_features:
        output["features"] = _extract_features(entry)
    return output


async def fetch_uniprot(
    accession: str,
    include_features: bool = True,
    response_format: Literal["json", "markdown"] = "json",
    *,
    client: UniProtClient,
) -> dict[str, Any] | str:
    """Fetch a curated UniProtKB entry by accession."""
    params = FetchUniProtInput(
        accession=accession,
        include_features=include_features,
        response_format=response_format,
    )
    try:
        entry = await client.fetch_entry(params.accession)
    except AccessionNotFound as exc:
        return error_response(
            f"Accession '{exc.accession}' not found in UniProtKB.",
            suggestions=[
                "Check the accession is a valid 6–10 char UniProt accession (e.g., P01308).",
                "If you only have a gene name, search UniProt's web UI first or use bio_blast_search.",
            ],
        )
    except RateLimitExceeded:
        return error_response(
            "UniProt rate limit exceeded. Retry in a moment.",
            suggestions=["Reduce parallel UniProt calls — spec §7.1 caps at 5 concurrent, 0.2 s apart."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"UniProt API is unreachable: {exc.reason}.",
            suggestions=[
                "Transient upstream error. Retry in a few minutes.",
                f"Status: {exc.status_url}" if exc.status_url else "",
            ],
        )

    output = _build_output(entry, include_features=params.include_features)
    return format_response(output, params.response_format)
