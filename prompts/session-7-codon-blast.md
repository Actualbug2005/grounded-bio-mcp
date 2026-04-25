# Session 7 — Codon optimiser and BLAST

> Repo path: `prompts/session-7-codon-blast.md`
> Commit this file as the first action of session 7 (commit message: `docs: session 7 prompt — codon optimiser and BLAST tools`).

Previous session completed the literature, pathway, and interactions tools. Fifteen of eighteen tools now implemented with 176 offline tests passing and 15/15 smoke tests green against live APIs. This session implements the final two non-CRISPOR tools, completing the implementation of every spec tool that does not require local binary installation.

## Scope

Implement two tools end-to-end.

1. `bio_codon_optimise` — codon optimisation for recombinant expression (spec §4.19)
2. `bio_blast_search` — sequence similarity search via NCBI BLAST (spec §4.6)

The codon optimiser uses a new local computation pattern with no external API. BLAST follows the async-job pattern from session 3 but against NCBI's BLAST URL API rather than EBI's job dispatcher.

Register both tools in `server.py` with selection-guide updates. Extend the smoke test to cover both, bringing the total to 17/17 passing tools. After this session completes, only CRISPOR remains as an unimplemented tool, and CRISPOR is deferred to session 8 along with deployment work because of its operational complexity (local binary, genome indexes, multiple gigabyte disk footprint).

## Out of scope

CRISPOR (spec §4.7), deployment scripts, evaluation harness, and the session-2 `response_format="markdown"` drift are all deferred to later sessions.

## Why these two together

The two tools are quite different in shape, but pairing them works for two reasons. First, the codon optimiser is the simplest tool in the entire spec — pure local computation with no external API, no async handling, no rate limiting concerns — so it provides session momentum and gives Claude Code a quick early win. Second, BLAST has enough natural complexity that completing it alone would not be a satisfying session. The combination produces a complete session that closes out fourteen of the spec's eighteen tools and leaves only CRISPOR plus deployment work for the final session.

The two tools are also somewhat thematically related from the user's perspective: codon optimisation is what you do when designing a recombinant protein for expression, and BLAST is what you do when characterising a sequence you have or designed. Both are common steps in molecular biology workflow planning.

## Pre-work discovery requirements

Read spec sections 4.6 and 4.19 in full. Note the input schemas, output shapes, and any cross-references between these tools and existing ones. The BLAST output should be designed so that its top hits can be naturally chained to `bio_fetch_sequence` for retrieving the full sequence of a hit, and `bio_fetch_uniprot` for protein hits — verify the identifier formats are compatible.

Re-read the relevant memory entries. The session-3 EBI async-job pattern (`project_ebi_job_runner_pattern.md`) is conceptually similar to BLAST but the specific endpoint behaviours differ, so the pattern guides the design but does not transplant directly. The session-2 NCBI httpx pattern applies to BLAST since BLAST is also an NCBI service. The truncation helper applies to BLAST results when many hits are returned. The dict-return convention applies to both tools.

Investigate each API's current state.

For `python-codon-tables`, verify the library is at the version pinned in `pyproject.toml` and check what codon usage tables are currently available. The spec section 4.19 lists `ecoli_k12`, `h_sapiens`, `s_cerevisiae`, `p_pastoris`, `cho`, and `sf9` as supported organisms. Verify the library names for each match what the spec expects, and document any discrepancies. Check whether the library has been updated to use any newer codon usage data sources (Kazusa, GenScript, etc.) since the spec was written.

For NCBI BLAST, verify the URL API at `https://blast.ncbi.nlm.nih.gov/Blast.cgi`. The API uses URL-encoded form parameters with `CMD=Put` for submission and `CMD=Get` for status/result polling, which is unlike either EBI's dispatcher or any other API we have integrated. Verify the current parameter shape, particularly the `PROGRAM` (blastn/blastp/blastx/tblastn), `DATABASE` (nt/nr/refseq_protein/refseq_rna/swissprot), `MEGABLAST` flag for blastn, `EXPECT` (E-value threshold), `HITLIST_SIZE`, and `ENTREZ_QUERY` (organism filter) parameters. Check the response format for `CMD=Put` (returns RID — Request ID — and RTOE — estimated time of execution) and for `CMD=Get` (returns either WAITING status or the actual results in your chosen format).

Probe BLAST's rate limiting behaviour specifically. NCBI's E-utilities limits are documented (10 req/s with API key, 3 without), but BLAST queries are much heavier and NCBI may throttle them more aggressively in practice. Submit a small test query during pre-work and observe the actual behaviour — does the same NCBI_API_KEY rate limit apply, or are BLAST submissions counted differently? Are there any rate-limit headers returned? The spec section 7.1 has BLAST sharing the NCBI rate limit which may not be quite right.

Investigate BLAST's slow-job behaviour. BLAST jobs can legitimately take 30 seconds to 5 minutes for normal queries, and longer during peak hours. The async pattern from session 3 used a polling-with-jitter approach that should adapt to BLAST cleanly, but the timeout values and polling intervals will need tuning. Suggest 600-second default timeout (longer than the EBI tools' 300-second default for Clustal because BLAST is slower) with caller-configurable override up to a reasonable maximum (perhaps 1800 seconds = 30 minutes).

Check what BLAST output formats are available. BLAST supports XML, JSON, tabular, and pairwise text formats. For machine-readable output, JSON (FORMAT_TYPE=JSON2_S) is preferred over XML where available, but verify that the JSON format is stable and well-documented. Some older BLAST API documentation only mentioned XML, so JSON support may be newer and might have edge cases.

Consider the BLAST output size. A typical query returns 50 to 100 hits with detailed alignment information — query coverage, identities, gaps, alignment positions, and the actual aligned sequences. Even at the spec default of 20 hits, output can run to several hundred kilobytes. The truncation helper from earlier sessions applies here, with the natural strategy being to truncate the alignment sequences themselves while preserving the hit metadata (accession, description, E-value, bit score, identity percentage).

Report back any ambiguities or design questions before writing code.

## Specific design decisions requiring pre-approval

Three decisions to surface before implementation begins.

The first concerns the codon optimiser's algorithm. The simplest implementation picks the most-frequent codon for each amino acid in the target organism, which maximises CAI but produces a sequence with very uneven nucleotide composition that often expresses poorly in practice. A more sophisticated implementation samples codons probabilistically according to their frequency, which produces sequences with realistic CAI distributions and better expression in practice. The difference matters: the simple approach gives reproducible output (same input always produces same output) while the sophisticated approach gives variable output unless seeded. My leaning is to support both via an `algorithm: Literal["frequency_max", "frequency_weighted"]` parameter, with `frequency_max` as default for reproducibility. The weighted approach can be added with an explicit random seed parameter for reproducibility when desired.

The second concerns codon optimiser output verification. Beyond just returning the optimised sequence, the tool should compute and return the CAI (codon adaptation index) of the result, the GC content, and any unintended occurrences of restriction sites that the caller asked to avoid. The first two are standard. The third is interesting — if the caller asked to avoid `GAATTC` (EcoRI) but the optimised sequence happens to contain it anyway, the tool should report this as a constraint violation rather than silently shipping a problematic sequence. Question: should the tool retry with alternative codons when constraint violations are detected, or just report the violations and let the caller re-query with different parameters? The first is more user-friendly but more complex. The second is simpler and more transparent. My leaning is the simpler approach, with clear documentation that callers should re-query if violations are reported.

The third concerns BLAST result format. The tool's output schema should expose the per-hit information that callers actually need — accession, organism, description, E-value, bit score, identity percentage, query coverage, and alignment positions — without exposing every implementation detail of the BLAST XML/JSON response. The question is how to handle the alignment sequences themselves. Including them lets callers see exactly what aligned where, but they significantly inflate output size. Excluding them means callers cannot verify alignment quality without re-running BLAST. My leaning is to include alignment sequences for the top N hits (perhaps top 5) and exclude for the rest, with a parameter to expand if needed. This balances detail-when-useful against output size. Confirm whether this is the right trade-off or whether always-include or always-exclude is preferred.

## Implementation ordering

Build the codon optimiser first because it is faster to complete and provides session momentum.

Implement `bio_codon_optimise` per spec section 4.19 in `tools/codon_optimise.py`. No new client module is needed because the tool uses `python-codon-tables` directly without external API calls. Unit tests should cover the core algorithm with synthetic protein sequences and known codon tables, plus restriction site avoidance logic. Integration tests are not strictly necessary for a local-only tool but a small smoke test that optimises a known sequence (perhaps insulin's signal peptide) for E. coli and verifies the resulting CAI is in a reasonable range provides confidence that the library is working correctly.

After the codon optimiser is green and committed, implement BLAST. Build the NCBI BLAST client extension in `clients/ncbi.py` (extending the existing client rather than creating a new one, since BLAST is an NCBI service) with submit, poll-with-jitter-and-backoff, and result-fetch methods following the EBI pattern from session 3. Then implement `bio_blast_search` per spec section 4.6. Integration test with a small protein sequence query against swissprot (faster than nr) and verify that expected homologues are found.

The BLAST tool should respect the timeout convention from session 3 — caller can specify `max_wait_seconds` with a default of 600 seconds and a maximum cap of 1800 seconds. Empty result sets are valid (truly novel sequences with no homologues exist) and should be reported honestly rather than treated as errors.

After both tools are green, register them in `server.py` with appropriate annotations. The codon optimiser is `readOnly=true, destructive=false, openWorld=false, idempotent=true` for the frequency_max algorithm and `idempotent=true` for frequency_weighted only when a seed is provided (otherwise `idempotent=false` because results vary). The `openWorld=false` annotation distinguishes it from all other tools because it does not query external state — interesting edge case worth documenting in memory. BLAST is `readOnly=true, destructive=false, openWorld=true, idempotent=false` (database updates change results over time).

Update the selection guide in the `instructions` parameter to include both new tools. The codon optimiser handles the "design a sequence for expression" question. BLAST handles the "find similar sequences" question, complementing the existing `bio_align_sequences` tool which handles "align these specific sequences I already have" — the distinction between search and alignment is important and the selection guide should make it clear.

Extend the smoke test to cover both new tools, bringing the total to 17/17. Include one codon optimisation (insulin signal peptide for E. coli) and one BLAST search (a known protein sequence against swissprot).

## Specific things to watch for

The codon optimiser is the only tool in the project that does not query external data. This means it has no anti-hallucination concerns of the type the rest of the project addresses — there is no upstream database that could be wrong, no API to fail, no rate limit to hit. The tool's correctness depends entirely on the codon usage tables from `python-codon-tables` being accurate, and on the optimisation algorithm being implemented correctly. Document this clearly in memory because it is a genuinely different category of tool from everything else we have built.

NCBI BLAST has historically been less reliable than EBI's services, with occasional unexpected failures, queue stalls, and result-format inconsistencies. The integration tests should be more tolerant of transient failures than the EBI integration tests — perhaps with explicit retry-on-transient-error logic, or with documentation that flaky integration test results may indicate NCBI issues rather than our code issues. The tier-3-fallback discipline from session 3 applies — if BLAST behaves unexpectedly, return honest errors rather than fabricated partial results.

BLAST's organism filter via `ENTREZ_QUERY` accepts the same NCBI Entrez query syntax as `bio_blast_search`. Test that it works for common organism filters (e.g., `Felis catus[ORGN]`, `Mammalia[ORGN]`) and document any edge cases.

The codon optimiser's restriction site avoidance is implemented at the synonymous codon level — for each codon in the optimised sequence, verify that switching to another synonymous codon would not introduce a forbidden site, and verify that the chosen codon does not introduce one across codon boundaries. This is conceptually simple but requires careful index handling. Plan to write thorough unit tests covering boundary cases (site spans across codon boundary, site partially overlaps with the start or end of the sequence, multiple sites in close proximity).

## Deliverables checklist for session-end report

The session is complete when the following are all true.

The session-7 prompt is committed as the first action. The codon optimiser is implemented per spec section 4.19 with unit tests green. BLAST is implemented per spec section 4.6 with unit and integration tests green. The server registers both tools with appropriate annotations and the selection guide is updated. The smoke test exercises 17/17 tools live. At least three conventional-commit feature commits have been made (one per tool plus server registration plus smoke test extension; the codon optimiser may decompose into one or two commits depending on complexity), each with RED-phase evidence in the body. All tests pass offline with `pytest` and gated tests pass with `RUN_INTEGRATION=1 pytest`. Memory entries document the codon optimiser pattern (including the no-external-API distinction), the BLAST async pattern (including how it differs from EBI's), and any new findings about NCBI BLAST behaviour discovered during implementation.

## Procedural reminders

British English in docstrings, README, comments. Test-first with RED-phase assertion errors captured in commit bodies. Stop and ask before deviating from spec sections 4.6 or 4.19 in ways that change input or output schema. Use `serena` for symbol-level navigation across the now-substantial codebase. Apply the `superpowers:verification-before-completion` gate before each commit. Use `commit-commands:commit` for commit message formatting.

Start with pre-work discovery. Report findings and decisions, then wait for approval before implementing.
