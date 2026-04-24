"""`bio_fetch_bioactivity` — measured drug-target binding/activity data from ChEMBL.

Phase 1, MVP. See spec §4.10.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Fetch ChEMBL Bioactivity".

idempotentHint=False because ChEMBL accepts new assay submissions — the same
query may gain additional rows over time.

Critical (spec §4.10): enforce min_confidence ≥ 7 by default; low-confidence
assays must not be cited as direct binding affinities.

# TODO: implement per spec §4.10 (ChEMBL /activity.json with molecule_chembl_id
# or target_chembl_id, filtered by activity_types and min_confidence).
"""
