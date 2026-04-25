"""`bio_blast_search` — sequence similarity search via NCBI BLAST.

Spec §4.6. Annotations: readOnlyHint=True, destructiveHint=False,
openWorldHint=True, idempotentHint=False, title="BLAST Sequence Search".

idempotentHint=False — NCBI databases grow between runs and a query
issued today may return additional hits next month.

The async submit / poll / fetch primitives live on
:class:`~bioinformatics_mcp.clients.ncbi.NCBIClient` (separate
RateLimitedClient against blast.ncbi.nlm.nih.gov). This module is the
spec-shape adapter: it reshapes the verbose JSON2_S response into the
spec output, surfaces the description-list quirk via
``identical_sequence_count``, and inlines alignment strings only for
the top N hits to keep the structured payload tractable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from bioinformatics_mcp.clients.ncbi import BLAST_WEB_RESULT_URL_FMT
from bioinformatics_mcp.utils.errors import (
    ExternalServiceDown,
    JobFailed,
    JobTimeoutError,
    RateLimitExceeded,
    error_response,
)

DEFAULT_MAX_WAIT_SECONDS = 600
MAX_MAX_WAIT_SECONDS = 1800
ALIGNMENT_INLINE_TOP_N = 5


class BlastSearchInput(BaseModel):
    """Spec §4.6 input + the project's standard ``max_wait_seconds``
    extension for async-job tools (mirrors bio_align_sequences and
    bio_scan_domains).
    """

    query_sequence: str = Field(..., min_length=10, max_length=100000)
    program: Literal["blastn", "blastp", "blastx", "tblastn"]
    database: Literal["nt", "nr", "refseq_protein", "refseq_rna", "swissprot"]
    organism_filter: str | None = None
    max_hits: int = Field(default=20, ge=1, le=100)
    e_value: float = Field(default=10.0, ge=0.0)
    max_wait_seconds: int | None = Field(
        default=None, ge=30, le=MAX_MAX_WAIT_SECONDS
    )


async def bio_blast_search(
    query_sequence: str,
    program: str,
    database: str,
    organism_filter: str | None = None,
    max_hits: int = 20,
    e_value: float = 10.0,
    max_wait_seconds: int | None = None,
    *,
    client: Any,  # NCBIClient at runtime; duck-typed so tests pass a fake.
) -> dict[str, Any]:
    """Run a BLAST search and return per-hit summary + alignment data.

    See module docstring for design rationale. Empty hit lists are
    valid output (signal, not failure). Spec-output schema is stable
    across program/database combinations.
    """
    try:
        params = BlastSearchInput.model_validate(
            {
                "query_sequence": query_sequence,
                "program": program,
                "database": database,
                "organism_filter": organism_filter,
                "max_hits": max_hits,
                "e_value": e_value,
                "max_wait_seconds": max_wait_seconds,
            }
        )
    except ValidationError as exc:
        return error_response(
            f"Invalid input to bio_blast_search: {exc.errors()[0]['msg']}",
            suggestions=[
                "Query length must be 10–100 000 chars; program is one of "
                "blastn/blastp/blastx/tblastn; database is one of "
                "nt/nr/refseq_protein/refseq_rna/swissprot.",
                f"max_wait_seconds is capped at {MAX_MAX_WAIT_SECONDS}s.",
            ],
        )

    effective_wait = params.max_wait_seconds or DEFAULT_MAX_WAIT_SECONDS

    try:
        raw = await client.blast_run(
            program=params.program,
            database=params.database,
            query=params.query_sequence,
            max_hits=params.max_hits,
            e_value=params.e_value,
            organism_filter=params.organism_filter,
            max_wait_seconds=effective_wait,
        )
    except JobTimeoutError as exc:
        return error_response(
            str(exc),
            suggestions=[
                f"BLAST jobs against {params.database!r} can run several minutes "
                "during peak hours; retry with a higher max_wait_seconds (cap "
                f"{MAX_MAX_WAIT_SECONDS}).",
                f"The job is still queued at NCBI — check {exc.status_url}.",
            ],
        )
    except JobFailed as exc:
        return _job_failed_response(exc)
    except RateLimitExceeded as exc:
        return error_response(
            f"NCBI BLAST rate limit hit: {exc.service}.",
            suggestions=[
                "Wait at least 60 s before retrying; NCBI throttles BLAST "
                "submissions more aggressively than eutils.",
                "Set NCBI_API_KEY in the environment to access the higher tier.",
            ],
        )
    except ExternalServiceDown as exc:
        return error_response(
            str(exc),
            suggestions=[f"Check NCBI status at {exc.status_url}."],
        )

    return _format_response(raw, params=params)


def _job_failed_response(exc: JobFailed) -> dict[str, Any]:
    """Map a BLAST JobFailed to an actionable error_response.

    UNKNOWN is genuinely ambiguous (RID expired vs never existed); the
    suggestions surface both interpretations.
    """
    if exc.status == "UNKNOWN":
        return error_response(
            f"BLAST RID {exc.job_id!r} returned status UNKNOWN.",
            suggestions=[
                "The RID may have expired — NCBI keeps results for ~24 h. "
                "Re-submit the original query to get a fresh RID.",
                "If you typed the RID in by hand, double-check the format "
                "(typically 11 chars, e.g. YT52BZW4014). If freshly submitted "
                "and still UNKNOWN, the RID may never have been valid.",
            ],
        )
    if exc.status == "FAILED":
        return error_response(
            f"BLAST RID {exc.job_id!r} ended with status FAILED.",
            suggestions=[
                "Try a smaller/cleaner query or a more specific database "
                "(e.g. swissprot rather than nr).",
                "NCBI BLAST occasionally fails with no diagnostic — "
                "retry once before assuming the query is malformed.",
            ],
        )
    return error_response(
        str(exc),
        suggestions=[
            f"Unexpected BLAST status {exc.status!r}; usually transient — "
            "retry once.",
        ],
    )


def _format_response(raw: dict[str, Any], *, params: BlastSearchInput) -> dict[str, Any]:
    """Reshape JSON2_S into the spec output, applying the top-N alignment-
    inline rule."""
    report = raw["BlastOutput2"][0]["report"]
    search = report["results"]["search"]
    raw_hits = search.get("hits", [])

    formatted_hits: list[dict[str, Any]] = []
    for idx, hit in enumerate(raw_hits):
        descriptions = hit.get("description", [])
        canonical = descriptions[0] if descriptions else {}
        hsps = hit.get("hsps", [])
        if not hsps:
            continue
        hsp = hsps[0]
        align_len = hsp.get("align_len") or 0
        identity = hsp.get("identity") or 0
        identity_pct = round(100.0 * identity / align_len, 2) if align_len else 0.0
        query_span = (hsp.get("query_to") or 0) - (hsp.get("query_from") or 0) + 1
        coverage_pct = round(100.0 * query_span / search["query_len"], 2) if search.get("query_len") else 0.0

        formatted: dict[str, Any] = {
            "accession": canonical.get("accession", ""),
            "description": canonical.get("title", ""),
            "organism": canonical.get("sciname", ""),
            "taxid": canonical.get("taxid"),
            "subject_id": canonical.get("id", ""),
            "subject_length": hit.get("len"),
            "identical_sequence_count": len(descriptions),
            "e_value": hsp.get("evalue"),
            "bit_score": hsp.get("bit_score"),
            "score": hsp.get("score"),
            "identity_pct": identity_pct,
            "query_coverage_pct": coverage_pct,
            "query_from": hsp.get("query_from"),
            "query_to": hsp.get("query_to"),
            "hit_from": hsp.get("hit_from"),
            "hit_to": hsp.get("hit_to"),
            "align_length": align_len,
            "gaps": hsp.get("gaps", 0),
        }
        if idx < ALIGNMENT_INLINE_TOP_N:
            formatted["qseq"] = hsp.get("qseq", "")
            formatted["hseq"] = hsp.get("hseq", "")
            formatted["midline"] = hsp.get("midline", "")
        formatted_hits.append(formatted)

    return {
        "program": report.get("program"),
        "database": report.get("search_target", {}).get("db"),
        "blast_version": report.get("version"),
        "query_id": search.get("query_id"),
        "query_length": search.get("query_len"),
        "hit_count": len(formatted_hits),
        "alignment_inline_top_n": ALIGNMENT_INLINE_TOP_N,
        "hits": formatted_hits,
        "web_result_url_template": BLAST_WEB_RESULT_URL_FMT,
    }
