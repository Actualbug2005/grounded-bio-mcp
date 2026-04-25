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
    _compute_cai,
    _compute_gc_pct,
    _compute_rare_codon_count,
    _load_codon_table,
    _optimise_frequency_max,
    bio_codon_optimise,
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


# ---- frequency-max optimiser --------------------------------------------


def test_optimise_frequency_max_single_methionine() -> None:
    """Single-residue protein 'M' must round-trip to ATG (only Met codon)
    plus the most-frequent stop. With no avoid_sites, conflicts list is
    empty.
    """
    table = _load_codon_table("ecoli_k12")
    dna, conflicts = _optimise_frequency_max("M", table, avoid_sites=[])
    # Most-frequent E. coli stop is TAA at ~0.64; the implementation must
    # append it to give a translation-ready ORF.
    assert dna == "ATGTAA"
    assert conflicts == []


def test_optimise_frequency_max_picks_top_codon_per_residue() -> None:
    """For ML in E. coli: Met → ATG (only choice), Leu → CTG (top freq 0.5),
    plus stop TAA. Confirms the per-residue greedy pick is correct.
    """
    table = _load_codon_table("ecoli_k12")
    dna, conflicts = _optimise_frequency_max("ML", table, avoid_sites=[])
    assert dna == "ATGCTGTAA"
    assert conflicts == []


def test_optimise_frequency_max_rejects_invalid_amino_acid() -> None:
    """Pydantic regex catches this at the schema layer in the tool, but the
    bare optimiser must also fail loud rather than silently emit a partial
    sequence — this matches the project's anti-hallucination ethos.
    """
    table = _load_codon_table("ecoli_k12")
    with pytest.raises(ValueError, match="Unrecognised amino-acid 'Z'"):
        _optimise_frequency_max("MZ", table, avoid_sites=[])


def test_optimise_frequency_max_avoids_restriction_site_via_synonymous_swap() -> None:
    """E. coli naive top picks for 'LQ' produce CTG+CAG = CTGCAG, which
    is a PstI recognition site. The optimiser must swap one of the two
    codons to a synonym so the PstI site disappears.

    Leu top is CTG (0.50); alternatives include TTG/TTA (0.13 each).
    Gln top is CAG (0.65); the only alternative is CAA (0.35).
    Either swap dissolves the PstI site; the optimiser should pick the
    least-frequency-loss option (Q → CAA, since 0.35 > 0.13).
    """
    table = _load_codon_table("ecoli_k12")
    dna, conflicts = _optimise_frequency_max("LQ", table, avoid_sites=["CTGCAG"])
    assert "CTGCAG" not in dna, f"PstI site survived in {dna!r}"
    assert dna.startswith("CTGCAA"), (
        f"Expected CTGCAA prefix (Q→CAA preserves L's top codon), got {dna!r}"
    )
    assert conflicts == []


def test_optimise_frequency_max_reports_unavoidable_conflict() -> None:
    """When NO synonymous codon can avoid a forbidden site (e.g. site is
    intrinsic to the encoded amino acid), the optimiser must keep the
    top-frequency codon AND surface the conflict so the caller sees the
    constraint failed honestly rather than silently shipping a bad
    sequence.

    'M' → ATG. If we forbid 'ATG' itself, there's nowhere to go (Met has
    only one codon).
    """
    table = _load_codon_table("ecoli_k12")
    dna, conflicts = _optimise_frequency_max("M", table, avoid_sites=["ATG"])
    assert dna.startswith("ATG"), "Met has only one codon — must remain ATG"
    assert any(c["site"] == "ATG" and c["position"] == 0 for c in conflicts), (
        f"Expected ATG conflict at position 0, got {conflicts!r}"
    )


# ---- metrics ------------------------------------------------------------


def test_cai_of_all_top_codons_is_one() -> None:
    """A sequence built entirely from each amino acid's top-frequency codon
    has CAI = 1 by definition (Sharp & Li 1987 — geometric mean of
    relative adaptiveness w_i = f_i / f_max). Stop codons are excluded
    from the calculation per the standard convention.
    """
    table = _load_codon_table("ecoli_k12")
    dna, _ = _optimise_frequency_max("MASTER", table, avoid_sites=[])
    cai = _compute_cai(dna, table)
    assert cai == pytest.approx(1.0, abs=1e-9), f"All-top sequence should have CAI=1, got {cai}"


def test_cai_of_all_rare_codons_is_low() -> None:
    """Force-build a sequence using a rare Leu codon (CTA, freq 0.04 in
    E. coli — w = 0.04 / 0.50 = 0.08) and verify CAI reflects this. A
    pure-CTA sequence should have CAI ~ 0.08.
    """
    table = _load_codon_table("ecoli_k12")
    dna = "CTA" * 5  # five Leu codons, no stop appended
    cai = _compute_cai(dna, table)
    expected = 0.04 / 0.50
    assert cai == pytest.approx(expected, abs=1e-3), (
        f"Pure-CTA Leu sequence should give CAI ~{expected:.3f}, got {cai}"
    )


def test_gc_pct_simple_cases() -> None:
    """GC% is base-fraction × 100. Three hand-checked cases."""
    assert _compute_gc_pct("ATG") == pytest.approx(33.333, abs=0.01)
    assert _compute_gc_pct("GCGC") == 100.0
    assert _compute_gc_pct("ATAT") == 0.0
    assert _compute_gc_pct("") == 0.0


def test_rare_codon_count_uses_per_aa_relative_frequency() -> None:
    """A 'rare' codon is one whose per-amino-acid relative frequency falls
    below 0.1 (the conventional threshold). Stop codons are excluded.

    All-CTG (top Leu, freq 0.50) → 0 rare codons.
    All-CTA (rare Leu, freq 0.04) → 5 rare codons in a 15 nt sequence.
    """
    table = _load_codon_table("ecoli_k12")
    assert _compute_rare_codon_count("CTG" * 5, table) == 0
    assert _compute_rare_codon_count("CTA" * 5, table) == 5


# ---- tool entry point ---------------------------------------------------


# Insulin B chain (UniProt P01308 residues 25-54) — 30 aa, the prompt's
# nominated smoke-test substrate for E. coli expression.
INSULIN_B_CHAIN = "FVNQHLCGSHLVEALYLVCGERGFFYTPKT"


async def test_bio_codon_optimise_returns_spec_fields_for_insulin_b_chain() -> None:
    """End-to-end through the tool boundary: insulin B chain optimised for
    E. coli must return a dict carrying every spec §4.19 output field
    with sensible values (CAI in [0, 1], non-negative GC%, sequence length
    = (protein length + 1 stop) × 3).
    """
    out = await bio_codon_optimise(
        protein_sequence=INSULIN_B_CHAIN,
        target_organism="ecoli_k12",
        avoid_restriction_sites=[],
    )
    required_keys = {
        "optimised_sequence",
        "target_organism",
        "protein_length",
        "length_nt",
        "codon_adaptation_index",
        "gc_content_pct",
        "rare_codon_count",
        "restriction_conflicts",
    }
    assert required_keys.issubset(out.keys()), (
        f"Missing keys: {required_keys - out.keys()}"
    )
    assert out["protein_length"] == 30
    assert out["length_nt"] == 31 * 3, "30 residues + 1 stop = 93 nt"
    assert len(out["optimised_sequence"]) == 93
    # All-top-codon optimisation should produce CAI very close to 1
    # (Sharp & Li exact == 1 only when every chosen codon == max-freq for
    # its AA; insulin B has no avoid_sites so there's no forced swap).
    assert out["codon_adaptation_index"] == pytest.approx(1.0, abs=1e-6)
    assert 0.0 <= out["gc_content_pct"] <= 100.0
    assert out["rare_codon_count"] == 0
    assert out["restriction_conflicts"] == []
    assert out["target_organism"] == "ecoli_k12"


async def test_bio_codon_optimise_rejects_invalid_amino_acid_at_schema_layer() -> None:
    """The Pydantic regex on protein_sequence must reject non-standard one-
    letter codes (here: 'B' which is the Asn/Asp ambiguity code) before
    the optimiser sees the input. The tool catches the validation error
    and returns the project-standard error_response shape."""
    out = await bio_codon_optimise(
        protein_sequence="MABCDEFG",
        target_organism="ecoli_k12",
        avoid_restriction_sites=[],
    )
    assert isinstance(out, dict)
    assert "error" in out, f"Expected error_response shape, got {out!r}"
