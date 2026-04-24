"""`bio_fetch_sequence` — NCBI nucleotide/protein sequence fetch by accession.

Phase 1, MVP. See spec §4.1.

Annotations: ``readOnlyHint=True``, ``destructiveHint=False``,
``openWorldHint=True``, ``idempotentHint=True``, ``title="Fetch NCBI Sequence"``.

Implementation note: HTTP flows through
:class:`~bioinformatics_mcp.clients.ncbi.NCBIClient` (raw httpx +
``RateLimitedClient``); Biopython's ``SeqIO`` is only used to parse the
returned text. See ``clients/ncbi.py`` module docstring for rationale.
"""

from __future__ import annotations

from io import StringIO
from typing import Any, Literal

from Bio import SeqIO
from pydantic import BaseModel, Field

from bioinformatics_mcp.clients.ncbi import NCBIClient
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)
from bioinformatics_mcp.utils.formatting import ResponseFormat, format_response

ReturnType = Literal["fasta", "gb", "gp"]


class FetchSequenceInput(BaseModel):
    """Input schema for ``bio_fetch_sequence`` (spec §4.1)."""

    accession: str = Field(
        ...,
        min_length=3,
        max_length=30,
        description=(
            "NCBI accession (e.g., 'LC149874', 'NM_001242462', 'NP_001229391')."
        ),
    )
    database: Literal["nucleotide", "protein"] = Field(
        ...,
        description="Which NCBI database to query.",
    )
    rettype: ReturnType = Field(
        default="fasta",
        description=(
            "fasta=sequence only, gb=GenBank full record (nucleotide), "
            "gp=GenPept full record (protein)."
        ),
    )
    response_format: ResponseFormat = Field(
        default="json",
        description="Output rendering. 'markdown' uses a human-readable summary.",
    )


def _biopython_format(rettype: ReturnType) -> str:
    # Biopython uses a single "genbank" parser for both GenBank nucleotide
    # records and GenPept protein records. FASTA stays "fasta".
    return "fasta" if rettype == "fasta" else "genbank"


def _serialise_features(record: Any) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for feat in record.features:
        features.append(
            {
                "type": feat.type,
                "location": str(feat.location),
                "qualifiers": {k: list(v) for k, v in feat.qualifiers.items()},
            }
        )
    return features


def _parse(raw: str, rettype: ReturnType) -> dict[str, Any]:
    record = SeqIO.read(StringIO(raw), _biopython_format(rettype))
    sequence = str(record.seq)
    result: dict[str, Any] = {
        "accession": record.id,
        "rettype": rettype,
        "length": len(sequence),
        "description": record.description,
        "sequence": sequence,
    }
    if rettype in ("gb", "gp"):
        annotations = getattr(record, "annotations", {}) or {}
        result["organism"] = annotations.get("organism")
        result["molecule_type"] = annotations.get("molecule_type")
        result["features"] = _serialise_features(record)
    return result


async def fetch_sequence(
    accession: str,
    database: Literal["nucleotide", "protein"],
    rettype: ReturnType = "fasta",
    response_format: ResponseFormat = "json",
    *,
    client: NCBIClient,
) -> dict[str, Any] | str:
    """Fetch a sequence record from NCBI by accession.

    The tool registration in ``server.py`` injects the long-lived
    :class:`NCBIClient` via ``client=`` so the test suite can pass a
    mock-backed client without touching module-level globals.
    """
    params = FetchSequenceInput(
        accession=accession,
        database=database,
        rettype=rettype,
        response_format=response_format,
    )
    try:
        raw = await client.efetch(
            db=params.database,
            accession=params.accession,
            rettype=params.rettype,
        )
    except AccessionNotFound as exc:
        return error_response(
            f"Accession '{exc.accession}' not found in NCBI {exc.database}.",
            suggestions=[
                "Check the accession format — nucleotide accessions are "
                "typically NM_/NR_/XM_ + digits; protein accessions NP_/XP_.",
                "If you have only a gene or protein name, use bio_blast_search.",
            ],
        )
    except RateLimitExceeded as exc:
        return error_response(
            f"NCBI rate limit exceeded; retry in {exc.retry_after or 'a few'}s.",
            suggestions=[
                "Set NCBI_API_KEY to raise the limit from 3 req/s to 10 req/s.",
            ],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"NCBI API is unreachable: {exc.reason}.",
            suggestions=[
                "Transient upstream error. Retry in a few minutes.",
                f"Status: {exc.status_url}" if exc.status_url else "",
            ],
        )

    parsed = _parse(raw, params.rettype)
    parsed["database"] = params.database
    return format_response(parsed, params.response_format)
