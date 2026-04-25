"""Unit tests for ``bio_fold_sequence`` (spec §4.8).

The tool computes RNA / DNA secondary structure via the ViennaRNA Python
bindings (``import RNA``) — no upstream API at runtime, so tests are
wholly offline. The Turner 2004 (RNA) and Mathews 2004 (DNA) parameter
sets are deterministic; expected MFE values are hand-verified against
the reference build (ViennaRNA 2.7.2).

There is no separate ``RUN_INTEGRATION=1`` suite for this tool because
there is no upstream API to integrate with — the prompt's "integration
test against tRNA-Phe" is included here as an offline unit test (the
fold is fully deterministic; "integration" only adds value when an
external service is in the loop).
"""

from __future__ import annotations

import asyncio

import pytest

from bioinformatics_mcp.tools.fold_sequence import bio_fold_sequence


SHORT_HAIRPIN_RNA = "GGGAAAUCCC"
"""10-nt hand-checkable hairpin: 5'-GGG (paired) + AAAU (loop) + CCC-3'.
Folds to ``(((....)))`` at 37 °C under Turner 2004 with MFE -2.5 kcal/mol.
At the spec §4.8 minimum sequence length, exercises the boundary."""

SHORT_HAIRPIN_DNA = "GGGAAATCCC"
"""DNA equivalent of SHORT_HAIRPIN_RNA — same 5'-GGG / AAAT loop / CCC-3'
geometry under Mathews 2004 DNA parameters."""

# Yeast tRNA-Phe, canonical sequence with 3'-CCA tail (76 nt). Folds to the
# textbook four-helix cloverleaf at 37 °C; verified MFE -22.40 kcal/mol
# under ViennaRNA 2.7.2 / Turner 2004.
TRNA_PHE_YEAST = (
    "GCGGAUUUAGCUCAGUUGGGAGAGCGCCAGACUGAAGAUCUGGAGGUCCUGUGUUCGAUCCACAGAAUUCGCACCA"
)


# ---- short hairpin (RNA) ------------------------------------------------


async def test_bio_fold_sequence_returns_spec_fields_for_short_hairpin() -> None:
    """End-to-end through the tool boundary: a 10 nt RNA hairpin folded at
    37 °C must return a dict carrying every spec §4.8 output field with
    the deterministic Turner-2004 result.
    """
    out = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_RNA,
        sequence_type="rna",
        temperature=37.0,
    )
    required_keys = {
        "sequence",
        "sequence_type",
        "temperature_celsius",
        "length",
        "structure",
        "mfe_kcal_per_mol",
        "base_pair_probabilities",
        "provenance",
        "confidence",
    }
    assert required_keys.issubset(out.keys()), (
        f"Missing keys: {sorted(required_keys - out.keys())}"
    )
    assert out["sequence"] == SHORT_HAIRPIN_RNA
    assert out["sequence_type"] == "rna"
    assert out["temperature_celsius"] == 37.0
    assert out["length"] == 10
    assert out["structure"] == "(((....)))"
    assert out["mfe_kcal_per_mol"] == pytest.approx(-2.5, abs=0.01)
    assert out["provenance"]["source"] == "ViennaRNA"


async def test_bio_fold_sequence_lowercase_input_normalises_to_uppercase() -> None:
    """Tool must uppercase the sequence before folding so that lowercase
    input produces the same MFE as uppercase. The echoed ``sequence`` field
    in the response is the uppercased form.
    """
    out = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_RNA.lower(),
        sequence_type="rna",
        temperature=37.0,
    )
    assert out["sequence"] == SHORT_HAIRPIN_RNA
    assert out["mfe_kcal_per_mol"] == pytest.approx(-2.5, abs=0.01)


async def test_bio_fold_sequence_per_position_pair_probabilities_have_correct_shape() -> None:
    """``base_pair_probabilities.per_position`` is a list with one entry per
    sequence position, each in [0, 1]. The five paired positions of the
    short hairpin (GGG / CCC arms — six bases including the wobble) should
    have substantially higher pairing probability than the AAAU loop.
    """
    out = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_RNA,
        sequence_type="rna",
        temperature=37.0,
    )
    bpp = out["base_pair_probabilities"]
    assert isinstance(bpp["per_position"], list)
    assert len(bpp["per_position"]) == 10, (
        f"per_position length {len(bpp['per_position'])} != sequence length 10"
    )
    assert all(0.0 <= p <= 1.0 for p in bpp["per_position"]), (
        f"per_position has out-of-range entries: {bpp['per_position']}"
    )
    # Ends paired (positions 0 and 9), middle in loop (positions 4 and 5).
    assert bpp["per_position"][0] > bpp["per_position"][4], (
        "5' G expected to pair more than mid-loop A"
    )
    assert bpp["per_position"][9] > bpp["per_position"][5], (
        "3' C expected to pair more than mid-loop A"
    )
    assert 0.0 <= bpp["mean_pair_probability"] <= 1.0


# ---- short hairpin (DNA) ------------------------------------------------


async def test_bio_fold_sequence_dna_mode_uses_mathews_2004_parameters() -> None:
    """DNA mode must accept a T-bearing sequence and return a fold that
    reflects the Mathews 2004 DNA parameter set. The canonical 10 nt
    GGG/AAAT/CCC hairpin folds to the same dot-bracket as the RNA version
    (the geometry is identical) but the MFE differs because base-stacking
    energetics are weaker in B-form DNA.
    """
    out = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_DNA,
        sequence_type="dna",
        temperature=37.0,
    )
    assert out["sequence_type"] == "dna"
    assert out["sequence"] == SHORT_HAIRPIN_DNA
    assert out["structure"] == "(((....)))"
    assert out["provenance"]["parameter_set"] == "Mathews 2004 (DNA)"
    # MFE is negative (favourable) but typically much less so than the RNA
    # equivalent. Range here is permissive — the structural assertion is
    # the load-bearing part of the test.
    assert out["mfe_kcal_per_mol"] < 0.0


async def test_bio_fold_sequence_dna_mode_does_not_leak_params_to_subsequent_rna_call() -> None:
    """After a DNA fold the parameter table must restore to RNA Turner 2004
    so a follow-up RNA call gets the correct MFE. Without the finally-block
    restore, RNA folds after DNA folds would silently use Mathews 2004 and
    return wrong energies.
    """
    await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_DNA,
        sequence_type="dna",
        temperature=37.0,
    )
    out = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_RNA,
        sequence_type="rna",
        temperature=37.0,
    )
    assert out["mfe_kcal_per_mol"] == pytest.approx(-2.5, abs=0.01), (
        f"RNA MFE drifted to {out['mfe_kcal_per_mol']} after a DNA fold — "
        "parameter restore is broken."
    )
    assert out["provenance"]["parameter_set"] == "Turner 2004 (RNA)"


# ---- temperature --------------------------------------------------------


async def test_bio_fold_sequence_temperature_monotone_destabilises_structure() -> None:
    """Cooler folds should be more stable (more negative MFE) than warmer
    folds — basic free-energy thermodynamics. Verified for tRNA-Phe at
    10 / 37 / 65 °C against ViennaRNA 2.7.2: -38.57 / -22.40 / -6.98.
    """
    cold = await bio_fold_sequence(
        sequence=TRNA_PHE_YEAST,
        sequence_type="rna",
        temperature=10.0,
    )
    body = await bio_fold_sequence(
        sequence=TRNA_PHE_YEAST,
        sequence_type="rna",
        temperature=37.0,
    )
    hot = await bio_fold_sequence(
        sequence=TRNA_PHE_YEAST,
        sequence_type="rna",
        temperature=65.0,
    )
    assert cold["mfe_kcal_per_mol"] < body["mfe_kcal_per_mol"] < hot["mfe_kcal_per_mol"], (
        f"Temperature ordering broken: 10°C={cold['mfe_kcal_per_mol']}, "
        f"37°C={body['mfe_kcal_per_mol']}, 65°C={hot['mfe_kcal_per_mol']}"
    )


# ---- tRNA-Phe (the prompt's nominated test substrate) -------------------


async def test_bio_fold_sequence_yeast_trna_phe_folds_to_cloverleaf() -> None:
    """Yeast tRNA-Phe is the textbook test case for RNA structure
    prediction: a 76 nt sequence whose MFE structure is the four-helix
    cloverleaf (acceptor stem, D-stem, anticodon stem, TΨC stem). Verified
    against ViennaRNA 2.7.2 / Turner 2004 at 37 °C: MFE -22.40 kcal/mol
    with 21 base pairs.
    """
    out = await bio_fold_sequence(
        sequence=TRNA_PHE_YEAST,
        sequence_type="rna",
        temperature=37.0,
    )
    assert out["length"] == 76
    assert out["mfe_kcal_per_mol"] == pytest.approx(-22.40, abs=0.5)
    # The cloverleaf has 21 base pairs (each represented as one '(' in
    # dot-bracket); a tighter exact-equal would risk breaking on minor
    # parameter updates so we range-check.
    open_count = out["structure"].count("(")
    assert 18 <= open_count <= 24, (
        f"Cloverleaf should have ~21 base pairs, got {open_count} from "
        f"structure {out['structure']!r}"
    )
    # Sanity: the structure string is the same length as the sequence.
    assert len(out["structure"]) == 76


# ---- input validation ---------------------------------------------------


async def test_bio_fold_sequence_rejects_t_in_rna_alphabet() -> None:
    """An RNA call with a T-bearing sequence must fail loud — the T is a
    DNA base and ViennaRNA would silently treat it as an unrecognised
    character. The error names the offending character and the expected
    alphabet so the model can correct.
    """
    out = await bio_fold_sequence(
        sequence="GGGAAATCCC",  # the SHORT_HAIRPIN_DNA — T leaks if RNA mode used
        sequence_type="rna",
        temperature=37.0,
    )
    assert out.get("error") is True
    assert "T" in out["message"], (
        f"Error message should name the offending base T, got {out['message']!r}"
    )
    assert "ACGU" in out["message"], "Error should name expected RNA alphabet"


async def test_bio_fold_sequence_rejects_u_in_dna_alphabet() -> None:
    """Symmetric: DNA mode + a U-bearing sequence must error rather than
    fold an invalid input."""
    out = await bio_fold_sequence(
        sequence="GGGAAAUCCC",  # SHORT_HAIRPIN_RNA — U leaks if DNA mode used
        sequence_type="dna",
        temperature=37.0,
    )
    assert out.get("error") is True
    assert "U" in out["message"]
    assert "ACGT" in out["message"]


async def test_bio_fold_sequence_rejects_too_short_sequence() -> None:
    """Spec §4.8 requires sequence length 10-5000. Below 10 → schema error."""
    out = await bio_fold_sequence(
        sequence="GGGCCC",
        sequence_type="rna",
        temperature=37.0,
    )
    assert out.get("error") is True
    assert "Invalid input" in out["message"]


async def test_bio_fold_sequence_rejects_temperature_out_of_range() -> None:
    """Temperature must be in [0, 100] °C. Negative input → schema error."""
    out = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_RNA,
        sequence_type="rna",
        temperature=-10.0,
    )
    assert out.get("error") is True
    assert "Invalid input" in out["message"]


# ---- concurrency --------------------------------------------------------


async def test_bio_fold_sequence_concurrent_dna_and_rna_calls_do_not_corrupt() -> None:
    """Race-condition coverage. Without the asyncio.Lock around the
    parameter-table mutation, concurrent DNA + RNA calls could interleave
    a DNA params_load with an RNA fold and vice versa, silently producing
    wrong MFE values. Fire several calls of each kind concurrently and
    verify each call's result matches its single-call baseline.
    """
    rna_baseline = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_RNA,
        sequence_type="rna",
        temperature=37.0,
    )
    dna_baseline = await bio_fold_sequence(
        sequence=SHORT_HAIRPIN_DNA,
        sequence_type="dna",
        temperature=37.0,
    )
    tasks = []
    for _ in range(4):
        tasks.append(
            bio_fold_sequence(
                sequence=SHORT_HAIRPIN_RNA, sequence_type="rna", temperature=37.0
            )
        )
        tasks.append(
            bio_fold_sequence(
                sequence=SHORT_HAIRPIN_DNA, sequence_type="dna", temperature=37.0
            )
        )
    results = await asyncio.gather(*tasks)
    for i, out in enumerate(results):
        baseline = rna_baseline if i % 2 == 0 else dna_baseline
        assert out["structure"] == baseline["structure"], (
            f"Concurrent call {i} drifted: structure {out['structure']!r} != "
            f"baseline {baseline['structure']!r}"
        )
        assert out["mfe_kcal_per_mol"] == pytest.approx(
            baseline["mfe_kcal_per_mol"], abs=0.01
        ), f"Concurrent call {i} MFE drifted: {out['mfe_kcal_per_mol']} != {baseline['mfe_kcal_per_mol']}"
