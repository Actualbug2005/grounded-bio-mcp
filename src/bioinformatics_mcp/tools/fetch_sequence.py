"""`bio_fetch_sequence` — NCBI nucleotide/protein sequence fetch by accession.

Phase 1, MVP. See spec §4.1.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch NCBI Sequence".

# TODO: implement per spec §4.1 (Bio.Entrez.efetch, shared rate-limited client,
# tenacity retries on 429/503, feature-table parsing via Biopython SeqIO).
"""
