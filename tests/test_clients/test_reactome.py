"""Unit tests for ``ReactomeClient`` (spec §4.17, §7.1).

Thin wrapper around the Reactome Content Service. Three endpoints the
pathway tool needs:

* ``/data/query/{stId}`` — full pathway record (displayName, stId,
  goBiologicalProcess, literatureReference[], speciesName, figure[],
  releaseDate, summation, maxDepth).  **Note the path** — the obvious
  ``/data/pathway/{stId}`` returns 404; this errata was captured
  during pre-work probing on 2026-04-24.
* ``/data/mapping/UniProt/{acc}/pathways?species={taxon}`` — all
  pathways containing the given protein for a species.
* ``/search/query?query=...&types=Pathway[&species=...]`` — gene-symbol
  / free-text search returning pathway candidates, each stamped with
  a ``species: [...]`` list for cross-species disambiguation.

Error normalisation: 404 → ``AccessionNotFound`` ("pathway / mapping /
symbol not found"); 429 → ``RateLimitExceeded``; 5xx →
``ExternalServiceDown``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bioinformatics_mcp.clients.reactome import (
    REACTOME_BASE_URL,
    ReactomeClient,
)
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
)

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


# ---- query by stable ID --------------------------------------------------


@respx.mock
async def test_query_pathway_by_stable_id_returns_record(
    reactome_client: ReactomeClient,
) -> None:
    route = respx.get(
        f"{REACTOME_BASE_URL}/data/query/R-HSA-109581"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("reactome_query_R-HSA-109581.json")
        )
    )

    record = await reactome_client.query_pathway("R-HSA-109581")

    assert route.called
    # R-HSA-109581 is "Apoptosis" — verified live 2026-04-24. The spec
    # text calling it "Signalling by Interleukins" was wrong; errata
    # captured in memory.
    assert record["displayName"] == "Apoptosis"
    assert record["stId"] == "R-HSA-109581"
    assert record["speciesName"] == "Homo sapiens"
    # Literature references are included in the record.
    assert isinstance(record["literatureReference"], list)
    assert len(record["literatureReference"]) >= 1


@respx.mock
async def test_query_pathway_404_raises_not_found(
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

    with pytest.raises(AccessionNotFound) as exc:
        await reactome_client.query_pathway("R-HSA-99999999")

    assert exc.value.accession == "R-HSA-99999999"
    assert exc.value.database == "Reactome"


# ---- mapping: UniProt -> pathways ---------------------------------------


@respx.mock
async def test_mapping_uniprot_to_pathways_includes_species_filter(
    reactome_client: ReactomeClient,
) -> None:
    route = respx.get(
        f"{REACTOME_BASE_URL}/data/mapping/UniProt/P04637/pathways"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("reactome_mapping_TP53_uniprot.json")
        )
    )

    pathways = await reactome_client.mapping_to_pathways(
        resource="UniProt", identifier="P04637", species_taxon=9606
    )

    assert route.called
    assert dict(route.calls.last.request.url.params) == {"species": "9606"}
    assert len(pathways) >= 10
    assert all(p["speciesName"] == "Homo sapiens" for p in pathways)


# ---- search: gene symbol -> pathway candidates --------------------------


@respx.mock
async def test_search_pathways_by_symbol_uses_types_filter(
    reactome_client: ReactomeClient,
) -> None:
    route = respx.get(f"{REACTOME_BASE_URL}/search/query").mock(
        return_value=httpx.Response(200, text=_load("reactome_search_TP53.json"))
    )

    groups = await reactome_client.search_pathways(
        query="TP53", species="Homo sapiens"
    )

    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "TP53"
    assert params["types"] == "Pathway"
    assert params["species"] == "Homo sapiens"

    # Result envelope is ``{results: [{typeName, entries: [...]}]}``.
    assert isinstance(groups, list)
    assert groups
    assert "entries" in groups[0]
    first_entry = groups[0]["entries"][0]
    assert "stId" in first_entry
    assert "species" in first_entry  # list, for cross-species disambiguation


# ---- error normalisation -------------------------------------------------


@respx.mock
async def test_503_maps_to_service_down(reactome_client: ReactomeClient) -> None:
    respx.get(f"{REACTOME_BASE_URL}/data/query/R-HSA-109581").mock(
        return_value=httpx.Response(503, text="maintenance")
    )
    with pytest.raises(ExternalServiceDown) as exc:
        await reactome_client.query_pathway("R-HSA-109581")
    assert exc.value.service == "Reactome"


@respx.mock
async def test_429_maps_to_rate_limit(reactome_client: ReactomeClient) -> None:
    respx.get(f"{REACTOME_BASE_URL}/data/query/R-HSA-109581").mock(
        return_value=httpx.Response(429, text="")
    )
    with pytest.raises(RateLimitExceeded) as exc:
        await reactome_client.query_pathway("R-HSA-109581")
    assert exc.value.service == "Reactome"
