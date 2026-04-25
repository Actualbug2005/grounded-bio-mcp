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
