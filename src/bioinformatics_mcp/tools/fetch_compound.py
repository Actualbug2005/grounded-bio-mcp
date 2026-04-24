"""`bio_fetch_compound` — small-molecule / compound data from ChEMBL and/or PubChem.

Phase 1, MVP. See spec §4.9.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch Compound Data".

# TODO: implement per spec §4.9 (PubChem PUG REST + ChEMBL molecule API;
# support identifier types name/smiles/inchi/chembl_id/pubchem_cid; merge
# when source='both').
"""
