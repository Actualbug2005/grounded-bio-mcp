"""`bio_fetch_variant` — variant lookup by rsID or coordinates (Ensembl REST).

Phase 2. See spec §4.11.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Fetch Variant".

idempotentHint=False — Ensembl releases update allele frequencies and
ClinVar cross-references.

Design decisions (approved 2026-04-24):

* **Two outcomes, not three**: Ensembl returns HTTP 400 "not found" for
  both unannotated-but-real variants (e.g. ``rs1``) and clearly fake
  rsIDs. There is no "found-empty" branch the upstream can justify, so
  the tool surfaces ``found`` or a ``not_found`` error and supplements
  the ``found`` case with an ``annotation_richness`` object surfacing
  presence flags for clinical_significance, population_frequencies,
  and consequences.
* **Assembly routing is silent**: ``GRCh37`` transparently routes to
  ``grch37.rest.ensembl.org``; any other value (or ``None``) uses the
  GRCh38 default. The actual assembly used is echoed back as
  ``assembly_used`` in every response so callers always know which
  server supplied the answer.
* **MAF is derived from gnomADe when present**: 1000 Genomes is
  GRCh37-aligned, so on GRCh38 the top-level ``MAF`` and
  ``minor_allele`` come back null even for heavily-annotated variants
  like APOE ε4. The tool always requests ``?pops=1`` and picks the
  gnomADe:ALL frequency as ``maf`` with the source echoed for
  provenance; if gnomADe is absent, 1000G is used instead.
"""

from __future__ import annotations

import re
from typing import Any

from bioinformatics_mcp.clients.ensembl import EnsemblClient
from bioinformatics_mcp.utils.errors import AccessionNotFound, error_response

_RSID_PATTERN = re.compile(r"^[a-zA-Z]{2,}[0-9]+$")
_COORD_PATTERN = re.compile(r"^([^:]+):(\d+):([ACGTN-]+):([ACGTN-]+)$")

_GNOMADE_PREFERENCE = ("gnomADe:ALL", "gnomADe", "gnomAD", "1000GENOMES")


async def bio_fetch_variant(
    identifier: str,
    species: str = "human",
    assembly: str | None = None,
    *,
    client: EnsemblClient,
) -> dict[str, Any]:
    """Fetch a variant record by rsID or ``chr:pos:ref:alt`` coordinate.

    Returns ``{status: "found", assembly_used, variant, annotation_richness}``
    on success or an error payload on malformed input / not-found.
    """
    if not identifier or not identifier.strip():
        return error_response(
            "An rsID (e.g. 'rs429358') or coordinate string ('19:44908684:T:C') is required.",
            suggestions=[
                "Pass a dbSNP rsID like 'rs429358'.",
                "Or a GRCh38 coordinate string 'chr:pos:ref:alt', e.g. '19:44908684:T:C'.",
            ],
        )

    assembly_used = _normalise_assembly(assembly)

    coord_match = _COORD_PATTERN.match(identifier.strip())
    if coord_match:
        return await _lookup_by_coord(
            coord_match=coord_match,
            species=species,
            assembly=assembly,
            assembly_used=assembly_used,
            client=client,
        )
    if _RSID_PATTERN.match(identifier.strip()):
        return await _lookup_by_rsid(
            variant_id=identifier.strip(),
            species=species,
            assembly=assembly,
            assembly_used=assembly_used,
            client=client,
        )

    return error_response(
        f"Identifier '{identifier}' is neither an rsID nor a 'chr:pos:ref:alt' coordinate.",
        suggestions=[
            "rsIDs look like 'rs429358' (dbSNP).",
            "Coordinate strings look like '19:44908684:T:C' (chromosome:position:ref:alt).",
        ],
    )


async def _lookup_by_rsid(
    *,
    variant_id: str,
    species: str,
    assembly: str | None,
    assembly_used: str,
    client: EnsemblClient,
) -> dict[str, Any]:
    try:
        raw = await client.lookup_variation(
            species, variant_id, assembly=assembly, include_populations=True
        )
    except AccessionNotFound as exc:
        return error_response(
            f"Variant '{exc.accession}' not found in Ensembl ({assembly_used}).",
            suggestions=[
                "Ensembl cannot distinguish a real-but-unannotated variant from a fabricated rsID — both return the same error. Verify the rsID at dbSNP (https://www.ncbi.nlm.nih.gov/snp/).",
                "If you have genomic coordinates, try the 'chr:pos:ref:alt' form.",
                "If you only have a gene name, use bio_fetch_gene to locate its coordinates first.",
            ],
        )

    return _build_variant_response(raw, assembly_used=assembly_used)


async def _lookup_by_coord(
    *,
    coord_match: re.Match[str],
    species: str,
    assembly: str | None,
    assembly_used: str,
    client: EnsemblClient,
) -> dict[str, Any]:
    chrom, pos, ref, alt = coord_match.groups()
    region = f"{chrom}:{pos}-{int(pos) + max(len(ref), 1) - 1}"
    try:
        overlaps = await client.overlap_variation(
            species, region, assembly=assembly
        )
    except AccessionNotFound as exc:
        return error_response(
            f"No variations known at '{exc.accession}' in Ensembl ({assembly_used}).",
            suggestions=[
                "Verify the coordinates against the specified assembly — coordinates for a given rsID differ between GRCh37 and GRCh38.",
                "Try the rsID form if you know it.",
            ],
        )

    # Filter overlaps whose allele set contains both REF and ALT.
    ref_upper = ref.upper()
    alt_upper = alt.upper()
    match = next(
        (
            v
            for v in overlaps
            if _alleles_match(v.get("alleles", []), ref_upper, alt_upper)
        ),
        None,
    )
    if match is None:
        return error_response(
            f"No variant matching alleles {ref}/{alt} found at {chrom}:{pos} ({assembly_used}).",
            suggestions=[
                f"Ensembl reported {len(overlaps)} variation(s) at this position but none with this REF/ALT pair.",
                "Verify REF matches the reference base at this position in the specified assembly.",
                "Try the rsID form if you know it.",
            ],
        )

    # Fetch the full record for the matched rsID so the caller gets
    # populations + synonyms, which /overlap does not include.
    if match.get("id", "").startswith("rs"):
        try:
            raw = await client.lookup_variation(
                species, match["id"], assembly=assembly, include_populations=True
            )
        except AccessionNotFound:
            raw = match
    else:
        raw = match

    return _build_variant_response(raw, assembly_used=assembly_used)


def _alleles_match(alleles: list[str], ref: str, alt: str) -> bool:
    """Return True when the /overlap alleles list contains both REF and ALT.

    /overlap returns either a ``["REF", "ALT1", "ALT2", …]`` list or a
    single-string entry like ``["HGMD_MUTATION"]`` for records that
    lack explicit alleles — the latter never matches.
    """
    if not alleles or len(alleles) < 2:
        return False
    upper = {a.upper() for a in alleles}
    return ref in upper and alt in upper


def _build_variant_response(
    raw: dict[str, Any], *, assembly_used: str
) -> dict[str, Any]:
    """Shape the Ensembl response into the tool's public contract."""
    mappings = list(raw.get("mappings", []))
    populations = list(raw.get("populations", []))
    clinical_significance = list(raw.get("clinical_significance", []))
    most_severe = raw.get("most_severe_consequence")
    maf = _derive_maf(raw, populations)

    variant = {
        "id": raw.get("name") or raw.get("id", ""),
        "var_class": raw.get("var_class"),
        "synonyms": list(raw.get("synonyms", [])),
        "evidence": list(raw.get("evidence", [])),
        "ancestral_allele": _first_mapping_field(mappings, "ancestral_allele"),
        "alleles": _extract_alleles(mappings),
        "mappings": mappings,
        "most_severe_consequence": most_severe,
        "clinical_significance": clinical_significance,
        "maf": maf,
        "populations": populations,
    }

    richness = {
        "has_clinical_significance": bool(clinical_significance),
        "has_population_frequencies": bool(populations),
        "has_consequences": most_severe is not None
        and most_severe != "intergenic_variant",
    }

    return {
        "status": "found",
        "assembly_used": assembly_used,
        "variant": variant,
        "annotation_richness": richness,
    }


def _derive_maf(
    raw: dict[str, Any], populations: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Select the best available minor-allele-frequency record.

    Preference order (approved 2026-04-24):
        1. gnomADe:ALL — largest and most recent aggregate.
        2. Any gnomADe population.
        3. Any gnomAD group.
        4. 1000GENOMES:phase_3:ALL — GRCh37-aligned, so common on
           GRCh37 but often absent on GRCh38.
        5. Ensembl's own MAF / minor_allele top-level fields if set.

    Returns ``None`` if no frequency source is available.
    """
    if populations:
        # Pick the smallest allele freq per population — that's the MAF.
        # Then rank by preferred source.
        best: dict[str, Any] | None = None
        best_rank = len(_GNOMADE_PREFERENCE) + 1
        for pop in populations:
            freq = pop.get("frequency")
            if freq is None or freq > 0.5:
                continue
            pop_name = pop.get("population", "")
            for i, prefix in enumerate(_GNOMADE_PREFERENCE):
                if pop_name.startswith(prefix) and i < best_rank:
                    best = pop
                    best_rank = i
                    break
        if best is not None:
            return {
                "value": best["frequency"],
                "allele": best.get("allele"),
                "source": best.get("population"),
            }

    # Fall back to Ensembl's top-level MAF if present.
    top_maf = raw.get("MAF")
    if top_maf is not None:
        return {
            "value": top_maf,
            "allele": raw.get("minor_allele"),
            "source": "ensembl:top_level",
        }
    return None


def _extract_alleles(mappings: list[dict[str, Any]]) -> list[str]:
    for m in mappings:
        allele_string = m.get("allele_string")
        if allele_string:
            return allele_string.split("/")
    return []


def _first_mapping_field(
    mappings: list[dict[str, Any]], field: str
) -> Any | None:
    for m in mappings:
        if field in m:
            return m[field]
    return None


def _normalise_assembly(assembly: str | None) -> str:
    if assembly and assembly.upper() == "GRCH37":
        return "GRCh37"
    return "GRCh38"
