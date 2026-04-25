# Rename target survey — `bioinformatics-mcp` → `grounded-bio-mcp`

> **Purpose:** Pre-rename inventory so Session 8.5 is one-shot mechanical rather than archaeology under time pressure. Captures every file that contains the old name strings, classified into "rename verbatim", "review case-by-case", and "leave alone" so the bulk find-and-replace + manual passes both have a known target list.
>
> **Survey date:** 2026-04-25 (end of Session 8a; commit `1f08813` baseline). Re-run before Session 8.5 starts to catch any drift.
>
> **Counts:** 78 tracked files contain at least one of `bioinformatics_mcp`, `bioinformatics-mcp`, or `Bioinformatics MCP`.

## Conclusion first

**Bulk find-replace covers ~63 files** (all of `src/`, `tests/`, plus `pyproject.toml`, `README.md`, `.env.example`, `.gitignore`, `project-handoff.md`, the three new docs from this session, and `scripts/*.py` + `scripts/smoke_test_phase1a.py`). One-pass `sed` works because every occurrence in those files is a code reference (Python imports, distribution-name strings, config keys) rather than descriptive prose.

**~9 historical-prompt files stay verbatim.** `prompts/session-2-prompt.md` through `prompts/session-7-codon-blast.md` are session-specific design records; their content is historically accurate at the time those sessions ran, and rewriting them after the fact would falsify the project audit trail. The rename document for 8.5 itself (`prompts/session-8.5-rename.md`) is the only prompt whose content updates.

**1 file gets archived verbatim, not renamed:** `bioinformatics-mcp-spec.md` → `bioinformatics-mcp-spec-v2-archive.md` via `git mv` only — its content is the v2 spec, frozen for posterity. The v3 spec (`grounded-bio-mcp-spec.md`) lands net-new in 8.5.

**A handful of `Bioinformatics`-only (no `MCP` suffix) banner strings** need case-by-case review — see "Manual review" below.

## Bulk find-replace targets

`sed` substitutions to apply across these files in 8.5:

```
s/bioinformatics_mcp/grounded_bio_mcp/g
s/bioinformatics-mcp/grounded-bio-mcp/g
```

The two patterns don't collide (underscores vs hyphens are mutually exclusive at any one position).

### Source code (Python package — directory rename + every import + every docstring reference)

```
src/bioinformatics_mcp/__init__.py
src/bioinformatics_mcp/config.py
src/bioinformatics_mcp/server.py
src/bioinformatics_mcp/clients/alphafold.py
src/bioinformatics_mcp/clients/base.py
src/bioinformatics_mcp/clients/chembl.py
src/bioinformatics_mcp/clients/crispor.py
src/bioinformatics_mcp/clients/ebi.py
src/bioinformatics_mcp/clients/ensembl.py
src/bioinformatics_mcp/clients/europepmc.py
src/bioinformatics_mcp/clients/ncbi.py
src/bioinformatics_mcp/clients/pubchem.py
src/bioinformatics_mcp/clients/rcsb.py
src/bioinformatics_mcp/clients/reactome.py
src/bioinformatics_mcp/clients/string_db.py
src/bioinformatics_mcp/clients/uniprot.py
src/bioinformatics_mcp/tools/align_sequences.py
src/bioinformatics_mcp/tools/blast_search.py
src/bioinformatics_mcp/tools/codon_optimise.py
src/bioinformatics_mcp/tools/design_grna.py
src/bioinformatics_mcp/tools/fetch_alphafold.py
src/bioinformatics_mcp/tools/fetch_bioactivity.py
src/bioinformatics_mcp/tools/fetch_compound.py
src/bioinformatics_mcp/tools/fetch_gene.py
src/bioinformatics_mcp/tools/fetch_interactions.py
src/bioinformatics_mcp/tools/fetch_paper_fulltext.py
src/bioinformatics_mcp/tools/fetch_pathway.py
src/bioinformatics_mcp/tools/fetch_pdb.py
src/bioinformatics_mcp/tools/fetch_sequence.py
src/bioinformatics_mcp/tools/fetch_uniprot.py
src/bioinformatics_mcp/tools/fetch_variant.py
src/bioinformatics_mcp/tools/fold_sequence.py
src/bioinformatics_mcp/tools/predict_variant_effect.py
src/bioinformatics_mcp/tools/scan_domains.py
src/bioinformatics_mcp/tools/search_literature.py
```

The directory itself is moved with `git mv src/bioinformatics_mcp src/grounded_bio_mcp` *before* the find-replace — the imports inside the files then update consistently in the bulk pass.

### Tests (every import; one path reference per test file)

```
tests/test_clients/test_chembl.py
tests/test_clients/test_ebi.py
tests/test_clients/test_ensembl.py
tests/test_clients/test_europepmc.py
tests/test_clients/test_pubchem.py
tests/test_clients/test_reactome.py
tests/test_clients/test_string_db.py
tests/test_tools/test_align_sequences.py
tests/test_tools/test_blast_search.py
tests/test_tools/test_codon_optimise.py
tests/test_tools/test_design_grna.py
tests/test_tools/test_fetch_alphafold.py
tests/test_tools/test_fetch_bioactivity.py
tests/test_tools/test_fetch_compound.py
tests/test_tools/test_fetch_gene.py
tests/test_tools/test_fetch_interactions.py
tests/test_tools/test_fetch_paper_fulltext.py
tests/test_tools/test_fetch_pathway.py
tests/test_tools/test_fetch_pdb.py
tests/test_tools/test_fetch_sequence.py
tests/test_tools/test_fetch_uniprot.py
tests/test_tools/test_fetch_variant.py
tests/test_tools/test_fold_sequence.py
tests/test_tools/test_predict_variant_effect.py
tests/test_tools/test_scan_domains.py
tests/test_tools/test_search_literature.py
tests/test_utils/test_formatting.py
tests/test_utils/test_rate_limit.py
```

### Scripts (imports + path references)

```
scripts/fetch_codon_tables.py
scripts/probe_clustal_outfmts.py
scripts/probe_ebi_resulttypes.py
scripts/probe_iprscan_partial.py
scripts/smoke_test_phase1a.py
```

`scripts/fetch_genome.sh` (Session 8a addition) doesn't reference the package name — uses `CRISPOR_PATH` / `GENOME_DIR` env vars only — and stays unchanged through 8.5.

### Top-level config + docs

```
.env.example
.gitignore
README.md
project-handoff.md
pyproject.toml
docs/audit_session_8a.md
docs/crispor_install.md
docs/crispor_output_format.md
docs/rename_targets.md     ← this file
```

`pyproject.toml` needs three structural changes beyond the bulk find-replace:

- `[project]` → `name = "grounded-bio-mcp"`
- `[project.scripts]` → `grounded-bio-mcp = "grounded_bio_mcp.server:main"` (the bulk replace handles the value but the key changes too)
- `[tool.hatch.build.targets.wheel]` → `packages = ["src/grounded_bio_mcp"]`

Plus the version bump from `0.2.0` → `0.3.0` per the 8.5 plan.

## Files to leave alone

### Historical prompts (preserved verbatim — historical accuracy)

```
prompts/session-2-prompt.md
prompts/session-3-ebi-async.md
prompts/session-4-compounds.md
prompts/session-5-variants-gene.md
prompts/session-6-knowledge-bases.md
prompts/session-7-codon-blast.md
prompts/session-8a-fold-and-crispor.md
prompts/session-8b-deployment.md
```

These document what each session was about *at the time it ran*. Editing them after the fact would falsify the audit trail. Future readers expect to see the project's identity drift over time in these files. The 8.5 rename rationale will be captured in a new `prompts/session-8.5-rename.md` content update + a memory entry, not by retroactively editing earlier prompts.

`prompts/session-8.5-rename.md` itself **is** updated as part of 8.5 — it's the active prompt for that session, content updates per its own commit.

`prompts/session-8a-fold-and-crispor.md` and `prompts/session-8b-deployment.md`: these were written *before* the rename happens. They reference the old name describing what 8a / 8b will do under that name. After the rename they will describe past work that ran under the old name (8a) and future work that will run under the new name (8b). For 8b specifically, the deployment-step text uses the new name in a few places (e.g. `grounded-bio-mcp.devlin.lan`); the rest stays verbatim as historical record.

### Spec archive

```
bioinformatics-mcp-spec.md   →   bioinformatics-mcp-spec-v2-archive.md   (git mv only; content unchanged)
```

The v2 spec is the historical artefact that the project was built against. It stays preserved because the `acceptance criteria` line in 8.5 explicitly checks that `git grep` for `bioinformatics_mcp` or `bioinformatics-mcp` returns only this archived file.

## Manual review — `Bioinformatics`-only banner strings

Three locations use `Bioinformatics` (no `MCP` suffix) in title-case prose. These need case-by-case judgement during 8.5 because the rename target depends on context:

| Location | Current text | Suggested replacement |
|---|---|---|
| `src/bioinformatics_mcp/__init__.py` (module docstring) | `"""Bioinformatics primary-source MCP server.` | `"""grounded-bio-mcp — primary-source bioinformatics MCP server.` |
| `src/bioinformatics_mcp/server.py` `SERVER_INSTRUCTIONS` banner | `Bioinformatics Primary-Source MCP Server` | `grounded-bio-mcp — Primary-Source Bioinformatics MCP Server` |
| `bioinformatics-mcp-spec.md` `# Bioinformatics Primary-Source MCP Server — Specification v2` | (stays verbatim — archive) | (no change) |

The first two are the user-visible banner strings; the spec stays as the archived artefact.

## Verification commands for after the rename

After the bulk replace + manual review pass, these should produce the listed results:

```bash
# Should return only the archived spec:
git grep -lE 'bioinformatics_mcp|bioinformatics-mcp'

# Should return zero matches:
git grep -lE 'BIOINFORMATICS_MCP'

# Imports should round-trip:
python -c "import grounded_bio_mcp"

# Tests should still all pass:
pytest

# Smoke should still surface 19 tools live (loud-skip on bio_design_grna stays):
python scripts/smoke_test_phase1a.py   # rename to scripts/smoke_test.py optional
```

## Out-of-tree changes the user handles separately

Beyond the in-tree rename, post-rename external actions land on the user's plate (the rename prompt notes this):

- **Repository rename on GitHub/forge** before the local `git remote set-url`. The forge handles redirects so existing clones don't break immediately, but new clones use the new URL.
- **claude.ai connector** — when the LXC re-deploys in 8b under the new hostname, the existing connector's URL becomes stale. User updates it via Settings → Connectors.
- **Local clone re-target** — `git remote set-url origin git@github.com:USER/grounded-bio-mcp.git` after the forge rename.
