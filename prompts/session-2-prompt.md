# Session 2 — First four fetch tools

Previous session delivered scaffold + shared infrastructure (commits `865157b` and `7bf124f`). This session implements the first batch of tool logic: **four read-only fetch tools that share async HTTP patterns and have no heavy external dependencies.**

## Scope — this session only

Implement these four tools end-to-end, in this order:

1. `bio_fetch_sequence` — NCBI Entrez via Biopython (spec §4.1)
2. `bio_fetch_uniprot` — UniProt REST (spec §4.2)
3. `bio_fetch_pdb` — RCSB Data API (spec §4.3)
4. `bio_fetch_alphafold` — AlphaFold DB API (spec §4.4)

Plus the **`server.py` FastMCP app** that registers and exposes these four (and only these four). Future tools get registered in their own sessions.

## Out of scope — do not touch

- Any of the other 15 tools (stubs remain as stubs)
- Clustal Omega / InterProScan async job handling (that's session 3 pattern — deliberately deferred)
- CRISPOR, BLAST (heavyweight, separate sessions)
- `deploy/` provisioning scripts
- `scripts/health_check.py`
- `eval/` harness

## Rationale for this batch

These four are the right starting set because:
- All are synchronous single-request fetches (no job polling) — simplest shape
- They share the same async HTTP pattern via `clients/base.py` — validates our base client
- They give near-complete sequence + structure coverage together — a useful integration test at the end
- Each has a stable, long-lived test accession: `NM_001301717`, `P01308`, `1CRN`, `P01308` respectively

## Step-by-step

### 0. Pre-work discovery (before any code)

1. Read `bioinformatics-mcp-spec.md` §4.1–4.4 in detail — each tool's input schema, output shape, and implementation notes are authoritative
2. Re-read `mcp-server-dev:build-mcp-server` skill (key sections: `tool-design.md`, `server-capabilities.md`) — we want annotations, response format conventions, and error patterns aligned with current plugin guidance
3. Check `claude-mem` — retrieve the four persisted decisions from session 1 so you don't re-litigate them (framework, auth, annotations, deployment target)
4. Read the stub files for the four tools — they have spec citations in docstrings; confirm these are still accurate after spec v2.1

### 1. Build in TDD order, one tool at a time

**For each of the four tools:**

1. **Write the integration test first** (gated behind `RUN_INTEGRATION=1` env var per spec §10.2) that hits the real API with the known-stable test accession and asserts on a specific known-stable field (length for sequences, resolution for 1CRN, pLDDT range for AlphaFold)
2. **Write a unit test** using `respx` or `pytest-httpx` with a canned API response fixture stored in `tests/fixtures/`
3. **Implement the client** (`clients/ncbi.py`, `clients/uniprot.py`, `clients/rcsb.py`, `clients/alphafold.py`) using the shared `RateLimitedClient` from `clients/base.py`
4. **Implement the tool** in the appropriate `tools/*.py` file per the stub's spec citation
5. **Run unit test → pass**
6. **Run integration test with `RUN_INTEGRATION=1`** → verify against real API
7. **Commit** with conventional message (e.g., `feat: bio_fetch_sequence with NCBI client`)

Do not move on to the next tool until the previous one has both tests green and is committed.

### 2. `server.py` — after all four tools

Once all four tools + tests are green:

1. Create `src/bioinformatics_mcp/server.py` using FastMCP 3.x patterns
2. Register the four tools with:
   - Correct annotations per session 1's persisted defaults (`readOnlyHint: true, destructiveHint: false, openWorldHint: true, idempotentHint: true` for these four — all four are idempotent)
   - Human-readable `title` for each tool
   - Tool selection guide table (spec §3) embedded in the server's top-level description parameter so it appears in the MCP handshake — **this is important**, it's how future Claude instances will know to prefer tools over training data
3. Wire up streamable HTTP transport with `mcp.run(transport="http", host=..., port=...)` reading `MCP_BIND_HOST` / `MCP_BIND_PORT` from `config.py`
4. Add a simple stdio entry-point option for local MCP Inspector testing
5. **Manual verification with MCP Inspector** — run `mcp dev src/bioinformatics_mcp/server.py` (or equivalent), confirm all four tools appear with correct schemas and each returns valid output for its test accession
6. **Commit** — `feat: FastMCP server with first four fetch tools registered`

### 3. Integration smoke test

Write `scripts/smoke_test_phase1a.py` that calls all four tools via the server (not via direct Python imports) with the test accessions and prints a pass/fail summary. This is the session's "does it actually work end-to-end" check.

### 4. Session-end persistence

Before ending session, persist to `claude-mem`:
- Integration test accessions used (so future sessions reuse them)
- Any API quirks discovered (UniProt pagination, PDB entity_id gotchas, AlphaFold's version-string format, NCBI rate-limit behaviour observed vs documented)
- Any deviations from spec §4.1–4.4 with rationale

## Working-style reminders (unchanged from session 1)

- British English in docstrings, README, comments
- Test-first, with RED evidence captured in commit messages
- Each tool commit should include its own tests, fixtures, and client module
- Stop and ask before deviating from spec. The spec §4 tool specs are authoritative.
- Use `serena` MCP for symbol-level code navigation across the now-populated codebase
- Use `superpowers:verification-before-completion` as the gate before each commit
- Use `commit-commands:commit` for commit message formatting

## Specific technical notes

### NCBI (spec §4.1)
- Use `Bio.Entrez.efetch` — Biopython wraps the rate-limiting headers and retmode/rettype semantics
- Set `Entrez.email` from `EBI_EMAIL` env var (NCBI actually accepts and encourages this) and `Entrez.api_key` from `NCBI_API_KEY`
- Test accession `NM_001301717` is a human BRCA1 variant transcript — stable across RefSeq releases
- Feature-table parsing: use `SeqIO.read(..., "genbank")` for `rettype="gb"`, iterate `record.features`, serialise to `{type, location, qualifiers}` dicts
- Rate limit table (spec §7.1): 10 req/s with key, 3 req/s without

### UniProt (spec §4.2)
- Endpoint: `https://rest.uniprot.org/uniprotkb/{accession}.json`
- Parse with `pydantic` — UniProt JSON has a stable schema, worth modelling
- Test accession `P01308` = human insulin, ancient and stable
- Cross-references (`uniProtKBCrossReferences`) are where PDB / AlphaFold / RefSeq IDs live — surface these in output since they're the bridge to other tools

### RCSB PDB (spec §4.3)
- Metadata: `https://data.rcsb.org/rest/v1/core/entry/{pdb_id}`
- Polymer entity info: `https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}` — need to enumerate entity IDs from the entry response first (`rcsb_entry_container_identifiers.polymer_entity_ids`)
- Coordinates: `https://files.rcsb.org/download/{pdb_id}.cif`
- Test PDB `1CRN` = crambin, 0.54 Å resolution, trivial size (46 aa) — good fixture
- `include_coordinates=False` default is right; coords can be 100 KB+ per structure

### AlphaFold DB (spec §4.4)
- Metadata: `https://alphafold.ebi.ac.uk/api/prediction/{uniprot_accession}`
- Structure file: the metadata response includes a `pdbUrl` field — use that rather than constructing the URL manually (version string changes)
- Per-residue pLDDT lives in the B-factor column of the downloaded PDB file — parse with Biopython's `PDBParser` if returning full structure, or derive the summary (mean, per-region) from the file contents
- **Critical:** the pLDDT summary must be returned even when `format="summary"` — model needs confidence context to not over-trust low-confidence regions
- Test accession `P01308` (same as UniProt) — insulin has a predicted structure

## Deliverables checklist (for session-end report)

- [ ] Four client modules implemented, each using `RateLimitedClient`
- [ ] Four tool modules implemented, matching spec §4.1–4.4 schemas exactly
- [ ] Four fixture files under `tests/fixtures/` with canned API responses
- [ ] Unit tests for each tool (mocked, fast, run in CI without network)
- [ ] Integration tests for each tool (gated, require network, hit real APIs)
- [ ] `server.py` with FastMCP 3.x, streamable HTTP, tool selection guide in description, four tools registered
- [ ] `scripts/smoke_test_phase1a.py` end-to-end integration check
- [ ] Five commits (one per tool + one for server + smoke test) with conventional messages and RED-phase evidence in bodies
- [ ] `pytest` green on full test suite (unit tests only unless `RUN_INTEGRATION=1`)
- [ ] MCP Inspector manual verification passed
- [ ] `claude-mem` entries for any API quirks discovered

---

Start with the pre-work discovery. Report back with:
1. Spec §4.1–4.4 understanding confirmed (any ambiguities flagged)
2. Plugin-skill patterns confirmed applicable to these four tools
3. Four claude-mem decisions retrieved
4. Plan confirmed / any concerns before writing code

Don't write code until I've approved the pre-work report.
