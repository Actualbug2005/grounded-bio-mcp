# grounded-bio-mcp — Specification v3.0

**Project name:** `grounded-bio-mcp`
**Package import path:** `grounded_bio_mcp`
**Version:** 0.3.0 (rename + Phase 4 scope expansion)
**Previous identity:** `bioinformatics-mcp` v0.1.0 → v0.2.0 (1 → 19 tools), now superseded by this document
**Target deployer:** Devlin's Proxmox homelab (pve2 LXC), with the project published to GitHub under Apache-2.0
**Licence:** Apache-2.0
**Repository:** to be created at `grounded-bio-mcp` once the rename session lands

---

## 0. About this revision

Spec v3.0 is the rename + scope-expansion revision. Three things change from v2:

1. **Rename** to `grounded-bio-mcp` — the v2 working title `bioinformatics-mcp` was placeholder; this is the publish-ready identity.
2. **Architectural learnings** from sessions 1-7 are now first-class spec content, not session-by-session memory entries. The patterns that proved out (FastMCP 3.x, dict returns, `RateLimitedClient`, `EBIJobRunner`, `soft_cap_with_url_fallback`, NCBI httpx-plus-Biopython hybrid, server-side filter verification, identifier disambiguation) are documented as foundational.
3. **Scope expansion** — Phase 4 (16 clinical-genomics tools) specced in detail; Phases 5 and 6 outlined with full specs deferred to v3.1 / v3.2 as implementation approaches.

This spec stays accurate to what's about to be built rather than ambitious about what's months away. The iterative-discovery discipline that caught real spec errata in sessions 3 and 6 (Reactome `/data/query` not `/data/pathway`, InterProScan `PfamA` not `Pfam`, etc.) applies to spec writing too: specifying tools we haven't probed yet produces specs we'll revise heavily on contact with reality.

**Status of v2 deliverables (as of v3.0 publication):**

- 17 of 18 spec-v2 tools live and registered
- 1 tool (`bio_fold_sequence`) implemented but unregistered — caught up in Session 8a
- 205 offline tests passing, 16 integration tests live-verified, 17/17 smoke tests green against real APIs
- Deployment + CRISPOR remain — Sessions 8a/8b
- Errata from v2 captured in Section 8 below

---

## 1. Purpose

A Model Context Protocol server that grounds Claude's molecular biology answers in retrieval from primary databases, replacing model-hallucinated specifics with verified data fetched at query time.

**The failure mode this exists to fix.** Large language models confidently fabricate molecular biology specifics — residue numbers, binding affinities, DNA sequences, paper citations, off-target tables, allele frequencies, variant pathogenicity claims. Pattern-matching from training data produces plausible-looking but wrong answers in exactly the topics where wrong matters most: clinical interpretation, drug discovery, experimental design.

**The solution.** Replace generation with retrieval. Every claim about a specific molecule, structure, variant, paper, pathway, or interaction goes through a tool that fetches from an authoritative source — NCBI, UniProt, EBI, RCSB PDB, AlphaFold DB, Ensembl, ChEMBL, PubChem, Europe PMC, Reactome, STRING, ClinVar, gnomAD, PanelApp, HPO, AlphaMissense, SpliceAI, PharmGKB, OMIA — rather than letting the model answer from training data.

**Anti-hallucination targets** (the specific fabrication modes this spec is designed to catch):

- Fake residue numbers, domain boundaries, active sites → UniProt, InterPro, AlphaFold per-residue pLDDT
- Fake sequences and accessions → NCBI, UniProt
- Fake structures and interface residues → PDB, AlphaFold DB
- Fake variant rsIDs, allele frequencies, consequences → Ensembl VEP, gnomAD
- Fake variant pathogenicity classifications → ClinVar, AlphaMissense, SpliceAI, ACMG criteria orchestration
- Fake drug binding affinities, IC50s, target attributions → ChEMBL with confidence filtering
- Fake citations and misattributed findings → Europe PMC search + fulltext
- Fake CRISPR gRNA off-target tables → CRISPOR with real genome indexes
- Fake pathway membership and protein interactions → Reactome, STRING with seven-channel evidence
- Fake gene-phenotype associations and rare-disease panels → HPO, PanelApp, OMIM, OMIA
- Fake pharmacogenomic interactions → PharmGKB, CPIC

**Origin story.** The project began with a question about feline AIM/CD5L (Sugisawa et al. 2016, *Sci Rep* 6:35251) where a prior LLM transcript confidently attributed findings to "Miyazaki/Nakata work" without a verifiable DOI, and named specific residues that the actual paper does not. Verifying that case end-to-end — real Sugisawa fulltext from Europe PMC, real CD5L sequence and features from UniProt, real cryo-EM CD5L-IgM interface from PDB, real CRISPR guides from CRISPOR against felCat9 — became the founding worked example. The project deliberately scoped wider than feline genetics so the infrastructure serves human clinical genomics, peptide pharmacology, and any other primary-source-verifiable use case.

The working principle, applied recursively: when this server itself can't verify a claim, it says so, rather than fabricating a confident-looking answer. Empty results are reported as empty. Unknown classifications stay unknown. Server-side filters are verified, not trusted (ChEMBL leaks; STRING does not). The anti-hallucination principle covers this server's own outputs as rigorously as the upstream data it surfaces.

---

## 2. Architectural decisions (ratified by experience)

These are the load-bearing choices that proved out across Sessions 1-7. They are not negotiable without strong reason and a documented case for revision.

### 2.1 Language and framework: Python 3.11+ with FastMCP 3.x

Deviates from generic MCP guidance toward TypeScript. Justified because:

- **Biopython** is the canonical NCBI/Entrez parsing library. No comparable TypeScript equivalent.
- **ViennaRNA** has mature Python bindings; RNA folding is painful to reimplement.
- **CRISPOR** is a Python CLI; wrapping from Python avoids process-boundary serialisation complexity.
- **lxml** handles JATS XML and Entrezgene XML well; TypeScript's XML story is weaker.
- **python-codon-tables**, ChEMBL Python clients, and most cheminformatics libraries are Python-native.

Framework is **FastMCP 3.x** (jlowin's standalone `fastmcp` package), not the `mcp[cli]` SDK's bundled FastMCP 1.0 (frozen upstream). Rationale: actively developed, first-class streamable-HTTP transport, current MCP plugin guidance recommends it for new servers. Pin: `fastmcp>=3.0,<4.0`. Import: `from fastmcp import FastMCP`.

### 2.2 Transport: Streamable HTTP (primary), stdio (development)

- **Production:** Streamable HTTP via `mcp.run(transport="http", host=..., port=...)`. Server binds to `127.0.0.1` on the LXC; Caddy reverse-proxy fronts with bearer-token validation. Never bind to `0.0.0.0` directly — the proxy is the auth boundary, and `config.Settings._forbid_public_bind` enforces this with a startup error.
- **Development:** stdio for MCP Inspector testing via `MCP_TRANSPORT=stdio` environment override.

### 2.3 Authentication: static bearer token at proxy

The Caddy layer validates `Authorization: Bearer ${MCP_AUTH_TOKEN}` before proxying to the local server. The token is generated once via `openssl rand -hex 32` and lives in `/etc/grounded_bio_mcp/env`. This pattern is **deliberately scoped to self-hosted use** — it does not pass Anthropic's MCP directory submission criteria (which require OAuth). If the project ever needs public listing, auth has to move to OAuth per MCP spec 2025-11-25; that is a separate work item, not a regression.

### 2.4 Deployment: unprivileged LXC on Proxmox VE

Matches the existing homelab pattern.

- **Base:** Debian 13 (Trixie); Python 3.13 default with `python3.11` available as versioned package for CRISPOR fallback if needed
- **Resources:** 4 vCPU, 6 GB RAM, 30 GB root + 80 GB data mount
- **Bridge:** vmbr0, static IP
- **systemd service:** `grounded-bio-mcp.service`
- **Data mount:** `/var/lib/grounded_bio_mcp/` — genomes, cache, logs

PEP 668 enforced on Trixie; every Python install runs in a venv; never `pip install` at system level.

### 2.5 Tool naming and shape

**Naming:** `bio_{action}_{resource}`. No exceptions. Established Session 1.

**Returns:** plain `dict[str, Any]` from every tool function. Not Pydantic models. FastMCP wraps scalar returns in `{"result": ...}` but passes dicts through bare; consistent dict returns produce consistent client-side behaviour. Pydantic is for input validation only.

**Default annotations** (overrides documented per tool):

```python
{"readOnlyHint": True, "destructiveHint": False,
 "openWorldHint": True, "idempotentHint": True}
```

Documented overrides:

| Tool | Override | Reason |
|---|---|---|
| `bio_blast_search` | `idempotentHint=False` | NCBI databases grow over time |
| `bio_fetch_bioactivity` | `idempotentHint=False` | ChEMBL accepts new submissions |
| `bio_fetch_variant` | `idempotentHint=False` | Ensembl releases update annotations |
| `bio_predict_variant_effect` | `idempotentHint=False` | Same |
| `bio_search_literature` | `idempotentHint=False` | Europe PMC indexes new papers continuously |
| `bio_codon_optimise` | `openWorldHint=False` | Local-only computation, no upstream API |

Phase 4+ adds further `idempotentHint=False` cases for ClinVar (continuous classification updates), gnomAD (release cadence), AlphaMissense / SpliceAI (versioned model releases), PanelApp (panel curation updates).

### 2.6 Shared HTTP plumbing: `RateLimitedClient`

Lives in `utils/rate_limit.py`. Wraps `httpx.AsyncClient` with two independent gates:

1. **Concurrency cap** via bounded `asyncio.Semaphore` — at most N requests in flight per service.
2. **Minimum inter-request interval** via short asyncio lock + monotonic-clock bookkeeping — successive request *starts* are at least `min_interval_s` apart.

Per-service parameter tables live in `clients/base.py` `RATE_LIMITS`. Each service client (NCBI, UniProt, EBI, Ensembl, AlphaFold, RCSB, ChEMBL, PubChem, Europe PMC, Reactome, STRING, plus Phase 4 additions) gets one long-lived `RateLimitedClient` instance per process, lazily constructed via `lru_cache` in `server.py`.

**Critical invariant:** all HTTP calls go through `RateLimitedClient`. Direct `httpx` calls bypass the rate-limit accounting. `Bio.Entrez` is synchronous and would block the event loop; we use Biopython for parsing only, not for HTTP.

Tenacity-based retry on transient 429/503 is layered in each client module via `@retry(retry=retry_if_exception_type(...), wait=wait_exponential(...), stop=stop_after_attempt(4))`.

### 2.7 Async-job runner: `EBIJobRunner`

Lives in `clients/ebi.py`. Submit → poll → fetch pattern for EBI Job Dispatcher services (Clustal Omega, InterProScan, and any future EBI tools).

**Non-negotiable elements:**

- **Cancellation on timeout** — best-effort `DELETE /delete/{jobId}` before raising `JobTimeoutError`. Avoids orphaning jobs in EBI's queue. 404/405 logged at DEBUG (endpoint absent or job already gone), other errors at WARNING.
- **Polling jitter** — every poll wait is multiplied by `random.uniform(0.8, 1.2)`. Without jitter, multiple concurrent clients submitting at the same wall-clock second poll in lockstep and hammer the same poll-second. Documented in the runner's docstring with "do not 'simplify' to deterministic" prominently.
- **Backoff** — initial interval (typically 2 s) for the first 5 polls, then steps up to `max_interval` (typically 10 s).
- **Polling states** — `{PENDING, RUNNING, QUEUED}` are continuation states; `FINISHED` is success; `FAILED, ERROR, NOT_FOUND` raise `JobFailed`.

Same pattern adapted for NCBI BLAST in Session 7 with different cadence (15 s initial → 60 s after 5 min wall time, per NCBI batch-job etiquette).

### 2.8 Output truncation: `soft_cap_with_url_fallback`

Lives in `utils/formatting.py`. Used by every tool that can legitimately produce oversized output: `bio_fetch_pdb` (mmCIF coordinates), `bio_align_sequences` (alignment text), `bio_scan_domains` (matches), `bio_fetch_gene` (GO list), `bio_fetch_paper_fulltext` (sections), `bio_blast_search` (alignments), and Phase 4+ tools where applicable.

Soft cap is typically 200 KB for structured output, 300 KB for fulltext, 2 MB for mmCIF coordinates. On overflow, returns metadata plus a direct URL to the upstream resource. The error message names the overage_noun ("Structure too large…" / "Alignment too large…") so callers see what was truncated.

### 2.9 NCBI httpx-plus-Biopython hybrid

Established Session 2. All HTTP through `RateLimitedClient`; Biopython used only for parsing (`SeqIO.read`, `PDBParser`). `Bio.Entrez.efetch` is never called directly — it would route around the rate limiter and block the event loop. Same pattern applied to Gene tool (Session 5), BLAST tool (Session 7).

### 2.10 Server-side filter verification

Upstream filters cannot be trusted without verification.

- **ChEMBL `confidence_score__gte`** — leaky. Returns activities whose joined assay has confidence below threshold. Client-side re-enforcement is mandatory; `bio_fetch_bioactivity` surfaces the drop count as `below_threshold_excluded`.
- **STRING `required_score`** — clean. Verified Session 6: 30 partners at `required_score=900` all had `score ≥ 0.998`. Trusted with a DEBUG-level canary in case of regression.

**Pattern for new tools:** before relying on a server-side filter, probe with a known-result query and verify the filter is honest. Document the result in memory with verification date.

### 2.11 Identifier disambiguation pattern

When name-based lookups return multiple candidates, surface a `candidate_*_ids` array with disambiguation context (organism, chromosome, common name, species list — whichever applies) instead of arbitrarily picking one. Used in `bio_fetch_compound` (PubChem CIDs for stereoisomer/salt families), `bio_fetch_gene` (cross-species symbol ambiguity), `bio_fetch_pathway` (Reactome cross-species).

Phase 4 extends this pattern to: `bio_fetch_clinvar` (variants with multiple submissions or conflicting interpretations), `bio_fetch_panelapp` (panels matching multiple R-codes), `bio_match_phenotype_genes` (HPO terms matching multiple gene candidates).

### 2.12 Email-handling contract per upstream

Different APIs implement courtesy email differently. Per-API pattern:

- **EBI Job Dispatcher** — required in `email` form parameter; submission errors without it. Tool layer surfaces the missing-email case as a graceful error with a clear "set EBI_EMAIL" suggestion rather than letting the upstream submission fail opaquely.
- **STRING** — requested in user-agent header; `STRING_USER_EMAIL=...` env var; client logs WARNING at init if absent but proceeds.
- **NCBI** — accepted (and encouraged) in the `email` URL parameter; we send `EBI_EMAIL` if set since NCBI accepts the same address.
- **PubChem, RCSB, AlphaFold, UniProt, Ensembl, ChEMBL, Reactome, Europe PMC** — no email required; courtesy User-Agent identifying the project still sent.

---

## 3. Tool selection guide

Embedded verbatim in the server's `instructions` field so models listing tools see *which* tool to reach for *first* for each question type. The guide is the single most important anti-pattern-matching surface — without it, models default to training-data answers even when relevant tools are exposed.

The current production guide lives in `server.py` `SERVER_INSTRUCTIONS`. Phase 4 additions extend the table with clinical-genomics rows (variant pathogenicity → ClinVar; population frequency → gnomAD; phenotype → gene candidates → HPO; UK genetic test eligibility → PanelApp; pharmacogenomic interactions → PharmGKB; deep splicing prediction → SpliceAI; missense pathogenicity prediction → AlphaMissense; veterinary genetic disease → OMIA).

The full guide is reproduced in Section 5 alongside the Phase 4 tool specs and re-embedded in `server.py` after Phase 4 registration.

---

## 4. Existing tools (Phases 1-3) — reference

The 18 spec-v2 tools are live and stable; this section is reference, not re-specification. Source code lives at `src/grounded_bio_mcp/tools/{tool_name}.py`. Per-tool memory entries at `~/.claude/projects/.../memory/MEMORY.md` capture API quirks discovered during implementation.

| # | Tool | Phase | Source | Status |
|---|---|---|---|---|
| 1 | `bio_fetch_sequence` | 1 | NCBI Entrez | live |
| 2 | `bio_fetch_uniprot` | 1 | UniProt KB | live |
| 3 | `bio_fetch_pdb` | 1 | RCSB Data API + files.rcsb.org | live |
| 4 | `bio_fetch_alphafold` | 1 | EBI AlphaFold DB | live |
| 5 | `bio_align_sequences` | 1 | EBI Clustal Omega (Job Dispatcher) | live |
| 6 | `bio_blast_search` | 1 | NCBI BLAST URL API | live |
| 7 | `bio_design_grna` | 1 | CRISPOR (local subprocess) | **Session 8a** |
| 8 | `bio_fold_sequence` | 1 | ViennaRNA (local) | implemented; **register Session 8a** |
| 9 | `bio_fetch_compound` | 1 | ChEMBL + PubChem | live |
| 10 | `bio_fetch_bioactivity` | 1 | ChEMBL | live |
| 11 | `bio_fetch_variant` | 2 | Ensembl REST | live |
| 12 | `bio_predict_variant_effect` | 2 | Ensembl VEP | live |
| 13 | `bio_scan_domains` | 2 | EBI InterProScan (Job Dispatcher) | live |
| 14 | `bio_search_literature` | 2 | Europe PMC | live |
| 15 | `bio_fetch_paper_fulltext` | 2 | Europe PMC fulltext | live |
| 16 | `bio_fetch_gene` | 2 | NCBI Gene | live |
| 17 | `bio_fetch_pathway` | 3 | Reactome Content Service | live |
| 18 | `bio_fetch_interactions` | 3 | STRING REST | live |
| 19 | `bio_codon_optimise` | 3 | python-codon-tables + bundled Kazusa | live |

**Total v2 tools:** 19 (one — `bio_fold_sequence` — registered in Session 8a).

Schemas, output shapes, and per-tool API quirks are not re-specified here. The source modules carry full docstrings; the v2 spec is preserved in git history at `bioinformatics-mcp-spec.md` for archival reference. This v3 document supersedes v2 as the canonical specification.

---

## 5. Phase 4 — Clinical genomics (16 tools, full specifications)

Phase 4 is the largest single phase in the project and is where `grounded-bio-mcp` becomes meaningfully differentiated from the existing biomedical MCP ecosystem (BioContextAI, BioMCP, MCPmed). Coverage of UK clinical-genomics surfaces (PanelApp panels with R-codes, NHS Genomic Medicine Service Test Directory eligibility), full ACMG/AMP variant-classification orchestration, AlphaMissense + SpliceAI pre-computed prediction lookups, and OMIA veterinary genetic-disease lookups are all currently absent or thin in the published landscape.

**Phase 4 partitions into three sub-phases:**

- **5a. REST batch (10 tools)** — direct wrappers over ClinVar, gnomAD, PanelApp, HPO, AlphaMissense, SpliceAI, PharmGKB, NHS Test Directory, OMIA, plus a thin pathogenicity-aggregator. Sessions 10-13.
- **5b. Orchestration (6 tools)** — ACMG criteria evaluation, variant classification, eligibility resolution, audit chain, comparison, candidate disambiguator. Sessions 14-15. These are where the cross-cutting provenance framework (Section 6) earns its keep.

**Clinical disclaimer applies to all Phase 4 outputs.** This server returns information retrieved from public databases. It is not a medical device, does not provide diagnosis, and does not substitute for clinical genetics consultation. ACMG criteria evaluation produces an *evidence summary* against criteria definitions; clinical interpretation requires a qualified clinical scientist. See Section 11.

### 5.1 `bio_fetch_clinvar` — variant clinical significance

**Source:** NCBI ClinVar via E-utilities (`esearch` + `esummary` + `efetch&rettype=vcv`)
**Idempotency:** false (continuous classification updates)

**Inputs:**
- `variant`: one of:
  - VCV accession (`VCV000012345.1`)
  - SCV accession (`SCV000012345.1`)
  - HGVS.c (`NM_007294.4:c.5266dupC`)
  - HGVS.g (`NC_000017.11:g.43093883dupC`)
  - rsID (`rs80357906`) — resolves via dbSNP cross-reference
- `assembly`: `"GRCh38"` (default) | `"GRCh37"`
- `include_submissions`: bool (default false) — return per-submitter SCV records, not just aggregated VCV

**Output:**
```python
{
  "vcv_accession": "VCV000017661.4",
  "variation_id": 17661,
  "name": "NM_007294.4(BRCA1):c.5266dupC (p.Gln1756fs)",
  "type": "Duplication",
  "germline_classification": {
    "value": "Pathogenic",
    "review_status": "criteria provided, multiple submitters, no conflicts",
    "review_stars": 2,
    "last_evaluated": "2024-08-15",
    "submission_count": 47
  },
  "somatic_classification": null,  # or analogous structure
  "oncogenicity_classification": null,  # or analogous structure
  "molecular_consequences": ["frameshift_variant"],
  "gene_symbols": ["BRCA1"],
  "conditions": [
    {"name": "Hereditary breast ovarian cancer syndrome",
     "medgen_id": "C0677776", "omim_id": "604370"},
    ...
  ],
  "submissions": [...] if include_submissions else None,
  "provenance": {
    "source": "NCBI ClinVar",
    "fetched_at": "2026-04-25T11:47:50Z",
    "vcv_version": "VCV000017661.4",
    "data_release": "ClinVar release 2026-04-08",
    "url": "https://www.ncbi.nlm.nih.gov/clinvar/variation/17661/"
  },
  "confidence": {
    "review_stars": 2,
    "review_basis": "criteria provided, multiple submitters, no conflicts",
    "interpretation": "expert-reviewed assertions exist; multiple independent submitters concur"
  }
}
```

**Critical implementation notes:**

- **Review stars are the confidence signal.** 0 = no assertion criteria; 1 = single submitter or conflicting; 2 = multiple non-conflicting submitters; 3 = expert panel; 4 = practice guideline. Surface as both raw count and human-readable basis. Models must not collapse to "Pathogenic" without context — a 0-star Pathogenic from a single self-classifying submitter is *very different* from a 3-star expert-panel Pathogenic.
- **Conflicting interpretations** must surface as `review_status: "criteria provided, conflicting interpretations"` plus a `conflicts` array with each interpretation and submitter count. Do not silently pick one.
- **HGVS resolution** uses ClinVar's variant search rather than locally re-deriving — ClinVar's normalisation handles the canonical/alternative-transcript ambiguity.
- **Three classification axes** (germline / somatic / oncogenicity) all surfaced when present; null when absent rather than collapsing.

**Anti-hallucination targets:** fabricated VCV accessions, made-up review status, invented submission counts, oversimplified pathogenicity claims that ignore conflicts or low-star evidence.

### 5.2 `bio_fetch_population_frequency` — gnomAD allele frequencies

**Source:** gnomAD v4.1 GraphQL API (`https://gnomad.broadinstitute.org/api`)
**Idempotency:** false (release cadence)

**Inputs:**
- `variant`: HGVS.g (`NC_000017.11:g.43093883dupC`), VCF-style (`17-43093883-C-CC`), or rsID
- `dataset`: `"gnomad_r4"` (default) | `"gnomad_r3"` | `"gnomad_r2_1"` | `"exac"`
- `include_subpopulations`: bool (default true)

**Output:**
```python
{
  "variant_id": "17-43093883-C-CC",
  "rsid": "rs80357906",
  "consequence": "frameshift_variant",
  "exome": {
    "ac": 0, "an": 1614098, "af": 0.0,
    "ac_hom": 0, "filter": "PASS",
    "subpopulations": {
      "afr": {"ac": 0, "an": 41156, "af": 0.0},
      "amr": {"ac": 0, "an": 70934, "af": 0.0},
      "asj": {"ac": 0, "an": 26134, "af": 0.0},
      "eas": {"ac": 0, "an": 49180, "af": 0.0},
      "fin": {"ac": 0, "an": 67996, "af": 0.0},
      "mid": {"ac": 0, "an": 5836, "af": 0.0},
      "nfe": {"ac": 0, "an": 1144146, "af": 0.0},
      "rmi": {"ac": 0, "an": 9528, "af": 0.0},
      "sas": {"ac": 0, "an": 78388, "af": 0.0},
      ...
    }
  },
  "genome": {...},
  "joint": {...},  # gnomAD v4 joint exome+genome
  "interpretation": {
    "is_absent": true,
    "is_rare": true,  # AF < 0.0001
    "is_common": false,  # AF >= 0.05
    "max_subpop_af": 0.0,
    "max_subpop": null
  },
  "provenance": {
    "source": "gnomAD v4.1.0",
    "fetched_at": "2026-04-25T11:47:50Z",
    "data_release": "gnomAD v4.1.0 (2024-04-19)",
    "api_version": "GraphQL v4",
    "url": "https://gnomad.broadinstitute.org/variant/17-43093883-C-CC?dataset=gnomad_r4"
  },
  "confidence": {
    "filter_status": "PASS",
    "allele_number_adequacy": "high",  # AN > 100k = high; 10-100k = moderate; <10k = low
    "interpretation": "high-quality filter pass with adequate sample size for rare-variant frequency estimation"
  }
}
```

**Critical implementation notes:**

- **Subpopulation max AF is BS3/BA1/BS1 evidence in ACMG.** Surface `max_subpop_af` and `max_subpop` explicitly so the orchestration layer (Section 5.13) can score allele-frequency criteria correctly.
- **Filter status matters** — non-PASS variants (`AC0`, `RF`, `InbreedingCoeff`) should not be treated as confident frequency estimates.
- **Allele number adequacy** — small AN (e.g. exome-only data on a regulatory region) makes AF estimates noisy. Surface this as a confidence flag.
- **Joint dataset** (gnomAD v4) — if both exome and genome data are present, use the joint dataset as the primary frequency estimate.

**Anti-hallucination targets:** invented allele frequencies, fabricated subpopulation breakdowns, claims of rarity unsupported by adequate sample size.

### 5.3 `bio_fetch_panelapp` — UK PanelApp gene panels

**Source:** PanelApp (Genomics England) API (`https://panelapp.genomicsengland.co.uk/api/v1/`)
**Idempotency:** false (panel curation updates)
**Notable:** UK-clinical-pathway specific. Largely absent from existing biomedical MCP servers (BioContextAI, BioMCP, MCPmed all USA-centric).

**Inputs:**
- `panel`: panel ID (`123`), R-code (`R208`), or panel name (`"Inherited breast cancer and ovarian cancer"`)
- `version`: panel version (default: latest signed-off)
- `confidence_level`: `"green"` (default) | `"amber"` | `"red"` | `"all"` — green = confirmed, amber = borderline, red = candidate

**Output:**
```python
{
  "panel_id": 635,
  "name": "Inherited breast cancer and ovarian cancer",
  "version": "5.0",
  "version_signed_off": "2024-11-15",
  "disease_group": "Cancer and tumour syndromes",
  "disease_subgroup": "Familial breast and ovarian cancer",
  "r_codes": ["R208"],
  "test_indications": [
    {"code": "R208",
     "name": "Inherited breast cancer and ovarian cancer",
     "type": "Rare disease - WGS"}
  ],
  "genes": [
    {
      "symbol": "BRCA1",
      "ensembl_gene_id": "ENSG00000012048",
      "hgnc_id": "HGNC:1100",
      "confidence_level": 3,  # green
      "moi": "MONOALLELIC, autosomal or pseudoautosomal, NOT imprinted",
      "phenotypes": ["Breast-ovarian cancer, familial, susceptibility to, 1 605724"],
      "evaluations_count": 12,
      "ready": true
    },
    ...
  ],
  "candidate_panels": null,  # or array if name lookup matched multiple
  "provenance": {
    "source": "Genomics England PanelApp",
    "fetched_at": "2026-04-25T11:47:50Z",
    "panel_version": "5.0",
    "url": "https://panelapp.genomicsengland.co.uk/panels/635/"
  },
  "confidence": {
    "panel_status": "signed off",  # vs "promoted" / "internal"
    "interpretation": "signed-off panel, suitable for clinical use under NHS GMS Test Directory"
  }
}
```

**Critical implementation notes:**

- **Confidence levels are integer-coded** in the API (3=green, 2=amber, 1=red); surface human-readable form and integer.
- **R-codes link to NHS Test Directory.** PanelApp panels with R-codes are clinically commissioned; panels without are research-grade. The `r_codes` array is the bridge to `bio_fetch_test_directory` (Section 5.8).
- **Mode of inheritance (MOI)** is critical for downstream interpretation; surface verbatim from PanelApp's controlled vocabulary, not paraphrased.

**Anti-hallucination targets:** invented panel IDs, fabricated R-code mappings, made-up gene-confidence assignments, MOI claims that do not match PanelApp's actual annotation.

### 5.4 `bio_match_phenotype_genes` — HPO phenotype → gene lookup

**Source:** HPO (Human Phenotype Ontology) API + JAX HPO annotations (`https://hpo.jax.org/api/hpo/`)
**Idempotency:** false (continuous curation)

**Inputs:**
- `phenotype`: HPO term ID (`HP:0001250`) or natural-language phenotype description
- `min_evidence`: `"any"` (default) | `"published"` | `"orphanet"`

**Output:** matched HPO term, candidate-disambiguation array if natural-language input matched multiple, associated genes with evidence sources, OMIM disease cross-references, and confidence based on annotation source (Orphanet > OMIM > literature).

**Anti-hallucination targets:** fabricated HPO term IDs, invented gene-phenotype associations, conflated phenotypic terms.

### 5.5 `bio_fetch_alphamissense` — pre-computed missense pathogenicity

**Source:** AlphaMissense pre-computed scores (Google DeepMind 2023 release; ~71M scored missense variants for canonical human transcripts)
**Backend:** Ensembl VEP plugin endpoint (which exposes AlphaMissense alongside other plugins) — preferred over hosting the raw 5GB tabix files locally
**Idempotency:** true (model release is fixed; v1 published Sept 2023)

**Inputs:**
- `variant`: HGVS.p (`NP_009225.1:p.Gln1756Profs`), HGVS.c (transcript-aware), or `gene:p.aaref{pos}aalt` shortcut (`BRCA1:p.R71G`)

**Output:** AlphaMissense score (0-1), classification (`likely_benign` < 0.34 < `ambiguous` < 0.564 < `likely_pathogenic`), transcript context, model version, and explicit caveat that this is a *prediction* not an *observation* and ACMG PP3/BP4 weight depends on score magnitude per Pejaver et al. 2022.

**Anti-hallucination targets:** invented AlphaMissense scores, miscalibrated thresholds, conflation of AlphaMissense with other in-silico predictors.

### 5.6 `bio_predict_splice_effect` — SpliceAI predictions

**Source:** SpliceAI pre-computed scores (Illumina 2019; pre-computed for ~30M variants in Ensembl/RefSeq) + on-demand prediction for variants outside the pre-computed set
**Backend:** Broad Institute SpliceAI Lookup API (`https://spliceailookup-api.broadinstitute.org/`) for pre-computed; on-demand prediction is heavier and can be deferred to a Phase 5b async tool if needed.
**Idempotency:** true (pre-computed); false (on-demand if model re-run)

**Inputs:** HGVS.g or VCF-style variant; assembly.

**Output:** four SpliceAI delta scores (Acceptor Gain, Acceptor Loss, Donor Gain, Donor Loss) with positions, max delta score, classification (`high` ≥ 0.8, `moderate` ≥ 0.5, `low` ≥ 0.2, `negligible` < 0.2), and the standard caveat that SpliceAI is mechanistically informative for splice-altering predictions but does not directly score pathogenicity.

**Anti-hallucination targets:** invented SpliceAI deltas, fabricated splice-site positions, claims of splice disruption unsupported by the actual model output.

### 5.7 `bio_fetch_pharmacogenomics` — PharmGKB drug-gene interactions

**Source:** PharmGKB API (`https://api.pharmgkb.org/v1/`)
**Idempotency:** false (continuous curation; CPIC guidelines update)

**Inputs:**
- `query`: drug name, gene symbol, RSID, or `{drug, gene}` pair
- `evidence_min`: `"1A"` (highest CPIC) through `"4"` (preliminary); default `"3"`

**Output:** drug-gene pairs with PharmGKB level of evidence (1A through 4), CPIC guideline if present, star-allele assignments where applicable (e.g. CYP2C19 *2/*2 = poor metaboliser), recommendation summary, and provenance to specific CPIC version.

**Anti-hallucination targets:** invented drug-gene interactions, conflated CPIC levels of evidence, fabricated star-allele recommendations.

### 5.8 `bio_fetch_test_directory` — NHS GMS Test Directory eligibility

**Source:** NHS Genomic Medicine Service National Test Directory — published as a versioned spreadsheet on `england.nhs.uk`. No formal API; we mirror the current published version into a versioned local lookup table updated on a defined cadence (quarterly minimum, or when NHS England publishes a new version).
**Idempotency:** false (NHS publishes new versions; current is v8.1)
**Notable:** entirely absent from existing biomedical MCP ecosystem.

**Inputs:**
- `query`: R-code (`R208`) or test indication name
- Optional: `commissioning_route` (`"WGS"` | `"WES"` | `"panel"` | `"single_gene"`) for pathway filter

**Output:** R-code, test name, eligibility criteria text (verbatim from NHS Test Directory), commissioning route (rare disease WGS / cancer / R-code panel), associated PanelApp panel ID where mapped, and provenance citing the specific Test Directory version retrieved.

**Anti-hallucination targets:** invented R-codes, fabricated eligibility criteria, conflation of England NHS GMS pathways with Wales/Scotland/NI which run differently.

**Maintenance:** the lookup table is regenerated by `scripts/refresh_nhs_test_directory.py`, which fetches the published spreadsheet, parses it, and writes a JSON lookup with checksum + retrieval date. The tool refuses to return data older than 90 days without an explicit `allow_stale=True` flag, surfacing the staleness in the response.

### 5.9 `bio_fetch_omia` — Online Mendelian Inheritance in Animals

**Source:** OMIA database (`https://www.omia.org/api/`) — Sydney School of Veterinary Science
**Idempotency:** false
**Notable:** unique to this server; OMIA appears in zero other published biomedical MCP servers as of v3.0 publication. Exists in the project because the founding example (feline AIM/CD5L) was a veterinary-genetics case and the project's identity includes that origin (see Section 16).

**Inputs:**
- `query`: gene symbol, OMIA phenotype ID (`OMIA:000953-9685`), or `{species, phenotype}` pair
- `species`: optional taxonomic filter (`"Felis catus"`, `"Canis lupus familiaris"`, etc.)

**Output:** OMIA phene ID, species, phenotype name, OMIM cross-reference if present, gene associations with mutation details where curated, references with PubMed IDs, and provenance.

**Anti-hallucination targets:** invented OMIA IDs, conflation of species-specific phenotypes (cat HCM is not dog DCM), fabricated OMIM-OMIA cross-references.

### 5.10 `bio_aggregate_pathogenicity` — multi-source pathogenicity convergence

**Source:** internal aggregation; no upstream API
**Idempotency:** matches inputs (deterministic given fixed upstream snapshot)

**Inputs:** variant identifier (HGVS or VCF-style); list of sources to query (default: `["clinvar", "alphamissense", "spliceai", "gnomad"]`).

**Behaviour:** calls the relevant Phase 4 tools concurrently, aggregates results, surfaces *agreement and conflict*, and returns a multi-source evidence summary. Does **not** produce a synthesised pathogenicity verdict — that's `bio_classify_variant_acmg`'s job (Section 5.13). This tool is the thin aggregator; the orchestrator is the criteria-evaluator.

**Output:** structured per-source results, agreement summary (e.g. "ClinVar: Pathogenic 2-star; AlphaMissense: 0.94 likely_pathogenic; SpliceAI: max=0.02 negligible; gnomAD: AF=0 absent"), conflict flags, and provenance chain spanning all sources called.

**Anti-hallucination targets:** silent verdict synthesis that buries evidence weight; conflicting-source collapsing.

### 5.11 `bio_compare_variants` — variant set comparison

**Source:** internal; orchestrates `bio_fetch_clinvar` + `bio_fetch_population_frequency` across a set
**Inputs:** list of variants (HGVS or VCF-style); comparison criteria (clinical significance, population frequency, gene, etc.)
**Output:** tabular comparison with per-variant data and explicit divergence highlighting. Used for comparing candidate variants in research or clinical interpretation contexts.

### 5.12 `bio_fetch_acmg_criteria` — ACMG/AMP criteria reference

**Source:** internal lookup table backed by the published Richards et al. 2015 paper (Genet Med 17:405-424) plus subsequent refinements (Pejaver et al. 2022 for PP3/BP4 calibration; ClinGen SVI working-group recommendations 2018-2024).
**Inputs:** ACMG criterion code (`PVS1`, `PS1`, `PM2`, `PP3`, etc.) or `"all"`
**Output:** criterion definition, evidence required, weighting (very strong / strong / moderate / supporting), and authoritative source citation.
**Idempotency:** true (versioned reference data)

**Critical:** this is the *reference* for ACMG criteria. The *application* of criteria to a specific variant is `bio_classify_variant_acmg` (Section 5.13). Keeping these separate avoids silent rule drift.

### 5.13 `bio_classify_variant_acmg` — ACMG criteria orchestration

**Source:** internal orchestrator; calls multiple Phase 4 tools, evaluates ACMG criteria per Richards 2015 + Pejaver 2022, returns evidence summary.
**Idempotency:** false (depends on upstream classifications)

**Inputs:** variant identifier; transcript context; assembly; clinical context (optional — affects PP4).

**Behaviour:**

1. Fetch ClinVar, gnomAD, AlphaMissense, SpliceAI in parallel.
2. Evaluate each ACMG criterion against fetched evidence:
   - **PVS1** (very strong, null variant in gene where LoF causes disease) — requires gene-LoF-mechanism lookup; flag if uncertain.
   - **PS1** (same amino acid change as known pathogenic) — ClinVar HGVS.p match.
   - **PM2** (absent or rare in population databases) — gnomAD `max_subpop_af` < threshold per gene.
   - **PP3** (computational prediction) — AlphaMissense + SpliceAI per Pejaver 2022 calibration; *not* a uniform "PP3 if predicted pathogenic" — score magnitude determines weight.
   - **PP5/BP6** — ClinVar pathogenicity (note: 2018 ClinGen SVI recommendation deprecates these; flag with deprecation notice).
   - ... etc for full criteria set.
3. Return per-criterion evaluation with **evidence trace** (which fetched data point supported each criterion), aggregate classification per Richards 2015 combining-criteria rules, and **explicit per-criterion caveats** where evidence is borderline or absent.

**Output:**
```python
{
  "variant": "NM_007294.4:c.5266dupC",
  "evaluated_criteria": {
    "PVS1": {"applies": true, "weight": "very strong",
             "evidence": "frameshift in BRCA1; LoF is established mechanism (PMID:...)",
             "confidence": "high"},
    "PM2_supporting": {"applies": true, "weight": "supporting",
                       "evidence": "gnomAD v4.1 absent (AF=0, AN=1.6M)",
                       "confidence": "high"},
    "PP3": {"applies": false, "reason": "frameshift, in-silico predictors not informative"},
    ...
  },
  "classification": {
    "value": "Pathogenic",
    "rule_applied": "1 PVS1 + ≥1 PS or PM = Pathogenic (Richards 2015 Table 5)",
    "confidence": "high"
  },
  "evidence_trace": [
    {"criterion": "PVS1", "tool": "bio_fetch_clinvar",
     "data_point": "molecular_consequences=['frameshift_variant']",
     "fetched_at": "..."},
    {"criterion": "PM2_supporting", "tool": "bio_fetch_population_frequency",
     "data_point": "exome.af=0.0, exome.an=1614098",
     "fetched_at": "..."},
    ...
  ],
  "clinical_disclaimer": "This is an evidence summary based on Richards 2015 + Pejaver 2022 recommendations. It does not constitute clinical interpretation. Clinical genetics consultation required.",
  "provenance": {...}  # composite of all upstream fetches
}
```

**Critical implementation notes:**

- **Evidence trace is non-negotiable.** Every criterion must point at the fetched data point that supported (or failed to support) it. This is the cross-cutting provenance framework's clinical-genomics application — see Section 6.
- **Pejaver 2022 calibration for PP3/BP4** must be implemented faithfully; "PP3 if predicted pathogenic" is the wrong rule. Score magnitude maps to weight: strong / moderate / supporting / not applicable.
- **Deprecated criteria** (PP5, BP6) — the tool can evaluate them but flags deprecation per ClinGen SVI 2018 recommendation, encouraging users not to rely on them.
- **Combining-criteria rules** (Richards 2015 Table 5) implemented faithfully; the rule applied is reported in the output.
- **Clinical disclaimer always present in output.**

**Anti-hallucination targets:** invented ACMG criterion applications, mis-weighted PP3/BP4, silent omission of conflicts, classifications unsupported by traceable evidence.

### 5.14 `bio_resolve_test_eligibility` — clinical test eligibility orchestration

**Source:** internal orchestrator; calls `bio_fetch_test_directory` + `bio_fetch_panelapp` + clinical context.

**Inputs:** clinical phenotype (HPO terms or natural-language description) + family history + suspected-condition context.

**Output:** candidate R-codes with eligibility-criteria match analysis, associated PanelApp panel summaries, and explicit "this does not replace clinical genetics referral" disclaimer.

**Anti-hallucination targets:** fabricated R-code eligibility, conflated test pathways.

### 5.15 `bio_audit_classification_chain` — audit-trail reconstruction

**Source:** internal; introspects and replays a previous `bio_classify_variant_acmg` evidence trace.

**Inputs:** classification result from a previous call, with provenance chain.

**Behaviour:** re-fetches each upstream data point (where idempotent) or notes which sources may have changed since original classification (where not idempotent), surfaces the diff, and flags whether the original classification's evidence still holds.

**Use case:** clinical-grade auditability — six months after a classification, was the evidence still current? Has ClinVar updated? Did gnomAD release a new version with higher AN? The chain provides a definitive answer rather than a confidently-stated guess.

**Anti-hallucination targets:** invented audit chains, claimed re-verification without actual re-fetch, false claims of unchanged status.

### 5.16 `bio_disambiguate_variant` — variant identifier disambiguator

**Source:** internal; orchestrates Mutalyzer / Variant Recoder for HGVS normalisation + transcript-context handling.

**Inputs:** ambiguous variant identifier (transcript-naïve HGVS, ambiguous protein change, etc.).

**Output:** normalised canonical HGVS, candidate transcripts list with disambiguation context (RefSeq vs Ensembl, MANE Select status), and clear surfacing of cases where the input is genuinely ambiguous and cannot be uniquely resolved without more context.

**Anti-hallucination targets:** silent variant resolution that picks wrong transcript; conflated MANE Select vs MANE Plus Clinical.

---

## 6. Cross-cutting provenance and confidence framework

This is the architectural addition that distinguishes `grounded-bio-mcp` from being-yet-another-biomedical-MCP-wrapper. The framework applies retroactively to all existing Phase 1-3 tools and natively to Phases 4-6.

### 6.1 The principle

Every tool output carries:

1. **Provenance** — what was fetched, from where, at what time, what version of the upstream data, the canonical URL to verify.
2. **Confidence** — what does this server know about how trustworthy this answer is? Is the upstream source authoritative? How recent? How well-curated? Are there competing answers?
3. **Caveats** — what *does not* follow from the data fetched? Where are the limits?

The framework forces tools to be honest about their epistemic standing rather than producing flat "here is the answer" outputs that read as certain regardless of underlying uncertainty.

### 6.2 Provenance schema

Every tool returns a `provenance` field with at minimum:

```python
{
  "source": str,           # human-readable source name ("NCBI ClinVar")
  "fetched_at": str,       # ISO 8601 UTC timestamp of the fetch
  "data_release": str,     # versioned identifier where applicable
                           # ("ClinVar release 2026-04-08", "gnomAD v4.1.0")
  "url": str,              # canonical URL to verify upstream
  "api_endpoint": str,     # where the data actually came from (debug aid)
  "tool_version": str,     # this server's version when fetched ("0.3.0")
}
```

Composite tools (orchestrators) carry a `provenance_chain` array — every upstream fetch's provenance, in call order.

### 6.3 Confidence schema

Every tool returns a `confidence` field with at minimum:

```python
{
  "level": "high" | "moderate" | "low" | "unknown",
  "basis": str,            # why this confidence level
  "interpretation": str,   # human-readable guidance for downstream use
}
```

Tools with structured confidence signals (ClinVar review stars, ChEMBL confidence scores, AlphaFold pLDDT, gnomAD allele-number adequacy, BLAST E-values, STRING combined scores) carry the structured signal alongside the human-readable interpretation. The structured signal is what orchestrators key off; the interpretation is what users read.

### 6.4 Caveats — explicit not-knowing

When a tool's domain has known epistemic limits, those limits are surfaced in output rather than left implicit. Examples:

- **AlphaFold predictions** — pLDDT < 70 surfaced as "low confidence; structure may not match experimental reality"; pLDDT < 50 surfaced as "very low; treat as disorder prediction not structure prediction"
- **AlphaMissense** — surfaced caveat "model output is a *prediction*; PP3 weight per Pejaver 2022 calibration is supporting, not strong, except at extreme score magnitudes"
- **ChEMBL bioactivity** — confidence < 7 records excluded by default; null-confidence records always excluded (project decision); below-threshold drop count surfaced as `below_threshold_excluded`
- **STRING interactions** — combined-score basis decomposed by evidence channel; "interaction supported by text-mining only" is qualitatively different from "interaction supported by experimental evidence + database curation"
- **Europe PMC fulltext** — when fulltext is unavailable, return the abstract + DOI rather than synthesising content; flag `fulltext_available: false` so models don't claim to have read what they couldn't fetch

### 6.5 Retrofit to Phase 1-3 tools

Phase 1-3 tools were built before the framework was formalised. They already carry provenance-equivalent information (URLs, fetch timestamps, version identifiers) and confidence signals (where applicable: pLDDT, confidence scores, E-values, review stars). The retrofit work — Session 14-15 — is:

1. Standardising the field names (`provenance`, `confidence`, `caveats`) across all tools
2. Adding the structured `interpretation` field where tools currently surface raw signals only
3. Adding `provenance_chain` to composite outputs
4. Adding the `tool_version` field uniformly

The retrofit is a non-breaking enrichment — existing fields stay; new fields get added. Does not require a major version bump.

### 6.6 Why this matters for clinical genomics specifically

Clinical-grade interpretation requires audit-trail provenance. A pathogenicity classification carried out today must be re-checkable in six months — was ClinVar's classification stable? Did gnomAD release new data? Did AlphaMissense's calibration paper get superseded? The `bio_audit_classification_chain` tool (Section 5.15) is the user-facing surface; the cross-cutting provenance framework is the underlying infrastructure that makes it possible.

This is also where the project's anti-hallucination principle applies recursively — the server must not confidently claim things it cannot verify, including claims about *its own answers*.

---

## 7. Phase 5 + Phase 6 — outline only

Specifications below are intentionally high-level. Full per-tool specs land in spec v3.1 (Phase 5a wedge tool) and v3.2 (Phase 5/6 remainder) as implementation approaches, following the iterative-discovery discipline that has produced reliable specs for Phases 1-3.

### 7.1 Phase 5 — Structural and predictive biology

**Phase 5a (REST simulation tools, 5 tools — sequential rollout starting v0.4.0):**

- `bio_predict_stability` (DynaMut2) — protein stability change on point mutation. **First community-novel tool**; ships standalone in Session 9 as a wedge to validate the publishable claim before committing to the full Phase 5 batch.
- `bio_predict_protein_protein_affinity` (mCSM-PPI2) — interface mutation effect on PPI affinity.
- `bio_predict_protein_ligand_affinity` (mCSM-lig) — ligand-binding affinity change on point mutation.
- `bio_screen_missense_3d` (Missense3D) — missense impact on 3D structure (clash, cavity, charge change).
- `bio_normal_modes` (NMA via DynaMut/elNémo) — normal-mode analysis for global flexibility.

These are wrappers over Edinburgh / Oxford / Imperial computational-biology REST services. Pattern is similar to InterProScan (submit, poll, fetch), differences captured per-tool when implementation begins.

**Phase 5b (async cloud, 2 tools):**

- `bio_predict_complex_alphafold` (AlphaFold-Multimer via cloud submission)
- `bio_predict_complex_status` (status check for above)

Heavier than the Phase 5a tools — runs are minutes-to-hours not seconds. Async pattern with persistent job IDs and recovery.

**Phase 5c (deferred to Phase 7 — local-compute backend):**

- Local AlphaFold / Rosetta / GROMACS compute on BRAT (i9-12900K, RTX 4060 Ti 16GB, 32GB DDR5-6000) accessed via RPC from the LXC. Substantially more complex than REST wrappers; deferred until the REST wrappers prove out and there's clear demand for local compute.

### 7.2 Phase 6 — Special-interest cross-disciplinary

**Phase 6a (peptide PTMs, 4 tools):**

- `bio_predict_disulfide_bonds` — cysteine pairing prediction (DiANNA / DISULFIND).
- `bio_predict_ptm_sites` — phosphorylation, glycosylation, other PTM site prediction (NetPhos, NetNGlyc, etc.).
- `bio_predict_peptide_stability` — proteolytic stability prediction.
- `bio_predict_peptide_membrane_permeability` — membrane-permeability classifiers (relevant for therapeutic peptides).

These connect directly to user's peptide-research interest (BPC-157, TB-500, GHK-Cu, Epitalon, melanocortin peptides). Project value is verification — peptide-research literature is high-volume and uneven; tool-grounded answers reduce reliance on confident-sounding but unverified claims.

**Phase 6b (sleep chronobiology, 2 tools):**

- `bio_fetch_circadian_genes` — circadian-rhythm gene database (CGDB / CircaDB).
- `bio_predict_chronotype_variants` — variant impact on chronotype (PER1/2/3, BMAL1, CRY1/2, etc.).

Smaller subset of Phase 6 — connects to user's sleep-science interest. Lower project priority but earned its place in scope through cross-disciplinary fit with the wider grounded-bio mission.

### 7.3 Sequencing rationale

Phase 4 (clinical genomics) before Phase 5 (structural prediction) before Phase 6 (special-interest):

1. Phase 4 is the largest novelty contribution to the published MCP ecosystem and the most mature tooling.
2. Phase 5 needs the cross-cutting provenance framework retrofit complete (Session 14-15) before adding more novel-output tools.
3. Phase 6 is dependent on the above being stable and is special-interest enough that it can land late without harming the core project.

---

## 8. Errata captured from v2 → v3 migration

These are the specification errors caught during Phase 1-3 implementation. Each is captured with the tool affected, the error in v2, the correction, and the session in which it surfaced.

| # | Tool / Section | v2 error | v3 correction | Session |
|---|---|---|---|---|
| 1 | §4.5 Clustal Omega | "conserved-column count" missing from four-stat output spec | Added to output schema; column count alongside identity %, similarity %, gaps % | 3 |
| 2 | §4.10 bioactivity | null-confidence record handling unspecified | Project decision: always-excluded regardless of threshold; surfaced as `null_confidence_excluded` count | 4 |
| 3 | §4.11 variants | three-outcome shape (found-rich / found-empty / not-found) not implementable | Ensembl collapses to two outcomes; project uses found/not_found with `annotation_richness` flags | 5 |
| 4 | §4.12 VEP | `chrom:pos:ref:alt` input format does not literally map to VEP's region URL | Translation layer with REF length determining end position; documented in tool docstring | 5 |
| 5 | §4.13 InterProScan | default `["Pfam", "SMART", "CDD"]` would fail at EBI | Canonical EBI names: `["PfamA", "SMART", "CDD"]`; PROSITE splits to `PrositeProfiles`+`PrositePatterns`; SuperFamily/Gene3d casing matters | 6 |
| 6 | §4.17 Reactome | endpoint `/data/pathway/{stId}` 404s | Correct endpoint `/data/query/{stId}` | 6 |
| 7 | §4.17 Reactome | example R-HSA-109581 mis-named "Signalling by Interleukins" | Actual: Apoptosis | 6 |
| 8 | §6 evaluation | accession NM_001301717 mis-claimed as BRCA1/HTT | Actual: CCR7 (chemokine receptor) | 6 |
| 9 | §6 evaluation | PDB 1CRN resolution mis-claimed as 0.54 Å | Actual: 1.5 Å (0.54 Å belongs to 1EJG, a re-refinement of crambin) | 6 |
| 10 | §7.1 rate-limit table | BLAST polling cadence conflated with E-utilities request rate | BLAST: 15 s initial → 60 s after 5 min wall time. Different host (`blast.ncbi.nlm.nih.gov`) from generic E-utilities; documented separately | 7 |
| 11 | §10.2 evaluation Q10 | wording presupposed Sugisawa 2016 names specific residues | Verified by full-text fetch the paper does not; question correct as negative-verification test, wording clarified | 6 |

The general pattern: **probe before specifying**. Spec v2's errors clustered in tools where the upstream API hadn't been hit before writing the spec. Spec v3 retains the same risk for Phase 4+ tools that haven't been probed yet — that's why Phase 5/6 are outline-only.

---

## 9. Deployment

### 9.1 LXC provisioning on Proxmox VE pve2

Target: unprivileged LXC on Proxmox VE 9.x, Debian 13 (Trixie) base.

```
Resources:    4 vCPU, 6 GB RAM, 30 GB root + 80 GB data mount
Network:      vmbr0, static IP on the homelab LAN
Hostname:     grounded-bio-mcp
DNS:          grounded-bio-mcp.devlin.lan (or equivalent local zone)
Privileged:   no
Nesting:      no
Features:     keyctl=1
```

Install steps (abbreviated; full recipe in Session 8b prompt):

1. Provision LXC via Proxmox UI / `pct create`
2. Update + base packages (`build-essential`, `curl`, `git`, `python3.13-venv`, `python3.13-dev`, `bwa`, `caddy`, `gnupg`)
3. Create system user `grounded-bio-mcp` with home `/opt/grounded_bio_mcp`
4. Clone repository to `/opt/grounded_bio_mcp/app`
5. Create venv at `/opt/grounded_bio_mcp/venv`, `pip install -e ".[deploy]"`
6. Install CRISPOR to `/opt/crispor` (separate venv if Python version compatibility requires)
7. Create data directories: `/var/lib/grounded_bio_mcp/{genomes,cache,logs}`, owned by `grounded-bio-mcp`
8. Download genome indexes — felCat9 (~1 GB), hg38 (~3.2 GB), mm39 (~2.8 GB) — through download-gate workflow (one fetch at a time, explicit user approval, surfaced URLs and sizes)
9. Configure `/etc/grounded_bio_mcp/env` with `EBI_EMAIL`, `MCP_AUTH_TOKEN`, `STRING_USER_EMAIL`, optional `NCBI_API_KEY`
10. Install systemd service `grounded-bio-mcp.service`
11. Configure Caddy reverse proxy with bearer-token validation
12. Enable + start service; verify smoke test passes against deployed endpoint
13. Verify connection from claude.ai (Settings → Connectors → Add custom connector)

### 9.2 systemd service

```ini
[Unit]
Description=grounded-bio-mcp server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=grounded-bio-mcp
Group=grounded-bio-mcp
WorkingDirectory=/opt/grounded_bio_mcp/app
EnvironmentFile=/etc/grounded_bio_mcp/env
ExecStart=/opt/grounded_bio_mcp/venv/bin/python -m grounded_bio_mcp.server
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/grounded_bio_mcp /tmp
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictRealtime=yes
LockPersonality=yes
SystemCallArchitectures=native
MemoryMax=4G
TasksMax=512

[Install]
WantedBy=multi-user.target
```

### 9.3 Caddy reverse proxy

```caddy
grounded-bio-mcp.devlin.lan {
    @authorized header Authorization "Bearer {env.MCP_AUTH_TOKEN}"

    handle @authorized {
        reverse_proxy 127.0.0.1:8080 {
            transport http {
                read_buffer 64KB
            }
        }
    }

    handle {
        respond "Unauthorized" 401
    }

    log {
        output file /var/log/caddy/grounded-bio-mcp.log {
            roll_size 100MB
            roll_keep 10
        }
        format json
    }
}
```

The bearer token is supplied to Caddy via systemd `EnvironmentFile=/etc/caddy/env` so it doesn't appear in the Caddyfile or in process arguments.

### 9.4 Connection from claude.ai

Settings → Connectors → Add custom connector:

- **URL:** `https://grounded-bio-mcp.devlin.lan/mcp`
- **Headers:** `Authorization: Bearer ${MCP_AUTH_TOKEN}`
- **Transport:** Streamable HTTP

Local DNS resolution via UCG Max DNS records — `grounded-bio-mcp.devlin.lan` → LXC IP. TLS via Caddy's local-CA mode (`tls internal`) for the homelab zone, or a real cert from the homelab's existing PKI if present.

### 9.5 Rate limit table — corrected

| Service | Concurrency | Min interval | Notes |
|---|---|---|---|
| NCBI E-utilities | 3 | 350 ms | 10/sec with API key; ~3/sec without |
| NCBI BLAST URL API | 1 | 15 s → 60 s | Different host (`blast.ncbi.nlm.nih.gov`); polling cadence steps up after 5 min wall time |
| UniProt | 5 | 100 ms | Generous limit; courtesy User-Agent appreciated |
| EBI Job Dispatcher | 4 | 250 ms | Email required in form parameter |
| Ensembl | 3 | 70 ms | 15/sec hard cap; 70 ms keeps headroom |
| AlphaFold DB | 3 | 100 ms | Static-files service; lenient |
| RCSB PDB | 3 | 100 ms | Data API + files.rcsb.org |
| ChEMBL | 3 | 200 ms | Confidence filter is leaky — verify client-side |
| PubChem | 3 | 200 ms | PUG REST; 5 req/sec hard limit |
| Europe PMC | 3 | 200 ms | Fulltext API separate; same client |
| Reactome | 3 | 100 ms | Content Service is generous |
| STRING | 3 | 1 s | User explicitly requests email in user-agent |
| Kazusa codon DB | 1 | 1 s | Off-spec one-off fetches; rare |
| **Phase 4 additions:** | | | |
| ClinVar (NCBI) | shares NCBI E-utilities client | | |
| gnomAD GraphQL | 2 | 500 ms | GraphQL API; conservative |
| PanelApp | 3 | 200 ms | Generous limit |
| HPO API | 3 | 200 ms | JAX-hosted; lenient |
| AlphaMissense (Ensembl plugin) | shares Ensembl client | | |
| SpliceAI Lookup (Broad) | 2 | 300 ms | Rate-limit unclear; conservative |
| PharmGKB | 3 | 250 ms | Authenticated API tier different |
| NHS Test Directory | n/a | n/a | Local lookup; refresh task is rate-limited per NHS-England courtesy |
| OMIA | 2 | 500 ms | Smaller service; conservative |

---

## 10. Testing and evaluation

### 10.1 Test categories

- **Offline unit tests** — mocked HTTP, fixture-driven; full pytest run < 10 s; current count 205.
- **Integration tests** — live API; gated behind `RUN_INTEGRATION=1` env; require real `EBI_EMAIL`; current count 16; full run ~80 s.
- **Smoke tests** — end-to-end through `RateLimitedClient` against real upstreams; one per registered tool; gated behind `EBI_EMAIL`; current count 17; will be 19 after Session 8a (CRISPOR + folding); will scale per Phase 4.

### 10.2 RED-phase commit discipline

Every feature commit body contains the actual `AssertionError` text from the initial failing test. This is the project's quality gate against tests-written-after-the-fact masquerading as TDD. Established Session 1, applied uniformly across all sessions, will continue.

### 10.3 Conventional commits

Prefixes: `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`, `probe:`. Tool implementations average 3+ commits each (initial test, implementation, registration; often plus refactors and follow-up fixes). Session-prompt commit is always the first action of a session.

### 10.4 Evaluation harness — 10 verifiable Q/A pairs

Spec §10.4 (carried forward from v2). Ten questions, each answerable end-to-end through the deployed server, with verifiable factual answers grounded in upstream sources. Used as a deployment acceptance test (Session 8b) and as ongoing regression check (cron-scheduled, smoke-test-equivalent).

Final exact answers determined by first run against the deployed system; question set covers:

1. CCR7 sequence retrieval (NM_001301717) — `bio_fetch_sequence`
2. CD5L UniProt features (Q08758) — `bio_fetch_uniprot`
3. Crambin (1CRN) resolution — `bio_fetch_pdb` → 1.5 Å
4. AlphaFold model for human BRCA1 — `bio_fetch_alphafold` with pLDDT distribution
5. Sugisawa 2016 fulltext residue claim — `bio_fetch_paper_fulltext` (negative verification: paper does not name specific residues)
6. BRCA1 c.5266dupC ClinVar classification — `bio_fetch_clinvar` → Pathogenic 2-star (Phase 4)
7. SLC6A4 5-HTTLPR variant frequency in EUR — `bio_fetch_population_frequency` (Phase 4)
8. R208 panel content — `bio_fetch_panelapp` (Phase 4)
9. PER3 chronotype variant ACMG classification — `bio_classify_variant_acmg` (Phase 4)
10. CRISPR guides for human BRCA1 exon 11 — `bio_design_grna`

Q1-5 and Q10 land in Session 8b (with Phase 1-3 + CRISPOR live). Q6-9 land as Phase 4 tools come online.

---

## 11. Licensing and disclaimers

### 11.1 Licence: Apache-2.0

Chosen for permissiveness compatible with both academic publication and commercial use, with explicit patent-grant clause.

`LICENSE` file at repo root carries the standard Apache-2.0 text. `NOTICE` file enumerates third-party components and their licences (FastMCP MIT, Biopython BSD-3, ViennaRNA — custom academic licence — see Section 11.4).

### 11.2 Per-source citation requirement

Every tool output's `provenance` field must enable citation of the upstream source. README and per-tool docstrings explicitly state the citation expectation. Users embedding `grounded-bio-mcp` in publications cite the upstream sources, not (or not only) this server.

A `CITATION.cff` file at repo root provides this server's own citation metadata for cases where the server itself is methodologically cited (e.g. methods sections describing data-retrieval approach).

### 11.3 Clinical disclaimer

Embedded in:

1. README at repo root — prominent section
2. `bio_classify_variant_acmg` output — every response
3. `bio_resolve_test_eligibility` output — every response
4. `bio_fetch_clinvar`, `bio_fetch_population_frequency`, `bio_fetch_panelapp`, `bio_fetch_pharmacogenomics` — in confidence/interpretation field

Standard text:

> This server returns information retrieved from public databases. It is not a medical device, does not provide diagnosis, and does not substitute for clinical genetics consultation. Variant classifications, eligibility assessments, and pharmacogenomic recommendations require interpretation by qualified clinical scientists. Outputs are provided for informational and research purposes only.

### 11.4 ViennaRNA licence containment

ViennaRNA ships under a custom academic licence (research / educational / commercial use permitted; redistribution-for-fee prohibited; attribution required). The `bio_fold_sequence` tool calls ViennaRNA via Python bindings; the licence's redistribution clause is non-permissive and Apache-2.0-incompatible at the redistribution boundary if the project were to ship ViennaRNA itself.

**Mitigation:** the project does not redistribute ViennaRNA. Users `pip install` it from PyPI under the upstream licence; `bio_fold_sequence` invokes the `RNA` Python module at runtime. The `bio_fold_sequence` tool's source files are clearly marked as the licence-touching boundary and the rest of the codebase remains Apache-2.0. The README + NOTICE document the dependency and its licence terms. Final approach decided in Session 8a as part of `bio_fold_sequence` registration.

### 11.5 No clinical-grade assertion

`grounded-bio-mcp` is **not** UK MHRA, US FDA, or CE-IVD certified. It does not claim to be clinical software. Users who wish to deploy it in regulated clinical environments are responsible for their own conformity assessment.

---

## 12. Implementation order

Authoritative session sequence as of v3.0 publication:

| Session | Scope | Output | Notes |
|---|---|---|---|
| 8a | Audit unregistered tools; register `bio_fold_sequence`; smoke to 19/19; implement `bio_design_grna` (CRISPOR) locally | tools 18, 19 live; smoke 19/19 | Dev machine; download-gate for one test genome (felCat9 — smallest) |
| 8.5 | Atomic rename `bioinformatics-mcp` → `grounded-bio-mcp`; bump 0.2.0 → 0.3.0 | rename complete; v3 spec landed | Single mechanical session; smoke before/after; README update |
| 8b | LXC provisioning on pve2; genome indexes (felCat9, hg38, mm39); Caddy + bearer auth + systemd; evaluation harness §10.4; end-to-end via claude.ai | production deployment | Three genome downloads with download-gate per fetch |
| Soak | 30-day production use | failure-mode log; smoke-test cron in place | No new tools; observe |
| 9 | `bio_predict_stability` (DynaMut2) — first community-novel tool | Phase 5a wedge; v0.4.0 ships | Validates publishable claim with single tool before Phase 4 batch |
| 10 | Phase 4: `bio_fetch_clinvar`, `bio_fetch_population_frequency` | 2 Phase 4 tools live | REST batch begins |
| 11 | Phase 4: `bio_fetch_panelapp`, `bio_fetch_test_directory` | 2 Phase 4 tools live | UK clinical-pathway differentiator |
| 12 | Phase 4: `bio_match_phenotype_genes` (HPO), `bio_fetch_alphamissense`, `bio_predict_splice_effect` | 3 Phase 4 tools live | Phenotype + prediction surfaces |
| 13 | Phase 4: `bio_fetch_pharmacogenomics`, `bio_fetch_omia`, `bio_aggregate_pathogenicity` | 3 Phase 4 tools live | Phase 4 REST batch complete (10 tools) |
| 14 | Phase 4 orchestration: `bio_fetch_acmg_criteria`, `bio_classify_variant_acmg`, `bio_disambiguate_variant` | 3 orchestration tools live | ACMG criteria orchestration |
| 15 | Phase 4 orchestration: `bio_resolve_test_eligibility`, `bio_audit_classification_chain`, `bio_compare_variants` + cross-cutting provenance retrofit to Phase 1-3 tools | Phase 4 complete (16 tools); provenance framework everywhere | v0.5.0 ships |
| 16+ | Phase 5a remainder + 5b async + Phase 6 specs evolved per implementation experience | TBD | Spec v3.1 lands here |

Total tool count at end of Session 15: **35** (19 Phase 1-3 + 16 Phase 4). Phase 5a remainder + Phase 5b + Phase 6 additions during Session 16+ bring the project toward the originally-targeted ~50.

---

## 13. Documentation deliverables

A mkdocs-material site published at `https://devlin.github.io/grounded-bio-mcp/` (or equivalent), comprising:

- **Home** — project elevator pitch, anti-hallucination thesis, install one-liner
- **Quick start** — venv setup, `.env` configuration, first tool call via MCP Inspector
- **Tool reference** — one page per tool, generated from docstrings + examples
- **Selection guide** — the model-facing tool-selection table, also embedded in `server.py`
- **Architecture** — Section 2 of this spec, expanded with diagrams
- **Provenance & confidence framework** — Section 6 of this spec
- **Clinical genomics** — Phase 4 tools with worked examples
- **Deployment** — LXC setup, Caddy config, systemd service, troubleshooting
- **Contributing** — TDD discipline, conventional commits, session-prompt-first workflow
- **Spec archive** — v2 (preserved for history), v3 (this document), v3.1+ as published
- **Citations** — how to cite the server itself; how to cite upstream sources

mkdocs-material handles cross-linking, search, and dark/light themes well. Build pinned in `requirements-docs.txt`. CI builds on push to `main`; deploys via GitHub Pages.

---

## 14. Upstream contribution pathway

Two primary contribution targets:

### 14.1 BioContextAI registry submission

BioContextAI maintains a curated registry of biomedical MCP servers (Schaefer et al., *Nature Biotechnology* 2025). Submission process: PR to `BioContextAI/registry` repository with server metadata YAML.

**Eligibility timing:** after Session 15 completes (Phase 4 orchestration + provenance retrofit). At that point the project demonstrates substantial novelty (UK clinical pathways, ACMG orchestration with evidence trace, OMIA, cross-cutting provenance) beyond existing registry entries.

Submission requires: server description, tool list, licence (Apache-2.0 ✓), self-hosting documentation, citation metadata.

### 14.2 Upstream PRs to existing biomedical MCP servers

Where `grounded-bio-mcp` solves a problem also relevant to BioContextAI / BioMCP / MCPmed entries, contribute back:

- **Provenance framework** — could land as a shared library or a reference implementation; pitch to BioContextAI maintainers post-Session 15.
- **PanelApp tool** — if BioContextAI doesn't gain a UK-pathway tool by then, offer the implementation upstream.
- **NHS Test Directory tool** — likely unique to this server; less likely to be contributed upstream but registry-listable.

The contribution pathway is opportunistic. Project value isn't gated on upstream acceptance; the project stands alone whether or not the wider ecosystem absorbs its specific tooling.

---

## 15. Novelty tagging audit

Per-tool novelty assessment as of v3.0 publication, against published biomedical MCP ecosystem (BioContextAI Knowledgebase MCP, BioMCP, MCPmed registry entries, public GitHub).

| Tool | Novelty | Notes |
|---|---|---|
| `bio_fetch_sequence` | shared | NCBI sequence wrapping is widely covered |
| `bio_fetch_uniprot` | shared | UniProt wrapping covered in BioContextAI |
| `bio_fetch_pdb` | shared | PDB wrapping covered |
| `bio_fetch_alphafold` | shared | AlphaFold DB covered |
| `bio_align_sequences` | partially novel | Clustal Omega via EBI Job Dispatcher with cancellation+jitter is uncommon |
| `bio_blast_search` | partially novel | NCBI BLAST URL API wrappers exist but the polling-cadence + UNKNOWN-ambiguity handling is more careful than typical |
| `bio_design_grna` | likely novel | CRISPOR wrapping with local genome indexes — checked; not seen in published MCP ecosystem |
| `bio_fold_sequence` | partially novel | ViennaRNA exists in some MCP servers; less common as tool than as standalone CLI |
| `bio_fetch_compound` | shared | Both ChEMBL and PubChem covered separately; dual-source with disambiguation is partially novel |
| `bio_fetch_bioactivity` | partially novel | ChEMBL bioactivity wrapping with leaky-filter defence is uncommon |
| `bio_fetch_variant` | shared | Ensembl variant covered |
| `bio_predict_variant_effect` | shared | Ensembl VEP covered |
| `bio_scan_domains` | partially novel | InterProScan via Job Dispatcher is uncommon |
| `bio_search_literature` | shared | Europe PMC search covered |
| `bio_fetch_paper_fulltext` | partially novel | Fulltext fetch — closes citation-verification loop; uncommon in MCP ecosystem |
| `bio_fetch_gene` | shared | NCBI Gene covered |
| `bio_fetch_pathway` | shared | Reactome covered |
| `bio_fetch_interactions` | shared | STRING covered |
| `bio_codon_optimise` | likely novel | Codon optimisation as MCP tool — checked; not seen |
| **Phase 4:** | | |
| `bio_fetch_clinvar` | partially shared | BioMCP covers ClinVar; the review-stars surfacing + conflicts-aware output is more careful |
| `bio_fetch_population_frequency` | partially shared | BioMCP covers gnomAD; subpopulation breakdown + AN-adequacy framework is novel |
| `bio_fetch_panelapp` | **novel** | UK PanelApp absent from existing ecosystem |
| `bio_match_phenotype_genes` | partially novel | HPO appears in some servers; the candidate-disambiguation pattern is uncommon |
| `bio_fetch_alphamissense` | partially novel | AlphaMissense wrappers exist; Pejaver 2022 calibration handling is uncommon |
| `bio_predict_splice_effect` | partially shared | SpliceAI Lookup wrappers exist in some servers |
| `bio_fetch_pharmacogenomics` | partially shared | PharmGKB covered in some ecosystem entries |
| `bio_fetch_test_directory` | **novel** | NHS GMS Test Directory absent from existing ecosystem |
| `bio_fetch_omia` | **novel** | OMIA absent from existing ecosystem |
| `bio_aggregate_pathogenicity` | **novel** | Multi-source aggregation with conflict-surfacing — checked; not seen |
| `bio_compare_variants` | partially novel | Variant comparison appears in some research tools; MCP-tool form uncommon |
| `bio_fetch_acmg_criteria` | partially novel | Reference data; useful as orchestration pre-req |
| `bio_classify_variant_acmg` | **novel** | Full ACMG criteria orchestration with evidence trace and Pejaver 2022 calibration — checked; not seen at this depth |
| `bio_resolve_test_eligibility` | **novel** | UK clinical-pathway eligibility orchestration absent from existing ecosystem |
| `bio_audit_classification_chain` | **novel** | Audit-trail reconstruction over upstream-state-change — checked; not seen |
| `bio_disambiguate_variant` | partially novel | Mutalyzer/Variant Recoder wrappers exist; MCP form uncommon |

**Summary:**

- 11 tools shared with existing ecosystem (canonical wrappers)
- 13 tools partially novel (existing equivalents lack key features this version provides)
- 11 tools fully novel (no existing equivalent in published biomedical MCP ecosystem)

That's a roughly 60% novelty contribution across the full Phase 4 plan, against an ecosystem that has substantial existing coverage of Phase 1-3 surfaces. The novelty concentration is heaviest in Phase 4 clinical genomics, especially UK-clinical-pathway tooling and ACMG criteria orchestration.

---

## 16. Origin story and project identity

This section is non-technical. It documents *why this server exists* in a form that will survive contributors, downstream users, and time. The project is unusual in having a clean origin narrative; preserving it serves later collaborators trying to understand design decisions that read as opinionated.

### 16.1 The Iggy lineage

The project began with a domestic-cat genetics question: a Maine Coon kitten (Iggy, named after the Ig Nobel Prizes) prompted reading about feline cardiomyopathy genetics, which led to the broader feline AIM/CD5L paper (Sugisawa et al., *Sci Rep* 6:35251, 2016). A prior LLM transcript about that paper confidently attributed findings to non-existent "Miyazaki/Nakata work" without verifiable DOI, and named specific residues that the actual paper does not.

### 16.2 The verification-first response

The pattern was familiar — confident-sounding fabrication of specifics, in a domain where wrong answers are not just unhelpful but actively harmful (someone might cite the made-up Miyazaki/Nakata reference; someone might design a CRISPR guide based on hallucinated residue numbers). The response was infrastructural, not content-level: replace the confident-sounding answer with a server that retrieves verifiable specifics from primary databases, and refuses to confabulate when retrieval fails.

The founding worked example — verifying CD5L sequence and features from UniProt, the Sugisawa fulltext from Europe PMC, the cryo-EM CD5L-IgM interface from PDB, and CRISPR guides from CRISPOR against felCat9 — became the project's first integration test.

### 16.3 The cross-disciplinary scope

Project scope expanded beyond feline genetics deliberately. The same fabrication mode appears in human clinical genomics (ACMG classifications, gnomAD frequencies, ClinVar review status), peptide pharmacology (binding affinities, residue claims), structural biology (interface residues, conformational claims). Building a feline-specific server would have served Iggy but missed the wider point. The current scope spans human + veterinary + structural + pharmacological + literature-citation grounding because the underlying problem is shared.

The user's own cross-disciplinary interests — sleep chronobiology, oral chemistry, peptide research, aquarium ecology, IT systems — informed the wider scope in a structurally similar way: each domain has its own confident-fabrication failure mode, each benefits from primary-source grounding, each can be addressed with the same architectural pattern.

### 16.4 The publishable trajectory

The decision to license Apache-2.0 and publish from day one rather than building privately was based on the recognition that the wider biomedical MCP ecosystem (BioContextAI, BioMCP, MCPmed) is solving overlapping problems and benefits from interoperable patterns. This server's specific contributions — UK clinical-pathway tooling, full ACMG orchestration with evidence trace, the cross-cutting provenance framework — are useful upstream as well as downstream. Contribution pathway documented in Section 14.

### 16.5 The user's working principle

"Truth and accuracy over convenience; correct me if I'm wrong; I'll do the same for you." This principle has shaped specification quality across every session. Pre-work reports consistently catch real spec errata (eleven captured in Section 8). Tools surface their own limits rather than hiding them. The project's anti-hallucination thesis applies recursively to its own engineering decisions: when the project's specifications are unverified, they get probed before being committed; when the project's tools cannot verify a claim, they say so.

That principle is the project's identity, more than any specific tool list.

---

**End of specification v3.0.**
