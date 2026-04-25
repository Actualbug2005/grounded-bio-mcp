"""Unit tests for ``bio_fold_sequence`` (spec §4.8).

The tool computes RNA / DNA secondary structure via the ViennaRNA Python
bindings (``import RNA``) — no upstream API at runtime, so tests are
wholly offline. The Turner 2004 (RNA) and Mathews 2004 (DNA) parameter
sets are deterministic; expected MFE values are hand-verified against
the reference build (ViennaRNA 2.7.2).
"""

from __future__ import annotations

import pytest

from bioinformatics_mcp.tools.fold_sequence import bio_fold_sequence


SHORT_HAIRPIN_RNA = "GGGAAAUCCC"
"""10-nt hand-checkable hairpin: 5'-GGG (paired) + AAAU (loop) + CCC-3'.
Folds to ``(((....)))`` at 37 °C under Turner 2004 with MFE -2.5 kcal/mol.
At the spec §4.8 minimum sequence length, exercises the boundary."""


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
