"""`bio_design_grna` — CRISPR gRNA design with off-target analysis (CRISPOR wrapper).

Phase 1, MVP (heaviest tool, deferred to end of phase 1). See spec §4.7.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Design CRISPR gRNA (CRISPOR)".

The single most important anti-hallucination tool in the server — real
off-target tables instead of fabricated ones.

# TODO: implement per spec §4.7 (wrap /opt/crispor/crispor.py via CRISPOR_PYTHON
# subprocess; genome indexes under GENOME_DIR; expose Doench 2016 + MIT + CFD
# scores; classify off-targets as CDS/intron/intergenic).
"""
