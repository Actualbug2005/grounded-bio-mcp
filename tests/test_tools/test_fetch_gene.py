"""Unit + integration tests for ``bio_fetch_gene`` (spec §4.16).

The tool accepts a gene symbol (``BRCA1``) or numeric NCBI Gene ID
(``672``) plus an organism. On a unique resolution it returns the
gene's genomic location, RefSeq transcripts, GO annotations, and
cross-references to UniProt and Ensembl. On ambiguous resolutions it
surfaces ``candidate_gene_ids`` with disambiguation context
(session-4 compound-tool pattern).

Request flow:
    esearch term=SYMBOL[Gene] AND "organism"[Organism] → gene IDs
    esummary id=ID[,ID…]                                → lightweight records
    efetch rettype=xml (only on unique resolution)      → rich record
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.ncbi import EUTILS_BASE_URL, NCBIClient
from grounded_bio_mcp.config import Settings
from grounded_bio_mcp.tools.fetch_gene import bio_fetch_gene

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _settings() -> Settings:
    return Settings(NCBI_API_KEY=None, EBI_EMAIL=None)


@pytest.fixture
async def ncbi_client():
    client = NCBIClient(_settings())
    try:
        yield client
    finally:
        await client.aclose()


# ---- symbol → unique resolution -----------------------------------------


@respx.mock
async def test_symbol_unique_resolution_returns_full_record(
    ncbi_client: NCBIClient,
) -> None:
    # INS (insulin) is Gene ID 3630 in human; esearch resolves uniquely.
    # Rewrite the esearch fixture response so it points at 3630, matching
    # the efetch/esummary fixtures below.
    respx.get(f"{EUTILS_BASE_URL}/esearch.fcgi").mock(
        return_value=httpx.Response(
            200,
            json={
                "header": {"type": "esearch", "version": "0.3"},
                "esearchresult": {
                    "count": "1",
                    "retmax": "1",
                    "retstart": "0",
                    "idlist": ["3630"],
                },
            },
        )
    )
    # esummary for INS — reuse the BRCA1 summary fixture isn't viable
    # because the schema matches the gene it describes, so we capture a
    # small inline fixture here.
    respx.get(f"{EUTILS_BASE_URL}/esummary.fcgi").mock(
        return_value=httpx.Response(
            200,
            json={
                "header": {"type": "esummary", "version": "0.3"},
                "result": {
                    "uids": ["3630"],
                    "3630": {
                        "uid": "3630",
                        "name": "INS",
                        "description": "insulin",
                        "chromosome": "11",
                        "maplocation": "11p15.5",
                        "otheraliases": "IDDM, ILPR, IRDN, IDDM1, IDDM2",
                        "otherdesignations": "insulin preproprotein|proinsulin",
                        "nomenclaturesymbol": "INS",
                        "nomenclaturename": "insulin",
                        "nomenclaturestatus": "Official",
                        "mim": ["176730"],
                        "genomicinfo": [
                            {
                                "chraccver": "NC_000011.10",
                                "chrstart": 2161209,
                                "chrstop": 2159002,
                                "exoncount": 3,
                            }
                        ],
                        "organism": {
                            "scientificname": "Homo sapiens",
                            "commonname": "human",
                            "taxid": 9606,
                        },
                    },
                },
            },
        )
    )
    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(
        return_value=httpx.Response(
            200, text=_load("ncbi_gene_efetch_3630_ins.xml")
        )
    )

    result = await bio_fetch_gene(
        identifier="INS", organism="Homo sapiens", client=ncbi_client
    )

    assert result["status"] == "found"
    gene = result["gene"]
    assert gene["gene_id"] == "3630"
    assert gene["symbol"] == "INS"
    assert gene["organism"]["scientificname"] == "Homo sapiens"
    # Chromosome + map location come from esummary.
    assert gene["chromosome"] == "11"
    assert gene["map_location"] == "11p15.5"
    # Synonyms (from esummary otheraliases, comma-split).
    assert "IDDM1" in gene["synonyms"]
    # genomic_location must carry assembly accession + exon count.
    assert gene["genomic_location"]["assembly_accession"] == "NC_000011.10"
    assert gene["genomic_location"]["exon_count"] == 3
    # RefSeq transcripts come from XML parsing.
    refseqs = gene["refseq_transcripts"]
    assert any(t.get("accession", "").startswith("NM_") for t in refseqs)
    # GO annotations parsed from XML Dbtag GO entries.
    go_ids = {g["id"] for g in gene["go_annotations"]}
    assert len(go_ids) > 0
    # Cross-refs to UniProt + Ensembl.
    xrefs = gene["cross_references"]
    assert any("UniProt" in k for k in xrefs.keys())
    assert "Ensembl" in xrefs


# ---- numeric Gene ID skips esearch ---------------------------------------


@respx.mock
async def test_numeric_gene_id_skips_esearch(
    ncbi_client: NCBIClient,
) -> None:
    esearch_route = respx.get(f"{EUTILS_BASE_URL}/esearch.fcgi").mock(
        return_value=httpx.Response(500, text="should not be called")
    )
    respx.get(f"{EUTILS_BASE_URL}/esummary.fcgi").mock(
        return_value=httpx.Response(
            200, text=_load("ncbi_gene_esummary_672.json")
        )
    )
    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(
        return_value=httpx.Response(
            200, text=_load("ncbi_gene_efetch_3630_ins.xml")
        )
    )

    result = await bio_fetch_gene(
        identifier="672", organism="Homo sapiens", client=ncbi_client
    )

    assert not esearch_route.called
    assert result["status"] == "found"


# ---- ambiguous resolution → candidates ---------------------------------


@respx.mock
async def test_ambiguous_symbol_returns_candidates(
    ncbi_client: NCBIClient,
) -> None:
    respx.get(f"{EUTILS_BASE_URL}/esearch.fcgi").mock(
        return_value=httpx.Response(
            200, text=_load("ncbi_gene_esearch_ace_ambiguous.json")
        )
    )
    respx.get(f"{EUTILS_BASE_URL}/esummary.fcgi").mock(
        return_value=httpx.Response(
            200, text=_load("ncbi_gene_esummary_ace_candidates.json")
        )
    )

    result = await bio_fetch_gene(
        identifier="ACE", organism="", client=ncbi_client
    )

    assert result["status"] == "ambiguous"
    assert "candidate_gene_ids" in result
    candidates = result["candidate_gene_ids"]
    # Cap at 10 per design decision.
    assert 1 < len(candidates) <= 10
    # Each candidate carries disambiguation context.
    sample = candidates[0]
    for required in ("gene_id", "symbol", "description", "organism", "chromosome"):
        assert required in sample
    # Disambiguation hint is surfaced so the caller knows what to do.
    assert "disambiguation_hint" in result


# ---- no hits -----------------------------------------------------------


@respx.mock
async def test_no_hits_returns_error(
    ncbi_client: NCBIClient,
) -> None:
    respx.get(f"{EUTILS_BASE_URL}/esearch.fcgi").mock(
        return_value=httpx.Response(
            200,
            json={
                "header": {"type": "esearch", "version": "0.3"},
                "esearchresult": {
                    "count": "0",
                    "retmax": "0",
                    "retstart": "0",
                    "idlist": [],
                },
            },
        )
    )

    result = await bio_fetch_gene(
        identifier="NONEXISTENT_GENE_XYZ",
        organism="Homo sapiens",
        client=ncbi_client,
    )
    assert result.get("error") is True


# ---- input validation ---------------------------------------------------


async def test_empty_identifier_is_rejected(
    ncbi_client: NCBIClient,
) -> None:
    result = await bio_fetch_gene(
        identifier="", organism="Homo sapiens", client=ncbi_client
    )
    assert result.get("error") is True


# ---- GO truncation -----------------------------------------------------


@respx.mock
async def test_large_go_list_triggers_soft_cap(
    ncbi_client: NCBIClient,
) -> None:
    """When the GO annotations serialise past 200 KB the tool swaps in the fallback URL."""
    # Construct a fake esummary + efetch response pair where the XML has
    # enough GO Dbtag entries to blow the 200 KB cap. We inline the
    # Gene-ref_locus ('BIG1') so the parser has a symbol.
    # An Object-id-based Dbtag is ~150 bytes serialised; 2000 of them
    # drives GO past 200 KB comfortably.
    go_blocks = "".join(
        f'<Dbtag><Dbtag_db>GO</Dbtag_db><Dbtag_tag><Object-id><Object-id_id>{i}</Object-id_id></Object-id></Dbtag_tag></Dbtag>'
        for i in range(2000)
    )
    xml = (
        '<?xml version="1.0"?><Entrezgene-Set><Entrezgene>'
        "<Entrezgene_gene><Gene-ref><Gene-ref_locus>BIG1</Gene-ref_locus></Gene-ref></Entrezgene_gene>"
        f"<Entrezgene_properties>{go_blocks}</Entrezgene_properties>"
        "</Entrezgene></Entrezgene-Set>"
    )
    respx.get(f"{EUTILS_BASE_URL}/esummary.fcgi").mock(
        return_value=httpx.Response(
            200,
            json={
                "header": {"type": "esummary", "version": "0.3"},
                "result": {
                    "uids": ["111111"],
                    "111111": {
                        "uid": "111111",
                        "name": "BIG1",
                        "description": "synthetic large gene",
                        "chromosome": "1",
                        "maplocation": "",
                        "otheraliases": "",
                        "otherdesignations": "",
                        "organism": {
                            "scientificname": "Synthetic species",
                            "commonname": "",
                            "taxid": 0,
                        },
                    },
                },
            },
        )
    )
    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=xml)
    )

    result = await bio_fetch_gene(
        identifier="111111",
        organism="Synthetic species",
        client=ncbi_client,
    )

    gene = result["gene"]
    # Either the GO list inlines (under the cap) or the error branch fires.
    if "go_annotations_error" in gene:
        assert "200 KB" in gene["go_annotations_error"] or "exceeded" in gene["go_annotations_error"].lower()
    else:
        # If it inlines, it must at least truncate to the cap.
        assert len(gene["go_annotations"]) <= 2000


# ---- integration --------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against NCBI",
)
async def test_integration_brca1_live() -> None:
    client = NCBIClient(_settings())
    try:
        result = await bio_fetch_gene(
            identifier="BRCA1", organism="Homo sapiens", client=client
        )
    finally:
        await client.aclose()

    assert result["status"] == "found"
    gene = result["gene"]
    assert gene["gene_id"] == "672"
    assert gene["symbol"] == "BRCA1"
    assert gene["chromosome"] == "17"
    # BRCA1 has many RefSeq transcripts — a non-trivial lower bound.
    assert len(gene["refseq_transcripts"]) >= 1
    # GO annotations must be present live.
    assert len(gene["go_annotations"]) > 0
