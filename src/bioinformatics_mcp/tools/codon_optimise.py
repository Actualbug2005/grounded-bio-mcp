"""`bio_codon_optimise` — codon optimisation for recombinant expression.

Phase 3. See spec §4.19.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=False,
idempotentHint=True, title="Codon-Optimise Sequence".

openWorldHint=False — the only tool in the project that does not query
external state at runtime. Codon usage tables come from two static sources:

- Three organisms ship with ``python-codon-tables`` (``ecoli_k12``,
  ``h_sapiens``, ``s_cerevisiae``);
- Three are bundled under ``src/bioinformatics_mcp/data/codon_tables/``
  as Kazusa CSVs (``p_pastoris``, ``cho``, ``sf9``) — see
  ``scripts/fetch_codon_tables.py`` for the refresh procedure.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import python_codon_tables as pct
from python_codon_tables.python_codon_tables import table_with_U_replaced_by_T

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
