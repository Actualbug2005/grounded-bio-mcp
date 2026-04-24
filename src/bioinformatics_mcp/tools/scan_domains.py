"""`bio_scan_domains` — domain architecture prediction via EBI InterProScan.

Phase 2. See spec §4.13.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Scan Protein Domains (InterProScan)".

# TODO: implement per spec §4.13 (EBI InterProScan async REST — same
# submit/poll/fetch pattern as Clustal Omega; requires EBI_EMAIL; can take
# 2–5 min for long sequences).
"""
