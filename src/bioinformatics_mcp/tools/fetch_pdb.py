"""`bio_fetch_pdb` — experimentally-determined protein structure fetch from RCSB.

Phase 1, MVP. See spec §4.3.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch PDB Structure".

# TODO: implement per spec §4.3 (metadata via https://data.rcsb.org/rest/v1/core/entry/,
# coordinates via https://files.rcsb.org/download/{pdb_id}.cif; honour include_coordinates
# and chain_filter params).
"""
