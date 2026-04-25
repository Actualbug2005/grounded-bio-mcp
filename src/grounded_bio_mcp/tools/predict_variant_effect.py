"""`bio_predict_variant_effect` — Ensembl VEP consequence prediction.

Phase 2. See spec §4.12.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Predict Variant Effect (VEP)".

idempotentHint=False — Ensembl VEP output depends on the current
release's transcript annotations and scoring matrices.

Design decisions (approved 2026-04-24):

* **Input format with auto-detection**. A narrow regex
  ``^[^:]+:\\d+:[ACGTN-]+:[ACGTN-]+$`` classifies the input as a region
  string; everything else falls through to HGVS. HGVS notation never
  matches this regex because HGVS always includes a transcript or
  genomic prefix with a dot (``:c.``, ``:p.``, ``:g.``), so the
  classifier is unambiguous. ``input_format="hgvs"`` or ``"region"``
  bypasses detection.
* **End position from REF length.** For region notation, the end
  coordinate is ``start + len(REF) - 1`` so deletions span their full
  reference range. Single-base insertions keep ``end == start`` and
  pass the alternate allele directly (Ensembl tolerates the longer ALT).
* **Empty-consequence REF-mismatch hint.** VEP silently returns empty
  consequences when the supplied REF disagrees with the reference base
  at that position. Rather than adding a pre-flight API call to verify
  REF, the tool passes through and — if VEP returns no consequences for
  a region input — surfaces a hint in the output that REF mismatch is a
  possible cause. One-call cost.
* **Three parallel consequence lists.** VEP's response carries
  ``transcript_consequences``, ``regulatory_feature_consequences``,
  and ``intergenic_consequences`` depending on where the variant falls.
  All three are echoed so callers don't have to special-case based on
  variant location.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from grounded_bio_mcp.clients.ensembl import EnsemblClient
from grounded_bio_mcp.utils.errors import AccessionNotFound, error_response

_COORD_PATTERN = re.compile(r"^([^:]+):(\d+):([ACGTN-]+):([ACGTN-]+)$")

_REF_MISMATCH_HINT = (
    "VEP returned no consequences. One common cause is a REF allele that "
    "disagrees with the reference base at this position in the specified "
    "assembly. Verify REF against the reference sequence (or look up the "
    "known rsID via bio_fetch_variant and re-run with HGVS notation)."
)


async def bio_predict_variant_effect(
    variant: str,
    species: str = "human",
    input_format: Literal["hgvs", "region", "auto"] = "auto",
    assembly: str | None = None,
    *,
    client: EnsemblClient,
) -> dict[str, Any]:
    """Predict functional consequences for a variant via Ensembl VEP.

    Accepts HGVS.c / HGVS.p / HGVS.g notation or a ``chr:pos:ref:alt``
    coordinate string. Returns three parallel consequence lists plus a
    top-level ``most_severe_consequence`` summary.
    """
    if not variant or not variant.strip():
        return error_response(
            "A variant in HGVS notation or 'chr:pos:ref:alt' form is required.",
            suggestions=[
                "HGVS.p example: 'ENSP00000252486.4:p.Cys130Arg'.",
                "HGVS.c example: 'ENST00000252486.9:c.388T>C'.",
                "Coord example: '19:44908684:T:C'.",
            ],
        )

    assembly_used = _normalise_assembly(assembly)
    variant = variant.strip()

    resolved_format = _resolve_format(variant, input_format)

    try:
        if resolved_format == "region":
            results = await _predict_region(
                variant=variant,
                species=species,
                assembly=assembly,
                client=client,
            )
        else:
            results = await client.vep_hgvs(
                species, variant, assembly=assembly
            )
    except AccessionNotFound as exc:
        return error_response(
            f"VEP could not process '{exc.accession}'.",
            suggestions=[
                "Check the HGVS notation — Ensembl expects a transcript or genomic prefix (e.g. 'ENST…:c.388T>C', 'ENSP…:p.Cys130Arg', 'NC_000019.10:g.44908684T>C').",
                "Or use the 'chr:pos:ref:alt' form for a coordinate-based query.",
            ],
        )

    return _build_vep_response(
        results,
        assembly_used=assembly_used,
        input_format_used=resolved_format,
        flag_empty_hint=(resolved_format == "region"),
    )


def _resolve_format(
    variant: str, input_format: Literal["hgvs", "region", "auto"]
) -> Literal["hgvs", "region"]:
    if input_format != "auto":
        return input_format
    return "region" if _COORD_PATTERN.match(variant) else "hgvs"


async def _predict_region(
    *,
    variant: str,
    species: str,
    assembly: str | None,
    client: EnsemblClient,
) -> list[dict[str, Any]]:
    match = _COORD_PATTERN.match(variant)
    if match is None:
        raise AccessionNotFound(accession=variant, database="Ensembl")
    chrom, pos_str, ref, alt = match.groups()
    start = int(pos_str)
    end = start + max(len(ref), 1) - 1
    region = f"{chrom}:{start}-{end}"
    return await client.vep_region(
        species,
        region=region,
        strand=1,
        allele=alt.upper(),
        assembly=assembly,
    )


def _build_vep_response(
    results: list[dict[str, Any]],
    *,
    assembly_used: str,
    input_format_used: Literal["hgvs", "region"],
    flag_empty_hint: bool,
) -> dict[str, Any]:
    if not results:
        payload: dict[str, Any] = {
            "status": "empty",
            "assembly_used": assembly_used,
            "input_format_used": input_format_used,
            "transcript_consequences": [],
            "regulatory_feature_consequences": [],
            "intergenic_consequences": [],
            "most_severe_consequence": None,
        }
        if flag_empty_hint:
            payload["hints"] = [_REF_MISMATCH_HINT]
        return payload

    # VEP returns a list; for single-variant input we merge the first entry's
    # consequence lists but preserve every ``input`` echo.
    primary = results[0]
    return {
        "status": "predicted",
        "assembly_used": assembly_used,
        "input_format_used": input_format_used,
        "input": primary.get("input") or primary.get("id"),
        "most_severe_consequence": primary.get("most_severe_consequence"),
        "transcript_consequences": list(
            primary.get("transcript_consequences", [])
        ),
        "regulatory_feature_consequences": list(
            primary.get("regulatory_feature_consequences", [])
        ),
        "intergenic_consequences": list(
            primary.get("intergenic_consequences", [])
        ),
        "colocated_variants": list(primary.get("colocated_variants", [])),
    }


def _normalise_assembly(assembly: str | None) -> str:
    if assembly and assembly.upper() == "GRCH37":
        return "GRCh37"
    return "GRCh38"
