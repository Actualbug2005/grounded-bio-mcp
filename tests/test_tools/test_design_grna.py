"""Unit tests for ``bio_design_grna`` (spec §4.7).

CRISPOR is invoked via subprocess at runtime; tests stub the ``CrisporRunner``
to return hand-crafted TSV fixtures so the wrapper's parse + transform logic
is fully exercised without touching the bundled CRISPOR install. The
``CRISPOR_LIVE=1`` integration test (see ``test_design_grna_live.py`` once it
lands) exercises the live subprocess path against the bundled sacCer3
genome — but only on machines where Rosetta or native arm64 binaries make
the bundled x86_64 binaries runnable. On Apple Silicon dev machines
without Rosetta the live path is skipped; the LXC in Session 8b is the
canonical live-exec environment.

Fixtures under ``tests/fixtures/crispor_*.tsv`` are hand-crafted
derivatives of CRISPOR's output format (see ``docs/crispor_output_format.md``)
rather than copies of CRISPOR's bundled sample TSVs — the licence
boundary stays clean (CRISPOR is academic-free / commercial-paid;
this project is Apache-2.0 from Session 8.5 onward).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bioinformatics_mcp.clients.crispor import (
    CrisporRunFailed,
    CrisporRunner,
    GenomeIndexNotFound,
)
from bioinformatics_mcp.tools.design_grna import bio_design_grna

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---- runner fakes -------------------------------------------------------


class FakeRunner(CrisporRunner):
    """Test double that returns pre-baked TSVs without running a subprocess.

    Inherits from CrisporRunner so the type contract matches the production
    runner exactly; only ``run`` is overridden. Last-call args captured on
    ``self.last_call`` so individual tests can assert command construction.
    """

    def __init__(self, guides_tsv: str, offtargets_tsv: str) -> None:
        super().__init__(
            crispor_python=Path("/dev/null"),
            crispor_path=Path("/dev/null"),
            genomes_dir=Path("/dev/null"),
            timeout_s=300.0,
        )
        self._guides_tsv = guides_tsv
        self._offtargets_tsv = offtargets_tsv
        self.last_call: dict[str, object] | None = None

    async def run(
        self,
        genome: str,
        target_sequence: str,
        pam: str = "NGG",
        max_off_target_mismatches: int = 4,
    ) -> tuple[str, str]:
        self.last_call = {
            "genome": genome,
            "target_sequence": target_sequence,
            "pam": pam,
            "max_off_target_mismatches": max_off_target_mismatches,
        }
        return self._guides_tsv, self._offtargets_tsv


class RaisingRunner(CrisporRunner):
    """Test double that raises a configurable exception on every run."""

    def __init__(self, error: Exception) -> None:
        super().__init__(
            crispor_python=Path("/dev/null"),
            crispor_path=Path("/dev/null"),
            genomes_dir=Path("/dev/null"),
            timeout_s=300.0,
        )
        self._error = error

    async def run(
        self,
        genome: str,
        target_sequence: str,
        pam: str = "NGG",
        max_off_target_mismatches: int = 4,
    ) -> tuple[str, str]:
        raise self._error


# ---- TSV builders for ad-hoc fixtures -----------------------------------


def _build_guides_tsv(
    *rows: dict[str, str], with_cctop: bool = False
) -> str:
    """Construct a guides TSV with the spec column set, one row per dict.

    Setting ``with_cctop=True`` adds the ``CCTop-Score`` column between
    Azimuth and Out-of-Frame-Score — the hg19-vs-sacCer3 column variance
    we need to test parse-defensive header handling against.
    """
    columns = [
        "seqId",
        "guideId",
        "targetSeq",
        "mitSpecScore",
        "offtargetCount",
        "targetGenomeGeneLocus",
        "Doench '16-Score",
        "Doench '16-Old-Score",
        "Chari-Score",
        "Xu-Score",
        "Doench '14-Score",
        "Wang-Score",
        "Moreno-Mateos-Score",
        "Azimuth in-vitro-Score",
    ]
    if with_cctop:
        columns.append("CCTop-Score")
    columns.append("Out-of-Frame-Score")
    lines = ["#" + "\t".join(columns)]
    for row in rows:
        lines.append("\t".join(row.get(c, "") for c in columns))
    return "\n".join(lines) + "\n"


def _build_offtargets_tsv(*rows: dict[str, str]) -> str:
    """Construct an off-targets TSV with the spec column set."""
    columns = [
        "seqId",
        "guideId",
        "guideSeq",
        "offtargetSeq",
        "mismatchPos",
        "mismatchCount",
        "mitOfftargetScore",
        "cfdOfftargetScore",
        "chrom",
        "start",
        "end",
        "strand",
        "locusDesc",
    ]
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(row.get(c, "") for c in columns))
    return "\n".join(lines) + "\n"


def _happy_runner() -> FakeRunner:
    """Three-guide sacCer3-style result with mixed locusDesc + full scoring."""
    return FakeRunner(
        guides_tsv=(FIXTURES / "crispor_guides_happy.tsv").read_text(),
        offtargets_tsv=(FIXTURES / "crispor_offtargets_happy.tsv").read_text(),
    )


# ---- spec-shape happy path ----------------------------------------------


async def test_bio_design_grna_returns_spec_fields_for_sacCer3_target() -> None:
    """End-to-end through the tool boundary: a sacCer3 target with three
    guide hits must return a dict carrying every spec §4.7 output field —
    ``guides``, ``candidate_guides_count``, ``returned_guides_count``,
    ``provenance``, ``confidence``, ``genome``, ``pam`` — with the top
    guide ranked first by MIT specificity score and PAM split correctly
    from the 23 nt targetSeq.
    """
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        max_guides=10,
        runner=_happy_runner(),
    )
    required_keys = {
        "guides",
        "candidate_guides_count",
        "returned_guides_count",
        "provenance",
        "confidence",
        "genome",
        "pam",
    }
    assert required_keys.issubset(out.keys()), (
        f"Missing keys: {sorted(required_keys - out.keys())}"
    )
    assert out["genome"] == "sacCer3"
    assert out["pam"] == "NGG"
    assert isinstance(out["guides"], list)
    assert len(out["guides"]) == 3, f"expected 3 guides, got {len(out['guides'])}"

    top = out["guides"][0]
    assert top["sequence"] == "GCAGGCATGTACGTACGTAC", (
        f"top guide spacer should be 20 nt with PAM stripped; got {top.get('sequence')!r}"
    )
    assert top["pam"] == "AGG"
    assert top["specificity_score"] == 95
    assert top["strand"] == "+"
    assert isinstance(top.get("efficiency_scores"), dict)
    assert top["efficiency_scores"].get("doench16") == 68
    assert isinstance(top.get("off_targets"), list)
    assert top.get("off_target_summary", {}).get("0_mm", -1) >= 0


async def test_bio_design_grna_sorts_top_n_by_mit_specificity_descending() -> None:
    """Guides come back sorted by mitSpecScore descending; max_guides caps
    how many are returned. ``candidate_guides_count`` reflects the full
    set CRISPOR found, ``returned_guides_count`` reflects what's in the
    response.
    """
    out = await bio_design_grna(
        target_sequence="C" * 500,
        genome="sacCer3",
        pam="NGG",
        max_guides=2,
        runner=_happy_runner(),
    )
    assert out["candidate_guides_count"] == 3
    assert out["returned_guides_count"] == 2
    scores = [g["specificity_score"] for g in out["guides"]]
    assert scores == [95, 65], f"expected [95, 65] descending; got {scores}"


async def test_bio_design_grna_runner_call_args_match_input() -> None:
    """The runner receives the uppercased target sequence + the literal
    genome / pam / mismatch-ceiling values from the input — no silent
    rewriting.
    """
    runner = _happy_runner()
    await bio_design_grna(
        target_sequence=("acgtacgt" * 7) + "acgtacgt",  # 64 nt lowercase
        genome="sacCer3",
        pam="NGG",
        max_guides=5,
        max_off_target_mismatches=3,
        runner=runner,
    )
    assert runner.last_call is not None
    assert runner.last_call["genome"] == "sacCer3"
    assert runner.last_call["pam"] == "NGG"
    assert runner.last_call["max_off_target_mismatches"] == 3
    assert runner.last_call["target_sequence"] == "ACGTACGT" * 8


# ---- locusDesc / off-target classification ------------------------------


async def test_bio_design_grna_classifies_off_targets_into_cds_intron_intergenic() -> None:
    """Each off-target's locusDesc is decomposed into ``locus_class``
    (CDS / intron / intergenic / unknown) and the original ``locus_desc``
    is preserved verbatim. The fixture covers exon, intron, and intergenic
    explicitly; ``unknown`` is exercised in a separate case below.
    """
    out = await bio_design_grna(
        target_sequence="G" * 500,
        genome="sacCer3",
        pam="NGG",
        max_guides=10,
        runner=_happy_runner(),
    )
    classes_seen = {
        ot["locus_class"]
        for guide in out["guides"]
        for ot in guide["off_targets"]
    }
    assert {"CDS", "intron", "intergenic"}.issubset(classes_seen), (
        f"Expected CDS/intron/intergenic; got {sorted(classes_seen)}"
    )
    # Verify locus_desc preserved verbatim for the top guide's intron OT.
    top = out["guides"][0]
    intron_ot = next(
        ot for ot in top["off_targets"] if ot["locus_class"] == "intron"
    )
    assert intron_ot["locus_desc"] == "intron:NUP100"


async def test_bio_design_grna_classifies_unrecognised_locus_desc_as_unknown() -> None:
    """A locusDesc that does not match any of the three documented prefixes
    (e.g. CRISPOR shipping a new annotation type) maps to "unknown" rather
    than crashing the parser. Forward-compatible with future CRISPOR
    output drift.
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "targetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mitSpecScore": "80",
            "offtargetCount": "1",
            "targetGenomeGeneLocus": "exon:GENE1",
        }
    )
    offtargets_tsv = _build_offtargets_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "guideSeq": "ACGTACGTACGTACGTACGTAGG",
            "offtargetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mismatchPos": "....................",
            "mismatchCount": "0",
            "mitOfftargetScore": "1.0",
            "cfdOfftargetScore": "1.0",
            "chrom": "chrI",
            "start": "100",
            "end": "123",
            "strand": "+",
            "locusDesc": "regulatoryRegion:enhancer42",  # novel prefix
        }
    )
    runner = FakeRunner(guides_tsv, offtargets_tsv)
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    assert out["guides"][0]["off_targets"][0]["locus_class"] == "unknown"
    assert (
        out["guides"][0]["off_targets"][0]["locus_desc"]
        == "regulatoryRegion:enhancer42"
    )


# ---- score nullability (NotEnoughFlankSeq + friends) --------------------


async def test_bio_design_grna_surfaces_NotEnoughFlankSeq_as_per_guide_null() -> None:
    """NotEnoughFlankSeq across every score column means CRISPOR couldn't
    extract enough flanking sequence — the wrapper surfaces each as null
    in efficiency_scores plus the reason in score_unavailable_reason.
    Distinguishes "not computable" from "computed zero" downstream.
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "shortIn",
            "guideId": "30forw",
            "targetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mitSpecScore": "68",
            "offtargetCount": "0",
            "targetGenomeGeneLocus": "exon:CAN1",
            "Doench '16-Score": "NotEnoughFlankSeq",
            "Doench '16-Old-Score": "NotEnoughFlankSeq",
            "Chari-Score": "NotEnoughFlankSeq",
            "Xu-Score": "NotEnoughFlankSeq",
            "Doench '14-Score": "NotEnoughFlankSeq",
            "Wang-Score": "NotEnoughFlankSeq",
            "Moreno-Mateos-Score": "NotEnoughFlankSeq",
            "Azimuth in-vitro-Score": "NotEnoughFlankSeq",
            "Out-of-Frame-Score": "NotEnoughFlankSeq",
        }
    )
    runner = FakeRunner(guides_tsv, _build_offtargets_tsv())
    out = await bio_design_grna(
        target_sequence="A" * 100,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    g = out["guides"][0]
    # specificity_score is from CRISPOR's BWA-derived MIT score, which
    # does not depend on flanking sequence — stays populated.
    assert g["specificity_score"] == 68
    # Every flanking-dependent score is null with reason.
    for spec_key in (
        "doench16",
        "doench16_old",
        "chari",
        "xu",
        "doench14",
        "wang",
        "moreno_mateos",
        "azimuth",
        "out_of_frame",
    ):
        assert g["efficiency_scores"][spec_key] is None, (
            f"{spec_key} should be null when NotEnoughFlankSeq; got "
            f"{g['efficiency_scores'][spec_key]}"
        )
        assert (
            g["score_unavailable_reason"][spec_key]
            == "insufficient flanking sequence"
        )


async def test_bio_design_grna_empty_score_cell_treated_as_not_computed() -> None:
    """An empty score cell (e.g. when --noEffScores was passed) maps to
    null with reason "score not computed" — distinct from
    NotEnoughFlankSeq's reason. Lets the model see why a score is missing.
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "targetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mitSpecScore": "80",
            "offtargetCount": "0",
            "targetGenomeGeneLocus": "exon:GENE1",
            # All score columns left as empty strings.
        }
    )
    runner = FakeRunner(guides_tsv, _build_offtargets_tsv())
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    g = out["guides"][0]
    assert g["efficiency_scores"]["doench16"] is None
    assert g["score_unavailable_reason"]["doench16"] == "score not computed"


# ---- column variance (CCTop-Score) --------------------------------------


async def test_bio_design_grna_handles_cctop_score_column_for_hg19_style_runs() -> None:
    """hg19's bundled sample emits a CCTop-Score column that sacCer3's
    does not. The wrapper indexes by header name (not position) so the
    extra column is parsed cleanly into efficiency_scores under "cctop".
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "hg19_T",
            "guideId": "447rev",
            "targetSeq": "ATTGAGTGACCACTCTACGGTGG",
            "mitSpecScore": "95",
            "offtargetCount": "46",
            "targetGenomeGeneLocus": "intergenic:FBXL18-ACTB",
            "Doench '16-Score": "70",
            "Doench '16-Old-Score": "66",
            "Chari-Score": "96",
            "Xu-Score": "0.995",
            "Doench '14-Score": "41",
            "Wang-Score": "90",
            "Moreno-Mateos-Score": "47",
            "Azimuth in-vitro-Score": "43",
            "CCTop-Score": "0.834",
            "Out-of-Frame-Score": "53",
        },
        with_cctop=True,
    )
    runner = FakeRunner(guides_tsv, _build_offtargets_tsv())
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="hg19",
        pam="NGG",
        runner=runner,
    )
    g = out["guides"][0]
    assert g["efficiency_scores"]["doench16"] == 70
    assert g["efficiency_scores"]["cctop"] == pytest.approx(0.834)
    assert g["efficiency_scores"]["azimuth"] == 43


async def test_bio_design_grna_unknown_score_columns_land_in_additional_scores() -> None:
    """If CRISPOR ships a new score column the wrapper hasn't been taught
    about, that column survives in additional_scores rather than silently
    disappearing — defensive against future CRISPOR upgrades.
    """
    columns = [
        "seqId",
        "guideId",
        "targetSeq",
        "mitSpecScore",
        "offtargetCount",
        "targetGenomeGeneLocus",
        "Doench '16-Score",
        "Out-of-Frame-Score",
        "FuturisticNewScore",  # unknown
    ]
    guides_tsv = (
        "#" + "\t".join(columns) + "\n"
        + "T\t10forw\tACGTACGTACGTACGTACGTAGG\t80\t0\texon:GENE1\t68\t52\t0.42\n"
    )
    runner = FakeRunner(guides_tsv, _build_offtargets_tsv())
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    g = out["guides"][0]
    assert g["efficiency_scores"]["doench16"] == 68
    assert g["additional_scores"].get("FuturisticNewScore") == "0.42"


# ---- off-target table mechanics -----------------------------------------


async def test_bio_design_grna_off_target_summary_buckets_count_by_mismatches() -> None:
    """off_target_summary aggregates rows by mismatchCount — one bucket
    per unique mismatch count seen, including 0_mm for the on-target
    self-match.
    """
    out = await bio_design_grna(
        target_sequence="T" * 500,
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    # Guide 250forw fixture: 1×0_mm, 1×1_mm, 2×2_mm.
    middle = next(g for g in out["guides"] if g["guide_id"] == "250forw")
    assert middle["off_target_summary"] == {"0_mm": 1, "1_mm": 1, "2_mm": 2}
    assert middle["total_off_targets"] == 4
    assert middle["off_targets_truncated"] is False


async def test_bio_design_grna_truncates_off_target_list_at_100() -> None:
    """A guide with more than 100 off-targets has its off_targets list
    capped at 100 with off_targets_truncated=True; total_off_targets
    preserves the original count so the cap is visible to callers.
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "targetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mitSpecScore": "20",
            "offtargetCount": "150",
            "targetGenomeGeneLocus": "exon:GENE1",
        }
    )
    # 150 off-targets, mostly mm=4.
    rows = []
    for i in range(150):
        rows.append(
            {
                "seqId": "T",
                "guideId": "10forw",
                "guideSeq": "ACGTACGTACGTACGTACGTAGG",
                "offtargetSeq": f"ACGTACGTACGTACGTAC{i:03d}AGG",
                "mismatchPos": "..............**....",
                "mismatchCount": "4",
                "mitOfftargetScore": "0.05",
                "cfdOfftargetScore": "0.02",
                "chrom": f"chr{i}",
                "start": str(1000 + i),
                "end": str(1023 + i),
                "strand": "+",
                "locusDesc": "intergenic:G1-G2",
            }
        )
    offtargets_tsv = _build_offtargets_tsv(*rows)
    runner = FakeRunner(guides_tsv, offtargets_tsv)
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    g = out["guides"][0]
    assert len(g["off_targets"]) == 100, (
        f"truncation cap broken: got {len(g['off_targets'])} entries"
    )
    assert g["off_targets_truncated"] is True
    assert g["total_off_targets"] == 150
    # Bucket count still reflects all rows.
    assert g["off_target_summary"]["4_mm"] == 150


async def test_bio_design_grna_guide_with_no_off_targets() -> None:
    """A guide with no off-target rows in the off-targets TSV (e.g. the
    sequence is unique in the genome with no mismatch-tolerant hits)
    surfaces empty off_targets, empty summary, and cfd_specificity=None.
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "targetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mitSpecScore": "100",
            "offtargetCount": "0",
            "targetGenomeGeneLocus": "exon:GENE1",
        }
    )
    runner = FakeRunner(guides_tsv, _build_offtargets_tsv())
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    g = out["guides"][0]
    assert g["off_targets"] == []
    assert g["off_target_summary"] == {}
    assert g["cfd_specificity"] is None


async def test_bio_design_grna_cfd_specificity_excludes_on_target_self_match() -> None:
    """CFD specificity per Doench 2016 = 1 / (1 + sum(cfd_score))
    over off-targets *excluding* the on-target 0-mm self-match. For the
    happy-fixture top guide (one off-target with cfd=0.32, plus on-target
    cfd=1.0 excluded), specificity = 1 / (1 + 0.32) = 0.7576.
    """
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    top = out["guides"][0]
    assert top["cfd_specificity"] == pytest.approx(0.7576, abs=1e-3)


# ---- strand parsing -----------------------------------------------------


async def test_bio_design_grna_reverse_strand_guides_have_minus_strand() -> None:
    """guideId ending in 'rev' → strand '-'; ending in 'forw' → strand '+'.
    Position is the integer prefix in either case.
    """
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    rev = next(g for g in out["guides"] if g["guide_id"] == "350rev")
    assert rev["strand"] == "-"
    assert rev["position"] == 350
    forw = next(g for g in out["guides"] if g["guide_id"] == "150forw")
    assert forw["strand"] == "+"
    assert forw["position"] == 150


# ---- on-target locus derivation -----------------------------------------


async def test_bio_design_grna_on_target_locus_derived_from_zero_mm_row() -> None:
    """The on-target genomic location is the chrom:start of the 0-mm
    self-match in the off-target table — surfaced as
    "<genome>:<chrom>:<start>".
    """
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    top = out["guides"][0]
    assert top["on_target_position"] == "sacCer3:chrV:100150"


async def test_bio_design_grna_on_target_locus_none_when_no_zero_mm_match() -> None:
    """If no 0-mm row exists (novel sequence not in the genome), the
    on-target position is None — honest about not having a location
    rather than fabricating one.
    """
    guides_tsv = _build_guides_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "targetSeq": "ACGTACGTACGTACGTACGTAGG",
            "mitSpecScore": "80",
            "offtargetCount": "1",
            "targetGenomeGeneLocus": "exon:GENE1",
        }
    )
    offtargets_tsv = _build_offtargets_tsv(
        {
            "seqId": "T",
            "guideId": "10forw",
            "guideSeq": "ACGTACGTACGTACGTACGTAGG",
            "offtargetSeq": "ACGTACGTACGTACGTAC**AGG",
            "mismatchPos": "..................**",
            "mismatchCount": "2",
            "mitOfftargetScore": "0.1",
            "cfdOfftargetScore": "0.15",
            "chrom": "chrIV",
            "start": "5000",
            "end": "5023",
            "strand": "+",
            "locusDesc": "intergenic:GENE1-GENE2",
        }
    )
    runner = FakeRunner(guides_tsv, offtargets_tsv)
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    assert out["guides"][0]["on_target_position"] is None


# ---- input validation ---------------------------------------------------


async def test_bio_design_grna_rejects_short_target_sequence() -> None:
    """target_sequence must be 50-2000 nt. Below 50 → schema error."""
    out = await bio_design_grna(
        target_sequence="ACGT" * 5,  # 20 nt
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    assert out.get("error") is True
    assert "Invalid input" in out["message"]


async def test_bio_design_grna_rejects_non_acgtn_alphabet() -> None:
    """target_sequence alphabet must be ACGTN only — non-canonical bases
    (e.g. IUPAC ambiguity codes like R/Y) reject at the schema layer.
    """
    out = await bio_design_grna(
        target_sequence="ACGTRYKM" * 10,  # 80 chars, mostly invalid
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    assert out.get("error") is True


# ---- runner failure paths -----------------------------------------------


async def test_bio_design_grna_genome_not_found_returns_actionable_error() -> None:
    """When the runner reports a missing genome, the tool returns the
    project-standard error_response shape with suggestions that name
    fetch_genome.sh + the bundled sacCer3 fallback path.
    """
    runner = RaisingRunner(
        GenomeIndexNotFound(
            genome="sacCer3",
            expected_path=Path("/opt/crispor/genomes/sacCer3"),
            missing=["sacCer3.fa.bwt", "sacCer3.segments.bed"],
        )
    )
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    assert out.get("error") is True
    assert "sacCer3" in out["message"]
    assert "sacCer3.fa.bwt" in out["message"]
    assert any("fetch_genome.sh" in s for s in out["suggestions"])


async def test_bio_design_grna_crispor_run_failed_returns_actionable_error() -> None:
    """When CRISPOR's subprocess exits non-zero, the tool returns a
    structured error rather than letting the exception propagate.
    """
    runner = RaisingRunner(
        CrisporRunFailed(
            genome="sacCer3", returncode=1, stderr="bwa: index missing"
        )
    )
    out = await bio_design_grna(
        target_sequence="A" * 200,
        genome="sacCer3",
        pam="NGG",
        runner=runner,
    )
    assert out.get("error") is True
    assert "CRISPOR subprocess failed" in out["message"]
    assert any("bwa" in s.lower() for s in out["suggestions"])


# ---- provenance + confidence --------------------------------------------


async def test_bio_design_grna_provenance_carries_genome_and_versions() -> None:
    """Provenance must include source=CRISPOR, genome echoed, tool_version,
    fetched_at ISO timestamp, and the upstream URL — enough for a caller
    to reproduce or cite the run.
    """
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        runner=_happy_runner(),
    )
    prov = out["provenance"]
    assert prov["source"] == "CRISPOR"
    assert prov["genome"] == "sacCer3"
    assert "tool_version" in prov
    assert prov["fetched_at"].endswith("+00:00") or "T" in prov["fetched_at"]
    assert "crispor" in prov["url"].lower()
