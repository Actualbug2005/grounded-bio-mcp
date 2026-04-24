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

from bioinformatics_mcp import __version__
from bioinformatics_mcp.clients.alphafold import AlphaFoldClient
from bioinformatics_mcp.clients.base import RATE_LIMITS
from bioinformatics_mcp.clients.chembl import ChEMBLClient
from bioinformatics_mcp.clients.ebi import EBIJobRunner
from bioinformatics_mcp.clients.ncbi import NCBIClient
from bioinformatics_mcp.clients.pubchem import PubChemClient
from bioinformatics_mcp.clients.rcsb import RCSBClient
from bioinformatics_mcp.clients.uniprot import UniProtClient
from bioinformatics_mcp.config import get_settings
from bioinformatics_mcp.tools.align_sequences import bio_align_sequences as _align_impl
from bioinformatics_mcp.tools.fetch_alphafold import fetch_alphafold
from bioinformatics_mcp.tools.fetch_bioactivity import (
    bio_fetch_bioactivity as _bioactivity_impl,
)
from bioinformatics_mcp.tools.fetch_compound import (
    bio_fetch_compound as _compound_impl,
)
from bioinformatics_mcp.tools.fetch_pdb import fetch_pdb
from bioinformatics_mcp.tools.fetch_sequence import fetch_sequence
from bioinformatics_mcp.tools.fetch_uniprot import fetch_uniprot
from bioinformatics_mcp.tools.scan_domains import bio_scan_domains as _scan_impl
from bioinformatics_mcp.utils.errors import error_response
from bioinformatics_mcp.utils.rate_limit import RateLimitedClient

# ---------------------------------------------------------------------------
# Server-level instructions — spec §3 tool selection guide.
#
# Embedded verbatim in the MCP handshake's ``instructions`` field so the
# model picks the right primary-source tool instead of answering from
# training data, even when the answer feels certain.
# ---------------------------------------------------------------------------
SERVER_INSTRUCTIONS = """\
Bioinformatics Primary-Source MCP Server
========================================

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

bio_fetch_compound answers "what IS this compound" — SMILES, InChI, MW,
LogP, clinical phase, synonyms — from ChEMBL (drug-curated) and PubChem
(broad chemistry) with explicit per-field provenance.

bio_fetch_bioactivity answers "what does this compound DO" — measured
IC50/Ki/Kd values against named targets, filtered to ChEMBL confidence
≥ 7 by default so low-quality assays aren't cited as binding affinities.
Do NOT pattern-match drug target or affinity values from training data
— use this tool.

Phase 1 exposes eight tools: the six above plus bio_fetch_compound and
bio_fetch_bioactivity. Later phases add BLAST, CRISPR guide design,
variants, literature, pathways, and interactions.

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
        headers={"User-Agent": "bioinformatics-mcp/0.2 (+ebi-jobdispatcher)"},
    )


@lru_cache(maxsize=1)
def _chembl_client() -> ChEMBLClient:
    return ChEMBLClient()


@lru_cache(maxsize=1)
def _pubchem_client() -> PubChemClient:
    return PubChemClient()


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
    name="bioinformatics-mcp",
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
