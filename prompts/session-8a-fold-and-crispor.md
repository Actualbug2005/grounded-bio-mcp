# Session 8a — Audit, `bio_fold_sequence` registration, and `bio_design_grna` (CRISPOR) implementation

> **Scope:** Local development machine only. Audit unregistered tools, register `bio_fold_sequence` (the Session 7 audit gap), implement `bio_design_grna` against CRISPOR with one test genome (felCat9). LXC deployment + remaining genome indexes are Session 8b. Rename to `grounded-bio-mcp` is Session 8.5.
>
> **Spec reference:** `bioinformatics-mcp-spec.md` v2 §4.7 (CRISPOR), §4.8 (ViennaRNA). Spec v3.0 supersedes after Session 8.5; this session uses v2 because the rename happens between 8a and 8b.
>
> **Pre-approval decisions baked in below.** Pre-work report still expected; pre-work approval covers scope-level decisions. Runtime safety gates (download approval) are still per-egress, per the Session 7 pattern with Kazusa.

---

## Pre-approval decisions (scope-level, no further confirmation needed)

| # | Decision | Rationale |
|---|---|---|
| 1 | Session 8 splits into 8a (this) + 8b (deployment) | Session 8 as originally scoped is three risk categories in one — implementation, infrastructure, validation. Splitting bounds each session. |
| 2 | Audit existing-but-unregistered tools as first action | The `bio_fold_sequence` gap surfaced because Session 7 counted tools at the end. Audit-first surfaces any other gaps before adding new work. |
| 3 | Register `bio_fold_sequence` before starting CRISPOR work | Lower-risk task; gets the tool count to 18 cleanly; smoke test extends to 18 tools. CRISPOR work begins on a clean baseline. |
| 4 | Implement CRISPOR with one test genome (felCat9) only | felCat9 is smallest (~1 GB); validates the CRISPOR subprocess + genome-index plumbing without committing to all three downloads on dev machine. hg38 + mm39 land on the LXC in 8b (with download-gate per fetch). |
| 5 | felCat9 genome download approval is per-egress, not pre-approved | Session 7 pattern. Surface URL + size + target path in the work; wait for explicit user approval before fetch. |
| 6 | If CRISPOR Python venv compatibility forces Python 3.11 on a 3.13-default system, install separate venv at `/opt/crispor/venv` (or local equivalent on dev machine); document the version split in the tool's docstring | CRISPOR's age means 3.13 compatibility is uncertain; allow falling back without redoing the work |
| 7 | ViennaRNA invocation: subprocess (`RNAfold`) preferred over Python module to keep Apache-2.0 / GPL boundary clean | See spec v3 §11.4. If subprocess approach has unacceptable latency or interface issues, use Python module and clearly mark the GPL-touching boundary. Document final choice in the registration commit. |
| 8 | `bio_design_grna` defaults: `pam=NGG`, `genome=felCat9` for this session's tests, `score_method=cfd` (CFD off-target scoring), `output_top_n=10` | Sensible defaults for the most common SpCas9 case |

---

## Scope

### A. Audit (first action; ~30 minutes)

Walk the codebase and verify every implementation file under `src/bioinformatics_mcp/tools/` corresponds to a registration in `server.py`. The Session 7 gap was `bio_fold_sequence` — implementation existed, registration didn't. Look for any other such gaps.

For each tool found in `tools/` directory:

1. Confirm tool has a registration block in `server.py` (the `@mcp.tool(...)` decorator + selection-guide entry)
2. Confirm tool appears in smoke test (`scripts/smoke_test_phase1a.py`)
3. Confirm tool has unit tests in `tests/`
4. Confirm tool's annotations match documented overrides

Produce an audit report (committed as `docs/audit_session_8a.md`) listing every tool with status across the four checks. If gaps exist beyond the known `bio_fold_sequence` case, document them and address them in this session's scope (subject to time).

**Commit:** `docs: session 8a tool registration audit`

### B. `bio_fold_sequence` registration (~half day)

The implementation exists (per Session 7 audit). Tasks:

1. Read the existing implementation; confirm it matches v2 §4.8 spec.
2. Verify ViennaRNA installation pattern — subprocess (`RNAfold`) preferred per pre-approval decision 7.
3. Add unit tests covering: deterministic mode (fixed temperature), constraint folding (if implemented), output schema, error handling for invalid input.
4. Add integration test (gated `RUN_INTEGRATION=1`) — fold a known sequence and verify against expected ViennaRNA output (e.g. tRNA-Phe yeast — CCCAGGCUUAUACUGCUUUGAUGCAGCUAGGCC... — well-characterised cloverleaf with predictable MFE).
5. Add to smoke test — `bio_fold_sequence` should be the 18th smoke-tested tool.
6. Register in `server.py` with selection-guide entry: "RNA secondary structure prediction → `bio_fold_sequence`. Inputs RNA sequence; returns dot-bracket notation, MFE, structure-image URL where applicable. Local computation; no external API."
7. Update `README.md` tool-count from 17 to 18.

**Commits expected:**

1. `test(fold_sequence): RED — initial test asserts ViennaRNA wrapper output schema` (with AssertionError text in body)
2. `feat(fold_sequence): subprocess wrapper for RNAfold` (or Python-module if final choice)
3. `test(fold_sequence): integration test against tRNA-Phe`
4. `feat(server): register bio_fold_sequence (18/18 tools live, plus CRISPOR pending)`
5. `test(smoke): extend smoke to 18 tools — fold_sequence`
6. `docs(README): tool count 17 → 18`

### C. CRISPOR implementation — `bio_design_grna` (~1.5-2 days)

This is the heaviest tool in the spec. Reference v2 §4.7.

**Sub-tasks:**

#### C.1 CRISPOR install on dev machine

1. Clone CRISPOR: `git clone https://github.com/maximilianh/crisporWebsite /opt/crispor` (or local equivalent — confirm path with user during pre-work)
2. Install system dependencies via apt/brew: `bwa` (BWA aligner — required for CRISPOR off-target scanning)
3. Create CRISPOR venv: `python3.11 -m venv /opt/crispor/venv` (Python 3.11 if 3.13 incompatible)
4. Install CRISPOR Python requirements: `cd /opt/crispor && /opt/crispor/venv/bin/pip install -r requirements.txt`
5. Verify `crispor.py --help` works

Document install steps in `docs/crispor_install.md` for replication on the LXC in Session 8b.

#### C.2 felCat9 genome index download — **DOWNLOAD GATE**

Surface for user approval **before fetching:**

- Genome: felCat9 (cat reference assembly)
- Source URL: `http://crispor.tefor.net/genomes/felCat9.tar.gz` (or equivalent — verify exact URL with user, may have moved)
- Expected size: ~1 GB compressed
- Target path: `/var/lib/bioinformatics_mcp/genomes/felCat9/` on dev machine
- Index files post-extraction: `~3 GB`

Wait for explicit user approval (not the Session 7 conversation-level approval — the runtime gate). Once approved:

1. Fetch with progress reporting (`curl -L --progress-bar` or equivalent)
2. Verify checksum if upstream publishes one; otherwise document file size + SHA256 of downloaded archive in provenance JSON
3. Extract to target path
4. Verify CRISPOR can find the index: `crispor.py felCat9 ...` should not error on missing index

#### C.3 Tool implementation

Per v2 §4.7:

**Inputs:**

```python
{
  "genome": str,           # "felCat9", "hg38", "mm39", etc.
  "target_sequence": str,  # genomic sequence containing the target site
                           # OR genomic coordinates "felCat9:chrA1:120000-120023"
  "pam": str = "NGG",      # SpCas9 default; "NAG", "TTTV" (Cpf1) supported
  "score_method": str = "cfd",  # "cfd" | "mit" | "both"
  "output_top_n": int = 10,
  "max_off_target_mismatches": int = 4,
}
```

**Output:**

```python
{
  "guides": [
    {
      "sequence": "GCAGGCATGTACGTACGTAC",
      "pam": "AGG",
      "position": "felCat9:chrA1:120010",
      "strand": "+",
      "specificity_score": 78,        # MIT score
      "cfd_specificity": 0.91,
      "efficiency_scores": {
          "doench16": 65,
          "moreno_mateos": 58,
          ...
      },
      "off_targets": [
          {"chromosome": "chrA1", "position": 5840000,
           "sequence": "GCAaGCATGgACGTACGTAC", "mismatches": 2,
           "cfd_score": 0.31},
          ...
      ],
      "off_target_summary": {"0_mm": 1, "1_mm": 0, "2_mm": 3,
                             "3_mm": 12, "4_mm": 47}
    },
    ...
  ],
  "candidate_guides_count": 27,
  "returned_guides_count": 10,
  "provenance": {
    "source": "CRISPOR",
    "fetched_at": "...",
    "tool_version": "...",
    "crispor_version": "...",
    "genome": "felCat9",
    "genome_index_date": "...",
    "url": "https://crispor.tefor.net/"
  },
  "confidence": {
    "level": "high",
    "basis": "real off-target scan against indexed genome",
    "interpretation": "off-target table is exhaustive within mismatch tolerance; "
                      "guides with CFD specificity >0.8 are typically suitable for "
                      "experimental use; verify in your context"
  }
}
```

**Implementation pattern:**

1. Subprocess invocation: `subprocess.run([crispor.py, genome, sequence, ...], capture_output=True, timeout=300)`
2. Parse CRISPOR's tab-separated output (CRISPOR has stable output format; verify against current version)
3. Sort by specificity, return top N
4. Handle failure modes:
   - CRISPOR not installed → clear error
   - Genome index missing → clear error directing to install
   - Subprocess timeout (default 300 s) → clear timeout error
   - Empty result (no guides found in target sequence) → empty `guides` array, not error
5. Truncation: if `off_targets` for a guide exceeds 100 entries, truncate with `off_targets_truncated: true` and `total_off_targets: N`

**Tests:**

- Unit: mock subprocess, verify input parsing, output shape, error handling
- Integration (`RUN_INTEGRATION=1`): real CRISPOR call against felCat9 with a known-good test sequence; assert guide count > 0 and provenance is populated
- Smoke: simple felCat9 design at a known target locus

**Commits expected:**

1. `docs(crispor): install steps for dev machine + felCat9 index`
2. `feat(clients): CRISPOR subprocess client with timeout + index-missing detection`
3. `test(design_grna): RED — initial test asserts CRISPOR wrapper output schema`
4. `feat(design_grna): subprocess invocation, output parsing, top-N sort`
5. `feat(design_grna): off-target truncation + provenance population`
6. `test(design_grna): integration test against felCat9 known-target locus`
7. `feat(server): register bio_design_grna (19/19 tools live)`
8. `test(smoke): extend smoke to 19 tools — design_grna`
9. `docs(README): tool count 18 → 19; CRISPOR install pointer`

### D. Pre-rename housekeeping

The rename to `grounded-bio-mcp` happens in Session 8.5. To make that session as mechanical as possible:

1. Identify every file that contains `bioinformatics_mcp` or `bioinformatics-mcp` strings — produce a rename-target list as `docs/rename_targets.md`. This pre-survey makes Session 8.5 a one-shot atomic operation rather than archaeology under time pressure.
2. Note any external references (claude.ai connector configuration, bookmarks, etc.) that will need updating post-rename. The user will handle these; this is a heads-up document.

**Commit:** `docs: rename target survey for Session 8.5 (bioinformatics-mcp → grounded-bio-mcp)`

---

## Out of scope (deferred to Session 8b)

- Deployment to LXC on pve2
- hg38 + mm39 genome index downloads
- Caddy reverse proxy + bearer auth
- systemd service
- Evaluation harness (10 Q/A pairs per spec §10.4)
- claude.ai connector configuration

---

## Pre-work report expected

In the established style:

1. CRISPOR install verification on dev machine (does the existing approach work? any blockers?)
2. ViennaRNA subprocess vs Python module — recommendation with rationale
3. felCat9 genome URL verification (URL still valid? size correct?)
4. CRISPOR output format verification (any breaking changes from spec assumption? probe with `crispor.py --help` and a sample run if practical)
5. Audit findings — any registration gaps beyond `bio_fold_sequence`?
6. Any other spec errata noticed during pre-work

User welcomes correction; if something in this prompt is wrong, surface it before starting work.

---

## Acceptance criteria

- [ ] Audit report committed; gaps (if any beyond `bio_fold_sequence`) addressed or scoped to follow-up
- [ ] `bio_fold_sequence` registered, tested, smoke green
- [ ] `bio_design_grna` implemented, registered, tested against felCat9, smoke green
- [ ] `pytest` all green (offline)
- [ ] `RUN_INTEGRATION=1 pytest` all green (live APIs + CRISPOR + felCat9)
- [ ] Smoke test 19/19
- [ ] README tool-count updated; rename-target survey committed
- [ ] Per-tool memory entries added (CRISPOR pattern, ViennaRNA pattern, audit-first discipline)
- [ ] No regressions in any previously-live tool

---

## Notes for session 8b prep

The two genome downloads still pending (hg38 ~3.2 GB, mm39 ~2.8 GB) hit the same download-gate pattern and should be pre-listed in the 8b prompt with URLs + sizes + target LXC paths so the gate has the context it needs.

The CRISPOR install steps documented in `docs/crispor_install.md` will be replicated on the LXC. Any dev-machine-specific paths (homebrew on macOS, etc.) will need translation to the Debian Trixie LXC environment.
