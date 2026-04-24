"""`bio_fetch_uniprot` — curated UniProt protein record fetch by accession.

Phase 1, MVP. See spec §4.2.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch UniProt Record".

# TODO: implement per spec §4.2 (GET https://rest.uniprot.org/uniprotkb/{acc}.json,
# return sequence + features + cross-refs).
"""
