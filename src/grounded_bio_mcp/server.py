"""FastMCP 3.x server — spec §2, §5, §9.

Exposes the four phase-1a read tools over streamable HTTP (production)
or stdio (local MCP Inspector testing). The server's ``instructions``
field carries the spec §3 tool-selection guide verbatim so models listing
tools see *which* tool to reach for *first* for each question type —
that's how we replace training-data pattern matching with primary-source
fetches rather than merely exposing tools the model ignores.

Transport is HTTP by default; set ``MCP_TRANSPORT=stdio`` in the
environment to switch to stdio (for ``fastmcp dev`` / Inspector runs).
The HTTP server binds per ``config.get_settings()`` (spec §9.5), which
forbids public bind addresses because Caddy is the auth boundary.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Literal

from fastmcp import FastMCP

from grounded_bio_mcp import __version__
from grounded_bio_mcp.clients.alphafold import AlphaFoldClient
from grounded_bio_mcp.clients.base import RATE_LIMITS
from grounded_bio_mcp.clients.chembl import ChEMBLClient
from grounded_bio_mcp.clients.crispor import CrisporRunner
from grounded_bio_mcp.clients.ebi import EBIJobRunner
from grounded_bio_mcp.clients.ensembl import EnsemblClient
from grounded_bio_mcp.clients.europepmc import EuropePMCClient
from grounded_bio_mcp.clients.ncbi import NCBIClient
from grounded_bio_mcp.clients.pubchem import PubChemClient
from grounded_bio_mcp.clients.rcsb import RCSBClient
from grounded_bio_mcp.clients.reactome import ReactomeClient
from grounded_bio_mcp.clients.string_db import StringDBClient
from grounded_bio_mcp.clients.uniprot import UniProtClient
from grounded_bio_mcp.config import get_settings
from grounded_bio_mcp.tools.align_sequences import bio_align_sequences as _align_impl
from grounded_bio_mcp.tools.blast_search import bio_blast_search as _blast_impl
from grounded_bio_mcp.tools.codon_optimise import (
    bio_codon_optimise as _codon_optimise_impl,
)
from grounded_bio_mcp.tools.design_grna import (
    bio_design_grna as _design_grna_impl,
)
from grounded_bio_mcp.tools.fetch_alphafold import fetch_alphafold
from grounded_bio_mcp.tools.fetch_bioactivity import (
    bio_fetch_bioactivity as _bioactivity_impl,
)
from grounded_bio_mcp.tools.fetch_compound import (
    bio_fetch_compound as _compound_impl,
)
from grounded_bio_mcp.tools.fetch_gene import bio_fetch_gene as _gene_impl
from grounded_bio_mcp.tools.fetch_interactions import (
    bio_fetch_interactions as _interactions_impl,
)
from grounded_bio_mcp.tools.fetch_paper_fulltext import (
    bio_fetch_paper_fulltext as _fulltext_impl,
)
from grounded_bio_mcp.tools.fetch_pathway import bio_fetch_pathway as _pathway_impl
from grounded_bio_mcp.tools.fetch_pdb import fetch_pdb
from grounded_bio_mcp.tools.fetch_sequence import fetch_sequence
from grounded_bio_mcp.tools.fetch_uniprot import fetch_uniprot
from grounded_bio_mcp.tools.fetch_variant import bio_fetch_variant as _variant_impl
from grounded_bio_mcp.tools.fold_sequence import (
    bio_fold_sequence as _fold_impl,
)
from grounded_bio_mcp.tools.predict_variant_effect import (
    bio_predict_variant_effect as _vep_impl,
)
from grounded_bio_mcp.tools.scan_domains import bio_scan_domains as _scan_impl
from grounded_bio_mcp.tools.search_literature import (
    bio_search_literature as _literature_impl,
)
from grounded_bio_mcp.utils.errors import error_response
from grounded_bio_mcp.utils.rate_limit import RateLimitedClient

# ---------------------------------------------------------------------------
# Server-level instructions — spec §3 tool selection guide.
#
# Embedded verbatim in the MCP handshake's ``instructions`` field so the
# model picks the right primary-source tool instead of answering from
# training data, even when the answer feels certain.
# ---------------------------------------------------------------------------
SERVER_INSTRUCTIONS = """\
grounded-bio-mcp — Primary-Source Bioinformatics MCP Server
===========================================================

Grounds molecular-biology answers in live fetches from primary databases
(NCBI, UniProt, RCSB PDB, EBI AlphaFold DB, and more in later phases).
When a question falls into any category below, prefer calling the listed
tool over answering from training data — training data ages; primary
databases are current.

Tool selection guide (spec §3):

| Question type                                        | First tool                 | Fallback                                 |
|------------------------------------------------------|----------------------------|------------------------------------------|
| "What is the sequence of gene/protein X?"            | bio_fetch_sequence (NCBI) or bio_fetch_uniprot | BLAST search by name (later phase)    |
| "What domains are in protein X?"                     | bio_fetch_uniprot          | bio_scan_domains (InterProScan)          |
| "What's the structure of protein X?"                 | bio_fetch_pdb (if known)   | bio_fetch_alphafold (predicted)          |
| "What's the AlphaFold prediction for X?"             | bio_fetch_alphafold        | —                                        |
| "How similar are these N sequences?"                 | bio_align_sequences        | —                                        |
| "Does this uncharacterised protein have Pfam hits?"  | bio_scan_domains           | bio_fetch_uniprot (if curated)           |
| "What are the properties / structure of compound X?" | bio_fetch_compound         | bio_fetch_bioactivity (for binding data) |
| "What does compound X bind to, and how tightly?"     | bio_fetch_bioactivity      | bio_fetch_compound (for structure first) |
| "Where is gene X on the genome? What are its exons?" | bio_fetch_gene (NCBI)      | —                                        |
| "Does variant rsXXX exist? What are its alleles/MAF?"| bio_fetch_variant (Ensembl)| bio_predict_variant_effect (for function)|
| "What's the functional consequence of this variant?" | bio_predict_variant_effect | bio_fetch_variant (for population context)|
| "Does paper X exist? Find papers about Y."           | bio_search_literature      | —                                        |
| "What does paper X actually say?"                    | bio_fetch_paper_fulltext   | bio_search_literature (for candidate IDs)|
| "What pathway is protein X in?"                      | bio_fetch_pathway          | bio_fetch_uniprot (for protein-level context)|
| "What does protein X interact with?"                 | bio_fetch_interactions     | —                                        |
| "What sequences are similar to this one?"            | bio_blast_search           | bio_align_sequences (for known set)      |
| "Design a DNA sequence to express protein X in host Y." | bio_codon_optimise      | —                                        |
| "Will this ssODN / RNA fold well? What's its structure?" | bio_fold_sequence       | —                                        |
| "Design a CRISPR guide for target X."                | bio_design_grna            | —                                        |

bio_fetch_compound answers "what IS this compound" — SMILES, InChI, MW,
LogP, clinical phase, synonyms — from ChEMBL (drug-curated) and PubChem
(broad chemistry) with explicit per-field provenance.

bio_fetch_bioactivity answers "what does this compound DO" — measured
IC50/Ki/Kd values against named targets, filtered to ChEMBL confidence
≥ 7 by default so low-quality assays aren't cited as binding affinities.
Do NOT pattern-match drug target or affinity values from training data
— use this tool.

bio_fetch_gene answers "what gene is this and where is it" — NCBI Gene
ID, official symbol, chromosome + coordinates, exon count, RefSeq
transcripts (NM_/NP_), GO annotations, and cross-refs to UniProt and
Ensembl. When a symbol resolves to multiple Gene IDs (cross-species or
alias ambiguity), the tool surfaces candidate_gene_ids with
disambiguation context instead of picking arbitrarily.

bio_fetch_variant answers "does this variant exist, and what are its
alleles / population frequencies / clinical significance" — Ensembl
/variation lookup by rsID or chr:pos:ref:alt. Ensembl cannot
distinguish a real-but-unannotated rsID from a fabricated one (both
return the same 400 error); the tool exposes two honest outcomes
(found / not_found) plus an annotation_richness object with presence
flags for clinical_significance, population_frequencies, and
consequences. DO NOT invent rsIDs or allele frequencies from training
data — this tool is the anti-hallucination surface for variants.

bio_predict_variant_effect answers "what's the functional consequence
of this variant" — Ensembl VEP with HGVS.c/p/g or chr:pos:ref:alt
input. Returns three parallel consequence lists (transcript,
regulatory_feature, intergenic) so callers don't special-case based
on where the variant falls; SIFT and PolyPhen scores come through when
Ensembl provides them.

bio_search_literature answers "does paper X exist? what papers discuss
Y?" — Europe PMC search with free-text or MeSH terms, year range, and
open-access filter. Each hit carries fulltext_available so callers know
which ones can be followed up on with bio_fetch_paper_fulltext. This is
the front door to citation verification — do NOT pattern-match paper
titles or DOIs from training data, use this tool.

bio_fetch_paper_fulltext answers "what does paper X actually say?" —
Europe PMC JATS fulltext retrieval with an honest four-state
availability enum (full_xml / abstract_only / metadata_only / not_found)
plus a fulltext_unavailable_reason when fulltext is not retrievable.
Sections are a flat list of {title, level, text}; callers can filter
to specific sections (e.g. ["Methods"]). The tool NEVER fabricates
content for unavailable papers — if we only have an abstract, we return
the abstract and say so.

bio_fetch_pathway answers "what pathway is protein X in? what is
pathway R-HSA-nnnnnn?" — Reactome Content Service with three input
modes (pathway_id, uniprot, gene_symbol). Strict species filtering by
default; cross_species=True surfaces candidate_pathways for
disambiguation when a symbol exists in multiple organisms.

bio_fetch_interactions answers "what does protein X interact with?" —
STRING interaction partners with a seven-channel evidence breakdown
(neighbourhood, fusion, co_occurrence, coexpression, experimental,
database, textmining). SCORE SCALE: input min_score is 0-1000 (so 700
means 0.7 threshold); output scores are 0-1. Every response echoes
score_scale so the distinction is visible in structured output.
Default min_score=700 is high confidence; use 400 for medium, 900
for highest. DO NOT fabricate protein-protein interactions from
training data — use this tool.

bio_blast_search answers "find sequences similar to this one" — NCBI
BLAST URL API submit→poll→fetch with the four standard programs
(blastn/blastp/blastx/tblastn) and the five common databases
(nt/nr/refseq_protein/refseq_rna/swissprot). Distinct from
bio_align_sequences: BLAST searches the entire database for matches to
your query, while align_sequences aligns a known set of sequences you
already have. Hits chain naturally to bio_fetch_uniprot (protein
accessions like P01316) and bio_fetch_sequence (any NCBI accession).
Top-5 hits include alignment strings; the rest are metadata-only.
identical_sequence_count surfaces when one BLAST hit collapses
multiple identical DB records into a single result. Empty hits are
valid (truly novel sequences exist) and should not be treated as
errors. Set max_wait_seconds higher (up to 1800) for nr / blastn jobs
that legitimately take many minutes during peak hours.

bio_codon_optimise answers "design a DNA sequence to express this
protein in host Y" — greedy frequency-max codon optimisation against
six expression-host codon usage tables (E. coli, human, S. cerevisiae,
P. pastoris, CHO, Sf9), with synonymous-codon swaps to avoid forbidden
restriction sites. Reports CAI, GC%, rare-codon count, and any
restriction-site conflicts that survived (genuinely unavoidable).
openWorldHint=False — no upstream API, all data ships locally
(python-codon-tables for three organisms; bundled Kazusa CSVs for the
other three under data/codon_tables/).

bio_fold_sequence answers "what's the predicted secondary structure of
this RNA / DNA?" — ViennaRNA Python bindings under the Turner 2004
(RNA) or Mathews 2004 (DNA) parameter set, returning MFE dot-bracket
structure, ΔG in kcal/mol, and a per-position base-pairing probability
summary from the equilibrium partition function. Default temperature
37 °C; range 0-100 °C. Like bio_codon_optimise this is local-only
(openWorldHint=False) — no upstream API. Deterministic for fixed
inputs; provenance carries the ViennaRNA version so any reported
structure can be reproduced exactly.

bio_design_grna answers "design a CRISPR guide for this target with
real off-target analysis." CRISPOR subprocess against an indexed
genome; returns top-N guides ranked by MIT specificity, each with
spacer + PAM split, on-target locus, per-model efficiency scores
(Doench '16 / Moreno-Mateos / Azimuth / etc.), CFD specificity
computed locally per Doench 2016, and a real off-target table —
every row a BWA-verified mismatch-tolerant hit, every locus_class a
CRISPOR segments.bed lookup. NotEnoughFlankSeq in efficiency scores
surfaces as null + reason rather than silent zero. The single most
important anti-hallucination tool in the server: DO NOT pattern-match
guide sequences or off-target tables from training data — use this
tool. Genomes must be pre-indexed under GENOME_DIR; sacCer3 ships
with CRISPOR, felCat9/hg38/mm39 install per the LXC genome-fetch
process.

Phase 1 exposed eight tools. Phase 2 added bio_fetch_gene,
bio_fetch_variant, and bio_predict_variant_effect. Phase 3 added
bio_search_literature, bio_fetch_paper_fulltext (spec §4.14, §4.15),
bio_fetch_pathway (§4.17), and bio_fetch_interactions (§4.18).
Session 7 added bio_blast_search (§4.6) and bio_codon_optimise (§4.19).
Session 8a added bio_fold_sequence (§4.8) and bio_design_grna (§4.7),
completing the v2 spec — 19 tools live. Phase 4 (additional design /
validation tools) lands in subsequent sessions.

Confidence caveats (anti-hallucination):
  - AlphaFold predictions include per-residue pLDDT; regions with pLDDT < 70
    are unreliable and the tool surfaces an overall low_confidence_warning
    when the mean falls below that threshold.
  - bio_fetch_pdb returns mmCIF coordinates only when ``include_coordinates``
    is set, and soft-caps inlined files at ~2 MB; for larger structures the
    response includes a direct ``files.rcsb.org`` URL.
"""

_READ_ONLY_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
    "idempotentHint": True,
}

# Tools that compute purely from local data with no upstream API touch.
# Currently bio_codon_optimise (python-codon-tables + bundled Kazusa CSVs)
# and bio_fold_sequence (ViennaRNA Python bindings). openWorldHint flips to
# False because there is no external corpus that could change between calls;
# idempotentHint stays True because both algorithms are deterministic for
# fixed inputs.
_LOCAL_COMPUTE_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
    "idempotentHint": True,
}

# ---------------------------------------------------------------------------
# Long-lived clients
#
# Each upstream-service client wraps a single ``httpx.AsyncClient`` with an
# internal connection pool; recreating them per request would thrash the
# pool and evade the per-service rate-limit state. ``lru_cache`` keeps one
# instance per client type for the lifetime of the process.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _ncbi_client() -> NCBIClient:
    return NCBIClient(get_settings())


@lru_cache(maxsize=1)
def _uniprot_client() -> UniProtClient:
    return UniProtClient()


@lru_cache(maxsize=1)
def _rcsb_client() -> RCSBClient:
    return RCSBClient()


@lru_cache(maxsize=1)
def _alphafold_client() -> AlphaFoldClient:
    return AlphaFoldClient()


@lru_cache(maxsize=1)
def _ebi_client() -> RateLimitedClient:
    """One shared EBI rate-limited client, reused by all Job Dispatcher tools.

    Must be shared across `_clustalo_runner()` and `_iprscan5_runner()` so
    the EBI per-IP cap (3 concurrent / 500 ms, spec §7.1) applies across
    both services. Two separate instances would give 6 concurrent and
    violate EBI's terms.
    """
    params = RATE_LIMITS["ebi"]
    return RateLimitedClient(
        max_concurrent=params.max_concurrent,
        min_interval_s=params.min_interval_s,
        timeout=60.0,
        headers={"User-Agent": "grounded-bio-mcp/0.2 (+ebi-jobdispatcher)"},
    )


@lru_cache(maxsize=1)
def _chembl_client() -> ChEMBLClient:
    return ChEMBLClient()


@lru_cache(maxsize=1)
def _pubchem_client() -> PubChemClient:
    return PubChemClient()


@lru_cache(maxsize=1)
def _ensembl_client() -> EnsemblClient:
    return EnsemblClient()


@lru_cache(maxsize=1)
def _europepmc_client() -> EuropePMCClient:
    return EuropePMCClient()


@lru_cache(maxsize=1)
def _reactome_client() -> ReactomeClient:
    return ReactomeClient()


@lru_cache(maxsize=1)
def _string_client() -> StringDBClient:
    # STRING_USER_EMAIL is optional (unlike EBI_EMAIL); missing email
    # only produces a warning at client init. See `clients/string_db.py`.
    return StringDBClient(user_email=get_settings().string_user_email)


@lru_cache(maxsize=1)
def _crispor_runner() -> CrisporRunner:
    """Single CRISPOR subprocess runner per server process.

    Paths come from settings: ``CRISPOR_PATH`` (CRISPOR install dir),
    ``CRISPOR_PYTHON`` (the venv Python that resolves CRISPOR's
    requirements), ``GENOME_DIR`` (where indexed genomes live). Defaults
    target the LXC layout (``/opt/crispor`` etc.); dev overrides via
    ``.env``. The runner itself is stateless — each ``run`` call
    materialises its own temp directory — so caching one instance per
    process is safe under concurrent tool invocations.
    """
    s = get_settings()
    return CrisporRunner(
        crispor_python=s.crispor_python,
        crispor_path=s.crispor_path,
        genomes_dir=s.genome_dir,
        timeout_s=300.0,
    )


@lru_cache(maxsize=1)
def _clustalo_runner() -> EBIJobRunner:
    return EBIJobRunner("clustalo", _ebi_client())


@lru_cache(maxsize=1)
def _iprscan5_runner() -> EBIJobRunner:
    return EBIJobRunner("iprscan5", _ebi_client())


def _require_ebi_email() -> str | None:
    """Return the server's EBI_EMAIL, or None if not configured."""
    return get_settings().ebi_email


mcp: FastMCP = FastMCP(
    name="grounded-bio-mcp",
    instructions=SERVER_INSTRUCTIONS,
    version=__version__,
)


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Fetch NCBI Sequence",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_sequence(
    accession: str,
    database: Literal["nucleotide", "protein"],
    rettype: Literal["fasta", "gb", "gp"] = "fasta",
    response_format: Literal["json", "markdown"] = "json",
) -> dict[str, Any] | str:
    """Fetch a nucleotide or protein sequence from NCBI by accession.

    Returns the sequence plus metadata (length, organism, and — when
    rettype is ``gb``/``gp`` — a parsed feature table). Use this when you
    already have an accession (NM_/NR_/XM_/NP_/XP_/…); to search by gene
    or protein name, use ``bio_blast_search`` in a later phase.
    """
    return await fetch_sequence(
        accession=accession,
        database=database,
        rettype=rettype,
        response_format=response_format,
        client=_ncbi_client(),
    )


@mcp.tool(
    title="Fetch UniProt Record",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_uniprot(
    accession: str,
    include_features: bool = True,
    response_format: Literal["json", "markdown"] = "json",
) -> dict[str, Any] | str:
    """Fetch a curated UniProtKB protein record by accession.

    Returns sequence, organism, features (domains, disulfide bonds, active
    sites, …) when ``include_features=True``, and cross-references grouped
    by database (PDB, AlphaFoldDB, RefSeq, InterPro, Pfam, …) — these
    cross-references are the bridge to ``bio_fetch_pdb``,
    ``bio_fetch_alphafold``, and ``bio_fetch_sequence``. For uncharacterised
    sequences with no UniProt entry, use ``bio_scan_domains`` (InterProScan,
    later phase).
    """
    return await fetch_uniprot(
        accession=accession,
        include_features=include_features,
        response_format=response_format,
        client=_uniprot_client(),
    )


@mcp.tool(
    title="Fetch PDB Structure",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_pdb(
    pdb_id: str,
    include_coordinates: bool = False,
    chain_filter: str | None = None,
) -> dict[str, Any]:
    """Fetch experimentally-determined protein structure metadata from RCSB.

    Returns resolution, experimental method, deposition date, chain
    sequences, space group, R-factors, and (when ``include_coordinates``
    is True) the mmCIF file itself, soft-capped at ~2 MB. For predicted
    rather than experimental structures, use ``bio_fetch_alphafold``.
    """
    return await fetch_pdb(
        pdb_id=pdb_id,
        include_coordinates=include_coordinates,
        chain_filter=chain_filter,
        client=_rcsb_client(),
    )


@mcp.tool(
    title="Fetch AlphaFold Prediction",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_alphafold(
    uniprot_accession: str,
    format: Literal["pdb", "cif", "summary"] = "summary",  # noqa: A002 — spec name
) -> dict[str, Any]:
    """Fetch AlphaFold2 predicted structure from the EBI AlphaFold Database.

    Always returns a pLDDT summary (overall mean plus N-term / middle /
    C-term per-region means). When ``format`` is ``pdb`` or ``cif``, the
    full structure file is included alongside the summary. For
    experimentally-determined structures, use ``bio_fetch_pdb`` first —
    AlphaFold predictions with mean pLDDT < 70 are unreliable and the
    tool surfaces a low-confidence warning in that case.
    """
    return await fetch_alphafold(
        uniprot_accession=uniprot_accession,
        format=format,
        client=_alphafold_client(),
    )


@mcp.tool(
    title="Align Sequences (Clustal Omega)",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_align_sequences(
    sequences: list[dict[str, Any]],
    sequence_type: Literal["protein", "dna", "rna"],
    output_format: Literal["clustal", "fasta", "msf"] = "clustal",
) -> dict[str, Any]:
    """Multiple sequence alignment via EBI Clustal Omega (async EBI Job Dispatcher).

    Accepts 2-500 sequences as ``{id, sequence}`` dicts. Returns the raw
    alignment plus four statistics (alignment_length,
    conserved_columns_count, strict_identity_pct,
    mean_pairwise_identity_pct, gap_pct) — see ``tools/align_sequences.py``
    for exact definitions. Long gap stretches in divergent sequences are
    valid output, not tool errors.
    """
    email = _require_ebi_email()
    if not email:
        return error_response(
            "bio_align_sequences requires EBI_EMAIL to be set at the server — "
            "EBI mandates an email on every Job Dispatcher submission.",
            suggestions=[
                "Set EBI_EMAIL in the server's environment (.env on dev, "
                "systemd EnvironmentFile on the LXC).",
            ],
        )
    return await _align_impl(
        sequences=sequences,
        sequence_type=sequence_type,
        output_format=output_format,
        runner=_clustalo_runner(),
        email=email,
    )


@mcp.tool(
    title="Scan Protein Domains (InterProScan)",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_scan_domains(
    sequence: str,
    applications: list[
        Literal["Pfam", "SMART", "PROSITE", "CDD", "SUPERFAMILY", "Gene3D"]
    ]
    | None = None,
    max_wait_seconds: int | None = None,
) -> dict[str, Any]:
    """Protein domain architecture prediction via EBI InterProScan.

    Returns a flattened per-match list with signature + InterPro
    cross-reference + location + e-value/score. Empty ``matches`` is a
    valid result, not an error. Spec-facing database names ("Pfam",
    "PROSITE", etc.) are mapped to EBI-canonical names at submission
    time — "Pfam" becomes "PfamA", "PROSITE" expands to two databases.

    ``max_wait_seconds`` overrides the 600 s default and caps at 1800 s.
    On timeout this tool returns a hard error with the job ID — partial
    results during a RUNNING job are not implemented in this build.
    """
    email = _require_ebi_email()
    if not email:
        return error_response(
            "bio_scan_domains requires EBI_EMAIL to be set at the server.",
            suggestions=[
                "Set EBI_EMAIL in the server's environment (.env on dev, "
                "systemd EnvironmentFile on the LXC).",
            ],
        )
    return await _scan_impl(
        sequence=sequence,
        applications=applications,
        max_wait_seconds=max_wait_seconds,
        runner=_iprscan5_runner(),
        email=email,
    )


@mcp.tool(
    title="Fetch Compound Data (ChEMBL + PubChem)",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_compound(
    identifier: str,
    identifier_type: Literal[
        "name", "smiles", "inchi", "chembl_id", "pubchem_cid"
    ],
    source: Literal["chembl", "pubchem", "both"] = "both",
) -> dict[str, Any]:
    """Fetch structured compound data (SMILES, InChI, MW, LogP, clinical phase, synonyms).

    Queries ChEMBL (drug-curated) and/or PubChem (broad chemistry). When
    ``source='both'`` the two databases are queried in parallel where
    possible; ChEMBL wins on fields both sources provide (SMILES, InChI,
    formula, MW, LogP, H-bond counts, rotatable bonds). Per-field
    provenance under ``sources``.

    PubChem name lookups may return multiple CIDs (stereoisomer/salt
    families). In that case the first CID is used and
    ``candidate_pubchem_cids`` + ``disambiguation_hint`` are surfaced so
    a follow-up query can pick a specific member.

    For measured target binding data on this compound, follow up with
    ``bio_fetch_bioactivity``.
    """
    return await _compound_impl(
        identifier=identifier,
        identifier_type=identifier_type,
        source=source,
        chembl=_chembl_client(),
        pubchem=_pubchem_client(),
    )


@mcp.tool(
    title="Fetch ChEMBL Bioactivity",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
        # ChEMBL accepts new assay submissions — the same query may gain
        # rows over time.
        "idempotentHint": False,
    },
)
async def bio_fetch_bioactivity(
    query_type: Literal["compound", "target"],
    identifier: str,
    activity_types: list[
        Literal["IC50", "Ki", "Kd", "EC50", "AC50", "Potency"]
    ]
    | None = None,
    max_results: int = 50,
    min_confidence: int = 7,
    offset: int = 0,
) -> dict[str, Any]:
    """Fetch measured bioactivity (IC50/Ki/Kd/...) from ChEMBL.

    Two query directions:
      - ``query_type='compound'``: give a compound ChEMBL ID
        (e.g. ``CHEMBL25`` for aspirin); returns what it binds to.
      - ``query_type='target'``: give a target ChEMBL ID
        (e.g. ``CHEMBL204`` for thrombin) or a UniProt accession
        (e.g. ``P00734``); the tool resolves UniProt → target and
        returns what hits it.

    ``min_confidence`` defaults to 7 — ChEMBL's "direct single-protein
    target assigned" threshold. Lowering this admits assays with weak
    target mapping; DO NOT lower without a specific reason. Records
    whose joined assay has null confidence are always excluded; records
    below threshold are also excluded (ChEMBL's server-side filter is
    leaky, so the tool re-enforces client-side). Both exclusion counts
    appear in the output.

    Pagination via ``max_results`` (1-500, default 50) and ``offset``.
    ``page_meta.truncated`` + ``page_meta.next_offset`` indicate when
    additional pages are available.
    """
    return await _bioactivity_impl(
        query_type=query_type,
        identifier=identifier,
        activity_types=list(activity_types) if activity_types else None,
        max_results=max_results,
        min_confidence=min_confidence,
        offset=offset,
        chembl=_chembl_client(),
    )


@mcp.tool(
    title="Fetch Variant (Ensembl)",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
        # Ensembl releases update allele frequencies, ClinVar
        # cross-references, and consequence predictions over time.
        "idempotentHint": False,
    },
)
async def bio_fetch_variant(
    identifier: str,
    species: str = "human",
    assembly: str | None = None,
) -> dict[str, Any]:
    """Look up a variant by rsID or ``chr:pos:ref:alt`` coordinates via Ensembl.

    Returns alleles, genomic mapping, clinical significance, and
    population frequencies (gnomADe preferred for MAF, falling back
    to 1000G). Ensembl cannot distinguish an unannotated real rsID
    from a fabricated one — the tool reports ``found`` with an
    ``annotation_richness`` object (presence flags for
    clinical_significance / population_frequencies / consequences) or
    ``not_found`` honestly. ``assembly="GRCh37"`` routes to the legacy
    server; ``assembly_used`` is echoed in every response.
    """
    return await _variant_impl(
        identifier=identifier,
        species=species,
        assembly=assembly,
        client=_ensembl_client(),
    )


@mcp.tool(
    title="Predict Variant Effect (VEP)",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
        # Ensembl releases retune transcript models and scoring
        # matrices, so VEP output for the same input can shift.
        "idempotentHint": False,
    },
)
async def bio_predict_variant_effect(
    variant: str,
    species: str = "human",
    input_format: Literal["hgvs", "region", "auto"] = "auto",
    assembly: str | None = None,
) -> dict[str, Any]:
    """Predict functional consequences for a variant via Ensembl VEP.

    Accepts HGVS.c / HGVS.p / HGVS.g (e.g. ``ENSP00000252486.4:p.Cys130Arg``)
    or ``chr:pos:ref:alt`` (e.g. ``19:44908684:T:C``). Returns three
    parallel consequence lists (transcript, regulatory_feature,
    intergenic) plus SIFT/PolyPhen scores when available. For empty
    region-format results the tool surfaces a REF-mismatch hint — VEP
    silently returns no consequences when REF disagrees with the
    assembly rather than producing an explicit error.
    """
    return await _vep_impl(
        variant=variant,
        species=species,
        input_format=input_format,
        assembly=assembly,
        client=_ensembl_client(),
    )


@mcp.tool(
    title="Fetch NCBI Gene",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_gene(
    identifier: str,
    organism: str = "Homo sapiens",
) -> dict[str, Any]:
    """Fetch an NCBI Gene record by symbol or Gene ID.

    Accepts a gene symbol (``BRCA1``) or numeric NCBI Gene ID (``672``).
    Returns genomic location (chromosome, assembly accession, exon
    count), RefSeq transcripts (NM_/NP_/XM_/XR_/XP_), GO annotations,
    and cross-references to UniProt, Ensembl, HGNC, MGI, and MIM.
    Ambiguous symbols return ``candidate_gene_ids`` with
    disambiguation context rather than picking arbitrarily — re-query
    with a specific Gene ID or organism to resolve.
    """
    return await _gene_impl(
        identifier=identifier,
        organism=organism,
        client=_ncbi_client(),
    )


@mcp.tool(
    title="Search Literature (Europe PMC)",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
        # Europe PMC continuously indexes new papers; the same query
        # yields different ranked results over time.
        "idempotentHint": False,
    },
)
async def bio_search_literature(
    query: str,
    max_results: int = 20,
    open_access_only: bool = False,
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, Any]:
    """Search Europe PMC for papers matching a query.

    Accepts free text, MeSH terms, or prefixed forms like
    ``DOI:10.1038/srep35251`` or ``AUTH:"Miyazaki T"``. Each hit carries
    metadata (title, authors, journal, year, DOI, PMID, PMC ID,
    abstract) plus a ``fulltext_available`` flag so callers know which
    papers can be followed up on with ``bio_fetch_paper_fulltext``.
    ``year_from`` / ``year_to`` bound publication year; ``open_access_only``
    restricts to papers with OPEN_ACCESS status at Europe PMC.

    Zero hits is an honest ``status="found"`` with an empty papers list
    — "no matches" is never conflated with "lookup failed".
    """
    return await _literature_impl(
        query=query,
        max_results=max_results,
        open_access_only=open_access_only,
        year_from=year_from,
        year_to=year_to,
        client=_europepmc_client(),
    )


@mcp.tool(
    title="Fetch Paper Full Text",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_paper_fulltext(
    identifier: str,
    identifier_type: Literal["pmc", "doi"],
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch full text of an open-access paper from Europe PMC.

    Returns a four-state ``availability`` enum so callers can tell
    exactly what they got:

    * ``full_xml`` — JATS full text retrieved; ``sections`` is a flat
      list of ``{title, level, text}`` with level=1 for top-level
      sections and level>=2 for subsections. ``figures`` carries
      label + caption per figure.
    * ``abstract_only`` — paper exists but fulltext is unreachable;
      ``fulltext_unavailable_reason`` explains why (``"paper not in
      PMC"``, ``"PMC ID exists but fulltext XML returned 404"``, or
      ``"closed-access paper"``).
    * ``metadata_only`` — paper resolves with no abstract and no
      fulltext.
    * ``not_found`` — identifier did not resolve in Europe PMC.

    ``sections`` filter is optional case-insensitive substring match
    on level=1 section titles (e.g. ``["Methods"]``). The tool NEVER
    fabricates content for unavailable papers — we return what we
    have and say what we do not. This is the anti-hallucination
    surface for citation verification.
    """
    return await _fulltext_impl(
        identifier=identifier,
        identifier_type=identifier_type,
        sections=sections,
        client=_europepmc_client(),
    )


@mcp.tool(
    title="Fetch Reactome Pathway",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_pathway(
    identifier: str,
    identifier_type: Literal["pathway_id", "gene_symbol", "uniprot"],
    species: str = "Homo sapiens",
    cross_species: bool = False,
) -> dict[str, Any]:
    """Fetch Reactome pathway data.

    Three input modes:

    * ``identifier_type='pathway_id'`` — Reactome stable ID
      (e.g. ``R-HSA-109581`` for Apoptosis); returns the full
      pathway record (name, species, summary, GO biological process,
      literature references, figures, release date).
    * ``identifier_type='uniprot'`` — UniProt accession; returns all
      Reactome pathways containing that protein for the given species.
    * ``identifier_type='gene_symbol'`` — gene symbol; runs
      ``/search/query`` and returns matching pathways. Strict species
      filtering by default (only the requested species); pass
      ``cross_species=True`` to surface ``candidate_pathways`` from
      all species for disambiguation (same pattern as
      ``bio_fetch_gene`` and ``bio_fetch_compound``).
    """
    return await _pathway_impl(
        identifier=identifier,
        identifier_type=identifier_type,
        species=species,
        cross_species=cross_species,
        client=_reactome_client(),
    )


@mcp.tool(
    title="Fetch STRING Interactions",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_fetch_interactions(
    identifier: str,
    species_taxon: int = 9606,
    min_score: int = 700,
    max_partners: int = 20,
) -> dict[str, Any]:
    """Fetch STRING protein-protein interaction partners.

    **SCORE SCALE — critical, surfaces in every response:**
    ``min_score`` is on the **0-1000 input scale** (700 = 0.7
    threshold). Output ``combined_score`` and the seven evidence
    sub-scores are on the **0-1 scale**. Every response carries a
    ``score_scale`` field so the distinction is visible in
    structured output. A request for ``min_score=0`` or ``min_score=1``
    is rejected — you almost certainly confused the scales; spec
    §4.18 clamps to 150-1000.

    Defaults: ``species_taxon=9606`` (human; 10090=mouse, 9685=cat);
    ``min_score=700`` (high confidence, 900=highest, 400=medium);
    ``max_partners=20``.

    Output includes seven evidence channels per edge
    (``neighbourhood``, ``fusion``, ``co_occurrence``, ``coexpression``,
    ``experimental``, ``database``, ``textmining``) so callers can
    distinguish directly-observed experimental interactions from
    text-mining co-occurrence. Zero sub-scores are preserved —
    presence of 0 means "STRING knows this channel does not
    contribute"; absence would mean "we do not know whether it was
    evaluated".
    """
    return await _interactions_impl(
        identifier=identifier,
        species_taxon=species_taxon,
        min_score=min_score,
        max_partners=max_partners,
        client=_string_client(),
    )


@mcp.tool(
    title="BLAST Sequence Search",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
        # NCBI databases grow between runs; the same query may return
        # additional hits next month. idempotentHint=False on purpose.
        "idempotentHint": False,
    },
)
async def bio_blast_search(
    query_sequence: str,
    program: Literal["blastn", "blastp", "blastx", "tblastn"],
    database: Literal["nt", "nr", "refseq_protein", "refseq_rna", "swissprot"],
    organism_filter: str | None = None,
    max_hits: int = 20,
    e_value: float = 10.0,
    max_wait_seconds: int | None = None,
) -> dict[str, Any]:
    """Sequence-similarity search against NCBI BLAST.

    Submit → poll → fetch with NCBI-etiquette polling (15 s initial,
    ramping to 60 s after 5 min wall time, jittered 0.8-1.2×). Default
    timeout 600 s; caller-overridable up to 1800 s for slow nr/blastn
    jobs during peak hours. Empty hit lists are valid output, not an
    error — truly novel sequences with no homologues exist.

    Per-hit output: accession, organism, E-value, bit score, %identity,
    %query coverage, alignment positions. Top-5 hits also include
    qseq/hseq/midline so callers can verify alignment quality. Multiple
    identical sequences across DB records collapse into one hit with
    ``identical_sequence_count`` reflecting the collapse.

    organism_filter accepts NCBI Entrez query syntax — e.g.
    ``"Felis catus[ORGN]"``, ``"Mammalia[ORGN]"``.

    BLAST hits chain naturally to ``bio_fetch_uniprot`` (UniProt
    accessions in the swissprot database) and ``bio_fetch_sequence``
    (any NCBI accession).
    """
    return await _blast_impl(
        query_sequence=query_sequence,
        program=program,
        database=database,
        organism_filter=organism_filter,
        max_hits=max_hits,
        e_value=e_value,
        max_wait_seconds=max_wait_seconds,
        client=_ncbi_client(),
    )


@mcp.tool(
    title="Codon-Optimise Sequence",
    annotations=_LOCAL_COMPUTE_ANNOTATIONS,
)
async def bio_codon_optimise(
    protein_sequence: str,
    target_organism: Literal[
        "ecoli_k12", "h_sapiens", "s_cerevisiae", "p_pastoris", "cho", "sf9"
    ],
    avoid_restriction_sites: list[str] | None = None,
) -> dict[str, Any]:
    """Codon-optimise a protein for one of six recombinant expression hosts.

    Greedy frequency-max algorithm: for each residue picks the
    organism's highest-frequency codon, swapping to a synonym only when
    the top pick would introduce a forbidden restriction site.
    Synonymous swaps that still leave a forbidden site (rare; usually
    means the only synonyms all carry the site) are reported in
    ``restriction_conflicts`` so callers see the constraint failed
    honestly rather than getting a silently-bad sequence.

    Returns ``optimised_sequence`` (DNA, with the highest-frequency stop
    codon appended), ``codon_adaptation_index`` (Sharp & Li 1987 over
    non-stop codons), ``gc_content_pct``, ``rare_codon_count`` (per-AA
    relative frequency < 0.1), and ``restriction_conflicts``.

    The ONLY tool with ``openWorldHint=False``: codon usage tables ship
    locally (three from ``python-codon-tables``, three bundled Kazusa
    CSVs under ``data/codon_tables/``), so output is fully reproducible
    and the tool has no upstream dependency at runtime.
    """
    return await _codon_optimise_impl(
        protein_sequence=protein_sequence,
        target_organism=target_organism,
        avoid_restriction_sites=avoid_restriction_sites,
    )


@mcp.tool(
    title="Fold RNA/DNA Sequence",
    annotations=_LOCAL_COMPUTE_ANNOTATIONS,
)
async def bio_fold_sequence(
    sequence: str,
    sequence_type: Literal["rna", "dna"],
    temperature: float = 37.0,
) -> dict[str, Any]:
    """RNA / DNA secondary structure prediction via ViennaRNA Python bindings.

    Returns the spec §4.8 output: ``structure`` (MFE dot-bracket),
    ``mfe_kcal_per_mol`` (the equilibrium ΔG of the MFE structure under
    Turner 2004 (RNA) or Mathews 2004 (DNA) parameters), and
    ``base_pair_probabilities`` (mean pair probability and per-position
    pairing probability list from the equilibrium partition function).

    Default temperature 37 °C; range 0-100 °C. Determinism: ViennaRNA's
    MFE algorithm is fully deterministic for fixed inputs; the
    ``provenance`` block carries the ViennaRNA version so any result
    can be reproduced exactly. Like ``bio_codon_optimise``, this tool
    is local-only (``openWorldHint=False``) — no upstream API.
    """
    return await _fold_impl(
        sequence=sequence,
        sequence_type=sequence_type,
        temperature=temperature,
    )


@mcp.tool(
    title="Design CRISPR gRNA (CRISPOR)",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def bio_design_grna(
    target_sequence: str,
    genome: str,
    pam: Literal["NGG", "NG", "NNGRRT", "TTTV"] = "NGG",
    max_guides: int = 10,
    max_off_target_mismatches: int = 4,
) -> dict[str, Any]:
    """CRISPR gRNA design with real off-target analysis (CRISPOR wrapper).

    Returns ranked guides (top-N by MIT specificity) for ``target_sequence``
    against an indexed genome, with **real** off-target tables — every
    off-target row is a verified mismatch-tolerant hit that BWA found in
    the genome, not a fabricated row. This is the single most important
    anti-hallucination tool in the server: without it, models produce
    plausible-looking guide sequences and entirely invented off-target
    tables that look credible until biologists try to use them.

    Per-guide output: 20 nt spacer + 3 nt PAM split, on-target locus
    (chrom:start derived from the 0-mm self-match in the off-target
    table), MIT specificity score, CFD specificity computed locally per
    Doench 2016, per-model efficiency scores (Doench '16 / Moreno-Mateos
    / Azimuth / etc.) with explicit nullability — ``NotEnoughFlankSeq``
    surfaces as null with ``score_unavailable_reason`` rather than
    silently zero.

    Off-target table per guide carries chromosome, position, sequence,
    mismatch count, mismatch pattern, MIT + CFD scores, strand, and a
    ``locus_class`` (CDS / intron / intergenic / unknown) from CRISPOR's
    segments.bed. Truncated at 100 entries with ``off_targets_truncated``
    + ``total_off_targets`` so the cap is visible.

    Genome must be pre-indexed under ``GENOME_DIR/<genome>/``; sacCer3
    ships with CRISPOR. felCat9 / hg38 / mm39 install on the LXC during
    Session 8b. PAM defaults to NGG (SpCas9); NG / NNGRRT (SaCas9) /
    TTTV (Cpf1) supported.
    """
    return await _design_grna_impl(
        target_sequence=target_sequence,
        genome=genome,
        pam=pam,
        max_guides=max_guides,
        max_off_target_mismatches=max_off_target_mismatches,
        runner=_crispor_runner(),
    )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    """CLI entry point — dispatches based on ``MCP_TRANSPORT`` (default http)."""
    settings = get_settings()
    _configure_logging(settings.log_level)
    transport = os.environ.get("MCP_TRANSPORT", "http").lower()
    if transport == "stdio":
        # stdio is the transport ``fastmcp dev`` / MCP Inspector uses.
        mcp.run(transport="stdio")
    else:
        # Streamable HTTP, bound to 127.0.0.1 per spec §2.2; Caddy is the
        # auth boundary. config.Settings refuses public binds.
        mcp.run(
            transport="http",
            host=settings.mcp_bind_host,
            port=settings.mcp_bind_port,
        )


if __name__ == "__main__":  # pragma: no cover — CLI entry
    main()
