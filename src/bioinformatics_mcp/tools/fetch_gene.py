"""`bio_fetch_gene` — NCBI Gene record with genomic context.

Phase 2. See spec §4.16.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch NCBI Gene".

# TODO: implement per spec §4.16 (Bio.Entrez.esearch on gene database →
# Entrez.esummary + Entrez.efetch; return coords, exon structure, RefSeq
# transcripts, GO annotations, cross-refs).
"""
