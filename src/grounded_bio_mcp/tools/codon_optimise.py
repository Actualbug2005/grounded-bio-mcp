"""`bio_codon_optimise` — codon optimisation for recombinant expression.

Phase 3. See spec §4.19.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=False,
idempotentHint=True, title="Codon-Optimise Sequence".

openWorldHint=False — the only tool in the project that does not query
external state at runtime. Codon usage tables come from two static sources:

- Three organisms ship with ``python-codon-tables`` (``ecoli_k12``,
  ``h_sapiens``, ``s_cerevisiae``);
- Three are bundled under ``src/grounded_bio_mcp/data/codon_tables/``
  as Kazusa CSVs (``p_pastoris``, ``cho``, ``sf9``) — see
  ``scripts/fetch_codon_tables.py`` for the refresh procedure.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import python_codon_tables as pct
from pydantic import BaseModel, Field, ValidationError
from python_codon_tables.python_codon_tables import table_with_U_replaced_by_T

from grounded_bio_mcp.utils.errors import error_response

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "codon_tables"

# Spec §4.19 organism alias → resolution strategy. Library-backed entries
# delegate to ``pct.get_codons_table`` (which itself handles U→T); bundled
# entries point to a CSV in ``_DATA_DIR`` parsed inline.
_LIBRARY_ALIASES: dict[str, str] = {
    "ecoli_k12": "e_coli_316407",
    "h_sapiens": "h_sapiens_9606",
    "s_cerevisiae": "s_cerevisiae_4932",
}
_BUNDLED_FILES: dict[str, str] = {
    "p_pastoris": "p_pastoris_4922.csv",
    "cho": "cho_10029.csv",
    "sf9": "sf9_7108.csv",
}
SUPPORTED_ORGANISMS: tuple[str, ...] = tuple(_LIBRARY_ALIASES) + tuple(_BUNDLED_FILES)


@lru_cache(maxsize=len(SUPPORTED_ORGANISMS))
def _load_codon_table(organism: str) -> dict[str, dict[str, float]]:
    """Resolve a spec organism alias to a DNA codon-frequency table.

    Returns ``{aa: {codon: frequency}}`` with codons in DNA alphabet (T,
    not U) and frequencies normalised per amino acid (sum approx 1.0).
    """
    if organism in _LIBRARY_ALIASES:
        return pct.get_codons_table(_LIBRARY_ALIASES[organism])
    if organism in _BUNDLED_FILES:
        csv_text = (_DATA_DIR / _BUNDLED_FILES[organism]).read_text()
        return table_with_U_replaced_by_T(pct.csv_string_to_codons_dict(csv_text))
    raise ValueError(
        f"Unknown target_organism {organism!r}; supported: {sorted(SUPPORTED_ORGANISMS)}"
    )


def _optimise_frequency_max(
    protein: str,
    table: dict[str, dict[str, float]],
    *,
    avoid_sites: list[str],
) -> tuple[str, list[dict[str, int | str]]]:
    """Greedy frequency-max codon optimisation with restriction-site avoidance.

    Returns ``(dna_sequence, restriction_conflicts)``. The DNA sequence
    includes the highest-frequency stop codon at the end so the result is
    translation-ready. ``restriction_conflicts`` is a list of
    ``{"site": ..., "position": ...}`` entries — empty when avoidance
    succeeded for every forbidden site.

    Algorithm: for each residue, walk codons in descending frequency
    order; pick the first one that, when appended, introduces no
    forbidden site spanning the join (we only need to look at the tail
    of the sequence — any new site involves at least one base from the
    new codon). If every synonymous codon introduces a site, fall back
    to the highest-frequency codon and let the final-pass scan record
    the unavoidable conflict.
    """
    sites = [s.upper() for s in (avoid_sites or [])]
    max_site_len = max((len(s) for s in sites), default=0)
    pieces: list[str] = []
    cursor = 0  # index in the growing DNA sequence
    aas_with_stop = list(protein) + ["*"]

    for aa in aas_with_stop:
        if aa not in table:
            raise ValueError(
                f"Unrecognised amino-acid {aa!r} at position {len(pieces)} — "
                f"valid one-letter codes for this organism: {sorted(k for k in table if k != '*')}"
            )
        ranked = sorted(
            table[aa].items(),
            key=lambda kv: (-kv[1], kv[0]),  # freq desc, codon asc for tie-break
        )
        chosen = ranked[0][0]  # default fallback
        if sites:
            # We only need the tail to detect a site spanning the join.
            tail_start = max(0, cursor - (max_site_len - 1))
            tail = "".join(pieces)[tail_start:]
            for codon, _freq in ranked:
                window = tail + codon
                if not any(site in window for site in sites):
                    chosen = codon
                    break
        pieces.append(chosen)
        cursor += 3

    dna = "".join(pieces)
    conflicts = _scan_conflicts(dna, sites)
    return dna, conflicts


RARE_CODON_THRESHOLD = 0.1
"""Per-amino-acid relative-frequency cutoff below which a codon is 'rare'.
0.1 is the conventional threshold used by most online codon optimisers.
Stops are excluded from the count regardless of their frequency."""


def _build_codon_to_aa(table: dict[str, dict[str, float]]) -> dict[str, str]:
    return {codon: aa for aa, codons in table.items() for codon in codons}


def _compute_cai(dna: str, table: dict[str, dict[str, float]]) -> float:
    """Sharp & Li 1987 CAI: geometric mean of relative adaptiveness
    ``w_i = f_i / f_max(aa_i)`` over all *non-stop* codons in ``dna``.

    Met / Trp (single-codon AAs) contribute ``log(1) = 0`` to the
    geometric mean — i.e. they neither raise nor lower the CAI but DO
    count toward the denominator. Codons with ``f_i = 0`` are excluded
    from the calculation (Sharp's original convention).
    """
    codon_to_aa = _build_codon_to_aa(table)
    max_freqs = {aa: max(codons.values()) for aa, codons in table.items()}
    log_w_sum = 0.0
    n = 0
    for i in range(0, len(dna) - 2, 3):
        codon = dna[i : i + 3]
        aa = codon_to_aa.get(codon)
        if aa is None or aa == "*":
            continue
        f_i = table[aa][codon]
        f_max = max_freqs[aa]
        if f_i == 0 or f_max == 0:
            continue
        log_w_sum += math.log(f_i / f_max)
        n += 1
    if n == 0:
        return 0.0
    return math.exp(log_w_sum / n)


def _compute_gc_pct(dna: str) -> float:
    """G+C base count as a percentage of total length. Empty input → 0.0."""
    if not dna:
        return 0.0
    gc = sum(1 for ch in dna if ch in "GCgc")
    return 100.0 * gc / len(dna)


def _compute_rare_codon_count(
    dna: str,
    table: dict[str, dict[str, float]],
    *,
    threshold: float = RARE_CODON_THRESHOLD,
) -> int:
    """Count codons whose per-amino-acid relative frequency is below
    ``threshold``. Stops excluded; partial trailing codon excluded."""
    codon_to_aa = _build_codon_to_aa(table)
    rare = 0
    for i in range(0, len(dna) - 2, 3):
        codon = dna[i : i + 3]
        aa = codon_to_aa.get(codon)
        if aa is None or aa == "*":
            continue
        if table[aa][codon] < threshold:
            rare += 1
    return rare


class CodonOptimiseInput(BaseModel):
    """Spec §4.19 input schema. Standard 20 one-letter amino-acid codes
    only — ambiguity codes (B, J, O, U, X, Z) are rejected because the
    codon usage tables only carry the canonical 20."""

    protein_sequence: str = Field(
        ...,
        min_length=5,
        max_length=10000,
        pattern=r"^[ACDEFGHIKLMNPQRSTVWY]+$",
        description="Target protein sequence (one-letter codes, uppercase, no stops)",
    )
    target_organism: Literal[
        "ecoli_k12", "h_sapiens", "s_cerevisiae", "p_pastoris", "cho", "sf9"
    ]
    avoid_restriction_sites: list[str] = Field(default_factory=list)


async def bio_codon_optimise(
    protein_sequence: str,
    target_organism: str,
    avoid_restriction_sites: list[str] | None = None,
) -> dict[str, Any]:
    """Codon-optimise a protein for one of six expression hosts.

    Returns the spec §4.19 output: optimised DNA (with stop codon
    appended), CAI, GC%, rare-codon count, and any restriction-site
    conflicts that survived the synonymous-swap pass.
    """
    try:
        params = CodonOptimiseInput.model_validate(
            {
                "protein_sequence": protein_sequence.upper(),
                "target_organism": target_organism,
                "avoid_restriction_sites": [s.upper() for s in (avoid_restriction_sites or [])],
            }
        )
    except ValidationError as exc:
        return error_response(
            f"Invalid input to bio_codon_optimise: {exc.errors()[0]['msg']}",
            suggestions=[
                "Use only the standard 20 one-letter amino-acid codes "
                "(ACDEFGHIKLMNPQRSTVWY); ambiguity codes like B/J/O/U/X/Z "
                "are not supported because the codon usage tables don't carry them.",
                f"Allowed organisms: {sorted(SUPPORTED_ORGANISMS)}.",
            ],
        )

    for site in params.avoid_restriction_sites:
        if not site or any(ch not in "ACGT" for ch in site):
            return error_response(
                f"avoid_restriction_sites entry {site!r} is not a DNA sequence.",
                suggestions=[
                    "Each entry must be a non-empty DNA string over {A,C,G,T} — "
                    "e.g. 'GAATTC' for EcoRI, 'AAGCTT' for HindIII.",
                ],
            )

    table = _load_codon_table(params.target_organism)
    dna, conflicts = _optimise_frequency_max(
        params.protein_sequence,
        table,
        avoid_sites=params.avoid_restriction_sites,
    )
    return {
        "optimised_sequence": dna,
        "target_organism": params.target_organism,
        "protein_length": len(params.protein_sequence),
        "length_nt": len(dna),
        "codon_adaptation_index": round(_compute_cai(dna, table), 4),
        "gc_content_pct": round(_compute_gc_pct(dna), 2),
        "rare_codon_count": _compute_rare_codon_count(dna, table),
        "rare_codon_threshold": RARE_CODON_THRESHOLD,
        "restriction_conflicts": conflicts,
        "avoided_sites_count": len(params.avoid_restriction_sites),
        "stop_codon_appended": True,
    }


def _scan_conflicts(dna: str, sites: list[str]) -> list[dict[str, int | str]]:
    """Final-pass scan: every occurrence of any forbidden site, sorted by
    position. Multiple distinct sites at the same position are reported
    independently; multiple occurrences of the same site each get their
    own entry."""
    found: list[dict[str, int | str]] = []
    for site in sites:
        start = 0
        while True:
            idx = dna.find(site, start)
            if idx < 0:
                break
            found.append({"site": site, "position": idx})
            start = idx + 1
    found.sort(key=lambda entry: (entry["position"], entry["site"]))
    return found
