> Repo path: `prompts/session-6-knowledge-bases.md`
> Commit this file as the first action of session 6 (commit message: `docs: session 6 prompt — literature, pathway, and interactions tools`).

Previous session completed the variants and gene tools. Eleven of eighteen tools now implemented with 131 offline tests passing and 11/11 smoke tests green against live APIs. This session implements four more tools covering the "look up what is known about a biological entity in curated knowledge bases" surface.

## Scope

Implement four tools end-to-end, along with three new clients.

1. `bio_search_literature` — Europe PMC search (spec §4.14)
2. `bio_fetch_paper_fulltext` — Europe PMC full-text retrieval (spec §4.15)
3. `bio_fetch_pathway` — Reactome pathway data (spec §4.17)
4. `bio_fetch_interactions` — STRING protein-protein interactions (spec §4.18)

Plus three new clients: `clients/europepmc.py`, `clients/reactome.py`, `clients/string_db.py`. Register all four tools in `server.py` with selection-guide updates.

## Out of scope

The codon optimiser (spec §4.19), BLAST (spec §4.6), CRISPOR (spec §4.7), deployment scripts, and the evaluation harness all remain for later sessions. The session-2 `response_format="markdown"` drift is still on the backlog for a future spec revision.

## Why these four together

All four tools are single-request fetches against stable third-party REST APIs with no async job handling. They share the architectural shape of session 2 and session 4, which means they exercise existing infrastructure rather than introducing new patterns. Grouping them together makes sense because they are conceptually adjacent — they all answer "what is known about this biological entity in a curated database" — which means the selection-guide updates can be written coherently as one coordinated set rather than in multiple passes.

The two Europe PMC tools are paired because literature search and fulltext retrieval naturally chain together: search to find candidate papers, fetch fulltext to verify what a specific paper actually says. They share the Europe PMC client and several response-parsing utilities. The Reactome and STRING tools are each standalone and can be implemented independently.

## Priority ranking within the session

Implement in this order, which both matches the natural API-clustering and puts the highest-value tool in the sequence early enough that any unexpected complexity surfaces before the session runs long.

First, the two Europe PMC tools together. `bio_fetch_paper_fulltext` is the single most important tool in this session because it directly addresses the citation-verification failure mode that motivated the entire project. The original ChatGPT transcript that started this work confidently attributed findings to "Miyazaki/Nakata work" without a verifiable DOI, which is exactly the fabrication mode that fulltext retrieval prevents. Getting this tool right matters more than any other in the session. It is also the tool most likely to have behavioural surprises, because Europe PMC's fulltext response shape varies significantly between papers — some return structured XML with proper section markup, some return flattened text, some return metadata only with a redirect to the publisher. The probe during pre-work needs to understand this variance, and the tool's output shape needs to handle all cases honestly.

Second, the Reactome pathway tool. Reactome is the most conventional API of the four and should be the simplest to implement, so putting it after the Europe PMC work means we build confidence on the hardest tool first and then accelerate.

Third, the STRING interactions tool. STRING has one operational quirk worth planning for — they request that API users include a contact email in the HTTP user-agent header so they can reach out if usage causes problems. This is similar to EBI's email policy but implemented via header rather than form parameter, which means the existing `RateLimitedClient` needs either a small extension or STRING-specific header handling.

## Pre-work discovery requirements

This session has four new API surfaces to investigate, which means pre-work will take longer than in previous sessions. Do not rush the discovery phase just because the tools are individually simple. A thorough pre-work report should cover all four APIs.

Read spec sections 4.14, 4.15, 4.17, and 4.18 in full. Note the input schemas, output shapes, and the relationships between tools. The literature tools chain naturally (search produces PMCIDs which fetch consumes), and the pathway tool can take either pathway IDs or gene symbols as input, which creates a cross-reference surface with the existing gene tool.

Re-read the relevant memory entries. The fetch-tool patterns from sessions 2 and 4 apply directly here. The truncation helper pattern will be relevant for fulltext retrieval because some papers are very long. The null-confidence exclusion pattern from the bioactivity tool may be relevant for STRING's combined score filtering, which has a similar "threshold filter enforcement" flavour.

Investigate each API's current state and report findings.

For Europe PMC, verify the base URL at `https://www.ebi.ac.uk/europepmc/webservices/rest/`. Verify the search endpoint parameters, including the query syntax that Europe PMC supports (MeSH terms, author filters, date ranges, open-access flags). Verify the fulltext endpoint behaviour for papers in different states — papers with full XML available, papers with only abstract, papers with neither. Probe a specific paper we know the state of: PMC5059666 is Sugisawa 2016 which was one of the motivating papers for this whole project, and its fulltext availability should be confirmed. Test what happens for closed-access papers where only the abstract is publicly available.

For Reactome, verify the Content Service base URL at `https://reactome.org/ContentService/`. Verify the pathway lookup by stable ID, the pathway lookup by gene symbol, the pathway lookup by UniProt accession, and the pathway hierarchy navigation (parent and child pathways). Report whether Reactome requires any authentication or user identification.

For STRING, verify the base URL at `https://string-db.org/api/`. Verify the network endpoint parameters, particularly the species taxon ID handling (9606 for human, 10090 for mouse, 9685 for cat) and the combined score threshold behaviour. Check whether STRING's server-side combined-score filter is leaky in the same way ChEMBL's confidence filter was — if we request combined_score >= 700, does every returned edge actually meet that threshold? Verify the user-agent email requirement and the correct way to pass it.

For all four APIs, probe the rate limits and verify the spec section 7.1 values are correct. Spec says Europe PMC is 10 concurrent with 0.1s minimum interval, Reactome is 5 concurrent with 0.2s minimum interval, and STRING is 3 concurrent with 1s minimum interval. If the actual published limits differ, the rate limits table needs updating.

Check whether any of the APIs have quirks around identifier resolution. Europe PMC supports searching by DOI, PMID, PMCID, and text; the fulltext tool needs to handle all of these cleanly. Reactome supports pathway IDs in the format `R-HSA-109581` where HSA is species-specific; queries by gene symbol need species context. STRING identifiers require species context and can use gene symbols, UniProt accessions, or STRING-native protein IDs.

## Specific design decisions requiring pre-approval

Four decisions that will probably need your input before implementation begins, one per tool.

The first concerns fulltext availability reporting. Europe PMC's fulltext endpoint returns different response types depending on what is available for a paper. The tool could collapse these into a uniform response shape with an `availability` field describing what was retrieved (full XML with sections, plain text only, abstract only, nothing available), or it could raise distinct errors for each non-success case. The honest approach is probably the former because it lets a caller see gradations of availability rather than just success or failure, but the specific response shape for each availability level needs to be designed carefully so that callers can easily check what they got before trying to use it.

The second concerns Reactome cross-species pathway lookup. A gene symbol query for a pathway might return results across multiple species if the symbol exists in more than one organism. The same disambiguation pattern from the gene tool applies here — surface candidates with species context. The question is whether the default should be strict species filtering (match only the species parameter, error if no results in that species) or permissive matching (return all species and let the caller filter). My leaning is strict default, with permissive available via an explicit parameter.

The third concerns STRING's interaction type filtering. STRING distinguishes between experimental interactions, database-annotated interactions, text-mined interactions, co-expression predictions, and several other evidence types, each with its own sub-score contributing to the combined score. The tool could return a single combined score per edge, or it could return the full evidence breakdown. The spec section 4.18 mentions returning the evidence breakdown, which is the right choice because it lets callers distinguish "directly observed experimental interaction" from "text-mining co-occurrence" — these have very different confidence implications despite potentially having similar combined scores.

The fourth concerns whether to implement STRING's server-side combined-score threshold or enforce it client-side. ChEMBL's confidence filter was leaky (returned low-confidence rows despite filter parameter). We do not yet know if STRING's filter is similarly leaky. Pre-work should probe this. If STRING's filter is reliable, we can trust it. If it is leaky like ChEMBL, we need client-side re-enforcement with a `below_threshold_excluded` counter, following the session 4 pattern.

## Implementation ordering

Build in TDD order, one tool at a time.

Start with the Europe PMC client in `clients/europepmc.py`, implementing the methods both literature tools need (search, fulltext retrieval, metadata lookup). Unit tests with `respx` mocking for offline, integration tests gated on `RUN_INTEGRATION=1` for live API validation.

Implement `bio_search_literature` first. Integration test with a query that reliably returns Sugisawa 2016 as a top result. The output should include the usual paper metadata plus a clear indication of which results have full-text available in PMC versus which only have abstracts.

Implement `bio_fetch_paper_fulltext` second. Integration test with PMC5059666 specifically because it is the motivating paper for this whole project, and being able to fetch its full text is the concrete proof that the anti-hallucination architecture works as intended. The output should handle all availability levels honestly without fabricating content for unavailable papers.

Implement the Reactome client and `bio_fetch_pathway` third. Integration test with a stable pathway ID like R-HSA-109581 (which covers Signalling by Interleukins if I remember correctly, but verify during pre-work). Also test lookup by gene symbol with and without species filtering to exercise both code paths.

Implement the STRING client and `bio_fetch_interactions` fourth. Integration test with a well-studied protein like TP53 (P04637) against human (taxon 9606), expecting a rich network with diverse evidence types. Verify the combined-score threshold is enforced correctly (client-side if necessary). Include the user-agent email handling as part of the client's standard request setup.

After all four tools are green, register them in `server.py` with standard annotations. All four are read-only, idempotent in the short term (though literature search results can change as new papers are indexed, so `bio_search_literature` should be marked `idempotentHint=false`), open-world, and non-destructive. Update the selection guide in the `instructions` parameter to make clear which tool handles which question type.

Extend the smoke test to cover all four new tools, bringing the total to 15/15. Include one literature search, one fulltext retrieval, one pathway lookup, and one interaction query.

## Specific things to watch for

Europe PMC fulltext XML parsing is likely to be the most complex part of this session. Papers use the JATS XML schema which has nested section structures, figure captions, table markup, reference lists, supplementary material links, and inline formatting. The parser should extract readable structured text (sections with headers and prose) while discarding pure formatting markup. The `lxml` library from the existing dependencies handles this well. The truncation helper may need extension if a single section exceeds the soft cap.

Reactome's pathway data includes both human-readable names and structured identifiers for every participating entity. The output should include both so that callers can display readable names while also being able to chain to other tools using the structured IDs.

STRING's combined score is on a 0-1000 scale despite being documented in some places as 0-1. Verify the actual scale during pre-work and document clearly in the tool's output schema.

All three new clients should follow the pattern established in sessions 2 and 4. No new client architecture should be needed.

## Deliverables checklist for session-end report

The session is complete when the following are all true.

The session-6 prompt is committed as the first action. Three new clients are implemented with unit tests green. All four tools are implemented per spec sections 4.14, 4.15, 4.17, and 4.18 with unit and integration tests green. The server registers all four tools with appropriate annotations and the selection guide is updated. The smoke test exercises 15/15 tools live. At least seven conventional-commit feature commits have been made (one per client, one per tool, plus registration and smoke test extension), each with RED-phase evidence in the body. All tests pass offline with `pytest` and gated tests pass with `RUN_INTEGRATION=1 pytest`. Memory entries document the Europe PMC client pattern, the Reactome client pattern, the STRING client pattern with user-agent email handling, the fulltext availability response shape decision, any API quirks discovered during implementation, and confirmation or correction of spec section 7.1 rate limits for all three services.

## Procedural reminders

British English in docstrings, README, comments. Test-first with RED-phase assertion errors captured in commit bodies. Stop and ask before deviating from spec sections 4.14, 4.15, 4.17, or 4.18 in ways that change input or output schema. Use `serena` for symbol-level navigation across the now-substantial codebase. Apply the `superpowers:verification-before-completion` gate before each commit. Use `commit-commands:commit` for commit message formatting.

Given that this is a four-tool session rather than the two-or-three tool sessions we have been doing, expect the session to be longer than previous sessions. If you approach a natural stopping point mid-session (for example, after the two Europe PMC tools are complete and tested but before Reactome or STRING are started), that is a reasonable place to pause and send an interim checkpoint rather than pushing through to complete all four. The quality gates matter more than completing the full scope in one sitting.

Start with pre-work discovery. Report findings and decisions across all four APIs, then wait for approval before implementing.
