"""`bio_fetch_alphafold` — AlphaFold2 predicted structure fetch from EBI AlphaFold DB.

Phase 1, MVP. See spec §4.4.

Annotations: ``readOnlyHint=True``, ``destructiveHint=False``,
``openWorldHint=True``, ``idempotentHint=True``,
``title="Fetch AlphaFold Prediction"``.

**Critical anti-hallucination behaviour (spec §4.4):** the pLDDT summary
(overall mean and N-term / middle / C-term per-region breakdown) is
returned on *every* response, regardless of the ``format`` parameter.
The model needs the confidence context even when it asks for the full
structure; pLDDT < 70 regions are unreliable and must not be cited
without that caveat.

**URL handling:** the ``pdbUrl`` / ``cifUrl`` come from the metadata
response rather than being constructed locally. AlphaFold increments
the version string (``_v4``, ``_v5``, …) over time; hardcoding it is a
silent-break pattern we deliberately avoid.
"""

from __future__ import annotations

import io
from typing import Any, Literal

from Bio.PDB import PDBParser
from pydantic import BaseModel, Field

from grounded_bio_mcp.clients.alphafold import AlphaFoldClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)


class FetchAlphaFoldInput(BaseModel):
    """Input schema for ``bio_fetch_alphafold`` (spec §4.4)."""

    uniprot_accession: str = Field(
        ...,
        pattern=r"^[A-Z0-9]{6,10}$",
        description="UniProt accession to fetch prediction for (e.g., 'P01308').",
    )
    format: Literal["pdb", "cif", "summary"] = Field(
        default="summary",
        description=(
            "summary=metadata + pLDDT summary only; "
            "pdb/cif=summary AND the full structure file."
        ),
    )


def _split_thirds(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    """Split a per-residue list into three contiguous regions.

    Tail residues that don't divide evenly are pushed into the C-term
    region so no residue is dropped.
    """
    n = len(values)
    third = n // 3
    if third == 0:
        return values, [], []
    return values[:third], values[third : 2 * third], values[2 * third :]


def _region_summary(values: list[float], start_residue: int) -> dict[str, Any]:
    if not values:
        return {"residue_range": None, "mean_plddt": None, "residue_count": 0}
    return {
        "residue_range": [start_residue, start_residue + len(values) - 1],
        "mean_plddt": round(sum(values) / len(values), 2),
        "residue_count": len(values),
    }


def _parse_plddt_from_pdb(pdb_text: str) -> dict[str, Any]:
    """Extract per-CA B-factors (= pLDDT in AlphaFold PDBs) and summarise."""
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("af", io.StringIO(pdb_text))
    ca_bfactors: list[float] = [
        residue["CA"].get_bfactor()
        for model in structure
        for chain in model
        for residue in chain
        if "CA" in residue
    ]

    if not ca_bfactors:
        return {
            "residue_count": 0,
            "mean_plddt": None,
            "per_region": None,
            "low_confidence_warning": None,
        }

    n_term, middle, c_term = _split_thirds(ca_bfactors)
    # Start residues: N-term always at 1; middle at len(N)+1; C-term at len(N)+len(M)+1.
    n_start = 1
    m_start = n_start + len(n_term)
    c_start = m_start + len(middle)
    mean_plddt = round(sum(ca_bfactors) / len(ca_bfactors), 2)
    return {
        "residue_count": len(ca_bfactors),
        "mean_plddt": mean_plddt,
        "per_region": {
            "n_term": _region_summary(n_term, n_start),
            "middle": _region_summary(middle, m_start),
            "c_term": _region_summary(c_term, c_start),
        },
        # Spec §4.4: pLDDT < 70 is unreliable. Flag when the overall mean
        # falls below that so the model sees the caveat even in "summary".
        "low_confidence_warning": (
            "Overall mean pLDDT below 70 — prediction is unreliable; "
            "do not cite structural details without experimental support."
            if mean_plddt < 70
            else None
        ),
    }


def _build_metadata(prediction: dict[str, Any]) -> dict[str, Any]:
    return {
        "uniprot_accession": prediction.get("uniprotAccession"),
        "uniprot_id": prediction.get("uniprotId"),
        "uniprot_description": prediction.get("uniprotDescription"),
        "organism_scientific_name": prediction.get("organismScientificName"),
        "tax_id": prediction.get("taxId"),
        "model_entry_id": prediction.get("entryId"),
        "latest_version": prediction.get("latestVersion"),
        "model_created_date": prediction.get("modelCreatedDate"),
        "sequence_version_date": prediction.get("sequenceVersionDate"),
        "confidence_version": prediction.get("confidenceVersion"),
        "confidence_category": prediction.get("confidenceCategory"),
        "global_plddt_metric": prediction.get("globalMetricValue"),
        "pdb_url": prediction.get("pdbUrl"),
        "cif_url": prediction.get("cifUrl"),
        "has_pae": bool(prediction.get("paeDocUrl") or prediction.get("paeImageUrl")),
        "pae_doc_url": prediction.get("paeDocUrl"),
    }


async def fetch_alphafold(
    uniprot_accession: str,
    format: Literal["pdb", "cif", "summary"] = "summary",  # noqa: A002 — spec param name
    *,
    client: AlphaFoldClient,
) -> dict[str, Any]:
    """Fetch an AlphaFold2 prediction by UniProt accession."""
    params = FetchAlphaFoldInput(uniprot_accession=uniprot_accession, format=format)
    try:
        predictions = await client.fetch_prediction(params.uniprot_accession)
        if not predictions:
            raise AccessionNotFound(
                accession=params.uniprot_accession, database="AlphaFold DB"
            )
        prediction = predictions[0]
        pdb_url = prediction.get("pdbUrl")
        if not pdb_url:
            return error_response(
                f"AlphaFold metadata for '{params.uniprot_accession}' lacks a pdbUrl.",
                suggestions=[
                    "Retry — this is usually transient upstream data inconsistency.",
                ],
            )
        # Always download the PDB to compute the pLDDT summary — spec §4.4
        # requires the summary on every response regardless of `format`.
        pdb_text = await client.fetch_structure_file(
            pdb_url, identifier=params.uniprot_accession
        )
    except AccessionNotFound as exc:
        return error_response(
            f"No AlphaFold prediction for '{exc.accession}'.",
            suggestions=[
                "Check the UniProt accession exists via bio_fetch_uniprot.",
                "AlphaFold covers ~200 M sequences but not every UniProt entry — "
                "unreviewed TrEMBL fragments and viral proteins are sometimes missing.",
            ],
        )
    except RateLimitExceeded:
        return error_response(
            "AlphaFold DB rate limit exceeded. Retry in a moment.",
            suggestions=["Spec §7.1 caps AlphaFold at 5 concurrent, 0.2 s apart."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"AlphaFold API is unreachable: {exc.reason}.",
            suggestions=["Transient upstream error. Retry in a few minutes."],
        )

    result: dict[str, Any] = {
        **_build_metadata(prediction),
        "plddt_summary": _parse_plddt_from_pdb(pdb_text),
    }

    if params.format == "pdb":
        result["structure_format"] = "pdb"
        result["structure"] = pdb_text
    elif params.format == "cif":
        cif_url = prediction.get("cifUrl")
        if not cif_url:
            result["structure_format"] = None
            result["structure_error"] = "No cifUrl in AlphaFold metadata."
        else:
            try:
                cif_text = await client.fetch_structure_file(
                    cif_url, identifier=params.uniprot_accession
                )
            except (AccessionNotFound, ExternalServiceDown) as exc:
                result["structure_format"] = None
                result["structure_error"] = f"CIF download failed: {exc}"
            else:
                result["structure_format"] = "cif"
                result["structure"] = cif_text

    return result
