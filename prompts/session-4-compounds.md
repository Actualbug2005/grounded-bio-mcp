# Session 4 — Compound and bioactivity tools (ChEMBL + PubChem)

> Repo path: `prompts/session-4-compounds.md`
> Commit this file as the first action of session 4 (commit message: `docs: session 4 prompt — compound and bioactivity tools`).

Previous session delivered the EBI async-job infrastructure plus `bio_align_sequences` and `bio_scan_domains` (commits `59c2658` through `0a6b4b4`). Sixty-one unit tests green, six integration tests gated and healthy, zero regressions in session-2 tools. Memory audit confirms all nineteen memory entries are current, complete, and truthful against the working tree.

This session implements the two tools most directly relevant to the user's peptide and pharmacology research, plus opportunistically resolves the two deferred probes from session 3 while the integration test environment is being warmed up.

## Scope

Implement two tools end-to-end, plus housekeeping:

1. `bio_fetch_compound` — ChEMBL and PubChem compound lookups (spec §4.9)
2. `bio_fetch_bioactivity` — ChEMBL measured bioactivity data (spec §4.10)

Housekeeping at the start of the session, before the new tools:

- Set `EBI_EMAIL` in `.env` from the user's existing EBI account
- Run `RUN_INTEGRATION=1 pytest` on the session-3 EBI-dependent tests to verify they pass against real EBI infrastructure
- Execute the two deferred probes from `project_deferred_probes.md`: Clustal result-type verification and InterProScan partial-results streaming
- Update the relevant memory entries with probe results
- Only after the session-3 baseline is verified green, proceed to new tool work

## Out of scope

Everything not listed above. Specifically: CRISPOR, BLAST, variant tools, literature tools, pathway tools, gene tool, codon optimiser, deployment scripts, evaluation harness. All remain as stubs or unbuilt.

## Why these two tools

Both tools target what is probably the single highest anti-hallucination value surface in the project for the user's actual use patterns. When the user asks about binding affinities, IC50 values, clinical trial phases, or which targets a given compound hits, Claude currently pattern-matches from training data and occasionally fabricates plausible-looking-but-wrong numbers, drug names, or target attributions. ChEMBL has curated, peer-reviewed, source-cited bioactivity measurements that replace pattern-matching with retrieval. PubChem covers compound structural and chemical data with similar authority. Both are free, no authentication required, and stable.

The tools are also the same shape as session 2's single-request fetch tools, which means they exercise and reinforce the existing fetch-tool pattern rather than introducing new abstraction. This is deliberate. Sessions that consolidate existing patterns are valuable between sessions that introduce new ones, because they build confidence that the shared infrastructure genuinely scales.

## Pre-work discovery requirements

Before any code, read the following thoroughly and report back with findings:

Read spec sections 4.9 and 4.10 in full, noting the complete input and output schemas, the minimum confidence threshold convention for bioactivity, and the dual-database nature of the compound tool where ChEMBL and PubChem can both be queried and their results combined.

Re-read the session 2 and session 3 memory entries that established conventions. The fetch-tool pattern from session 2 applies directly here. The httpx-plus-Biopython hybrid pattern for NCBI does not apply because neither ChEMBL nor PubChem require specialised parsing beyond JSON deserialisation. The dict-return output convention applies. The truncation helper applies if either tool returns large payloads, though this is less likely for compound data than it was for alignment or structural data.

Investigate the current state of both APIs. For ChEMBL, verify the base URL, the resource paths for `molecule` and `activity` queries, the pagination convention if any, and the current schema of the bioactivity response, particularly which fields represent the confidence score, the activity type, the standard value, the standard units, and the source document reference. For PubChem, verify the PUG REST base URL, the compound property endpoint, the cross-reference endpoint that maps between identifier types, and the synonym endpoint structure.

Confirm that neither API requires authentication for our expected query volumes. ChEMBL publishes rate limit guidance; PubChem has a published "unreasonable use" threshold. Read both and confirm our rate limiter entries in `clients/base.py` are appropriate.

Check whether the two APIs have any known quirks around identifier resolution. PubChem in particular has subtle behaviour around name-based lookup where common drug names can resolve to multiple CIDs if stereoisomers or salts are involved. Report how you plan to handle ambiguous identifier resolution.

## Implementation ordering

Build in TDD order, one tool at a time, same discipline as previous sessions.

Before the compound tool, implement the ChEMBL and PubChem clients in `clients/chembl.py` and `clients/pubchem.py`. Both clients use the shared `RateLimitedClient` with their respective rate limit entries from spec section 7.1. Each client exposes a small set of methods covering the specific endpoints the two tools need. Do not over-engineer; implement only what the tools require, and extend later if more tools need these clients.

After the clients have unit tests passing, implement `bio_fetch_compound` following the spec section 4.9 schema. This tool accepts an identifier and identifier type, plus a source parameter that selects between ChEMBL, PubChem, or both. When both are selected, the tool queries both databases in parallel and merges the results, preferring ChEMBL for fields where both sources provide data (because ChEMBL is more rigorously curated for drug-like compounds, while PubChem has broader coverage of general chemistry). The merge logic should be explicit and documented rather than implicit; a reader of the code should be able to see exactly which fields come from which source. Integration test with aspirin, which resolves cleanly to CHEMBL25 and CID 2244 and has stable, well-documented properties.

After the compound tool is green and committed, implement `bio_fetch_bioactivity` following the spec section 4.10 schema. This tool is more nuanced than it appears. The spec mandates a minimum confidence threshold defaulting to 7, which filters out bioactivity measurements where the assay-to-target mapping is weak. This filter is not cosmetic; it is the single most important protection against returning misleading data. Low-confidence bioactivity entries often conflate multiple related targets, use non-physiological assay conditions, or represent measurements so old that the reported values are known to be unreliable. The default must be 7 and the field description must explain what the threshold means so callers understand what they are relaxing if they lower it.

The tool supports both compound-to-targets and target-to-compounds query directions. Both directions use the same underlying ChEMBL activity endpoint with different query parameters. Pagination is required because compounds like aspirin can have thousands of activity records across their history; the tool should support limit and offset parameters with sensible defaults (suggest limit 50, maximum 500) and clearly communicate when results are truncated. Integration test with aspirin (CHEMBL25) querying for its targets; expect to see COX-1 and COX-2 in the high-confidence results.

After both tools are green, register them in `server.py` with standard annotations (`readOnly=true, destructive=false, openWorld=true, idempotent=true`). Update the selection guide in the server's `instructions` parameter to include both new tools. The guide should distinguish clearly between "look up a compound's properties" (the compound tool) and "look up what a compound binds to, with what affinity" (the bioactivity tool), because those are the two most likely user intents and Claude needs to pick correctly.

Extend the smoke test to cover both new tools, bringing the total to 8/8 passing. Include one compound lookup from each database source and one bioactivity query in each direction.

## Specific design decisions requiring pre-approval

Three decisions that might need your input before implementation begins.

The first concerns parallel queries in the compound tool when source is `both`. The natural implementation uses `asyncio.gather` to query ChEMBL and PubChem concurrently, which is faster but doubles the rate of external requests during that single tool call. This is fine at our expected query volumes, but represents a deliberate design choice worth naming. The alternative is sequential querying which halves the concurrency impact at the cost of latency.

The second concerns how to handle the case where ChEMBL returns a compound but PubChem does not, or vice versa. Both APIs occasionally miss entries the other covers. The natural behaviour is to return whatever was found with a clear indication of which source returned data and which did not. The spec does not specify this behaviour explicitly, so the implementation choice is ours.

The third concerns the confidence threshold enforcement in the bioactivity tool. The spec section 4.10 defaults `min_confidence` to 7. But ChEMBL's confidence score field can be null in some records where the assay-to-target mapping was never scored. The question is whether null-confidence records should be included when the threshold is lowered, or always excluded regardless of threshold. The safer default is always-excluded, because we cannot verify that an unscored record meets any quality threshold, but this slightly reduces the tool's recall. Worth confirming.

## Deliverables checklist for session-end report

The session is complete when the following are all true.

The session-4 prompt is committed as the first action. The two deferred session-3 probes have been executed or formally deferred again with documented reasons. The session-3 EBI integration tests pass against real infrastructure with `EBI_EMAIL` set. The ChEMBL and PubChem clients are implemented with unit tests green. Both new tools are implemented per spec sections 4.9 and 4.10 with unit and integration tests green. The server registers both tools with appropriate annotations and the selection guide is updated. The smoke test exercises 8/8 tools live. At least five conventional-commit feature commits have been made, each with RED-phase evidence in the body. All tests pass offline with `pytest` and gated tests pass with `RUN_INTEGRATION=1 pytest`. Memory entries document the two probe outcomes, the ChEMBL and PubChem client patterns, the dual-source merge logic for the compound tool, and the null-confidence handling decision for the bioactivity tool.

## Procedural reminders

British English in docstrings, README, comments. Test-first with RED-phase assertion errors captured in commit bodies. Stop and ask before deviating from spec sections 4.9 or 4.10 in ways that change input or output schema. Use `serena` for symbol-level navigation across the now-substantial codebase. Apply the `superpowers:verification-before-completion` gate before each commit. Use `commit-commands:commit` for commit message formatting.

Start with pre-work discovery. Report findings and decisions, then wait for approval before implementing.
