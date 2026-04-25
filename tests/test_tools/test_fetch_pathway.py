"""Unit + integration tests for ``bio_fetch_pathway`` (spec §4.17).

Reactome pathway lookup in three input modes:

* ``identifier_type='pathway_id'`` — direct ``/data/query/{stId}``.
* ``identifier_type='uniprot'`` — ``/data/mapping/UniProt/{acc}/pathways``
  (species-filtered by default).
* ``identifier_type='gene_symbol'`` — ``/search/query`` pathway search
  with strict species filtering by default. ``cross_species=True``
  relaxes the filter and surfaces candidates from any species for
  disambiguation (same pattern as ``bio_fetch_gene``).

Integration test pins on R-HSA-109581 because Apoptosis is a stable
top-level pathway that will exist as long as Reactome does. (R-HSA-109581
is Apoptosis — the session-6 prompt's 'Signalling by Interleukins' gloss
was wrong; errata captured.)
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.reactome import (
    REACTOME_BASE_URL,
    ReactomeClient,
)
from grounded_bio_mcp.tools.fetch_pathway import bio_fetch_pathway

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def reactome_client():
    client = ReactomeClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- pathway_id: direct lookup ----------------------------------------


@respx.mock
async def test_pathway_id_returns_trimmed_record(
    reactome_client: ReactomeClient,
) -> None:
    respx.get(f"{REACTOME_BASE_URL}/data/query/R-HSA-109581").mock(
        return_value=httpx.Response(
            200, text=_load("reactome_query_R-HSA-109581.json")
        )
    )

    result = await bio_fetch_pathway(
        identifier="R-HSA-109581",
        identifier_type="pathway_id",
        client=reactome_client,
    )

    assert result["status"] == "found"
    pathway = result["pathway"]
    assert pathway["stable_id"] == "R-HSA-109581"
    # Apoptosis, not Signalling by Interleukins.
    assert pathway["name"] == "Apoptosis"
    assert pathway["species"] == "Homo sapiens"
    # Literature references trimmed to the fields a caller can chain on.
    assert isinstance(pathway["literature_references"], list)
    assert len(pathway["literature_references"]) >= 1
    first_ref = pathway["literature_references"][0]
    assert "pmid" in first_ref
    assert "title" in first_ref


# ---- pathway_id: not found ---------------------------------------------


@respx.mock
async def test_pathway_id_not_found_returns_error(
    reactome_client: ReactomeClient,
) -> None:
    respx.get(f"{REACTOME_BASE_URL}/data/query/R-HSA-99999999").mock(
        return_value=httpx.Response(
            404,
            json={
                "code": 404,
                "reason": "NOT_FOUND",
                "messages": ["Id: R-HSA-99999999 has not been found"],
            },
        )
    )

    result = await bio_fetch_pathway(
        identifier="R-HSA-99999999",
        identifier_type="pathway_id",
        client=reactome_client,
    )

    assert result["status"] == "not_found"
    assert "R-HSA-99999999" in result["message"]


# ---- uniprot: mapping --------------------------------------------------


@respx.mock
async def test_uniprot_returns_list_of_pathways(
    reactome_client: ReactomeClient,
) -> None:
    respx.get(
        f"{REACTOME_BASE_URL}/data/mapping/UniProt/P04637/pathways"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("reactome_mapping_TP53_uniprot.json")
        )
    )

    result = await bio_fetch_pathway(
        identifier="P04637",
        identifier_type="uniprot",
        species="Homo sapiens",
        client=reactome_client,
    )

    assert result["status"] == "found"
    assert result["count"] >= 10
    assert all(p["species"] == "Homo sapiens" for p in result["pathways"])
    # Each result is trimmed to fields the model actually wants.
    first = result["pathways"][0]
    assert set(first.keys()) >= {"stable_id", "name", "species"}


# ---- gene_symbol: strict species default -------------------------------


@respx.mock
async def test_gene_symbol_strict_species_filters_to_requested_species(
    reactome_client: ReactomeClient,
) -> None:
    respx.get(f"{REACTOME_BASE_URL}/search/query").mock(
        return_value=httpx.Response(200, text=_load("reactome_search_TP53.json"))
    )

    result = await bio_fetch_pathway(
        identifier="TP53",
        identifier_type="gene_symbol",
        species="Homo sapiens",
        client=reactome_client,
    )

    assert result["status"] == "found"
    # Every pathway returned carries Homo sapiens in its species list.
    assert result["count"] >= 1
    for p in result["pathways"]:
        assert "Homo sapiens" in p["species"]
    # Strict mode does NOT include candidate_pathways — that's the
    # cross_species branch.
    assert "candidate_pathways" not in result


# ---- gene_symbol: zero hits in requested species -----------------------


@respx.mock
async def test_gene_symbol_zero_hits_in_species_returns_not_found(
    reactome_client: ReactomeClient,
) -> None:
    respx.get(f"{REACTOME_BASE_URL}/search/query").mock(
        return_value=httpx.Response(
            200, text='{"results":[]}'
        )
    )

    result = await bio_fetch_pathway(
        identifier="NONEXISTENT_GENE_XYZ",
        identifier_type="gene_symbol",
        species="Homo sapiens",
        client=reactome_client,
    )

    assert result["status"] == "not_found"
    assert "NONEXISTENT_GENE_XYZ" in result["message"]


# ---- gene_symbol: cross_species disambiguation -------------------------


@respx.mock
async def test_gene_symbol_cross_species_returns_candidates(
    reactome_client: ReactomeClient,
) -> None:
    # Forge a search response with two species so the tool's
    # cross-species code path has something to disambiguate.
    respx.get(f"{REACTOME_BASE_URL}/search/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "typeName": "Results",
                        "entries": [
                            {
                                "stId": "R-HSA-1234567",
                                "name": "Human TP53 pathway",
                                "species": ["Homo sapiens"],
                                "type": "Pathway",
                            },
                            {
                                "stId": "R-MMU-1234568",
                                "name": "Mouse Trp53 pathway",
                                "species": ["Mus musculus"],
                                "type": "Pathway",
                            },
                        ],
                    }
                ]
            },
        )
    )

    result = await bio_fetch_pathway(
        identifier="TP53",
        identifier_type="gene_symbol",
        cross_species=True,
        client=reactome_client,
    )

    assert result["status"] == "ambiguous"
    assert "candidate_pathways" in result
    species = {p["species"][0] for p in result["candidate_pathways"]}
    assert species == {"Homo sapiens", "Mus musculus"}
    # Disambiguation hint tells caller how to resolve.
    assert "species" in result["disambiguation_hint"].lower()


# ---- input validation --------------------------------------------------


async def test_missing_identifier_returns_error(
    reactome_client: ReactomeClient,
) -> None:
    result = await bio_fetch_pathway(
        identifier="",
        identifier_type="pathway_id",
        client=reactome_client,
    )
    assert result.get("error") is True


# ---- integration ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against Reactome",
)
async def test_integration_r_hsa_109581_live() -> None:
    client = ReactomeClient()
    try:
        result = await bio_fetch_pathway(
            identifier="R-HSA-109581",
            identifier_type="pathway_id",
            client=client,
        )
    finally:
        await client.aclose()

    assert result["status"] == "found"
    # R-HSA-109581 is Apoptosis — verify by name rather than accepting
    # anything, so a fabricated / shifted record wouldn't pass.
    assert result["pathway"]["name"] == "Apoptosis"
