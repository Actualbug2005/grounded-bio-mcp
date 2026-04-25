# Session 8.5 — Atomic rename: `bioinformatics-mcp` → `grounded-bio-mcp` + version bump 0.2.0 → 0.3.0

> **Scope:** Mechanical rename, no functional changes. Single session, single atomic commit-set. Smoke test green before *and* after.
>
> **Pre-requisite:** Session 8a complete; `docs/rename_targets.md` exists with full target list. 19/19 smoke green on the pre-rename codebase.
>
> **Spec reference:** v3.0 §0 (rename rationale), §11.1 (Apache-2.0 licence — adopted at this session if not already in place).

---

## Pre-approval decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Single session, atomic commit-set | Avoid half-renamed states; minimise time the codebase has dual identity. |
| 2 | Repository rename on the platform first; local clone re-targets to new URL | Keeps git history intact. GitHub/forge handles the redirect. |
| 3 | Python package rename: `bioinformatics_mcp` → `grounded_bio_mcp` | Underscores per PEP 8; matches project rename. All imports update. |
| 4 | Distribution name: `grounded-bio-mcp` (hyphenated, PyPI-style) | Matches project name; underscores are package-only. |
| 5 | Version bump 0.2.0 → 0.3.0 in same commit-set | Signals identity shift + scope expansion (Phase 4 specced). 1.0.0 deferred until soak + Phase 4 deliver. |
| 6 | License file: confirm Apache-2.0 in place; add if not | Per v3 §11.1. May already be present; verify in pre-work. |
| 7 | Smoke test runs before rename (baseline) and after rename (verification) | Atomicity validation. |
| 8 | No functional changes in this session | Pure rename; any bug fixes / improvements deferred to subsequent sessions. |

---

## Pre-work checklist

Before starting:

1. Confirm Session 8a complete: 19/19 smoke green
2. Confirm `docs/rename_targets.md` exists and is current
3. Run smoke test once on `main` and capture pass output as the baseline
4. Confirm GitHub/forge repository renames work cleanly with active branches
5. Identify external references that need post-rename updating (claude.ai connector — will need URL update if connected via current name; user-side action)

---

## Rename targets (verify against `docs/rename_targets.md` from Session 8a)

### Mechanical rename (find-and-replace)

| Token | New value | Notes |
|---|---|---|
| `bioinformatics_mcp` | `grounded_bio_mcp` | Python package directory + imports |
| `bioinformatics-mcp` | `grounded-bio-mcp` | Distribution name, repo name, hyphenated refs |
| `Bioinformatics MCP` | `grounded-bio-mcp` | Title-case references in README, docstrings — verify case-by-case (some may stay descriptive) |
| `BIOINFORMATICS_MCP` | `GROUNDED_BIO_MCP` | Env var prefixes if any |

### Files to touch

- `pyproject.toml` — `name`, `version`, package paths, scripts entries
- `src/bioinformatics_mcp/` → `src/grounded_bio_mcp/` — directory rename + every `.py` file's imports
- `tests/` — every test file's imports
- `scripts/` — fetch_codon_tables.py, probe_*.py, smoke_test_phase1a.py — imports + path references
- `README.md` — title, install commands, imports in examples, repo URL
- `LICENSE` — verify Apache-2.0; replace if currently MIT or absent
- `NOTICE` — create if absent; enumerate third-party components per v3 §11.1
- `CITATION.cff` — create per v3 §11.2 with project name + version + author + ORCID (if user has one)
- `.env.example` — variable prefixes if changing
- `bioinformatics-mcp-spec.md` — preserve for archival; commit unchanged but rename to `bioinformatics-mcp-spec-v2-archive.md`
- `grounded-bio-mcp-spec.md` (v3.0) — land in this session as the new canonical spec
- `prompts/session-*.md` — historical sessions stay named `session-N-{topic}.md`; their *content* uses old name (historical accuracy); only the active prompt file `session-8.5-rename.md` updates
- Memory entries in `~/.claude/projects/.../memory/` — update index + create one new entry documenting the rename

### Files to leave alone

- Git history (don't rebase or rewrite)
- Old issue / PR references (link rot is fine for completed work)
- v2 spec file (preserved with `-v2-archive` suffix per above)

---

## Execution order

1. **Baseline** — smoke 19/19 green; capture output to `docs/rename_smoke_baseline.txt`
2. **Branch** — `git checkout -b session-8.5-rename`
3. **Forge rename** — rename repository on GitHub/forge from `bioinformatics-mcp` to `grounded-bio-mcp`; re-point local remote: `git remote set-url origin git@github.com:USER/grounded-bio-mcp.git`
4. **Package directory rename** — `git mv src/bioinformatics_mcp src/grounded_bio_mcp`
5. **Bulk find-and-replace** — across all tracked files except `bioinformatics-mcp-spec.md` (which gets archived separately):
   ```bash
   git ls-files | grep -v 'bioinformatics-mcp-spec.md' | xargs -I{} sed -i.bak \
     -e 's/bioinformatics_mcp/grounded_bio_mcp/g' \
     -e 's/bioinformatics-mcp/grounded-bio-mcp/g' \
     {} ; find . -name '*.bak' -delete
   ```
   Manual review of changes before commit; some title-case references may need staying-as-is (descriptive text vs project-name).
6. **Archive v2 spec** — `git mv bioinformatics-mcp-spec.md bioinformatics-mcp-spec-v2-archive.md`
7. **Add v3 spec** — `cp /path/to/grounded-bio-mcp-spec.md grounded-bio-mcp-spec.md` (the v3 from this conversation), `git add`
8. **Update pyproject.toml** — name + version
9. **Add LICENSE / NOTICE / CITATION.cff** if absent
10. **Run smoke test** — must be 19/19 green again
11. **Run full test suite** — `pytest` and `RUN_INTEGRATION=1 pytest` both green
12. **Commit** as the atomic commit-set (see below)
13. **Push branch + open PR / merge to main** — depending on user's workflow preference
14. **Tag release** — `git tag v0.3.0 && git push --tags`
15. **Memory entry** — `project_rename_grounded_bio_mcp.md` documenting rename rationale, version-bump reasoning, archived v2 spec location

---

## Commit-set

Atomic, in order:

1. `chore: archive v2 spec as bioinformatics-mcp-spec-v2-archive.md`
2. `chore!: rename package bioinformatics_mcp → grounded_bio_mcp` (the `!` denotes breaking change in conventional commits)
3. `chore!: rename distribution bioinformatics-mcp → grounded-bio-mcp`
4. `docs: add grounded-bio-mcp spec v3.0`
5. `docs: add LICENSE (Apache-2.0), NOTICE, CITATION.cff` (if any are net-new)
6. `chore: bump version 0.2.0 → 0.3.0`

The `!` markers are important — pip / dependency tooling treats this as a breaking change, which it is.

---

## Acceptance criteria

- [ ] Repository renamed on forge; `git remote -v` shows new URL
- [ ] Package directory `src/grounded_bio_mcp/` exists; old path absent
- [ ] All imports updated; `python -c "import grounded_bio_mcp"` succeeds
- [ ] All imports of old name absent: `git grep -i 'bioinformatics_mcp\|bioinformatics-mcp'` returns only the v2-archive spec file (which is intentionally preserved verbatim)
- [ ] `pyproject.toml` name = `grounded-bio-mcp`, version = `0.3.0`
- [ ] LICENSE = Apache-2.0; NOTICE present; CITATION.cff present
- [ ] v3 spec landed at `grounded-bio-mcp-spec.md`
- [ ] v2 spec preserved at `bioinformatics-mcp-spec-v2-archive.md`
- [ ] Smoke test 19/19 green
- [ ] `pytest` + `RUN_INTEGRATION=1 pytest` all green
- [ ] `git tag v0.3.0` pushed
- [ ] Memory entry committed
- [ ] User notified of post-rename actions: claude.ai connector update (Session 8b will reconfigure when LXC deploys), local clone re-clone or `git remote set-url`

---

## Out of scope

- Deployment (Session 8b)
- Any functional changes / new tools / bug fixes
- mkdocs site setup (later session, post-Phase 4 acceptance)
- BioContextAI registry submission (Session 15+)

---

## Notes

If the bulk find-and-replace produces unexpected matches (e.g. `bioinformatics_mcp` appears in a comment that should stay descriptive rather than being a code reference), surface those for case-by-case decision rather than blanket-replacing. The intent is rename of the *project identity*, not blanket-replacement of every English-language reference to "bioinformatics".

If the smoke test fails post-rename, **stop and diagnose** before committing. The rename is atomic by design; a half-broken rename merged to main is harder to recover from than a paused session.
