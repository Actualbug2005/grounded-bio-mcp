# grounded-bio-mcp — Project Handoff

> Purpose: Self-contained briefing for any new conversation continuing this project. Read this in full before issuing any session prompts. Pair with the canonical spec at `grounded-bio-mcp-spec.md` and the session prompts under `prompts/`.

## Project in one paragraph

A Model Context Protocol server that grounds Claude's biology answers in primary databases instead of pattern-matched training data. Eighteen tools wrapping NCBI, UniProt, RCSB PDB, AlphaFold DB, EBI Clustal Omega, EBI InterProScan, ChEMBL, PubChem, Ensembl, Europe PMC, Reactome, STRING, ViennaRNA, python-codon-tables, NCBI BLAST, and CRISPOR. The whole project exists because Claude (and other LLMs) confidently fabricate molecular biology specifics — residue numbers, binding affinities, sequences, citations, off-target tables — and the only reliable fix is replacing model-generated specifics with retrieval from authoritative sources. Every architectural decision flows from this principle, applied recursively to the project's own outputs as well as upstream data.

## Current state — end of session 7

Seventeen of nineteen tool files in `src/grounded_bio_mcp/tools/` are fully implemented, registered, smoke-tested, and unit-tested; the remaining two (`fold_sequence.py`, `design_grna.py`) are 14-line TODO stubs that land as Session 8a deliverables. Two hundred and five offline tests passing, sixteen integration tests passing under live API conditions, seventeen smoke tests passing end-to-end against real services. Zero regressions across all sessions. Memory system has roughly thirty-seven entries with no orphans or drift detected at last audit (session 4). The Session 8a opening audit (`docs/audit_session_8a.md`) corrects an earlier inaccurate classification of `bio_fold_sequence` as "implemented but not registered".

### Tools by status

Live and registered (17):

- bio_fetch_sequence (NCBI)
- bio_fetch_uniprot
- bio_fetch_pdb
- bio_fetch_alphafold
- bio_align_sequences (EBI Clustal Omega)
- bio_scan_domains (EBI InterProScan)
- bio_fetch_compound (ChEMBL + PubChem)
- bio_fetch_bioactivity (ChEMBL)
- bio_fetch_variant (Ensembl)
- bio_predict_variant_effect (Ensembl VEP)
- bio_fetch_gene (NCBI Gene)
- bio_search_literature (Europe PMC)
- bio_fetch_paper_fulltext (Europe PMC)
- bio_fetch_pathway (Reactome)
- bio_fetch_interactions (STRING)
- bio_codon_optimise (local + bundled Kazusa data)
- bio_blast_search (NCBI BLAST URL API)

Stub only — implementation in Session 8a (2):

- bio_fold_sequence (ViennaRNA, spec §4.8) — 14-line stub with TODO comment in module docstring; no callable function. Originally reported as "implemented but not registered" at the end of session 7; the Session 8a opening audit (`docs/audit_session_8a.md`) confirmed the file is a stub, not an implementation. Implementation lands in 8a using the Python bindings (`import RNA`) restricted to non-GLPK API surface.
- bio_design_grna (CRISPOR, spec §4.7) — 14-line stub with TODO comment. Heaviest tool in the spec; requires local CRISPOR install plus multi-gigabyte genome indexes. Implementation arc lands in 8a alongside fold_sequence; only felCat9 indexed on dev machine, with hg38 + mm39 deferred to the LXC in 8b.

### Infrastructure not yet built

- Deployment to LXC on Proxmox pve2 (spec §9)
- Evaluation harness with 10 verifiable Q/A pairs (spec §10.4)

## What session 8 needs to do

Three concrete deliverables, in roughly this order:

1. Register bio_fold_sequence and add to smoke test (15 minutes of work). Audit any other tools that might be in similar implemented-but-not-registered state.
2. Implement bio_design_grna with CRISPOR (the genuinely hard part of the session).
3. Deploy to LXC on pve2 with Caddy reverse proxy, systemd service, bearer token auth, and verified end-to-end operation. Build the evaluation harness as part of deployment verification.

Session 8 is the most operationally complex session in the project because it introduces a genuinely new tool category (subprocess wrapping with local genome indexes) AND deploys to real infrastructure. Probably worth more than one session if the user prefers conservative pacing — could split into 8a (bio_fold_sequence registration plus CRISPOR implementation) and 8b (deployment plus evaluation harness).

## Architectural decisions that are not negotiable

These are the load-bearing choices that have stayed constant across all seven sessions and should not be revisited without strong reason.

**Python with FastMCP 3.x as the framework.** Not the official mcp[cli] SDK's bundled FastMCP 1.0, which is frozen. The package is `fastmcp>=3.0,<4.0` from jlowin. Decided in session 1 after the Claude Code mcp-server-dev plugin recommended the standalone fastmcp package over the bundled version. Import is `from fastmcp import FastMCP`. Streamable HTTP transport via `mcp.run(transport="http", host=..., port=...)`.

**Static bearer token auth at the reverse proxy layer.** Not OAuth, not directory submission, not anything fancier. The server binds to 127.0.0.1 on the LXC; Caddy fronts it with bearer token validation; the token lives in `MCP_AUTH_TOKEN` env var. This pattern would not pass Anthropic directory submission but is correct for a self-hosted homelab connector.

**Dict returns from all tool functions.** Not Pydantic models. Pydantic is for input validation only. The reasoning is that FastMCP wraps scalar returns in `{"result": ...}` but passes dicts through bare, so consistent dict returns produce consistent client-side behaviour. Established session 2, confirmed session 4 when the question was raised.

**Tools follow `bio_{action}_{resource}` naming convention.** No exceptions. Established session 1.

**Tool annotations default to `readOnlyHint=true, destructiveHint=false, openWorldHint=true, idempotentHint=true` with explicit overrides documented per tool.** Documented exceptions are bio_blast_search (idempotent=false because NCBI databases update), bio_fetch_bioactivity (idempotent=false because ChEMBL accepts new submissions), bio_fetch_variant (idempotent=false because Ensembl releases update annotations), bio_predict_variant_effect (idempotent=false same reason), bio_search_literature (idempotent=false because new papers get indexed), and bio_codon_optimise (openWorld=false because it has no external API — the only such tool).

**British English throughout.** Docstrings, comments, README, memory entries, prompt files. Reflects user preference.

**Test-first discipline with RED-phase assertion errors captured in commit message bodies.** Not optional. Every feature commit body contains the actual AssertionError text from the initial failing test. This is the project's quality gate against tests-written-after-the-fact masquerading as TDD.

**Conventional commit messages.** `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`, `probe:` are the prefixes in use.

**Session prompts live in-tree under `prompts/`.** Each session commits its own prompt as the first action. Provides design audit trail alongside code.

**Memory entries persisted via auto-memory at `~/.claude/projects/.../memory/` with MEMORY.md as index.** Not via claude-mem MCP, which has been used inconsistently. Auto-memory is the canonical store. Audit before sessions five and beyond was clean; periodic audits are good practice.

## Anti-hallucination principles applied recursively

The project's core thesis is that AI fabrication of specifics is the failure mode worth solving. This applies at multiple layers:

**Upstream data verification.** Every tool fetches from a primary source rather than letting the model pattern-match. ChEMBL bioactivity data with confidence scores, Europe PMC fulltext for citation verification, AlphaFold predictions with pLDDT confidence, CRISPOR off-target analysis against real genome indexes.

**Tool output honesty.** Tools surface what they actually retrieved including confidence signals, provenance, and absences. AlphaFold pLDDT must accompany every structural prediction. Compound tool surfaces which database returned which field. Bioactivity tool exposes the seven evidence sub-scores. Variant tool surfaces annotation richness flags rather than collapsing to categorical outcomes. Empty results are reported as empty, not approximated.

**No fabrication in our own outputs.** When the EBI InterProScan partial-results probe revealed no genuine streaming behaviour exists, we did not invent fake `databases_completed` arrays. When ChEMBL's confidence filter turned out to be leaky, we re-enforced client-side rather than trusting the upstream filter. When Europe PMC fulltext is unavailable, we report that fact rather than synthesising content.

**Verification dates on probed claims.** Memory entries that capture API behaviour include the date of verification. Future sessions can re-probe if behaviour seems to have changed.

## Significant technical patterns established

**RateLimitedClient with shared semaphore per service.** Lives in `clients/base.py`. Every external HTTP call goes through it. Per-service entries in `RATE_LIMITS` dict. The semaphore is shared across all uses of one service so that two concurrent tool calls cannot exceed the per-service rate limit between them.

**Async-job runner pattern (EBIJobRunner).** Submit, poll with backoff and jitter, fetch result, cancel on timeout. Lives in `clients/ebi.py`. The cancellation-on-timeout is non-negotiable to avoid orphaning jobs in EBI's queue. The 0.8-1.2x jitter on poll intervals is non-negotiable to avoid lock-step behaviour across concurrent jobs sharing the IP. Same pattern adapted for NCBI BLAST in session 7 (different polling cadence: 15s initial, 60s after 5 minutes wall time, per NCBI batch-job etiquette).

**soft_cap_with_url_fallback truncation helper.** Lives in `utils/formatting.py`. Used by bio_fetch_pdb (coordinates), bio_align_sequences (alignment text), bio_scan_domains (matches), bio_fetch_gene (GO list), bio_fetch_paper_fulltext (sections), bio_blast_search (alignments). Soft cap typically 200 KB; on overflow returns metadata plus direct URL to upstream resource.

**NCBI httpx-plus-Biopython hybrid pattern.** Raw httpx through RateLimitedClient for HTTP transport; Biopython for parsing only. Bio.Entrez.efetch is synchronous and would block the event loop. Established session 2, applied to all NCBI tools (sequence, gene, BLAST).

**Identifier disambiguation pattern.** When name-based lookups return multiple candidates, surface a candidate list with disambiguation context (organism, chromosome, common name as appropriate). Used in bio_fetch_compound (PubChem), bio_fetch_gene (NCBI), bio_fetch_pathway (Reactome). Never silently picks one when the model could choose better with context.

**Server-side filter verification.** Upstream filters cannot be trusted without verification. ChEMBL confidence filter was leaky (76% leak rate at threshold 7). STRING combined-score filter was clean (zero leakage at threshold 900). Always probe before trusting.

**Email handling per API.** EBI requires email in form parameter (errors if missing); STRING requests email in user-agent header (warns if missing). Different APIs implement courtesy email differently; the project handles each according to that API's actual requirements.

## Session-by-session brief

Session 1 (scaffold and shared infrastructure): pyproject.toml with FastMCP 3.x, RateLimitedClient with TDD test, error handling utilities, config loading via pydantic-settings, project structure. Two commits.

Session 2 (first four fetch tools): bio_fetch_sequence, bio_fetch_uniprot, bio_fetch_pdb, bio_fetch_alphafold. Server registration with selection guide. Smoke test infrastructure. Five commits, 26 tests.

Session 3 (EBI async-job tools): EBIJobRunner with cancellation and jitter, bio_align_sequences, bio_scan_domains. Truncation helper extracted. Six commits, 61 tests.

Session 4 (compound and bioactivity): bio_fetch_compound (dual-source ChEMBL/PubChem), bio_fetch_bioactivity (with leaky-filter defence). Resolved deferred EBI probes. Eight commits, 110 tests.

Session 5 (variants and gene): bio_fetch_variant, bio_predict_variant_effect, bio_fetch_gene. Ensembl client with assembly routing. Seven commits, 131 tests.

Session 6 (literature, pathway, interactions): bio_search_literature, bio_fetch_paper_fulltext, bio_fetch_pathway, bio_fetch_interactions. Three new clients. The fulltext tool concretely closes the loop on the original Sugisawa 2016 hallucination case. Ten commits, 176 tests.

Session 7 (codon optimiser and BLAST): bio_codon_optimise with bundled Kazusa codon tables, bio_blast_search with NCBI BLAST URL API. Discovered bio_fold_sequence was implemented but never registered. Ten commits, 205 tests.

## Operational details for session 8

**CRISPOR install requirements.** Clone `https://github.com/maximilianh/crisporWebsite` to `/opt/crispor`. Create separate venv (Python 3.11 if 3.13 incompatible, both available on Trixie). Install requirements. System dependency: `bwa` package via apt. Genome indexes per-species, multi-gigabyte downloads, stored at `/var/lib/grounded_bio_mcp/genomes/`. Initial spec called for hg38, mm39, felCat9 pre-indexed.

**Genome index downloads will hit the download approval gate.** Same pattern as Kazusa codon tables in session 7 — surface URLs and sizes ahead of approval, wait for explicit go-ahead. Each genome is roughly 3 GB so this is a significant network operation deserving explicit user acknowledgement.

**Deployment target is LXC on Proxmox VE 9.x (pve2).** Debian 13 Trixie base (matches host OS). 4 vCPU, 6 GB RAM, 30 GB root, 80 GB data mount. Spec §9 has detailed provisioning steps.

**Caddy reverse proxy with bearer token auth.** Server binds 127.0.0.1:8080. Caddy fronts at `bio-mcp.devlin.lan` (or equivalent), validates `Authorization: Bearer ${MCP_AUTH_TOKEN}` header, proxies to local server.

**systemd service.** User `bio-mcp`, working directory `/opt/grounded_bio_mcp/app`, environment file `/etc/grounded_bio_mcp/env`. Hardening directives: NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp.

**Connection from claude.ai.** Settings → Connectors → Add custom connector. URL is the Caddy-fronted endpoint plus `/mcp`. Bearer token from `MCP_AUTH_TOKEN`. Transport: streamable HTTP.

**Evaluation harness per spec §10.4.** Ten Q/A pairs, each independent and verifiable. Spec already lists candidate questions; final exact answers determined by first run against the deployed system.

## Spec errata accumulated

These should roll into a v2.2 spec revision after deployment is complete and the system is in production use.

- §4.5 (Clustal Omega): "conserved-column count" missing from the four-stat output specification.
- §4.10 (bioactivity): null-confidence record handling not specified; project decision is always-excluded regardless of threshold.
- §4.11 (variants): three-outcome shape (found-rich, found-empty, not-found) is not implementable because Ensembl collapses to two outcomes; project uses found/not_found with annotation richness flags.
- §4.12 (VEP): chrom:pos:ref:alt input format does not literally map to VEP's region URL; requires translation with REF length determining end position.
- §4.13 (InterProScan): default `["Pfam", "SMART", "CDD"]` would fail at EBI; canonical EBI names are `["PfamA", "SMART", "CDD"]` (and PROSITE splits into PrositeProfiles + PrositePatterns, SuperFamily/Gene3d casing matters).
- §4.17 (Reactome): documented endpoint should be `/data/query/{stId}`, not `/data/pathway/{stId}` which 404s.
- §4.17 example: R-HSA-109581 is Apoptosis, not "Signalling by Interleukins" as the original spec text claimed.
- §6 evaluation example accession NM_001301717 is CCR7 (chemokine receptor), not BRCA1 or HTT as variously stated in earlier prompt revisions.
- §6 evaluation example PDB 1CRN resolution is 1.5 Å, not 0.54 Å (the latter is 1EJG, a re-refinement of crambin).
- §7 rate-limit table: BLAST polling cadence (15s initial → 60s after 5 min wall time) is different from E-utilities request rate, which the table conflates.
- §10.2 evaluation question 10 wording presupposes that Sugisawa 2016 names specific residues; verified by full-text fetch that the paper does not — the question is correct as a negative-verification test, but the wording could be clearer.

## How to run the existing system locally

Development environment is the user's macOS dev machine, Python 3.13/3.14. Install with:

```bash
git clone <repo>
cd bioinformatics
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env to set EBI_EMAIL, optionally NCBI_API_KEY, STRING_USER_EMAIL, MCP_AUTH_TOKEN
```

Run tests:

```bash
pytest                                    # offline only, ~5s
EBI_EMAIL=... RUN_INTEGRATION=1 pytest    # includes live API tests, ~80s
```

Run smoke test (requires EBI_EMAIL):

```bash
EBI_EMAIL=... python scripts/smoke_test_phase1a.py
```

Run server in stdio mode for MCP Inspector:

```bash
mcp dev src/grounded_bio_mcp/server.py
```

Or programmatically:

```bash
python -c "from fastmcp import Client; ..."
```

## Working style preferences for the user

The user is detail-oriented, asks questions when something does not match expectations, and explicitly prefers verification-first development. Their pre-work reports are consistently substantive and catch real spec errata; do not treat pre-work as a rubber-stamp formality. When the user approves something in conversation, that approval covers the agreed scope; runtime safety gates should still fire for specific operations (the download-gate-twice pattern from session 7 is correct).

The user is dyslexic and prefers conclusion-first structure with short units and scannable formatting. They respect technical accuracy over verbosity. They are comfortable with mechanistic explanations and welcome correction.

The user runs a Proxmox homelab (pve and pve2 both present, deployment target is pve2). They have an existing pattern of LXC-per-service that the deployment should match.

## Session 8 prompt drafting guidance

When ready to start session 8, prompt should cover:

- Audit existing tools for any other implemented-but-not-registered cases (catch-up before new work)
- Register bio_fold_sequence, extend smoke test to 18 tools
- Implement bio_design_grna with CRISPOR, including subprocess management, genome index handling, and download-gate workflow for genome fetches
- Build LXC on pve2 per spec §9
- Implement evaluation harness per spec §10.4

If splitting into 8a and 8b: 8a covers the audit, bio_fold_sequence, and CRISPOR implementation locally on dev machine. 8b covers LXC deployment, genome index installation on the LXC, and evaluation harness.

The download approval gate pattern from session 7 should be the model for genome index downloads. Surface URLs, expected sizes, and target paths before fetching; wait for explicit user approval per fetch operation.

## What this handoff does not contain

- Implementation details of individual tools — read the source code at `src/grounded_bio_mcp/`
- Spec details — read `grounded-bio-mcp-spec.md`
- Session-by-session reasoning — read `prompts/session-*.md` files in order
- API quirks discovered per-tool — read `~/.claude/projects/.../memory/MEMORY.md` and the entries it indexes
- Test fixtures — read `tests/fixtures/`

These are intentionally deferred to the canonical sources rather than duplicated here. The point of this handoff is orientation, not replication.

---

**End of handoff. A fresh session reading this should have enough context to continue the project without re-deriving any decisions, while knowing where to look for everything not covered in detail here.**
