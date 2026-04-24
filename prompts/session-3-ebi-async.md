# Session 3 — EBI async-job tools (Clustal Omega + InterProScan)

> Repo path: `prompts/session-3-ebi-async.md`
> Commit this file as the first action of session 3 (commit message: `docs: session 3 prompt — EBI async-job tools`).
> Going forward, every session prompt lives in `prompts/` and is committed as part of that session's work, so the design record stays alongside the code it shaped.

Previous session delivered four single-request fetch tools + server (commits `26a458a` through `7209eba`). 26/26 tests green, no spec deviations on schemas, three spec errata captured. **This session implements the EBI async-job pattern** — fundamentally different shape from session 2 because these tools submit a job, poll for completion, then fetch the result.

## Scope — this session only

Implement two tools end-to-end:

1. `bio_align_sequences` — Clustal Omega via EBI Job Dispatcher (spec §4.5)
2. `bio_scan_domains` — InterProScan via EBI Job Dispatcher (spec §4.13)

Plus a **shared async-job runner** in `clients/ebi.py` that both tools use, since the submit/poll/fetch pattern is identical across EBI's services and we'll likely reuse it for any future EBI tools.

Register both tools in `server.py`. Extend the smoke test to exercise all six tools end-to-end.

## Out of scope — do not touch

- Other 13 tools (stubs remain stubs)
- CRISPOR (heavyweight, separate session)
- BLAST (similar shape but slower and finicky — separate session)
- `bio_fetch_paper_fulltext` (Europe PMC has different async behaviour)
- `deploy/` provisioning
- `eval/` harness

## Why these two together (deliberate deviation from spec §12 ordering)

Spec §12 has Clustal in MVP and InterProScan in Phase 2. Pairing them by **shared infrastructure** (EBI Job Dispatcher) rather than by spec phase is the right call: building the runner once is materially better than building it twice. Spec §12 is a delivery plan, not a coupling diagram.

Both services use **EBI's Job Dispatcher API** with identical submit/poll/fetch semantics:

- `POST /run` with form-encoded params → returns plain-text job ID
- `GET /status/{jobId}` → returns plain-text status (`PENDING` / `RUNNING` / `FINISHED` / `FAILED` / `ERROR` / `NOT_FOUND`)
- `GET /resulttypes/{jobId}` → returns available result formats
- `GET /result/{jobId}/{resultType}` → returns the actual output
- `DELETE /delete/{jobId}` → cancels a running job (used on our timeout path)

By implementing both together we extract the shared runner immediately rather than refactoring later.

## Step-by-step

### 0. Pre-work discovery (before any code)

1. Re-read `bioinformatics-mcp-spec.md` §4.5 (`bio_align_sequences`) and §4.13 (`bio_scan_domains`) — input schemas, output shapes, `EBI_EMAIL` requirement
2. Re-check session-2 memory entries for patterns to carry forward (NCBI httpx pattern, FastMCP return-wrapping quirk, low-confidence-warning convention)
3. **Verify EBI Job Dispatcher endpoint structure as currently published** — base URL, parameter naming for both services, whether the `/delete/{jobId}` endpoint exists and what it returns. EBI moved from "Web Services" to "Job Dispatcher" relatively recently and parameter names have changed across versions. Don't trust my doc references — fetch current docs.
4. Check our `RateLimitedClient` (spec §7.1) — EBI is `max_concurrent=3, min_interval=0.5s`. This applies to **every** HTTP call (submit, each poll, status check, result fetch, cancellation), not just submission. Confirm the limiter handles this correctly.
5. Re-read `mcp-server-dev:build-mcp-server` `tool-design.md` for guidance on long-running tools / progress reporting / output size handling
6. **Find the session-2 truncation / pagination convention** (likely in `utils/formatting.py` or in the PDB/AlphaFold tool code where coordinate output is capped). Whatever pattern session 2 established for "this output is too big to inline" must be reused here. Don't invent a second one.

Report back any ambiguities before writing code, same as session 2.

### 1. Build the shared async-job runner first

`src/bioinformatics_mcp/clients/ebi.py` should expose something like:

```python
class EBIJobRunner:
    """Submit → poll → fetch pattern for EBI Job Dispatcher services.

    Polling uses exponential backoff with jitter to avoid lock-step
    behaviour across concurrent jobs sharing this IP. On wall-clock
    timeout, calls /delete/{jobId} to free the EBI queue slot before
    raising — orphan jobs are antisocial.
    """

    def __init__(self, service: str, rate_limited_client: RateLimitedClient):
        self.base_url = f"https://www.ebi.ac.uk/Tools/services/rest/{service}"
        self.client = rate_limited_client

    async def submit(self, params: dict) -> str: ...
    async def poll_until_complete(
        self,
        job_id: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
    ) -> Literal["FINISHED"]: ...
    async def fetch_result(self, job_id: str, result_type: str) -> bytes: ...
    async def list_result_types(self, job_id: str) -> list[dict]: ...
    async def cancel(self, job_id: str) -> bool: ...

    async def run(
        self,
        params: dict,
        result_type: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
    ) -> bytes:
        """Convenience: submit → poll → fetch in one call.
        Cancels the job on timeout before raising JobTimeoutError."""
        ...
```

#### Hard requirements for the runner

**1. All HTTP through `RateLimitedClient`.** Submit, every poll, status checks, result fetch, cancellation — global EBI semaphore (3 concurrent / 500ms interval). Not per-call-type.

**2. Polling backoff with jitter.**

- Start at `initial_interval` (default 2s)
- After 5 consecutive `RUNNING` polls, scale up: 2s → 5s → 10s, capped at `max_interval`
- **Add ±20% uniform jitter to every poll wait** — `actual_wait = base * uniform(0.8, 1.2)`. Without jitter, multiple concurrent clients hitting EBI from this server stay in lockstep and hammer the same poll-second. Jitter desynchronises them.
- Document the rationale in the docstring so future-you doesn't "simplify" it back to deterministic.

**3. Timeout handling with cancellation.**

- Raise `JobTimeoutError` (new exception class in `utils/errors.py`) with the job ID in the message so the caller can poll manually if needed
- **Before raising, call `self.cancel(job_id)`** — orphan jobs consume EBI queue slots and are antisocial. Best-effort: if cancellation fails (e.g. job already finished between poll and cancel), log at WARNING but still raise the timeout
- Per-tool-configurable timeout: 300s default for Clustal, 600s default for InterProScan
- The JobTimeoutError message should include the job ID and a polling URL the user can hit themselves: `"Job {jobId} did not complete in {timeout}s. Cancelled. Check status at https://www.ebi.ac.uk/Tools/services/rest/{service}/status/{jobId}"`

**4. Failure handling.** `FAILED`, `ERROR`, and `NOT_FOUND` each get distinct error responses with actionable suggestions (e.g. FAILED often means malformed input → suggest checking sequence types match).

**5. Logging.**
- INFO: submission with job ID, status transitions (PENDING→RUNNING→FINISHED), completion with elapsed time
- DEBUG: every poll
- WARNING: cancellation failures, EBI 5xx during polling
- EBI jobs failing in production benefit hugely from clear logs.

#### Tests for the runner (must pass before either tool depends on it)

- Unit tests with `respx` mocking submit + sequence of status responses + result fetch
- **Test the timeout-with-cancellation path** (mock that always returns RUNNING → expect JobTimeoutError raised AND cancel endpoint called)
- Test cancellation failure during timeout (cancel endpoint returns 500 → still raise JobTimeoutError, log warning)
- Test the FAILED status path
- Test the NOT_FOUND status path (job ID expired or invalid)
- **Test jitter is applied** — patch `random.uniform` and assert it's called with `(0.8, 1.2)` per poll
- Test rate-limit semaphore is respected during polling (multiple concurrent jobs share the EBI semaphore)
- One integration test gated by `RUN_INTEGRATION=1` that runs an actual tiny Clustal job to validate the whole loop against real EBI

### 2. Build `bio_align_sequences` (spec §4.5)

After `EBIJobRunner` tests are green:

1. **Integration test first** — three short insulin orthologues (human P01308, mouse P01326, bovine P01317), assert the alignment is non-empty + has the expected 3 sequence IDs
2. **Unit test** with cached EBI Clustal response fixture
3. **Implement `tools/align_sequences.py`** per spec §4.5:
   - Accept the spec input schema (list of `SequenceRecord`, `sequence_type`, `output_format`)
   - Submit via `EBIJobRunner` with appropriate Clustal Omega params (sequence type → `stype`, etc.)
   - Default `result_type="aln-clustal_num"` for Clustal output, configurable for FASTA/MSF
   - Parse the result: alignment text + statistics (length, identity %, gap %)

#### Statistics calculation — local, not from EBI

EBI returns identity/gap stats inconsistently across services and versions. Calculate ourselves with explicit definitions documented in the output schema's field `description`:

- **Identity %** = columns where every non-gap residue is the same, as % of non-gap-only columns. Strict identity, not similarity.
- **Gap %** = columns containing at least one gap, as % of total alignment length.
- **Mean pairwise identity %** = average over all pairs of (identical non-gap positions / aligned non-gap positions). Useful supplement to strict identity for divergent sequences.

Surface all three so the caller can pick the metric appropriate to their question.

#### Output truncation

For large alignments (50+ sequences, or alignment text > 200 KB), reuse the truncation pattern from session 2 (the PDB / AlphaFold coordinate-cap convention). Do not invent a new pattern. If session 2's pattern lives in `utils/formatting.py`, extend it; if it's per-tool, factor out now while there are only two truncation sites.

#### "This is expected" documentation

In the tool's docstring AND output schema description, pre-document non-bug outputs:
- "Long stretches of gaps in divergent sequences are valid alignment output, not a tool error."
- "Identity % can be 0 for very divergent sequences (twilight zone homologues, ancient orthologues, convergent evolution cases)."

This prevents fresh agents from treating these as bugs the way session 2 needed to pre-document the insulin pLDDT case.

#### Output type

Return a **Pydantic model**, not a bare dict or string. Per session 2's memory entry on FastMCP scalar wrapping: dicts pass through bare, scalars get wrapped in `{"result": ...}`. Pydantic model = always serialised to dict = always bare. Make this consistent across both tools.

4. Verify both tests pass
5. Commit: `feat: bio_align_sequences via EBI Clustal Omega`

### 3. Build `bio_scan_domains` (spec §4.13)

1. **Integration test first** — use feline AIM A0A1E1GEY0's SRCR3 region (thematic continuity with the project's anchor case) and assert that expected Pfam SRCR signature is returned. Alternative: human insulin matches Pfam INSULIN — faster but less interesting.
2. **Unit test** with cached InterProScan response fixture
3. **InterProScan-specific notes:**
   - Service path: `iprscan5` (not `iprscan`)
   - Required params: `email`, `sequence`, `appl` (comma-separated database list — defaults to spec §4.13's `["Pfam", "SMART", "CDD"]`). Verify current EBI parameter names; some database names have version suffixes (`PfamA` vs `Pfam`).
   - Result type for structured output: `json` preferred, `tsv` fallback
   - Long sequences can take 5+ minutes — `timeout=600` per-tool default
   - Output JSON shape: `results[0].matches[]`, each match with signature info, locations, scores

4. **"This is expected" documentation** in docstring + output schema:
   - "Empty `matches` array is a valid result (no recognised domains found), not a tool error."
   - "Multiple overlapping matches from different databases (Pfam + SMART hitting the same region) are normal — different signatures, same biology."

5. **Output as Pydantic model**, same convention as Clustal tool.

6. **Output truncation** for highly-multi-domain proteins — same pattern as Clustal, reused not reinvented.

7. Verify tests
8. Commit: `feat: bio_scan_domains via EBI InterProScan`

### 4. Update `server.py` to register both new tools

- Add `bio_align_sequences` and `bio_scan_domains` to the tool registration block
- Both get standard annotations: `readOnly=true, destructive=false, openWorld=true, idempotent=true`
- Update the `instructions` parameter — these tools should be findable from the selection guide table embedded there
- Commit: `feat: register align_sequences and scan_domains in server`

### 5. Extend smoke test

Update `scripts/smoke_test_phase1a.py` (rename to `smoke_test_phase1ab.py` if you prefer for clarity) to also call both new tools with their integration test inputs. Confirm 6/6 live pass.

If renaming, update README accordingly.

Commit: `test: extend smoke test to cover async-job tools`

### 6. Session-end persistence

Persist to memory:

- The `EBIJobRunner` pattern + that it's reusable for future EBI services
- **Cancellation-on-timeout convention** — explain why orphan jobs matter and that this is enforced, not optional
- **Jitter rationale** — why deterministic backoff was rejected
- Per-tool timeout convention (300s default, 600s for compute-heavy)
- InterProScan parameter quirks (`appl` comma-separated, `iprscan5` not `iprscan`, JSON result type)
- Clustal Omega param mapping (sequence type → stype value, etc.)
- **Identity % calculation convention** — strict (all-same), not similarity-based; mean pairwise also returned
- **Output truncation pattern** — confirm whether session 2's pattern was reused or extended; document the canonical location
- **Pydantic-model output convention** — to keep FastMCP wrapping behaviour predictable
- "This is expected" documentation pattern — extend the precedent set by session 2's pLDDT case
- Any new spec errata discovered

## Specific things to watch for

### EBI parameter naming inconsistencies

EBI services have historically been inconsistent about parameter names across services. Confirm before submitting:
- Clustal: `sequence` (single multi-FASTA blob), `stype`, `outfmt`
- InterProScan: `sequence`, `appl`, `goterms`, `pathways`

Fetch the current EBI docs for each service if uncertain. The Job Dispatcher publishes per-service parameter pages.

### Multi-FASTA construction for Clustal

Clustal needs a single `sequence` parameter containing all sequences in FASTA format concatenated (`>id1\nSEQ\n>id2\nSEQ\n...`). Watch for:
- ID validation: EBI's parser is strict about FASTA IDs (no spaces, no special characters in some versions)
- Sequence cleaning: strip whitespace, validate against expected charset for `sequence_type`

### InterProScan databases default

Spec §4.13 default is `["Pfam", "SMART", "CDD"]`. EBI's `appl` param wants those passed comma-separated. Some database names have version suffixes EBI requires — check current docs.

### EBI availability

EBI publishes a status page. If integration tests fail at the network/availability level rather than the logic level, check that page before assuming our code is broken. Worth noting in any failure report.

## Deliverables checklist for session-end report

- [ ] `prompts/session-3-ebi-async.md` committed as first action
- [ ] `clients/ebi.py` with `EBIJobRunner` class — submission, polling-with-jitter-and-backoff, cancellation-on-timeout, fetch
- [ ] `tools/align_sequences.py` per spec §4.5 — Pydantic output, three-statistic identity calculation, truncation reused from session 2
- [ ] `tools/scan_domains.py` per spec §4.13 — Pydantic output, "empty matches valid" documented, truncation reused
- [ ] Fixtures under `tests/fixtures/` for both services (submit, status sequence including timeout case, result, cancellation)
- [ ] Unit tests: runner (incl. timeout+cancel, jitter, FAILED, NOT_FOUND), both tools (offline, mocked)
- [ ] Integration tests: runner + both tools (gated, real API)
- [ ] `server.py` registers both tools with annotations + selection-guide updates
- [ ] Smoke test extended, 6/6 live pass
- [ ] At least 5 conventional-commit feature commits with RED evidence in bodies (prompt + runner + 2 tools + server + smoke; some may combine sensibly)
- [ ] `pytest` green (offline + gated paths)
- [ ] In-process `fastmcp.Client` verification of both new tools
- [ ] Memory entries per section 6 above

## Procedural reminders (unchanged)

- British English in docstrings, README, comments
- TDD with RED evidence captured in commit bodies
- Stop and ask before deviating from spec §4.5 / §4.13
- Use `serena` for symbol-level navigation
- `superpowers:verification-before-completion` as commit gate
- `commit-commands:commit` for commit messages

## Pre-work report expected

Same shape as session 2 — list ambiguities, deviations, decisions before writing code. Wait for approval before implementing.

Particular questions worth raising in pre-work if relevant:

- Where session 2's truncation pattern lives (so we know whether to reuse, extend, or factor out)
- EBI Job Dispatcher endpoint base URL + cancellation endpoint behaviour as currently documented
- Whether to expose `poll_interval`, `max_interval`, and `timeout` as tool-level inputs (caller-configurable) or keep them internal
- Whether to use a single shared `EBIJobRunner` instance per service (instantiated at server startup) or per-call instantiation
- **Clustal partial-results on timeout: pre-answered — hard error, no partial.** A partial MSA is not a subset of the correct MSA; it's a different alignment entirely. Progressive alignment is a global optimisation, so column assignments mid-process don't reflect the final result. Returning a partial alignment with `incomplete: true` would imply "the rest will look similar" which is false. Timeout = `JobTimeoutError`, no exceptions.
- **InterProScan partial-results on timeout: open question.** Different from Clustal because each protein-domain database (Pfam, SMART, CDD, etc.) scans independently. Matches from completed databases are real, complete, correct matches — just not the full set. A response shape like `{matches: [...], incomplete: true, databases_completed: ["Pfam", "SMART"], databases_pending: ["CDD"]}` is honestly informative.

  Pre-work investigation, in this order — stop at the first path that produces honest, verifiable output:

  1. **Does EBI's InterProScan API expose per-database completion status during a running job?** Check `/status/{jobId}`, `/resulttypes/{jobId}`, and any service-specific status endpoint while a job is `RUNNING`. If yes → partial-on-timeout is straightforwardly implementable using that data; do it.
  2. **Middle path: does fetching a result type (e.g. TSV) during a `RUNNING` job return partial output that's parseable and honestly partial?** Probe by submitting a multi-database InterProScan job, then in parallel poll `/result/{jobId}/tsv` repeatedly during the `RUNNING` window. Verify carefully — distinguish between:
     - 404 / 4xx until complete (no streaming, fall through)
     - 200 OK with empty body (no streaming, fall through)
     - 200 OK with stale buffered output that doesn't actually update (looks like streaming but isn't, fall through)
     - 200 OK with output that genuinely grows as databases complete (real streaming, usable)

     If real streaming exists: implement partial-on-timeout, but mandate a runtime sanity check on every partial result — "does the partial we got back actually parse as valid TSV with at least one match, and does the match count exceed the previous poll's count?" If the sanity check fails at runtime, treat as hard error rather than returning misleading partial data.
  3. **Fall back: hard error on timeout.** If neither (1) nor (2) produces honestly-populatable per-database status, raise `JobTimeoutError` same as Clustal. Do not invent or guess `databases_completed` — the project's anti-hallucination principle applies recursively to our own outputs, not just to upstream data.

  Document the probe results in pre-work report and the chosen path in memory. If EBI behaviour changes in future, the probe procedure stays valid as the re-verification method.

---

Start with pre-work discovery. Wait for approval before coding.
