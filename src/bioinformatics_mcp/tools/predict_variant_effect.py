"""`bio_predict_variant_effect` — Ensembl VEP consequence prediction.

Phase 2. See spec §4.12.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Predict Variant Effect (VEP)".

idempotentHint=False — Ensembl VEP output depends on the current release's
transcript annotations and scoring matrices.

# TODO: implement per spec §4.12 (Ensembl REST /vep/{species}/hgvs/{hgvs} or
# /vep/{species}/region/).
"""
