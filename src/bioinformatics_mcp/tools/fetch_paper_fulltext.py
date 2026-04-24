"""`bio_fetch_paper_fulltext` — full-text fetch for an open-access paper.

Phase 2. See spec §4.15.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch Paper Full Text".

idempotentHint=True — papers are immutable post-publication.

Critical (spec §4.15): closed-access papers must return a clear error
message directing the user to the publisher, not fake or truncated content.

# TODO: implement per spec §4.15 (Europe PMC /{PMCID}/fullTextXML; parse
# with lxml; return structured sections + figure/table captions).
"""
