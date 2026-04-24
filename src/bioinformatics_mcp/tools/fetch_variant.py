"""`bio_fetch_variant` — variant lookup by rsID or coordinates (Ensembl REST).

Phase 2. See spec §4.11.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Fetch Variant".

idempotentHint=False — Ensembl releases update allele frequencies and
ClinVar cross-references.

# TODO: implement per spec §4.11 (Ensembl REST /variation/{species}/{id} or
# /overlap/region/).
"""
