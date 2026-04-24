"""`bio_blast_search` — sequence similarity search via NCBI BLAST.

Phase 1, MVP (last). See spec §4.6.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="BLAST Sequence Search".

idempotentHint=False because NCBI databases grow between runs, so the same
query may return different hits over time.

# TODO: implement per spec §4.6 (NCBI BLAST URL API Put→Get, poll every 10 s,
# 600 s timeout, return partial results with warning on timeout).
"""
