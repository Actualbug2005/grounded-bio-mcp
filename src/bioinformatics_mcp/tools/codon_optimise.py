"""`bio_codon_optimise` — codon optimisation for recombinant expression.

Phase 3. See spec §4.19.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=False,
idempotentHint=True, title="Codon-Optimise Sequence".

openWorldHint=False — computation is purely local (python-codon-tables +
custom CAI optimisation). No network dependency.

# TODO: implement per spec §4.19 (python-codon-tables codon usage;
# CAI-maximisation optimisation with restriction-site avoidance; report CAI,
# GC content, rare codon count, and any unavoidable restriction conflicts).
"""
