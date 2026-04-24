# Bioinformatics Primary-Source MCP Server — Specification v2

**Target implementer:** Claude Code
**Target deployer:** Devlin's Proxmox homelab (pve2 LXC)
**Project name:** `bioinformatics_mcp`
**Version:** 2.0 (expanded tool set)

---

## 1. Purpose

A Model Context Protocol server that replaces model-hallucinated molecular biology facts with verified fetches from primary databases. Any Claude instance connected to this MCP can ground answers about sequences, structures, variants, compounds, bioactivity, alignments, and CRISPR designs in real, retrievable data from NCBI, UniProt, EBI, Ensembl, AlphaFold DB, RCSB PDB, ChEMBL, PubChem, InterPro, Europe PMC, Reactome, and STRING — rather than pattern-matched training data.

**Concrete first test case:** designing a CRISPR gRNA to correct the feline AIM/CD5L SRCR3 positively-charged cluster responsible for 1000-fold elevated IgM binding (Sugisawa et al. 2016, *Sci Rep* 6:35251).

**Deliberate scope choice:** this is a general bioinformatics grounding tool, not a cat-specific one. It handles human, mouse, any model organism, any sequenced pathogen, any pharmacology / peptide / SNP / protein-domain / structural / literature question. Scoping it narrowly to the cat problem would waste the infrastructure.

**Anti-hallucination targets:** this spec is deliberately designed to catch Claude's most common fabrication modes in bioinformatics —
- Fake residue numbers and domain boundaries (→ UniProt, InterPro)
- Fake sequences and accessions (→ NCBI, UniProt)
- Fake structures and interface residues (→ PDB, AlphaFold DB)
- Fake SNP consequences and allele frequencies (→ Ensembl VEP, gnomAD via Ensembl)
- Fake drug binding affinities, IC50s, and target interactions (→ ChEMBL, PubChem)
- Fake citations and misattributed findings (→ Europe PMC, PubMed)
- Fake gRNA sequences and off-target tables (→ CRISPOR)
- Fake pathway membership and protein-protein interactions (→ Reactome, STRING)

---

## 2. Architecture Decisions

### 2.1 Language: Python (FastMCP)

Deviates from the general MCP guidance toward TypeScript. Justified because:

- **Biopython** is the canonical NCBI/Entrez client; no comparable TypeScript equivalent
- **ViennaRNA** has mature Python bindings; RNA folding is painful to reimplement
- **CRISPOR** is a Python CLI — wrapping it from Python avoids process-boundary serialisation complexity
- **python-codon-tables** and most cheminformatics libraries are Python-native
- Devlin's homelab already runs Python services comfortably

### 2.2 Transport: Streamable HTTP (primary), stdio (optional)

- **Primary:** Streamable HTTP, so Claude.ai and Claude Code on any device can connect to the LXC over LAN/VPN
- **Optional:** stdio for local testing via MCP Inspector

Bind to `127.0.0.1` on the LXC, expose via reverse proxy (Caddy) with bearer-token auth. Do **not** bind to `0.0.0.0` directly.

### 2.3 Deployment: LXC on pve2

Matches Devlin's existing pattern. Updated resource requirements for expanded tool set:

- Unprivileged LXC, **Debian 13 (Trixie)** base — aligns with Proxmox VE 9.x host OS; ships Python 3.13 as default
- **4 vCPU, 6 GB RAM** (up from 2/4 — more APIs, InterProScan results can be large)
- **30 GB root + 80 GB mount** for genome indexes + ChEMBL cache + AlphaFold cache (`/var/lib/bioinformatics_mcp/`)
- Bridge `vmbr0`, static IP
- Systemd service: `bioinformatics-mcp.service`

**Python version policy:**
- MCP server runs on Python 3.13 (Trixie default) — `pyproject.toml` keeps `requires-python = ">=3.11"` for flexibility
- CRISPOR runs in a separate venv. If it proves incompatible with Python 3.13 (older codebase, may depend on removed stdlib modules), pin CRISPOR's venv to `python3.11` or `python3.12` — both are available as versioned packages on Trixie alongside 3.13. Verify CRISPOR compatibility during deployment phase.

**PEP 668 note:** Debian 13 enforces PEP 668 (no system-wide pip installs). All Python installs in this project use venvs per spec §9.2; no changes needed, but worth noting for Claude Code so it doesn't try `pip install` outside a venv during deployment.

### 2.4 Authentication and API Keys

| Service | Auth Required | Env Var | Notes |
|---------|--------------|---------|-------|
| NCBI E-utilities | Optional (recommended) | `NCBI_API_KEY` | 10 req/s with key, 3 req/s without |
| UniProt | None | — | — |
| EBI (Clustal, InterProScan) | Email required | `EBI_EMAIL` | Per EBI policy |
| Ensembl REST | None | — | — |
| AlphaFold EBI | None | — | — |
| RCSB PDB | None | — | — |
| ChEMBL | None | — | — |
| PubChem | None | — | Unreasonable use = blocked |
| Europe PMC | None | — | — |
| Reactome | None | — | — |
| STRING | None | — | Contact email in user-agent recommended |
| **MCP server itself** | Bearer token at proxy | `MCP_AUTH_TOKEN` | Validated at Caddy layer |

---

## 3. Tool Selection Guide

Before tool specs, a decision table so Claude picks the right tool for each question type. This lives in the server's top-level description so the model sees it when tools are listed.

| Question type | First tool to reach for | If that doesn't have it |
|---------------|-------------------------|-------------------------|
| "What is the sequence of gene/protein X?" | `bio_fetch_sequence` (NCBI) or `bio_fetch_uniprot` | BLAST search by name |
| "What domains are in protein X?" | `bio_fetch_uniprot` (curated) | `bio_scan_domains` (InterProScan) |
| "What's the structure of protein X?" | `bio_fetch_pdb` (if known) | `bio_fetch_alphafold` (predicted) |
| "Align these N sequences" | `bio_align_sequences` | — |
| "Find proteins similar to this sequence" | `bio_blast_search` | — |
| "Design a CRISPR guide for target X" | `bio_design_grna` | — |
| "Will this ssODN / RNA fold well?" | `bio_fold_sequence` | — |
| "What does SNP rsXXX do?" | `bio_fetch_variant` | `bio_predict_variant_effect` for consequences |
| "What's the effect of variant chrX:Y A>G?" | `bio_predict_variant_effect` | — |
| "What does compound X bind to?" | `bio_fetch_bioactivity` (ChEMBL) | `bio_fetch_compound` for structure |
| "What's the IC50 of drug X at target Y?" | `bio_fetch_bioactivity` | — |
| "Does paper X exist, and what does it actually say?" | `bio_search_literature` → `bio_fetch_paper_fulltext` | `bio_fetch_pubmed` (abstract only) |
| "What pathway is protein X in?" | `bio_fetch_pathway` (Reactome) | — |
| "What does protein X interact with?" | `bio_fetch_interactions` (STRING) | — |
| "Where is gene X on the genome? What exons?" | `bio_fetch_gene` (NCBI) | — |
| "Optimise this protein for expression in E. coli" | `bio_codon_optimise` | — |

**Meta-instruction for the server:** if a question falls into any of these categories, the model should prefer calling the relevant tool over answering from training data, even when the answer feels certain. Training data is outdated; primary databases are current.

---

## 4. Tool Specifications

All tools follow the naming convention `bio_{action}_{resource}`. All return structured content (JSON by default, Markdown on request via `response_format` parameter). All MVP read tools are annotated `readOnlyHint: true, destructiveHint: false, openWorldHint: true`.

### MVP — Phase 1 (10 tools, deliverable in ~2 weekends)

#### 4.1 `bio_fetch_sequence`

Fetch nucleotide or protein sequence from NCBI by accession.

**Input:**
```python
class FetchSequenceInput(BaseModel):
    accession: str = Field(..., description="NCBI accession (e.g., 'LC149874', 'NM_001242462', 'NP_001229391')", min_length=3, max_length=30)
    database: Literal["nucleotide", "protein"]
    rettype: Literal["fasta", "gb", "gp"] = Field(default="fasta", description="fasta=sequence only, gb=GenBank full record, gp=GenPept full record")
    response_format: Literal["json", "markdown"] = Field(default="json")
```

**Output:** sequence + metadata (length, organism, feature table if `gb`/`gp`). Feature table parsed with Biopython's `SeqIO` into structured list of `{type, location, qualifiers}`.

**Implementation:** `Bio.Entrez.efetch`. Shared rate-limited client (10 req/s with API key, 3 req/s without). Retry on transient 429/503 with exponential backoff via `tenacity`.

---

#### 4.2 `bio_fetch_uniprot`

Fetch protein record from UniProt by accession with curated domain/feature annotations.

**Input:**
```python
class FetchUniProtInput(BaseModel):
    accession: str = Field(..., pattern=r"^[A-Z0-9]{6,10}$", description="UniProt accession (e.g., 'A0A1E1GEY0', 'O43866', 'Q9QWK4')")
    include_features: bool = Field(default=True, description="Include domain/region annotations")
    response_format: Literal["json", "markdown"] = Field(default="json")
```

**Output:** sequence, length, organism, all annotated features (domains, disulfide bonds, active sites, PTMs, variants), cross-references (PDB, RefSeq, GenBank, AlphaFold, InterPro).

**Implementation:** `https://rest.uniprot.org/uniprotkb/{accession}.json`.

---

#### 4.3 `bio_fetch_pdb`

Fetch experimentally-determined protein structure from RCSB PDB.

**Input:**
```python
class FetchPDBInput(BaseModel):
    pdb_id: str = Field(..., pattern=r"^[0-9][A-Za-z0-9]{3}$", description="4-character PDB ID (e.g., '7XKB', '1CRN')")
    include_coordinates: bool = Field(default=False, description="If True, includes full atomic coordinates (can be large). If False, returns metadata only.")
    chain_filter: Optional[str] = Field(default=None, description="If set, only return data for this chain (e.g., 'A')")
```

**Output:** structure metadata (resolution, experimental method, deposition date), chain list with sequences, ligands, bound proteins, Ca²⁺/metal sites, R-factor, space group. If `include_coordinates=True`, also returns the mmCIF file content.

**Implementation:**
- Metadata: `https://data.rcsb.org/rest/v1/core/entry/{pdb_id}` + `/polymer_entity/{pdb_id}/{entity_id}`
- Coordinates: `https://files.rcsb.org/download/{pdb_id}.cif`

**Why MVP:** structural claims about proteins are a frequent fabrication zone. The 2024 cryo-EM paper (PDB 8VEJ) gave real AIM/IgM interface residues — that's a verifiable fetch, not a guess.

---

#### 4.4 `bio_fetch_alphafold`

Fetch AlphaFold2 predicted structure from EBI AlphaFold Database.

**Input:**
```python
class FetchAlphaFoldInput(BaseModel):
    uniprot_accession: str = Field(..., pattern=r"^[A-Z0-9]{6,10}$", description="UniProt accession to fetch prediction for")
    format: Literal["pdb", "cif", "summary"] = Field(default="summary", description="summary=metadata + pLDDT summary, pdb/cif=full structure file")
```

**Output:**
- Summary: UniProt ID, model version, mean pLDDT (overall confidence 0–100), per-region pLDDT (N-term, middle, C-term), presence of PAE (predicted aligned error) matrix
- Full PDB/CIF: structure file content

**Implementation:**
- Metadata: `https://alphafold.ebi.ac.uk/api/prediction/{accession}`
- Structure: `https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.pdb`

**Why MVP:** RCSB covers ~200K experimentally-determined structures. AlphaFold DB covers ~200M predictions. Together they give near-complete structural coverage.

**Critical note for the model:** pLDDT < 70 regions are unreliable; the server must return pLDDT alongside coordinates so the model doesn't over-trust low-confidence predictions.

---

#### 4.5 `bio_align_sequences`

Multiple sequence alignment via EBI Clustal Omega.

**Input:**
```python
class SequenceRecord(BaseModel):
    id: str = Field(..., min_length=1, max_length=50)
    sequence: str = Field(..., min_length=1, max_length=50000)

class AlignSequencesInput(BaseModel):
    sequences: List[SequenceRecord] = Field(..., min_length=2, max_length=500)
    sequence_type: Literal["protein", "dna", "rna"]
    output_format: Literal["clustal", "fasta", "msf"] = Field(default="clustal")
```

**Output:** alignment + statistics (length, pairwise identity %, gap %, conserved-column count). In Markdown mode, include a pretty-printed conservation view.

**Implementation:** EBI Clustal Omega async REST. Submit → poll every 2 s → fetch result. Timeout 300 s. Requires `EBI_EMAIL`.

---

#### 4.6 `bio_blast_search`

Sequence similarity search via NCBI BLAST.

**Input:**
```python
class BlastSearchInput(BaseModel):
    query_sequence: str = Field(..., min_length=10, max_length=100000, description="Raw sequence, no FASTA header")
    program: Literal["blastn", "blastp", "blastx", "tblastn"]
    database: Literal["nt", "nr", "refseq_protein", "refseq_rna", "swissprot"]
    organism_filter: Optional[str] = Field(default=None, description="Taxon restriction, e.g., 'Felis catus [ORGN]' or 'Mammalia [ORGN]'")
    max_hits: int = Field(default=20, ge=1, le=100)
    e_value: float = Field(default=10.0, ge=0.0)
```

**Output:** ranked hits with accession, description, % identity, E-value, bit score, alignment spans.

**Implementation:** NCBI BLAST URL API (`Put` → `Get`). Poll every 10 s, timeout 600 s. Return partial results with warning if timed out.

---

#### 4.7 `bio_design_grna`

CRISPR gRNA design with real off-target analysis — **the single most important anti-hallucination tool in this spec.**

**Input:**
```python
class DesignGRNAInput(BaseModel):
    target_sequence: str = Field(..., min_length=50, max_length=2000, pattern=r"^[ACGTNacgtn]+$")
    genome: str = Field(..., description="Genome assembly ID (e.g., 'felCat9', 'hg38', 'mm39', 'danRer11')")
    pam: Literal["NGG", "NG", "NNGRRT", "TTTV"] = Field(default="NGG", description="NGG=SpCas9, NG=SpCas9-NG, NNGRRT=SaCas9, TTTV=Cas12a")
    max_guides: int = Field(default=10, ge=1, le=50)
```

**Output:** ranked gRNAs, each with:
- 20 nt spacer + PAM sequence
- Cut site position in target
- Doench 2016 on-target efficiency score
- MIT specificity score
- CFD score
- Off-target sites (up to 4 mismatches) with genomic coordinates, mismatch count, CDS/intron/intergenic classification

**Implementation:** wrap `crispor.py` from `https://github.com/maximilianh/crisporWebsite`. Genome indexes pre-downloaded to `/var/lib/bioinformatics_mcp/genomes/{genome}/`. First run per genome takes ~30 s to warm cache.

**Genome management:** `scripts/fetch_genome.sh <genome_id>` downloads and indexes a genome on demand. Ship with `hg38`, `mm39`, `felCat9` pre-indexed.

---

#### 4.8 `bio_fold_sequence`

RNA / DNA secondary structure prediction.

**Input:**
```python
class FoldSequenceInput(BaseModel):
    sequence: str = Field(..., min_length=10, max_length=5000)
    sequence_type: Literal["rna", "dna"]
    temperature: float = Field(default=37.0, ge=0.0, le=100.0, description="Folding temperature in °C")
```

**Output:** MFE structure in dot-bracket notation, ΔG (kcal/mol), base-pairing probability summary (mean pairing probability per position).

**Implementation:** ViennaRNA Python bindings. For DNA, `RNA.params_load_DNA_Mathews2004()`. MFE from `fc.mfe()`.

---

#### 4.9 `bio_fetch_compound`

Small molecule / compound data — **critical for peptide and drug research.**

**Input:**
```python
class FetchCompoundInput(BaseModel):
    identifier: str = Field(..., description="Compound identifier: name, SMILES, InChI, ChEMBL ID, or PubChem CID")
    identifier_type: Literal["name", "smiles", "inchi", "chembl_id", "pubchem_cid"]
    source: Literal["chembl", "pubchem", "both"] = Field(default="both", description="Which database(s) to query")
```

**Output:**
- Canonical SMILES, InChI, InChIKey
- Molecular formula, MW, LogP, H-bond donors/acceptors, rotatable bonds
- Synonyms (brand names, IUPAC name, common names)
- ChEMBL ID and PubChem CID cross-reference
- Known targets (from ChEMBL, high-level list; detailed bioactivity via `bio_fetch_bioactivity`)
- Clinical phase (for drugs in ChEMBL: 0/1/2/3/4/Approved)

**Implementation:**
- PubChem: `https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{identifier_type}/{identifier}/JSON`
- ChEMBL: `https://www.ebi.ac.uk/chembl/api/data/molecule/search?q={query}` or `/molecule/{chembl_id}.json`

**Why MVP:** direct hit on Devlin's peptide and pharmacology work. Aspirin, GHK-Cu, BPC-157, semaglutide, tirzepatide — all have structured, verifiable entries. Replaces pattern-matched answers about compound properties.

---

#### 4.10 `bio_fetch_bioactivity`

Measured drug-target binding and activity data from ChEMBL.

**Input:**
```python
class FetchBioactivityInput(BaseModel):
    query_type: Literal["compound", "target"] = Field(..., description="Search by compound (what does this drug hit?) or by target (what hits this receptor?)")
    identifier: str = Field(..., description="ChEMBL ID (e.g., 'CHEMBL25' for aspirin, 'CHEMBL204' for thrombin) or UniProt accession for targets")
    activity_types: List[Literal["IC50", "Ki", "Kd", "EC50", "AC50", "Potency"]] = Field(default_factory=lambda: ["IC50", "Ki", "Kd"])
    max_results: int = Field(default=50, ge=1, le=500)
    min_confidence: int = Field(default=7, ge=0, le=9, description="ChEMBL confidence score (7+ = direct single protein target)")
```

**Output:** list of measured bioactivities, each with:
- Compound ChEMBL ID + preferred name
- Target ChEMBL ID + UniProt accession + preferred name
- Activity type (IC50/Ki/etc.), value, standard units (nM), relation (=, <, >)
- Assay description + assay type (B=binding, F=functional, A=ADMET, T=toxicity)
- ChEMBL confidence score (assay-to-target mapping confidence)
- Source document (PubMed ID, DOI)

**Implementation:** ChEMBL `https://www.ebi.ac.uk/chembl/api/data/activity.json?molecule_chembl_id={id}` or `?target_chembl_id={id}`, with filters applied server-side.

**Critical:** this is the tool that catches the most clinically-dangerous hallucinations — fake binding affinities with fake sources. The confidence score filter matters: low-confidence assays shouldn't be cited as binding affinities.

---

### Phase 2 — essential extensions (6 tools)

#### 4.11 `bio_fetch_variant`

Variant details by rsID or coordinates.

**Input:**
```python
class FetchVariantInput(BaseModel):
    identifier: str = Field(..., description="rsID (e.g., 'rs429358') or coordinate string ('7:140453136:A:T' for GRCh38)")
    species: str = Field(default="human", description="Species name (e.g., 'human', 'mouse', 'cat')")
    assembly: Optional[str] = Field(default=None, description="Genome assembly. Default: latest per species (GRCh38 for human).")
```

**Output:** alleles, genomic coordinates, mapped gene(s), consequences (synonymous/missense/etc.), MAF from 1000G/gnomAD, ClinVar significance if present.

**Implementation:** Ensembl REST `/variation/{species}/{id}` or `/overlap/region/`.

---

#### 4.12 `bio_predict_variant_effect`

Ensembl VEP — predict functional consequence of a variant.

**Input:**
```python
class VEPInput(BaseModel):
    variant: str = Field(..., description="HGVS notation (e.g., 'ENST00000003084:c.1431_1433delTTC') or 'chrom:pos:ref:alt' format")
    species: str = Field(default="human")
```

**Output:** per-transcript consequences (missense/stop-gained/splice site/etc.), SIFT score, PolyPhen score, affected protein position, amino acid change, regulatory feature overlaps.

**Implementation:** Ensembl REST `/vep/{species}/hgvs/{hgvs}` or `/vep/{species}/region/`.

---

#### 4.13 `bio_scan_domains`

InterProScan — predict domain architecture from a protein sequence.

**Input:**
```python
class ScanDomainsInput(BaseModel):
    sequence: str = Field(..., min_length=20, max_length=40000, description="Protein sequence")
    applications: List[Literal["Pfam", "SMART", "PROSITE", "CDD", "SUPERFAMILY", "Gene3D"]] = Field(default_factory=lambda: ["Pfam", "SMART", "CDD"])
```

**Output:** list of matches: signature ID, signature database, name, description, E-value, start/end positions.

**Implementation:** EBI InterProScan REST (async, same pattern as Clustal Omega). Requires `EBI_EMAIL`. Can take 2–5 min for long sequences.

**Why phase 2:** UniProt has curated domains for most proteins; InterProScan is the fallback for novel sequences, uncharacterised orthologues, or unreviewed TrEMBL entries. Uncurated feline CD5L variants might need this.

---

#### 4.14 `bio_search_literature`

Europe PMC search.

**Input:**
```python
class SearchLiteratureInput(BaseModel):
    query: str = Field(..., min_length=3, max_length=500, description="Europe PMC query syntax; accepts free text and MeSH terms")
    max_results: int = Field(default=20, ge=1, le=100)
    open_access_only: bool = Field(default=False)
    year_from: Optional[int] = Field(default=None, ge=1800, le=2100)
    year_to: Optional[int] = Field(default=None, ge=1800, le=2100)
```

**Output:** ranked papers: title, authors (first 5 + et al.), journal, year, DOI, PMID, PMC ID if open-access, abstract.

**Implementation:** `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=...&format=json`.

---

#### 4.15 `bio_fetch_paper_fulltext`

Fetch full text of an open-access paper.

**Input:**
```python
class FetchFullTextInput(BaseModel):
    identifier: str = Field(..., description="PMC ID (e.g., 'PMC5059666') or DOI")
    identifier_type: Literal["pmc", "doi"]
    sections: Optional[List[str]] = Field(default=None, description="If set, return only these sections (e.g., ['Methods', 'Results'])")
```

**Output:** paper structure (sections with headers), full text per section, figure/table captions.

**Implementation:** Europe PMC full-text API `https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML` — parse XML, return structured text.

**Critical:** this is the tool that would have let us verify Sugisawa 2016 directly instead of relying on abstract snippets. Open-access coverage only; closed-access papers return a redirect to the publisher and a clear error message.

---

#### 4.16 `bio_fetch_gene`

NCBI Gene record with full genomic context.

**Input:**
```python
class FetchGeneInput(BaseModel):
    identifier: str = Field(..., description="Gene symbol ('BRCA1', 'CD5L') or NCBI Gene ID (e.g., '672')")
    organism: str = Field(default="Homo sapiens", description="Scientific name of organism")
```

**Output:** NCBI Gene ID, official symbol, full name, synonyms, chromosome + band, genomic coordinates (start/end/strand) on reference assembly, RefSeq transcript list (NM_ accessions), exon structure (count + coordinates), GO annotations (biological process, molecular function, cellular component), cross-references to UniProt, Ensembl, MGI/MGD.

**Implementation:** `Bio.Entrez.esearch` on gene database → `Entrez.esummary` + `Entrez.efetch` for full record.

**Why needed:** step 1 of CRISPR design is "find the exon containing the target residue." Without this tool, Claude would have to guess.

---

### Phase 3 — nice-to-haves (3 tools)

#### 4.17 `bio_fetch_pathway`

Reactome pathway data.

**Input:**
```python
class FetchPathwayInput(BaseModel):
    identifier: str = Field(..., description="Reactome pathway ID (e.g., 'R-HSA-109581'), gene symbol, or UniProt ID")
    identifier_type: Literal["pathway_id", "gene_symbol", "uniprot"]
    species: str = Field(default="Homo sapiens")
```

**Output:** pathway name, summary, participating entities (proteins, small molecules, complexes), reactions, parent/child pathways.

**Implementation:** Reactome Content Service `https://reactome.org/ContentService/data/`.

---

#### 4.18 `bio_fetch_interactions`

STRING protein-protein interaction network.

**Input:**
```python
class FetchInteractionsInput(BaseModel):
    identifier: str = Field(..., description="Gene symbol, UniProt accession, or STRING ID")
    species_taxon: int = Field(default=9606, description="NCBI taxon ID (9606=human, 10090=mouse, 9685=cat)")
    min_score: int = Field(default=700, ge=150, le=1000, description="STRING combined score threshold (700=high confidence, 900=highest)")
    max_partners: int = Field(default=20, ge=1, le=100)
```

**Output:** interaction partners with STRING combined score, evidence breakdown (experimental, database, text-mining, co-expression, neighbourhood, fusion, co-occurrence).

**Implementation:** STRING REST `https://string-db.org/api/json/network?identifiers=...`.

---

#### 4.19 `bio_codon_optimise`

Codon optimisation for recombinant expression.

**Input:**
```python
class CodonOptimiseInput(BaseModel):
    protein_sequence: str = Field(..., min_length=5, max_length=10000, description="Target protein sequence")
    target_organism: Literal["ecoli_k12", "h_sapiens", "s_cerevisiae", "p_pastoris", "cho", "sf9"]
    avoid_restriction_sites: List[str] = Field(default_factory=list, description="Restriction enzyme sites to avoid, e.g., ['GAATTC' (EcoRI), 'AAGCTT' (HindIII)]")
```

**Output:** optimised DNA sequence, codon adaptation index (CAI), GC content, rare codon count, any restriction site conflicts (if avoidance failed).

**Implementation:** `python-codon-tables` for codon usage, custom optimisation using CAI maximisation with restriction site avoidance.

---

## 5. Project Structure

```
bioinformatics_mcp/
├── pyproject.toml
├── README.md
├── .env.example
├── src/
│   └── bioinformatics_mcp/
│       ├── __init__.py
│       ├── server.py                  # FastMCP app, tool registration, tool selection guide in server description
│       ├── config.py
│       ├── clients/
│       │   ├── __init__.py
│       │   ├── base.py                # RateLimitedClient, error helpers
│       │   ├── ncbi.py                # Entrez (sequence, gene, blast, pubmed)
│       │   ├── uniprot.py
│       │   ├── ebi.py                 # Clustal Omega, InterProScan (shared async pattern)
│       │   ├── ensembl.py             # Variants + VEP
│       │   ├── alphafold.py
│       │   ├── rcsb.py                # PDB
│       │   ├── chembl.py              # Compounds + bioactivity
│       │   ├── pubchem.py
│       │   ├── europepmc.py           # Literature search + fulltext
│       │   ├── reactome.py
│       │   └── string_db.py
│       ├── tools/                     # One file per tool
│       │   ├── __init__.py
│       │   ├── fetch_sequence.py
│       │   ├── fetch_uniprot.py
│       │   ├── fetch_pdb.py
│       │   ├── fetch_alphafold.py
│       │   ├── align_sequences.py
│       │   ├── blast_search.py
│       │   ├── design_grna.py
│       │   ├── fold_sequence.py
│       │   ├── fetch_compound.py
│       │   ├── fetch_bioactivity.py
│       │   ├── fetch_variant.py       # phase 2
│       │   ├── predict_variant_effect.py  # phase 2
│       │   ├── scan_domains.py        # phase 2
│       │   ├── search_literature.py   # phase 2
│       │   ├── fetch_paper_fulltext.py  # phase 2
│       │   ├── fetch_gene.py          # phase 2
│       │   ├── fetch_pathway.py       # phase 3
│       │   ├── fetch_interactions.py  # phase 3
│       │   └── codon_optimise.py      # phase 3
│       ├── models/
│       │   ├── __init__.py
│       │   └── schemas.py             # Shared Pydantic models
│       └── utils/
│           ├── __init__.py
│           ├── rate_limit.py
│           ├── formatting.py          # JSON ↔ Markdown
│           └── errors.py              # Actionable error response helpers
├── scripts/
│   ├── fetch_genome.sh                # download + index a genome for CRISPOR
│   ├── health_check.py                # end-to-end smoke test
│   └── evaluation.py                  # from mcp-builder skill
├── tests/
│   ├── test_clients/
│   ├── test_tools/
│   └── fixtures/                      # cached real API responses for offline tests
├── deploy/
│   ├── bioinformatics-mcp.service
│   ├── Caddyfile.example
│   └── lxc-provision.sh
└── eval/
    └── evaluation.xml
```

---

## 6. Dependencies

### Python packages (`pyproject.toml`)
```toml
[project]
name = "bioinformatics-mcp"
version = "0.2.0"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.2.0",
    "pydantic>=2.9",
    "httpx>=0.27",
    "biopython>=1.84",
    "viennarna>=2.6.4",
    "tenacity>=9.0",
    "python-dotenv>=1.0",
    "python-codon-tables>=0.1.12",   # for bio_codon_optimise
    "lxml>=5.0",                     # for Europe PMC fulltext XML parsing
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "pytest-httpx>=0.32", "respx>=0.21", "ruff>=0.7"]
```

### System packages (Debian 13 Trixie)
```
python3 python3-venv python3-dev python3-pip   # 3.13 by default on Trixie
python3.11 python3.11-venv                      # fallback for CRISPOR if 3.13 incompatible
build-essential libffi-dev   # for ViennaRNA native build
libxml2-dev libxslt1-dev     # for lxml
git curl                     # for CRISPOR + genome fetching
bwa                          # CRISPOR off-target search dependency
```

### External tools
- **CRISPOR**: clone `https://github.com/maximilianh/crisporWebsite` to `/opt/crispor`, install requirements in separate venv
- **Genome indexes**: pre-fetch `hg38`, `mm39`, `felCat9` via `scripts/fetch_genome.sh`
- **ChEMBL mirror (optional)**: for heavy use, a local SQLite ChEMBL download avoids API calls. Skip for v1.

---

## 7. Rate Limiting, Retries, Caching

### 7.1 Rate limiter (unchanged from v1)

Shared `RateLimitedClient` with per-service `max_concurrent` and `min_interval_s`. Use `tenacity` for retries on 429/503 with exponential backoff.

| Service | max_concurrent | min_interval_s |
|---------|----------------|----------------|
| NCBI (with API key) | 10 | 0.1 |
| NCBI (no key) | 3 | 0.34 |
| UniProt | 5 | 0.2 |
| EBI (Clustal, InterProScan) | 3 | 0.5 |
| Ensembl | 15 | 0.07 |
| AlphaFold EBI | 5 | 0.2 |
| RCSB | 10 | 0.1 |
| ChEMBL | 3 | 0.34 |
| PubChem | 5 | 0.2 |
| Europe PMC | 10 | 0.1 |
| Reactome | 5 | 0.2 |
| STRING | 3 | 1.0 |

### 7.2 Response caching

Add a light on-disk cache for deterministic lookups (sequence fetches, UniProt records, PDB metadata, literature searches with `year_to` set). Skip caching for variant tools (data updates), BLAST (database changes), and ChEMBL bioactivity (new assays).

- Cache dir: `/var/lib/bioinformatics_mcp/cache/`
- Implementation: `diskcache` package, keyed by tool name + sorted-JSON-of-input
- TTL: 30 days for sequence/structure fetches, 7 days for literature, off for mutable tools

Optional for MVP. Implement once >5 tools are working and API rate limits start to bite.

---

## 8. Error Handling

Every tool returns actionable errors. Standard pattern:

```python
from bioinformatics_mcp.utils.errors import error_response

try:
    result = await fetch(...)
except AccessionNotFound as e:
    return error_response(
        f"Accession '{e.accession}' not found in {e.database}.",
        suggestions=[
            "Check the accession format — protein accessions typically start with NP_, XP_, or a single letter + digits.",
            "If searching for a nucleotide, set database='nucleotide'.",
            f"Try bio_blast_search with the organism/gene name if the exact accession is unknown."
        ]
    )
except RateLimitExceeded as e:
    return error_response(
        f"{e.service} rate limit exceeded. Retry in {e.retry_after}s.",
        suggestions=[f"Set {e.env_var} env var for a higher rate limit."] if e.env_var else []
    )
except ExternalServiceDown as e:
    return error_response(
        f"{e.service} API is unreachable: {e.reason}.",
        suggestions=["This is a transient upstream error. Retry in a few minutes.", f"Status page: {e.status_url}" if e.status_url else ""]
    )
```

Never expose API keys, internal paths, or stack traces. Log those server-side only.

---

## 9. Deployment — pve2 LXC

### 9.1 LXC provisioning

```bash
# on pve2
# Note: verify exact template filename with `pveam available --section system | grep trixie`
# then `pveam download local <filename>` if not already present
pct create 200 local:vztmpl/debian-13-standard_13.1-1_amd64.tar.zst \
  --hostname bio-mcp \
  --cores 4 --memory 6144 --swap 2048 \
  --rootfs local-lvm:30 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1 \
  --unprivileged 1 \
  --onboot 1

pct start 200
# python3 on Trixie = 3.13; python3.11 kept available as fallback for CRISPOR if needed
pct exec 200 -- bash -c "apt update && apt install -y \
  python3 python3-venv python3-dev python3-pip \
  python3.11 python3.11-venv \
  git curl \
  build-essential libffi-dev libxml2-dev libxslt1-dev \
  bwa caddy"
```

### 9.2 Application install

```bash
pct exec 200 -- bash
adduser --system --group --home /opt/bioinformatics_mcp bio-mcp

# MCP server venv — Python 3.13 (Trixie default)
sudo -u bio-mcp git clone <repo> /opt/bioinformatics_mcp/app
sudo -u bio-mcp python3 -m venv /opt/bioinformatics_mcp/venv
sudo -u bio-mcp /opt/bioinformatics_mcp/venv/bin/pip install -e /opt/bioinformatics_mcp/app

# CRISPOR in its own venv
# First attempt: Python 3.13 (default). If install/runtime fails due to deprecated
# stdlib usage, rebuild venv with python3.11 — both are available on Trixie.
git clone https://github.com/maximilianh/crisporWebsite /opt/crispor
python3 -m venv /opt/crispor/venv
/opt/crispor/venv/bin/pip install -r /opt/crispor/requirements.txt
# Fallback if 3.13 fails: rm -rf /opt/crispor/venv && python3.11 -m venv /opt/crispor/venv && ...

# Data directories
mkdir -p /var/lib/bioinformatics_mcp/{genomes,cache,logs}
chown -R bio-mcp:bio-mcp /var/lib/bioinformatics_mcp

# Genome indexes (~3 GB each)
sudo -u bio-mcp /opt/bioinformatics_mcp/app/scripts/fetch_genome.sh felCat9
sudo -u bio-mcp /opt/bioinformatics_mcp/app/scripts/fetch_genome.sh hg38
sudo -u bio-mcp /opt/bioinformatics_mcp/app/scripts/fetch_genome.sh mm39
```

### 9.3 systemd service

```ini
# /etc/systemd/system/bioinformatics-mcp.service
[Unit]
Description=Bioinformatics MCP Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bio-mcp
Group=bio-mcp
WorkingDirectory=/opt/bioinformatics_mcp/app
EnvironmentFile=/etc/bioinformatics_mcp/env
ExecStart=/opt/bioinformatics_mcp/venv/bin/python -m bioinformatics_mcp.server
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/bioinformatics_mcp

[Install]
WantedBy=multi-user.target
```

### 9.4 Reverse proxy with auth (Caddy)

```caddy
# /etc/caddy/Caddyfile
bio-mcp.devlin.lan {
    @authed header Authorization "Bearer {env.MCP_AUTH_TOKEN}"
    handle @authed {
        reverse_proxy 127.0.0.1:8080
    }
    respond "Unauthorized" 401
}
```

### 9.5 Env file

```bash
# /etc/bioinformatics_mcp/env (chmod 600, owned by bio-mcp)
NCBI_API_KEY=<your_key_from_ncbi>
EBI_EMAIL=<your_email>
MCP_AUTH_TOKEN=<openssl rand -hex 32>
MCP_BIND_HOST=127.0.0.1
MCP_BIND_PORT=8080
CRISPOR_PATH=/opt/crispor
CRISPOR_PYTHON=/opt/crispor/venv/bin/python
GENOME_DIR=/var/lib/bioinformatics_mcp/genomes
CACHE_DIR=/var/lib/bioinformatics_mcp/cache
LOG_DIR=/var/lib/bioinformatics_mcp/logs
LOG_LEVEL=INFO
STRING_USER_EMAIL=<your_email>
```

---

## 10. Testing

### 10.1 Unit tests
- Mock HTTP responses per client with `pytest-httpx` or `respx`
- Pydantic validation boundary cases
- Rate limiter concurrent-access tests

### 10.2 Integration tests (opt-in via `RUN_INTEGRATION=1`)
Use stable, well-known accessions whose records don't change:
- `bio_fetch_sequence`: `NM_001301717` (human BRCA1 variant)
- `bio_fetch_uniprot`: `P01308` (human insulin)
- `bio_fetch_pdb`: `1CRN` (crambin — stable classic structure)
- `bio_fetch_alphafold`: `P01308` (predicted insulin structure)
- `bio_align_sequences`: three insulin orthologues
- `bio_fetch_compound`: `CHEMBL25` (aspirin) and PubChem CID 2244
- `bio_fetch_bioactivity`: `CHEMBL25` → expect COX-1, COX-2 hits
- `bio_fetch_variant`: `rs429358` (APOE ε4)
- `bio_search_literature`: `"Sugisawa 2016 AIM feline"` → expect PMC5059666

### 10.3 End-to-end smoke test
`scripts/health_check.py` calls each tool with a known-good input, prints pass/fail summary. Cron every 6 h. On failure, `curl` the Home Assistant webhook or UniFi notification endpoint.

### 10.4 MCP-level evaluation

`eval/evaluation.xml` with 10 Q/A pairs per the mcp-builder skill format. Candidate questions — answers determined empirically by first tool run:

1. "Length in amino acids of feline CD5L (UniProt A0A1E1GEY0)?"
2. "In human CD5L (UniProt O43866), at what residue position does the annotated SRCR3 domain begin?"
3. "Align feline (A0A1E1GEY0), mouse (Q9QWK4), and human (O43866) CD5L. At human alignment position 270, what residue is in feline?"
4. "For human PCSK9 exon 1 against hg38, what is the MIT specificity score of the highest-scoring SpCas9 gRNA?"
5. "Minimum free energy (ΔG) at 37°C of the RNA `GGGAAACCCUUUGGGAAACCC`?"
6. "Resolution (Å) of PDB structure 1CRN?"
7. "Mean pLDDT of the AlphaFold prediction for human insulin (P01308)?"
8. "According to ChEMBL, what is the highest-confidence measured IC50 (in nM) of aspirin (CHEMBL25) against human COX-1?"
9. "For rsID rs429358, what allele frequency does gnomAD report for the risk allele in European populations?"
10. "Does PMC5059666 (Sugisawa 2016) explicitly name specific amino acid residues as the feline AIM positively-charged cluster? Yes or No."

Answers locked in after first-run verification. Question 10 is an especially good evaluation because it directly tests the anti-hallucination purpose: the answer is No (per the full text fetch), and any Claude answering from training data would likely confabulate residue numbers.

---

## 11. Connection from Claude.ai / Claude Code

### 11.1 Claude.ai (remote MCP)
Settings → Connectors → Add custom connector:
- URL: `https://bio-mcp.devlin.lan/mcp`
- Auth: Bearer token (`MCP_AUTH_TOKEN` value)
- Transport: Streamable HTTP

### 11.2 Claude Code
`~/.config/claude-code/mcp.json`:
```json
{
  "mcpServers": {
    "bioinformatics": {
      "url": "https://bio-mcp.devlin.lan/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_AUTH_TOKEN}"
      }
    }
  }
}
```

For local stdio testing: `mcp dev src/bioinformatics_mcp/server.py`.

---

## 12. Implementation Order for Claude Code

Execute in this order — each step is independently verifiable via MCP Inspector before moving on.

### Phase 1 (MVP, ~2 weekends)

1. **Scaffold**: project structure, `pyproject.toml`, `.env.example`, empty stubs
2. **Shared infrastructure**: `config.py`, `utils/rate_limit.py` (with unit test), `utils/errors.py`, `utils/formatting.py`, `clients/base.py`
3. **`bio_fetch_sequence`** (NCBI) — tested with `NM_001301717`
4. **`bio_fetch_uniprot`** — tested with `P01308`
5. **`bio_fetch_pdb`** — tested with `1CRN`
6. **`bio_fetch_alphafold`** — tested with `P01308`
7. **`bio_align_sequences`** — tested with three insulin orthologues
8. **`bio_fold_sequence`** — tested with a known MFE hairpin
9. **`bio_fetch_compound`** (ChEMBL + PubChem) — tested with aspirin
10. **`bio_fetch_bioactivity`** — tested with `CHEMBL25` → expect COX hits
11. **`bio_blast_search`** — last of MVP (slowest to test)
12. **`bio_design_grna`** — requires LXC + genome indexes; heaviest tool, deferred to end of phase 1
13. **Deployment**: `lxc-provision.sh`, systemd unit, Caddyfile, smoke test cron

### Phase 2 (~1 weekend)

14. **`bio_fetch_gene`** (NCBI)
15. **`bio_fetch_variant`** + **`bio_predict_variant_effect`** (Ensembl)
16. **`bio_scan_domains`** (InterProScan)
17. **`bio_search_literature`** + **`bio_fetch_paper_fulltext`** (Europe PMC)

### Phase 3 (~weekend, optional)

18. **`bio_fetch_pathway`** (Reactome)
19. **`bio_fetch_interactions`** (STRING)
20. **`bio_codon_optimise`** (local)

### Post-implementation

21. **Evaluation**: `eval/evaluation.xml` with 10 Q/A, run `scripts/evaluation.py`
22. **Caching**: add `diskcache` for deterministic tools if rate limits bite

---

## 13. Success Criteria

The MCP server is correct when:

1. **Cat test case passes end-to-end**: real feline CD5L sequence from NCBI + real UniProt features + real alignment vs mouse/human + real AlphaFold predicted structure + real CRISPOR gRNAs with real off-target scores against felCat9 — zero fabricated residues, sequences, or scores.
2. **Pharmacology test case passes**: asked "what's the measured IC50 of aspirin against COX-1?", the server returns real ChEMBL bioactivity records with PubMed citations — the model cannot pattern-match an answer.
3. **Literature verification passes**: asked "does Sugisawa 2016 name specific residues?", the server fetches the full PMC text and the model can answer correctly without confabulating.
4. **All 10 evaluation Q/A pairs pass** via `scripts/evaluation.py`.
5. **Health check cron** has ≥99% 7-day uptime.
6. **Behavioural shift observable**: Claude connected to this MCP visibly prefers tool calls over training-data answers for in-scope questions.

---

## 14. Known Limitations and Non-Goals

- **No wet-lab substitute.** Binding, expression, in vivo efficacy remain bench experiments.
- **No live structure prediction.** AlphaFold DB gives pre-computed predictions; running AlphaFold live needs GPU resources pve2 lacks. ESMFold, ColabFold similarly excluded.
- **No molecular dynamics, docking, or phylogenetic tree building.** Wrong compute profile for an MCP.
- **No primer design (Primer3).** Scope creep; add if PCR work starts.
- **No commercial use.** Respect NCBI, EBI, Ensembl, ChEMBL, STRING, PubChem usage policies.
- **English only.** All databases are English.
- **ChEMBL confidence < 7 excluded from default bioactivity output.** Low-confidence assays can still mislead; included only on explicit request via `min_confidence` parameter.

---

## 15. References

- Sugisawa et al. 2016, *Sci Rep* 6:35251, DOI: 10.1038/srep35251
- Cryo-EM CD5L–IgM structure, *Nat Commun* 2024, DOI: 10.1038/s41467-024-53615-5
- MCP Protocol: https://modelcontextprotocol.io/
- FastMCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- CRISPOR: https://github.com/maximilianh/crisporWebsite — Concordet & Haeussler 2018, *NAR*, DOI: 10.1093/nar/gky354
- Biopython Entrez: https://biopython.org/docs/latest/Tutorial/chapter_entrez.html
- UniProt REST: https://www.uniprot.org/help/api
- EBI REST (Clustal, InterProScan): https://www.ebi.ac.uk/jdispatcher/docs/webservices
- Ensembl REST: https://rest.ensembl.org/
- AlphaFold DB API: https://alphafold.ebi.ac.uk/api-docs
- RCSB Data API: https://data.rcsb.org/
- ChEMBL API: https://www.ebi.ac.uk/chembl/api/data/docs
- PubChem PUG REST: https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest
- Europe PMC REST: https://europepmc.org/RestfulWebService
- Reactome Content Service: https://reactome.org/ContentService/
- STRING REST: https://string-db.org/help/api/

---

**End of specification v2.**

**Claude Code, when implementing:**
1. Follow `/mnt/skills/examples/mcp-builder/SKILL.md` — in particular, read `reference/mcp_best_practices.md` and `reference/python_mcp_server.md` before writing any tool.
2. Do not deviate from the tool naming convention `bio_{action}_{resource}`.
3. Include the tool selection guide (section 3 above) in the server's top-level `description` parameter so it appears in the MCP handshake — this is how the model knows which tool to pick.
4. Build one tool at a time in the order in section 12. Test each via MCP Inspector before moving on.
5. Run the evaluation in section 10.4 as the final correctness check.
