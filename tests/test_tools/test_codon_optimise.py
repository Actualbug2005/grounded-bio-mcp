"""Unit tests for ``bio_codon_optimise`` (spec §4.19).

The tool has no upstream API — codon usage tables are resolved from
``python_codon_tables`` (3 organisms) or from bundled Kazusa CSVs under
``src/bioinformatics_mcp/data/codon_tables/`` (the other 3). Tests are
therefore wholly offline; no integration suite exists for this tool.
"""

from __future__ import annotations

import pytest

from bioinformatics_mcp.tools.codon_optimise import (
    SUPPORTED_ORGANISMS,
    _load_codon_table,
)


# ---- loader -------------------------------------------------------------


def test_load_codon_table_ecoli_k12_returns_dna_codons() -> None:
    """The library-shipped E. coli table should resolve via the ``ecoli_k12``
    spec alias and return DNA codons (T, not U) with the canonical Met
    codon ``ATG`` carrying frequency 1.0.
    """
    table = _load_codon_table("ecoli_k12")
    assert "M" in table
    assert table["M"] == {"ATG": 1.0}
    # DNA alphabet — no U codons should appear anywhere.
    all_codons = {codon for aa in table.values() for codon in aa}
    assert not any("U" in codon for codon in all_codons), (
        f"Found U-bearing codons: {[c for c in all_codons if 'U' in c]}"
    )


def test_load_codon_table_bundled_organisms_resolve_to_dna_codons() -> None:
    """The three bundled Kazusa organisms (p_pastoris/cho/sf9) must each
    resolve to a complete DNA codon table — 21 amino-acid keys (20 + stop),
    six Leu codons, frequencies summing to ~1 per amino acid (modulo
    Kazusa's 2-decimal rounding).
    """
    for organism in ("p_pastoris", "cho", "sf9"):
        table = _load_codon_table(organism)
        assert len(table) == 21, f"{organism}: expected 21 keys (20 AA + stop), got {len(table)}"
        assert len(table["L"]) == 6, f"{organism}: Leu should have 6 codons, got {len(table['L'])}"
        # No U codons — bundled CSV uses U but loader replaces.
        all_codons = {codon for aa in table.values() for codon in aa}
        assert not any("U" in codon for codon in all_codons), f"{organism}: U codons leaked through"
        # Per-AA frequencies should sum to roughly 1.
        for aa, codons in table.items():
            assert 0.9 <= sum(codons.values()) <= 1.1, (
                f"{organism} {aa}: freqs sum to {sum(codons.values())}, expected ~1"
            )


def test_load_codon_table_unknown_organism_raises_value_error() -> None:
    """An organism alias not in SUPPORTED_ORGANISMS must raise ValueError —
    Pydantic catches this at the schema layer in normal use, but the loader
    is also called directly from tests and (potentially) tooling.
    """
    with pytest.raises(ValueError, match="Unknown target_organism"):
        _load_codon_table("not_a_real_organism")
    # Sanity: every supported alias actually loads.
    for alias in SUPPORTED_ORGANISMS:
        _load_codon_table(alias)
