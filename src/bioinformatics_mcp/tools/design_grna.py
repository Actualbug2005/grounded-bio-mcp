"""`bio_design_grna` — CRISPR gRNA design with off-target analysis (CRISPOR wrapper).

Phase 1, MVP — the heaviest tool in the spec, deferred to end of phase 1.
See spec §4.7.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Design CRISPR gRNA (CRISPOR)".

The single most important anti-hallucination tool in the server — real
off-target tables instead of fabricated ones.

# Implementation notes

CRISPOR is invoked as a subprocess via ``CrisporRunner`` (see
``clients/crispor.py``). The runner returns two raw TSV strings — guides
and off-targets — and this tool transforms them into the spec §4.7
output shape.

Parse strategy is **defensive on column set**: CRISPOR's column list
varies across genomes (hg19 emits a ``CCTop-Score`` column that sacCer3
does not), so the parser indexes by header name, never by position.
Unknown score-like columns land in ``additional_scores`` rather than
silently disappearing — future CRISPOR upgrades won't drop information
on the floor.

Score nullability: CRISPOR writes the literal ``NotEnoughFlankSeq`` in
score cells when the input sequence is too short to extract scoring
context (typically ~30 nt flank). This means *score not computable for
this guide*, not *score is zero* — surfaced as ``null`` in
``efficiency_scores`` with the reason captured under
``score_unavailable_reason``. Callers can distinguish "we didn't compute
it" from "we computed 0" without inspection of the source.

CFD specificity (``cfd_specificity``) is computed locally per Doench
2016: ``1 / (1 + sum(cfd_score))`` over off-targets excluding the
on-target self-match (mismatchCount == 0). CRISPOR emits per-off-target
CFD scores but no per-guide specificity column, so the wrapper produces
it for spec output completeness.

Off-target classification (``locus_class``): CRISPOR's locusDesc field
encodes both the segment type and the gene name(s) in three patterns —
``exon:GENE``, ``intron:GENE``, ``intergenic:GENE-GENE``. Mapped to the
spec's classification surface as CDS / intron / intergenic /
unknown. Original ``locusDesc`` preserved for callers that want the
gene names.

Off-target list is truncated at 100 entries per guide; truncation is
flagged on ``off_targets_truncated`` and the original count is preserved
under ``total_off_targets`` so callers see the cap explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from bioinformatics_mcp import __version__
from bioinformatics_mcp.clients.crispor import (
    CrisporError,
    CrisporRunner,
    GenomeIndexNotFound,
)
from bioinformatics_mcp.utils.errors import error_response

_OFF_TARGET_TRUNCATION_LIMIT = 100
"""Per-guide cap on off-target entries in the spec output. Surface
``off_targets_truncated`` + ``total_off_targets`` when exceeded so the
caller sees the cap rather than silently losing rows."""

_SCORE_COLUMN_TO_SPEC_KEY: dict[str, str] = {
    "Doench '16-Score": "doench16",
    "Doench '16-Old-Score": "doench16_old",
    "Chari-Score": "chari",
    "Xu-Score": "xu",
    "Doench '14-Score": "doench14",
    "Wang-Score": "wang",
    "Moreno-Mateos-Score": "moreno_mateos",
    "Azimuth in-vitro-Score": "azimuth",
    "CCTop-Score": "cctop",
    "Out-of-Frame-Score": "out_of_frame",
}
"""Maps CRISPOR's column names to spec output keys. Unknown columns
land in ``additional_scores`` so future score-model additions surface
rather than silently disappearing."""

_NON_SCORE_GUIDE_COLUMNS: frozenset[str] = frozenset(
    {
        "seqId",
        "guideId",
        "targetSeq",
        "mitSpecScore",
        "offtargetCount",
        "targetGenomeGeneLocus",
    }
)


class DesignGRNAInput(BaseModel):
    """Spec §4.7 input schema."""

    target_sequence: str = Field(
        ...,
        min_length=50,
        max_length=2000,
        pattern=r"^[ACGTNacgtn]+$",
    )
    genome: str = Field(..., min_length=1, max_length=50)
    pam: Literal["NGG", "NG", "NNGRRT", "TTTV"] = "NGG"
    max_guides: int = Field(default=10, ge=1, le=50)
    max_off_target_mismatches: int = Field(default=4, ge=0, le=4)


def _parse_score_cell(value: str) -> tuple[float | int | None, str | None]:
    """Parse a CRISPOR score cell. Returns ``(value, unavailable_reason)``.

    ``NotEnoughFlankSeq`` → ``(None, "insufficient flanking sequence")``.
    Empty / NA / None  → ``(None, "score not computed")``.
    Numeric           → ``(parsed_number, None)``.
    Anything else     → ``(None, "unparseable: …")``.
    """
    raw = value.strip()
    if raw == "NotEnoughFlankSeq":
        return None, "insufficient flanking sequence"
    if raw in {"", "NA", "None"}:
        return None, "score not computed"
    try:
        if "." in raw:
            return float(raw), None
        return int(raw), None
    except ValueError:
        return None, f"unparseable: {raw!r}"


def _read_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a CRISPOR-shaped TSV. Header may begin with ``#`` or not.

    Empty input → ``([], [])``. Short rows are right-padded with empty
    strings; over-long rows are truncated to the header width.
    """
    if not text.strip():
        return [], []
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return [], []
    header_line = lines[0]
    if header_line.startswith("#"):
        header_line = header_line[1:]
    header = header_line.split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split("\t")
        if len(values) < len(header):
            values = values + [""] * (len(header) - len(values))
        elif len(values) > len(header):
            values = values[: len(header)]
        rows.append(dict(zip(header, values)))
    return header, rows


def _split_target_seq(
    target_seq: str, pam_motif: str
) -> tuple[str, str]:
    """Split CRISPOR's 23 nt targetSeq into ``(20 nt spacer, 3 nt PAM)``.

    For SpCas9-family PAMs (NGG, NG) the PAM is on the 3' end.
    NNGRRT (SaCas9) follows the same 3'-PAM convention.
    TTTV (Cpf1) places PAM at the 5' end with longer spacers — outside
    the spec's primary case; for Cpf1 we surface targetSeq verbatim and
    leave PAM split empty.
    """
    if pam_motif == "TTTV":
        return target_seq, ""
    if len(target_seq) < 4:
        return target_seq, ""
    return target_seq[:-3], target_seq[-3:]


def _classify_locus(locus_desc: str) -> str:
    """Map CRISPOR locusDesc prefix to spec §4.7 classification."""
    if locus_desc.startswith("exon:"):
        return "CDS"
    if locus_desc.startswith("intron:"):
        return "intron"
    if locus_desc.startswith("intergenic:"):
        return "intergenic"
    return "unknown"


def _compute_cfd_specificity(
    offtargets: list[dict[str, str]],
) -> float | None:
    """CFD specificity per Doench 2016: ``1 / (1 + sum(cfd_score))`` over
    off-targets excluding the on-target self-match (mismatchCount==0).

    Returns ``None`` when the off-target table is empty (e.g. novel
    sequence not in the genome).
    """
    if not offtargets:
        return None
    score_sum = 0.0
    for ot in offtargets:
        try:
            mm = int(ot.get("mismatchCount", "0"))
        except ValueError:
            mm = 0
        if mm == 0:
            continue
        cfd, _ = _parse_score_cell(ot.get("cfdOfftargetScore", ""))
        if cfd is not None:
            score_sum += float(cfd)
    return round(1.0 / (1.0 + score_sum), 4)


def _on_target_locus(
    offtargets: list[dict[str, str]], genome: str
) -> str | None:
    """Find the on-target genomic location from the 0-mm self-match in
    the off-target table.

    Returns ``"<genome>:<chrom>:<start>"`` if a 0-mm row exists, else
    ``None`` (input not in genome — spec acknowledges this case as
    valid: novel synthetic sequences have no on-target locus).
    """
    for ot in offtargets:
        try:
            mm = int(ot.get("mismatchCount", "0"))
        except ValueError:
            continue
        if mm == 0:
            chrom = ot.get("chrom", "")
            start = ot.get("start", "")
            return f"{genome}:{chrom}:{start}"
    return None


async def bio_design_grna(
    target_sequence: str,
    genome: str,
    pam: str = "NGG",
    max_guides: int = 10,
    max_off_target_mismatches: int = 4,
    *,
    runner: CrisporRunner,
) -> dict[str, Any]:
    """CRISPR gRNA design via CRISPOR with real off-target analysis.

    Returns spec §4.7 output: ranked ``guides`` (top-N by MIT specificity),
    each with sequence + PAM split, on/off-target locus classification
    (CDS/intron/intergenic), per-model efficiency scores with explicit
    nullability for ``NotEnoughFlankSeq`` cells, off-target table with
    truncation flag, and a per-guide CFD specificity computed locally
    from the off-target rows. ``provenance`` carries genome + tool
    versions so results are reproducible.
    """
    try:
        params = DesignGRNAInput.model_validate(
            {
                "target_sequence": target_sequence,
                "genome": genome,
                "pam": pam,
                "max_guides": max_guides,
                "max_off_target_mismatches": max_off_target_mismatches,
            }
        )
    except ValidationError as exc:
        return error_response(
            f"Invalid input to bio_design_grna: {exc.errors()[0]['msg']}",
            suggestions=[
                "target_sequence: 50-2000 nt; alphabet ACGTN.",
                "pam: one of NGG, NG, NNGRRT, TTTV.",
                "max_guides: 1-50; max_off_target_mismatches: 0-4.",
            ],
        )

    try:
        guides_tsv, offtargets_tsv = await runner.run(
            genome=params.genome,
            target_sequence=params.target_sequence.upper(),
            pam=params.pam,
            max_off_target_mismatches=params.max_off_target_mismatches,
        )
    except GenomeIndexNotFound as exc:
        return error_response(
            f"Genome index '{exc.genome}' is not installed at "
            f"{exc.expected_path}. Missing files: {exc.missing}.",
            suggestions=[
                "Run scripts/fetch_genome.sh <genome> to install the index.",
                "On dev, sacCer3 ships pre-indexed under "
                "~/opt/crispor/genomes.sample/sacCer3/ — copy into the "
                "configured GENOME_DIR.",
            ],
        )
    except CrisporError as exc:
        return error_response(
            f"CRISPOR subprocess failed: {exc}",
            suggestions=[
                "Check the CRISPOR install at the path configured "
                "in CRISPOR_PATH.",
                "Verify the bwa binary is reachable from the CRISPOR "
                "bin directory.",
            ],
        )

    guides_header, guides_rows = _read_tsv(guides_tsv)
    _, offtargets_rows = _read_tsv(offtargets_tsv)

    grouped_offtargets: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in offtargets_rows:
        key = (row.get("seqId", ""), row.get("guideId", ""))
        grouped_offtargets.setdefault(key, []).append(row)

    spec_guides = [
        _build_spec_guide(
            row=row,
            guides_header=guides_header,
            offtargets=grouped_offtargets.get(
                (row.get("seqId", ""), row.get("guideId", "")), []
            ),
            params=params,
        )
        for row in guides_rows
    ]
    candidate_guides_count = len(spec_guides)

    spec_guides.sort(key=lambda g: g["specificity_score"], reverse=True)
    returned_guides = spec_guides[: params.max_guides]

    return {
        "guides": returned_guides,
        "candidate_guides_count": candidate_guides_count,
        "returned_guides_count": len(returned_guides),
        "genome": params.genome,
        "pam": params.pam,
        "max_off_target_mismatches": params.max_off_target_mismatches,
        "provenance": {
            "source": "CRISPOR",
            "tool_version": __version__,
            "fetched_at": datetime.now(UTC).isoformat(),
            "url": "https://crispor.gi.ucsc.edu/",
            "genome": params.genome,
        },
        "confidence": {
            "level": "high",
            "basis": (
                "Real off-target scan against the indexed genome with "
                "BWA; MIT and CFD scores from CRISPOR's standard pipeline"
            ),
            "interpretation": (
                "Guides with cfd_specificity > 0.8 are typically suitable "
                "for experimental use; verify in your context. Off-target "
                "tables are exhaustive within the configured mismatch "
                f"ceiling ({params.max_off_target_mismatches}); off-targets "
                "beyond that are not enumerated. NotEnoughFlankSeq in "
                "efficiency scores means the input sequence was too short "
                "to extract scoring context for that guide — surfaced as "
                "null with reason rather than zero."
            ),
        },
    }


def _build_spec_guide(
    row: dict[str, str],
    guides_header: list[str],
    offtargets: list[dict[str, str]],
    params: DesignGRNAInput,
) -> dict[str, Any]:
    """Translate one CRISPOR guide row into spec §4.7 per-guide shape."""
    guide_id = row.get("guideId", "")
    target_seq = row.get("targetSeq", "")
    spacer, pam_seq = _split_target_seq(target_seq, params.pam)

    try:
        mit_spec = int(row.get("mitSpecScore", "0"))
    except ValueError:
        mit_spec = 0

    strand = "-" if guide_id.endswith("rev") else "+"
    position_str = guide_id.replace("forw", "").replace("rev", "")
    try:
        position = int(position_str)
    except ValueError:
        position = -1

    efficiency_scores: dict[str, float | int | None] = {}
    score_unavailable: dict[str, str] = {}
    additional_scores: dict[str, str] = {}
    for column in guides_header:
        if column in _SCORE_COLUMN_TO_SPEC_KEY:
            spec_key = _SCORE_COLUMN_TO_SPEC_KEY[column]
            score_value, reason = _parse_score_cell(row.get(column, ""))
            efficiency_scores[spec_key] = score_value
            if reason is not None:
                score_unavailable[spec_key] = reason
        elif column not in _NON_SCORE_GUIDE_COLUMNS:
            additional_scores[column] = row.get(column, "")

    spec_offtargets: list[dict[str, Any]] = []
    summary_buckets: dict[str, int] = {}
    for ot in offtargets:
        try:
            mm_count = int(ot.get("mismatchCount", "0"))
        except ValueError:
            mm_count = 0
        bucket_key = f"{mm_count}_mm"
        summary_buckets[bucket_key] = summary_buckets.get(bucket_key, 0) + 1

        cfd_score, _ = _parse_score_cell(ot.get("cfdOfftargetScore", ""))
        mit_ot_score, _ = _parse_score_cell(
            ot.get("mitOfftargetScore", "")
        )
        try:
            start_pos = int(ot.get("start", "0"))
        except ValueError:
            start_pos = 0

        spec_offtargets.append(
            {
                "chromosome": ot.get("chrom", ""),
                "position": start_pos,
                "sequence": ot.get("offtargetSeq", ""),
                "mismatches": mm_count,
                "mismatch_pattern": ot.get("mismatchPos", ""),
                "strand": ot.get("strand", ""),
                "cfd_score": cfd_score,
                "mit_score": mit_ot_score,
                "locus_class": _classify_locus(ot.get("locusDesc", "")),
                "locus_desc": ot.get("locusDesc", ""),
            }
        )

    total_off_targets = len(spec_offtargets)
    truncated = total_off_targets > _OFF_TARGET_TRUNCATION_LIMIT
    if truncated:
        spec_offtargets = spec_offtargets[:_OFF_TARGET_TRUNCATION_LIMIT]

    return {
        "guide_id": guide_id,
        "sequence": spacer,
        "pam": pam_seq,
        "position": position,
        "strand": strand,
        "specificity_score": mit_spec,
        "cfd_specificity": _compute_cfd_specificity(offtargets),
        "efficiency_scores": efficiency_scores,
        "score_unavailable_reason": score_unavailable,
        "additional_scores": additional_scores,
        "on_target_locus": row.get("targetGenomeGeneLocus", ""),
        "on_target_position": _on_target_locus(offtargets, params.genome),
        "off_targets": spec_offtargets,
        "off_targets_truncated": truncated,
        "total_off_targets": total_off_targets,
        "off_target_summary": summary_buckets,
    }
