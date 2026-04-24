"""`bio_fetch_alphafold` — AlphaFold2 predicted structure fetch from EBI AlphaFold DB.

Phase 1, MVP. See spec §4.4.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch AlphaFold Prediction".

Critical behaviour (spec §4.4): always surface mean pLDDT with returned
coordinates so the model does not over-trust low-confidence predictions
(pLDDT < 70 is unreliable).

# TODO: implement per spec §4.4 (summary via /api/prediction/{acc}, full
# structure via /files/AF-{acc}-F1-model_v4.{pdb,cif}).
"""
