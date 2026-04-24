# Session 5 — Variants and gene tools (Ensembl + NCBI)

> Repo path: `prompts/session-5-variants-gene.md`
> Commit this file as the first action of session 5 (commit message: `docs: session 5 prompt — variants and gene tools`).

Previous session completed phase 1: eight tools implemented, registered, and integration-tested against live APIs. 110 tests passing under `RUN_INTEGRATION=1`, zero regressions. Memory audit confirmed twenty-five entries are current and truthful against the working tree.

This session begins phase 2 by implementing the genetic-variation and genomic-context tools. These three tools cover the anti-hallucination surface where Claude most commonly fabricates plausible-sounding rsIDs, allele frequencies, variant consequences, and genomic coordinates.

## Scope

Implement three tools end-to-end:

1. `bio_fetch_variant` — variant lookup by rsID or coordinates via Ensembl REST (spec §4.11)
2. `bio_predict_variant_effect` — VEP consequence prediction via Ensembl REST (spec §4.12)
3. `bio_fetch_gene` — gene record with genomic context via NCBI Gene (spec §4.16)

Plus the supporting Ensembl client in `clients/ensembl.py` (new), and registration of all three tools in `server.py` with selection-guide updates.

## Out of scope

Everything not listed above. Specifically: literature tools, pathway tools, interactions tool, codon optimiser, BLAST, CRISPOR, deployment scripts, evaluation harness. All remain unbuilt.

The session-2 `response_format="markdown"` drift is still on the list for a future spec revision, not this session.

## Why these three together

These three tools share a natural workflow shape. A caller asking "what's the effect of this variant in BRCA1" would plausibly chain them: `bio_fetch_gene` to locate BRCA1 on the genome and find its exons, `bio_fetch_variant` to look up a specific rsID and confirm it lies within those exons, `bio_predict_variant_effect` to predict the functional consequence. By implementing them in the same session we can design the data shapes to compose cleanly across this workflow, rather than retrofitting compatibility after the fact.

The variants pair specifically share the Ensembl REST API and benefit from a shared client. The gene tool uses the existing NCBI client from session 2, which means there's no new client work for that tool — it follows the established `bio_fetch_sequence` pattern but queries the Gene database instead of the nucleotide or protein databases.

All three are read-only, idempotent (gene records and variant annotations are stable in published Ensembl/NCBI releases), open-world, and non-destructive. Standard annotation set applies.

## Pre-work discovery requirements

Before any code, read and report back on the following.

Read spec sections 4.11, 4.12, and 4.16 in full. Note the input schemas, output shapes, and the cross-references between variants (which lift to gene context) and genes (which return exon coordinates relevant to variant location). Pay particular attention to spec §4.11's note about `assembly` defaulting to "latest per species" and how that interacts with Ensembl's actual behaviour — Ensembl uses GRCh38 for human by default but can serve GRCh37 via a separate REST endpoint, and the tool needs to handle this without surprising the caller.

Re-read the relevant memory entries from sessions 2 and 4. The NCBI client pattern from session 2 (`project_ncbi_httpx_pattern.md`) applies directly to `bio_fetch_gene` since we're querying a different NCBI database via the same E-utilities mechanism. The dict-return convention applies. The session-4 finding about server-side filters being potentially leaky (`project_chembl_leaky_filter.md`) is a general pattern worth keeping in mind: when Ensembl claims to filter or restrict results, verify the claim rather than trusting it.

Investigate the current state of Ensembl REST API. The base URL is `https://rest.ensembl.org/` for the main GRCh38 server, and `https://grch37.rest.ensembl.org/` for the legacy GRCh37 server. Verify the current endpoint paths for variation lookup (likely `/variation/{species}/{id}`), VEP (likely `/vep/{species}/hgvs/{hgvs}` or `/vep/{species}/region/{region}/{allele}`), and any rate limit guidance. Ensembl publishes rate limits in response headers (`X-RateLimit-*`); the existing `RateLimitedClient` infrastructure should handle these naturally but the values from spec §7.1 (15 concurrent, 70ms minimum interval) need cross-checking against Ensembl's currently published guidance.

For NCBI Gene specifically, verify the current behaviour of `Entrez.esearch` and `Entrez.esummary` against the gene database. The session-2 NCBI client used `efetch` for sequence retrieval; the gene tool needs `esearch` to resolve a gene symbol to a Gene ID, then either `esummary` for a structured summary or `efetch` with `retmode="xml"` for the full record. Confirm which gives the cleanest data shape for our needs (gene location, exon structure, RefSeq transcript list, GO annotations, cross-references).

Investigate one specific design question: how does Ensembl return null or missing data for variants that exist but have no annotations? An rsID like `rs1` (which exists but is essentially unannotated) versus `rs429358` (which is the APOE ε4 variant, heavily annotated) versus a clearly-fake rsID like `rs999999999999`. The tool's behaviour should be honest in all three cases — found-but-empty for the first, found-and-rich for the second, not-found for the third. Verify Ensembl's actual response shapes for each case so we can handle them correctly rather than collapsing distinct outcomes into the same response.

Check whether VEP's HGVS endpoint accepts only HGVS.c or also HGVS.p, HGVS.g notations. Spec §4.12 mentions HGVS without qualifier; the tool's input should be explicit about which notations are supported and reject others with a clear error message rather than silently failing or producing wrong results.

Report back any ambiguities or design questions before writing code.

## Specific design decisions requiring pre-approval

Three decisions that probably need your input before implementation begins.

The first concerns assembly handling. Ensembl serves GRCh38 by default but maintains a separate REST endpoint for GRCh37, which is still in clinical use because many existing variant databases reference GRCh37 coordinates. The tools could either silently route to the appropriate endpoint based on the `assembly` parameter, or require the caller to be explicit and refuse to mix. Silent routing is more convenient; explicit refusal is more defensive. The trade-off is that silent routing means a caller who specifies `assembly="GRCh37"` gets results from a different server than the default, and if they later issue an unrelated query without specifying assembly, they'll get GRCh38 results — easy to confuse. My initial leaning is silent routing with the actual assembly used surfaced in every response so the caller always knows what they got.

The second concerns gene-symbol-to-ID resolution. NCBI's gene database can return multiple hits for ambiguous gene symbols, particularly across species or for symbol synonyms. The pattern from session 4's compound tool was to surface candidates with disambiguation hints. The same pattern should apply here: if a gene symbol resolves to multiple Gene IDs, surface a `candidate_gene_ids` array with disambiguation context. Question is what context to include — the official symbol, full name, organism, and chromosome are probably the right minimum.

The third concerns VEP input flexibility. The spec mentions HGVS notation and `chrom:pos:ref:alt` format. These are two distinct input formats requiring different VEP endpoints (`/vep/{species}/hgvs/{hgvs}` versus `/vep/{species}/region/{region}/{allele}`). The tool could accept either format and route to the right endpoint internally, or require the caller to specify which format they're using. Internal routing is more convenient but introduces parser ambiguity — some HGVS notations could plausibly be parsed as region notation. Explicit format parameter is uglier but unambiguous.

## Implementation ordering

Build in TDD order, one tool at a time.

Implement the Ensembl client first in `clients/ensembl.py`. Both variant tools depend on it, so getting the client solid before implementing tools means the tools can be written against a stable foundation. The client should expose methods for variant lookup (single rsID or coordinate), VEP query (both HGVS and region modes), and any helper methods needed for assembly routing. Unit tests with `respx` mocking, integration tests with `RUN_INTEGRATION=1` gate.

After the Ensembl client has unit tests passing, implement `bio_fetch_variant` per spec §4.11. Integration test with `rs429358` (APOE ε4) since it's heavily annotated and stable. The output should clearly distinguish between found-rich, found-empty, and not-found outcomes. Surface the actual assembly used in every response.

Then implement `bio_predict_variant_effect` per spec §4.12. Integration test with the same APOE variant in HGVS notation (`ENSP00000252486.4:p.Cys130Arg` or equivalent — verify the current canonical form during pre-work). The output should include consequence terms, SIFT and PolyPhen scores where available, and affected transcript information per the spec.

Then implement `bio_fetch_gene` per spec §4.16. This tool extends the existing NCBI client rather than introducing new infrastructure. Integration test with BRCA1 since it's well-annotated and has a stable Gene ID (672). The output should include the gene location, exon structure, RefSeq transcripts, GO annotations, and cross-references to UniProt and Ensembl per the spec.

After all three tools are green, register them in `server.py` with standard annotations and update the selection guide in the `instructions` parameter. The selection guide updates should make clear which tool to use for which question type — gene-level questions go to `bio_fetch_gene`, variant-existence-and-properties questions go to `bio_fetch_variant`, variant-consequence questions go to `bio_predict_variant_effect`. Without clear selection guidance, the model could pick the wrong tool for the question.

Extend the smoke test to cover all three new tools, bringing the total to 11/11 passing. Include one variant lookup, one VEP query, and one gene fetch with appropriate test inputs.

## Specific things to watch for

Ensembl uses a unique identifier style for transcripts and proteins (`ENST00000...`, `ENSP00000...`) that includes a version suffix (`.4`, `.7`, etc.). The version suffix matters for some queries and not for others. Document the convention in the tool's docstring and handle the suffix correctly — generally accept both versioned and unversioned identifiers and pass through whatever the caller provided.

NCBI Gene records can be very large for well-studied genes — BRCA1's full record runs to tens of kilobytes with hundreds of GO annotations and cross-references. The truncation helper from session 3 (`soft_cap_with_url_fallback`) should be applied if any field grows too large; the GO annotations field is the most likely candidate. Reuse the existing helper rather than introducing a new pattern.

VEP's response shape varies depending on whether the variant lies within a gene, in regulatory regions, in intergenic space, or affects multiple overlapping transcripts. The output schema needs to handle all four cases without forcing the caller to special-case each. Returning a list of transcript-level consequences, even when there's only one, is cleaner than returning either-a-list-or-a-single-record.

Ensembl's rate limit headers may not match the spec §7.1 values exactly. If the actual published limits are different (more or less restrictive), update the `RATE_LIMITS` entry to match reality and document the change in memory. Trusting outdated rate limits either wastes capacity (if the limit went up) or causes 429 errors (if the limit went down).

The cross-reference surface across these three tools is rich. A gene record cross-references UniProt and Ensembl. A variant record cross-references the gene it lies within. A VEP result cross-references the affected transcripts and proteins. The output schemas should make these cross-references easy to follow — including IDs that the caller can pass to other tools without transformation. Consistency of identifier formatting across tools matters more than it might initially appear.

## Deliverables checklist for session-end report

The session is complete when the following are all true.

The session-5 prompt is committed as the first action. The Ensembl client is implemented with unit tests green. All three tools are implemented per spec sections 4.11, 4.12, and 4.16 with unit and integration tests green. The server registers all three tools with appropriate annotations and the selection guide is updated. The smoke test exercises 11/11 tools live. At least four conventional-commit feature commits have been made (Ensembl client, then one per tool), each with RED-phase evidence in the body. All tests pass offline with `pytest` and gated tests pass with `RUN_INTEGRATION=1 pytest`. Memory entries document the Ensembl client pattern, the assembly-routing decision, the gene-symbol disambiguation handling, and any new findings about API behaviour discovered during implementation.

## Procedural reminders

British English in docstrings, README, comments. Test-first with RED-phase assertion errors captured in commit bodies. Stop and ask before deviating from spec sections 4.11, 4.12, or 4.16 in ways that change input or output schema. Use `serena` for symbol-level navigation across the now-substantial codebase. Apply the `superpowers:verification-before-completion` gate before each commit. Use `commit-commands:commit` for commit message formatting.

Start with pre-work discovery. Report findings and decisions, then wait for approval before implementing.
