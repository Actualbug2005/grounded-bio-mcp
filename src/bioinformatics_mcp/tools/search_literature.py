"""`bio_search_literature` — Europe PMC literature search.

Phase 2. See spec §4.14.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Search Literature (Europe PMC)".

idempotentHint=False — the Europe PMC corpus grows continuously, so the
same query yields different ranked results over time.

# TODO: implement per spec §4.14 (Europe PMC search REST with query, limit,
# year_from/year_to, open_access_only filters).
"""
