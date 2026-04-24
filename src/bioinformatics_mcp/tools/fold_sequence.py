"""`bio_fold_sequence` — RNA / DNA secondary structure prediction (ViennaRNA).

Phase 1, MVP. See spec §4.8.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=False,
idempotentHint=True, title="Fold RNA/DNA Sequence".

openWorldHint=False because the computation is purely local (ViennaRNA
bindings) — no upstream API touched.

# TODO: implement per spec §4.8 (ViennaRNA Python bindings; for DNA use
# RNA.params_load_DNA_Mathews2004(); return MFE dot-bracket + ΔG + base-pairing
# probability summary).
"""
