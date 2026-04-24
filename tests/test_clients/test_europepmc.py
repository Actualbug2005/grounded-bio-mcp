"""Unit tests for ``EuropePMCClient`` (spec §4.14, §4.15, §7.1).

Thin wrapper around the Europe PMC REST endpoints the literature tools
need:

* ``/search?query=...&format=json&resultType=core`` — full metadata
  including ``abstractText``, ``isOpenAccess``, ``inPMC``, ``pmcid``,
  ``doi``, ``journalInfo``, ``meshHeadingList``.
* ``/{PMC_ID}/fullTextXML`` — JATS 1.4 XML for open-access papers.
  Returns 404 when no PMC fulltext is available.

The search endpoint is always called with ``resultType=core`` because
``lite`` drops the fields the tool layer needs to report availability
honestly (``abstractText``, ``isOpenAccess``, ``inPMC``, ``hasPDF``).

Fixtures captured live on 2026-04-24 against Europe PMC v6.9:
``epmc_search_sugisawa_core.json`` — motivation paper PMC5059666.
``epmc_search_by_doi.json`` — same paper, looked up via DOI.
``epmc_search_closed_access.json`` — non-PMC paper (closed access),
showing ``pmcid=None`` / ``inPMC=N`` / ``isOpenAccess=N``.
``epmc_fulltext_PMC5059666.xml`` — 112 KB JATS fulltext.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bioinformatics_mcp.clients.europepmc import (
    EUROPEPMC_BASE_URL,
    EuropePMCClient,
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
async def epmc_client():
    client = EuropePMCClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- search --------------------------------------------------------------


@respx.mock
async def test_search_returns_core_result_with_availability_fields(
    epmc_client: EuropePMCClient,
) -> None:
    route = respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(200, text=_load("epmc_search_sugisawa_core.json"))
    )

    payload = await epmc_client.search("Sugisawa 2016 AIM feline", max_results=3)

    assert route.called
    # Always ask for core, not lite — the availability flags live in core.
    params = dict(route.calls.last.request.url.params)
    assert params["resultType"] == "core"
    assert params["format"] == "json"
    assert params["pageSize"] == "3"
    assert params["query"] == "Sugisawa 2016 AIM feline"

    assert payload["hitCount"] == 3
    first = payload["resultList"]["result"][0]
    # Sugisawa 2016 should carry every availability flag the tool relies on.
    assert first["pmcid"] == "PMC5059666"
    assert first["isOpenAccess"] == "Y"
    assert first["inPMC"] == "Y"
    assert "AIM" in first["abstractText"]  # sanity: abstract mentions AIM


@respx.mock
async def test_search_passes_optional_filters(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            text='{"hitCount":0,"resultList":{"result":[]}}',
        )
    )

    await epmc_client.search(
        "renal disease cats",
        max_results=10,
        open_access_only=True,
        year_from=2015,
        year_to=2020,
    )

    request = respx.calls.last.request
    params = dict(request.url.params)
    # Spec §4.14: filters are encoded directly into the query string
    # because Europe PMC's API has no dedicated query parameters for them.
    assert "OPEN_ACCESS:Y" in params["query"]
    assert "PUB_YEAR:[2015 TO 2020]" in params["query"]
    assert "renal disease cats" in params["query"]


@respx.mock
async def test_search_by_doi_query(epmc_client: EuropePMCClient) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(200, text=_load("epmc_search_by_doi.json"))
    )

    payload = await epmc_client.search("DOI:10.1038/srep35251", max_results=1)

    first = payload["resultList"]["result"][0]
    # The by-DOI search resolves back to PMC5059666 cleanly — used by
    # ``bio_fetch_paper_fulltext`` to turn a DOI into a PMC ID.
    assert first["pmcid"] == "PMC5059666"
    assert first["doi"] == "10.1038/srep35251"


# ---- fulltext ------------------------------------------------------------


@respx.mock
async def test_fetch_fulltext_xml_returns_bytes(epmc_client: EuropePMCClient) -> None:
    route = respx.get(
        f"{EUROPEPMC_BASE_URL}/PMC5059666/fullTextXML"
    ).mock(
        return_value=httpx.Response(
            200,
            content=_load("epmc_fulltext_PMC5059666.xml").encode("utf-8"),
            headers={"content-type": "application/xml"},
        )
    )

    xml = await epmc_client.fetch_fulltext_xml("PMC5059666")

    assert route.called
    assert isinstance(xml, bytes)
    # Smoke-test: must parse, and the Sugisawa title must be present.
    assert b"Impact of feline AIM" in xml


@respx.mock
async def test_fetch_fulltext_xml_missing_raises_not_found(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/PMC999999999/fullTextXML").mock(
        return_value=httpx.Response(404, text="")
    )

    with pytest.raises(AccessionNotFound) as exc:
        await epmc_client.fetch_fulltext_xml("PMC999999999")

    # The identifier surfaces on the exception so the tool layer can cite
    # it in its error response.
    assert exc.value.accession == "PMC999999999"
    assert exc.value.database == "Europe PMC"


# ---- identifier normalisation -------------------------------------------


@respx.mock
async def test_fetch_fulltext_xml_accepts_bare_numeric_pmc_id(
    epmc_client: EuropePMCClient,
) -> None:
    """Callers can pass either 'PMC5059666' or '5059666' — both work."""
    route = respx.get(f"{EUROPEPMC_BASE_URL}/PMC5059666/fullTextXML").mock(
        return_value=httpx.Response(200, content=b"<article/>")
    )

    await epmc_client.fetch_fulltext_xml("5059666")

    assert route.called


# ---- error normalisation -------------------------------------------------


@respx.mock
async def test_search_503_maps_to_service_down(epmc_client: EuropePMCClient) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(503, text="maintenance")
    )

    with pytest.raises(ExternalServiceDown) as exc:
        await epmc_client.search("test", max_results=1)

    assert exc.value.service == "Europe PMC"


@respx.mock
async def test_search_429_maps_to_rate_limit(epmc_client: EuropePMCClient) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(429, text="")
    )

    with pytest.raises(RateLimitExceeded) as exc:
        await epmc_client.search("test", max_results=1)

    assert exc.value.service == "Europe PMC"
