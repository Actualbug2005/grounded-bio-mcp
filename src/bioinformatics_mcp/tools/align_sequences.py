"""`bio_align_sequences` — multiple sequence alignment via EBI Clustal Omega.

Phase 1, MVP. See spec §4.5.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Align Sequences (Clustal Omega)".

# TODO: implement per spec §4.5 (async EBI REST — submit, poll every 2 s,
# fetch result; 300 s timeout; requires EBI_EMAIL).
"""
