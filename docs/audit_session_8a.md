# Tool registration audit — Session 8a

> **Purpose:** Walk every implementation file under `src/bioinformatics_mcp/tools/` and confirm round-trip across registration, smoke test, unit tests, and annotation overrides. Discipline established because the Session 7 closing audit reported `bio_fold_sequence` as "implemented but not registered" — that report was inaccurate (the file is a stub, not an implementation), and the audit-first action of Session 8a exists so similar misclassifications surface before new work compounds them.
>
> **Scope:** `src/bioinformatics_mcp/tools/*.py` as of commit `b339399` (Session 8a prompt landed). Excludes `__init__.py`.
>
> **Audit date:** 2026-04-25.

## Conclusion

**17 tools fully round-trip; 2 tools are stub-only.** The two stubs (`bio_fold_sequence`, `bio_design_grna`) are 14-line files containing only a module docstring with a TODO note — no callable function, no signature, no implementation. Both land as Session 8a deliverables.

**No further gaps.** Every other tool has a registration block in `server.py`, a smoke-test entry in `scripts/smoke_test_phase1a.py`, a dedicated unit-test file under `tests/test_tools/`, and annotation overrides matching the README "Deviations from spec" table.

## Method

For each `.py` file in `src/bioinformatics_mcp/tools/` (excluding `__init__.py`):

1. **Registration:** confirm a `@mcp.tool(...)` decorator block exists in `server.py` referencing the tool's implementation function.
2. **Smoke:** confirm an entry exists in either the inline `cases` list (in `run()`) or `_build_cases()` (in `scripts/smoke_test_phase1a.py`).
3. **Unit tests:** confirm a matching `tests/test_tools/test_<name>.py` file exists.
4. **Annotation overrides:** confirm any `idempotentHint=False` or `openWorldHint=False` exception matches the documented set in [README.md](../README.md) under "Deviations from spec".

Stub files (no implementation) fail check (1) by definition; checks (2)–(4) are vacuous for them.

## Per-tool status

| # | Tool file | Registered | Smoke | Unit tests | Annotation override | Status |
|---|---|---|---|---|---|---|
| 1 | [align_sequences.py](../src/bioinformatics_mcp/tools/align_sequences.py) | yes | yes | [test_align_sequences.py](../tests/test_tools/test_align_sequences.py) | none | ✓ |
| 2 | [blast_search.py](../src/bioinformatics_mcp/tools/blast_search.py) | yes | yes | [test_blast_search.py](../tests/test_tools/test_blast_search.py) | `idempotentHint=False` | ✓ |
| 3 | [codon_optimise.py](../src/bioinformatics_mcp/tools/codon_optimise.py) | yes | yes | [test_codon_optimise.py](../tests/test_tools/test_codon_optimise.py) | `openWorldHint=False` | ✓ |
| 4 | [design_grna.py](../src/bioinformatics_mcp/tools/design_grna.py) | **no** | **no** | **absent** | — | **stub only** |
| 5 | [fetch_alphafold.py](../src/bioinformatics_mcp/tools/fetch_alphafold.py) | yes | yes | [test_fetch_alphafold.py](../tests/test_tools/test_fetch_alphafold.py) | none | ✓ |
| 6 | [fetch_bioactivity.py](../src/bioinformatics_mcp/tools/fetch_bioactivity.py) | yes | yes | [test_fetch_bioactivity.py](../tests/test_tools/test_fetch_bioactivity.py) | `idempotentHint=False` | ✓ |
| 7 | [fetch_compound.py](../src/bioinformatics_mcp/tools/fetch_compound.py) | yes | yes | [test_fetch_compound.py](../tests/test_tools/test_fetch_compound.py) | none | ✓ |
| 8 | [fetch_gene.py](../src/bioinformatics_mcp/tools/fetch_gene.py) | yes | yes | [test_fetch_gene.py](../tests/test_tools/test_fetch_gene.py) | none | ✓ |
| 9 | [fetch_interactions.py](../src/bioinformatics_mcp/tools/fetch_interactions.py) | yes | yes | [test_fetch_interactions.py](../tests/test_tools/test_fetch_interactions.py) | none | ✓ |
| 10 | [fetch_paper_fulltext.py](../src/bioinformatics_mcp/tools/fetch_paper_fulltext.py) | yes | yes | [test_fetch_paper_fulltext.py](../tests/test_tools/test_fetch_paper_fulltext.py) | none | ✓ |
| 11 | [fetch_pathway.py](../src/bioinformatics_mcp/tools/fetch_pathway.py) | yes | yes | [test_fetch_pathway.py](../tests/test_tools/test_fetch_pathway.py) | none | ✓ |
| 12 | [fetch_pdb.py](../src/bioinformatics_mcp/tools/fetch_pdb.py) | yes | yes | [test_fetch_pdb.py](../tests/test_tools/test_fetch_pdb.py) | none | ✓ |
| 13 | [fetch_sequence.py](../src/bioinformatics_mcp/tools/fetch_sequence.py) | yes | yes | [test_fetch_sequence.py](../tests/test_tools/test_fetch_sequence.py) | none | ✓ |
| 14 | [fetch_uniprot.py](../src/bioinformatics_mcp/tools/fetch_uniprot.py) | yes | yes | [test_fetch_uniprot.py](../tests/test_tools/test_fetch_uniprot.py) | none | ✓ |
| 15 | [fetch_variant.py](../src/bioinformatics_mcp/tools/fetch_variant.py) | yes | yes | [test_fetch_variant.py](../tests/test_tools/test_fetch_variant.py) | `idempotentHint=False` | ✓ |
| 16 | [fold_sequence.py](../src/bioinformatics_mcp/tools/fold_sequence.py) | **no** | **no** | **absent** | — | **stub only** |
| 17 | [predict_variant_effect.py](../src/bioinformatics_mcp/tools/predict_variant_effect.py) | yes | yes | [test_predict_variant_effect.py](../tests/test_tools/test_predict_variant_effect.py) | `idempotentHint=False` | ✓ |
| 18 | [scan_domains.py](../src/bioinformatics_mcp/tools/scan_domains.py) | yes | yes | [test_scan_domains.py](../tests/test_tools/test_scan_domains.py) | none | ✓ |
| 19 | [search_literature.py](../src/bioinformatics_mcp/tools/search_literature.py) | yes | yes | [test_search_literature.py](../tests/test_tools/test_search_literature.py) | `idempotentHint=False` | ✓ |

Five `idempotentHint=False` overrides + one `openWorldHint=False` override match the README's documented exception list exactly. No undocumented overrides; no documented overrides missing.

## Stub-file content

Both stubs are 14-line files with identical structure: triple-quoted module docstring containing a one-line tool description, the documented annotation overrides, and a `# TODO: implement per spec §X.Y` comment. No `import`, no function definition, no Pydantic models, no callable surface. They cannot be imported as `from bioinformatics_mcp.tools.fold_sequence import bio_fold_sequence` because that name does not exist; consequently they cannot be wired into `server.py` even as a placeholder.

This is why neither stub appears in `server.py`'s import block. It is also why the Session 7 closing audit's classification of `bio_fold_sequence` as "implemented but not registered" was inaccurate: there is nothing to register because there is no implementation. Both files are functionally on the same footing as not-yet-created tools — the file-system scaffolding is present, the code is absent.

## Correction to project-handoff.md

The project-handoff document landed earlier in this session (commit `b28fe80`) carried over the inaccurate classification verbatim. As part of the same commit set as this audit, [project-handoff.md](../project-handoff.md) is corrected to merge the previously-separate "Implemented but not registered" and "Not yet implemented" sections into a single "Stub only — implementation in Session 8a (2)" section, capturing the actual state of both files. The wording change is minimal; the substantive update is reclassifying `bio_fold_sequence` from "implemented" to "stub".

## Implications for Session 8a scope

**Section B (`bio_fold_sequence` registration)** in [prompts/session-8a-fold-and-crispor.md](../prompts/session-8a-fold-and-crispor.md) is now an implement-from-scratch task, not a register-existing-implementation task. The first sub-task ("Read the existing implementation; confirm it matches v2 §4.8 spec") is moot. The rest of the section's sub-tasks remain valid: implementation choice (per pre-work, ViennaRNA Python bindings — `import RNA` — restricted to non-GLPK API surface), unit tests, integration test against tRNA-Phe yeast, smoke entry, server registration, README tool-count update.

**Section C (`bio_design_grna` implementation)** is unchanged in shape — the file was always known to be unimplemented; the only update is that its existing stub file is acknowledged in this audit rather than treated as a surprise.

## Audit-first discipline

The Session 7 closing report's misclassification surfaced precisely because Session 8a started with an audit before any implementation work. Three errata in one pre-work cycle (this misclassification, `project-handoff.md` carry-over, and the felCat9 URL deadness) is exactly what the discipline catches. Worth retaining.
