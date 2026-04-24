"""`bio_fetch_interactions` — STRING protein-protein interaction network.

Phase 3. See spec §4.18.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch STRING Interactions".

# TODO: implement per spec §4.18 (STRING REST /api/json/network; include
# contact email in User-Agent via STRING_USER_EMAIL; filter by species_taxon,
# min_score, max_partners).
"""
