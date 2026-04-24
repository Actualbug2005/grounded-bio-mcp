"""Unit + integration tests for ``bio_search_literature`` (spec §4.14).

Europe PMC literature search. The tool takes a free-text query (plus
optional year / open-access filters), runs a single ``/search`` call
against the Europe PMC client with ``resultType=core``, and returns a
trimmed per-paper record: title, up to five authors + et-al flag,
journal, year, DOI, PMID, PMC ID, abstract, and a ``fulltext_available``
boolean so callers can tell which hits they could follow up on with
``bio_fetch_paper_fulltext``.

The integration test pins on ``Sugisawa 2016 AIM feline`` because it
reliably resolves PMC5059666 as hit #1 and is the motivating paper for
the whole project.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from bioinformatics_mcp.clients.europepmc import (
    EUROPEPMC_BASE_URL,
    EuropePMCClient,
)
from bioinformatics_mcp.tools.search_literature import bio_search_literature

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def epmc_client():
    client = EuropePMCClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- happy path: Sugisawa 2016 top hit ----------------------------------


@respx.mock
async def test_search_returns_trimmed_papers_with_availability(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(200, text=_load("epmc_search_sugisawa_core.json"))
    )

    result = await bio_search_literature(
        query="Sugisawa 2016 AIM feline",
        max_results=3,
        client=epmc_client,
    )

    assert result["status"] == "found"
    assert result["hit_count"] == 3
    papers = result["papers"]
    assert len(papers) == 3

    # Hit #1 is the motivating paper — full metadata must be carried.
    first = papers[0]
    assert first["pmcid"] == "PMC5059666"
    assert first["pmid"] == "27731392"
    assert first["doi"] == "10.1038/srep35251"
    assert first["title"].startswith("Impact of feline AIM")
    assert first["year"] == 2016
    assert first["journal"].lower() == "scientific reports"
    assert first["fulltext_available"] is True
    assert first["open_access"] is True
    assert "AIM" in first["abstract"]
    # Authors trimmed to at most 5 + an et_al flag.
    assert isinstance(first["authors"], list)
    assert len(first["authors"]) <= 5
    assert "et_al" in first
    assert first["et_al"] is True  # Sugisawa paper has 14 authors


# ---- filter composition ------------------------------------------------


@respx.mock
async def test_search_propagates_year_and_open_access_filters(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, text='{"hitCount":0,"resultList":{"result":[]}}'
        )
    )

    await bio_search_literature(
        query="renal disease cats",
        max_results=10,
        open_access_only=True,
        year_from=2015,
        year_to=2020,
        client=epmc_client,
    )

    request = respx.calls.last.request
    query_param = request.url.params["query"]
    assert "OPEN_ACCESS:Y" in query_param
    assert "PUB_YEAR:[2015 TO 2020]" in query_param


# ---- empty result ------------------------------------------------------


@respx.mock
async def test_search_zero_hits_returns_found_empty(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, text='{"hitCount":0,"resultList":{"result":[]}}'
        )
    )

    result = await bio_search_literature(
        query="xkjqwerty_nothing_matches_this",
        max_results=3,
        client=epmc_client,
    )

    # Zero hits is a valid honest outcome, not an error.
    assert result["status"] == "found"
    assert result["hit_count"] == 0
    assert result["papers"] == []


# ---- input validation --------------------------------------------------


async def test_short_query_returns_error(epmc_client: EuropePMCClient) -> None:
    result = await bio_search_literature(query="AI", client=epmc_client)
    # Spec §4.14: query min_length=3.
    assert result.get("error") is True
    assert "at least 3" in result["message"].lower() or "min_length" in result["message"].lower()


# ---- closed-access behaviour -------------------------------------------


@respx.mock
async def test_search_marks_closed_access_paper_fulltext_unavailable(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, text=_load("epmc_search_closed_access.json")
        )
    )

    result = await bio_search_literature(
        query="EXT_ID:41876449", max_results=1, client=epmc_client
    )

    first = result["papers"][0]
    assert first["pmcid"] is None
    assert first["fulltext_available"] is False
    assert first["open_access"] is False


# ---- integration -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against Europe PMC",
)
async def test_integration_sugisawa_live() -> None:
    client = EuropePMCClient()
    try:
        result = await bio_search_literature(
            query="Sugisawa 2016 AIM feline",
            max_results=3,
            client=client,
        )
    finally:
        await client.aclose()

    assert result["status"] == "found"
    assert result["hit_count"] >= 1
    # The motivation paper should be hit #1 for this query.
    first = result["papers"][0]
    assert first["pmcid"] == "PMC5059666"
    assert first["fulltext_available"] is True
