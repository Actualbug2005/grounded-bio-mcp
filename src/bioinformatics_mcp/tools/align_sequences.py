"""`bio_align_sequences` — multiple sequence alignment via EBI Clustal Omega.

Phase 1, MVP. See spec §4.5.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Align Sequences (Clustal Omega)".

**Return shape:** plain ``dict[str, Any]`` — no Pydantic output model.
Session 2's memory note on FastMCP wrapping shows dict returns pass
through bare; the Pydantic-wrapping mandate in the original session-3
prompt addressed scalar wrapping, which is a non-problem for dict-shaped
outputs. Uniform with fetch_pdb / fetch_alphafold.

**Alignment statistics — four metrics, all defined explicitly:**

- ``alignment_length``: total columns in the MSA
- ``conserved_columns_count``: columns where every sequence has the
  same residue AND no sequence has a gap (absolute count)
- ``strict_identity_pct``: conserved_columns_count / non-gap-only
  columns, expressed as percent. "Among positions where every sequence
  has a residue, how often is that residue the same?"
- ``mean_pairwise_identity_pct``: mean over all n*(n-1)/2 pairs of
  (matching non-gap positions / positions where both aligned).
  Robust against diverged sequences where strict identity approaches 0.
- ``gap_pct``: columns containing at least one gap, as percent of
  alignment length.

**"This is expected" pre-documentation** (not tool errors):
- Long gap stretches in divergent sequences are valid alignment output.
- Identity % can be 0 for very divergent sequences (twilight-zone
  homologues, ancient orthologues, convergent evolution).
- Empty output is never returned — EBI's Clustal always produces *some*
  alignment; zero-length would indicate an EBI bug, not a tool bug.
"""

from __future__ import annotations

import io
import re
from typing import Any, Literal

from Bio import AlignIO
from pydantic import BaseModel, Field

from bioinformatics_mcp.clients.ebi import EBIJobRunner
from bioinformatics_mcp.utils.errors import (
    ExternalServiceDown,
    JobFailed,
    JobTimeoutError,
    RateLimitExceeded,
    error_response,
)
from bioinformatics_mcp.utils.formatting import soft_cap_with_url_fallback

# Large alignments aren't inlined; caller gets a JobDispatcher result URL
# pointing at the raw EBI output. 200 KB is ~50 typical-length protein
# sequences at Clustal-num formatting; beyond that, inline bloats tool
# responses noticeably.
ALIGNMENT_SOFT_CAP_BYTES = 200 * 1024

# Map user-facing output_format → (EBI submission outfmt, EBI result-type ID,
# AlignIO parser name). The result-type IDs follow EBI Job Dispatcher's
# conventional "aln-" prefix naming (except "fa" → "aln-fasta"); these
# are not enumerated anywhere that can be fetched without a live job
# submission. Verify on first live run against /resulttypes/{jobId} and
# update if wrong.
_OUTPUT_FORMAT_MAP: dict[str, tuple[str, str, str]] = {
    "clustal": ("clustal_num", "aln-clustal_num", "clustal"),
    "fasta": ("fa", "aln-fasta", "fasta"),
    "msf": ("msf", "aln-msf", "msf"),
}


class SequenceRecord(BaseModel):
    """A single record in the submitted MSA input — spec §4.5."""

    id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        pattern=r"^\S+$",
        description="FASTA identifier. No whitespace (EBI's parser rejects it).",
    )
    sequence: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description="Raw sequence. Whitespace is stripped; alphabet is validated by EBI.",
    )


class AlignSequencesInput(BaseModel):
    """Input schema for ``bio_align_sequences`` (spec §4.5)."""

    sequences: list[SequenceRecord] = Field(..., min_length=2, max_length=500)
    sequence_type: Literal["protein", "dna", "rna"]
    output_format: Literal["clustal", "fasta", "msf"] = Field(default="clustal")


def _build_multifasta(records: list[SequenceRecord]) -> str:
    """Serialise records into a single multi-FASTA string for EBI submission."""
    parts: list[str] = []
    for r in records:
        cleaned = re.sub(r"\s+", "", r.sequence)
        parts.append(f">{r.id}\n{cleaned}\n")
    return "".join(parts)


def _compute_stats(alignment: Any) -> dict[str, Any]:
    """Derive the four alignment statistics from a parsed Bio.AlignIO alignment.

    Calculated locally rather than trusting EBI's identity/gap numbers —
    EBI returns these inconsistently across services and versions; owning
    the definitions means the caller gets the same interpretation of
    "identity %" across every alignment tool we ship.
    """
    n_seqs = len(alignment)
    length = alignment.get_alignment_length()

    if length == 0:
        return {
            "alignment_length": 0,
            "conserved_columns_count": 0,
            "strict_identity_pct": 0.0,
            "mean_pairwise_identity_pct": 0.0,
            "gap_pct": 0.0,
        }

    conserved_cols = 0
    gap_cols = 0
    non_gap_col_count = 0
    for i in range(length):
        col = alignment[:, i]
        has_gap = "-" in col
        if has_gap:
            gap_cols += 1
            continue
        non_gap_col_count += 1
        non_gap_chars = set(col)
        if len(non_gap_chars) == 1:
            conserved_cols += 1

    # Pairwise identity: per pair, count matching non-gap positions over
    # positions where both sequences have a residue (not a gap).
    pair_identities: list[float] = []
    sequences = [str(row.seq) for row in alignment]
    for i in range(n_seqs):
        for j in range(i + 1, n_seqs):
            si, sj = sequences[i], sequences[j]
            matches = 0
            compared = 0
            for a, b in zip(si, sj, strict=True):
                if a == "-" or b == "-":
                    continue
                compared += 1
                if a == b:
                    matches += 1
            if compared > 0:
                pair_identities.append(matches / compared)

    strict = conserved_cols / non_gap_col_count if non_gap_col_count else 0.0
    mean_pairwise = (
        sum(pair_identities) / len(pair_identities) if pair_identities else 0.0
    )
    gap_ratio = gap_cols / length

    return {
        "alignment_length": length,
        "conserved_columns_count": conserved_cols,
        "strict_identity_pct": round(100 * strict, 2),
        "mean_pairwise_identity_pct": round(100 * mean_pairwise, 2),
        "gap_pct": round(100 * gap_ratio, 2),
    }


async def bio_align_sequences(
    sequences: list[dict[str, Any]],
    sequence_type: Literal["protein", "dna", "rna"],
    output_format: Literal["clustal", "fasta", "msf"] = "clustal",
    *,
    runner: EBIJobRunner,
    email: str,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run Clustal Omega on N sequences and return alignment + statistics.

    Uses EBI's Job Dispatcher (submit → poll → fetch). Requires an
    EBI_EMAIL at the server for submission (EBI's terms).
    """
    # Pydantic validates each dict into SequenceRecord; raises on bad input.
    params = AlignSequencesInput.model_validate(
        {
            "sequences": sequences,
            "sequence_type": sequence_type,
            "output_format": output_format,
        }
    )
    outfmt, result_type, parser_name = _OUTPUT_FORMAT_MAP[params.output_format]

    multifasta = _build_multifasta(params.sequences)
    submission: dict[str, Any] = {
        "email": email,
        "sequence": multifasta,
        "stype": params.sequence_type,
        "outfmt": outfmt,
    }

    try:
        raw = await runner.run(
            params=submission,
            result_type=result_type,
            timeout=timeout,
        )
    except JobTimeoutError as exc:
        return error_response(
            str(exc),
            suggestions=[
                "Clustal usually finishes in under 30 s for modest inputs; a "
                "timeout here typically means EBI queue contention.",
                "Retry in a few minutes.",
            ],
            job_id=exc.job_id,
            cancelled=exc.cancelled,
        )
    except JobFailed as exc:
        return error_response(
            f"EBI Clustal job failed: {exc.status}.",
            suggestions=[
                "Common cause: mismatched sequence type (e.g. DNA passed as "
                "protein). Double-check sequence_type matches the sequences.",
                "Check EBI job status with its job ID if you need diagnostic output.",
            ],
            job_id=exc.job_id,
            status=exc.status,
        )
    except RateLimitExceeded:
        return error_response(
            "EBI rate limit exceeded. Retry in a moment.",
            suggestions=["Spec §7.1 caps EBI at 3 concurrent, 0.5 s apart."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"EBI API is unreachable: {exc.reason}.",
            suggestions=[
                "Transient upstream error. Retry in a few minutes.",
                "Check https://www.ebi.ac.uk/Tools/common/status",
            ],
        )

    alignment_text = raw.decode("utf-8", errors="replace")

    try:
        alignment = AlignIO.read(io.StringIO(alignment_text), parser_name)
    except ValueError as exc:
        # Parse failure → surface actionable message rather than crashing.
        return error_response(
            f"Unable to parse EBI {params.output_format} output: {exc}.",
            suggestions=[
                "This usually indicates EBI changed its result-type naming. "
                "Probe /resulttypes/{jobId} against a fresh submission and "
                "update _OUTPUT_FORMAT_MAP in tools/align_sequences.py.",
            ],
        )

    stats = _compute_stats(alignment)

    result: dict[str, Any] = {
        "output_format": params.output_format,
        "sequence_count": len(params.sequences),
        "alignment_statistics": stats,
    }
    result.update(
        soft_cap_with_url_fallback(
            alignment_text,
            cap_bytes=ALIGNMENT_SOFT_CAP_BYTES,
            fallback_url=(
                # Best-effort status URL; the caller can re-fetch the result
                # directly from EBI. We don't re-submit to avoid job-duplication.
                f"{runner.base_url}/result/<job_id>/{result_type} "
                "(request a fresh submission if the job has expired; EBI "
                "retains results for 7 days)"
            ),
            key_prefix="alignment",
            format_label=params.output_format,
            overage_noun="Alignment",
        )
    )
    return result
