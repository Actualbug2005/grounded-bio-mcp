"""Unit + integration tests for ``bio_fetch_paper_fulltext`` (spec §4.15).

The single most load-bearing tool in the project. Fetches full text of
an open-access paper, never fabricates content for unavailable papers,
and surfaces a four-state ``availability`` enum so callers can tell
what they actually got:

``full_xml`` — JATS XML retrieved and parsed into sections + figure
captions.
``abstract_only`` — paper exists in Europe PMC with a usable abstract
but no retrievable fulltext. ``fulltext_unavailable_reason`` explains
why: ``"paper not in PMC"``, ``"PMC ID exists but fulltext XML returned
404"``, or ``"closed-access paper"``.
``metadata_only`` — paper resolves but no abstract and no fulltext.
``not_found`` — identifier did not resolve in Europe PMC at all.

Integration test pins on PMC5059666 — the motivation paper for this
whole project. Being able to fetch its full text live is the concrete
proof that the anti-hallucination architecture works as intended.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.europepmc import (
    EUROPEPMC_BASE_URL,
    EuropePMCClient,
)
from grounded_bio_mcp.tools.fetch_paper_fulltext import bio_fetch_paper_fulltext

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _load_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


@pytest.fixture
async def epmc_client():
    client = EuropePMCClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- PMC5059666: full XML path ----------------------------------------


@respx.mock
async def test_pmc_lookup_returns_full_xml_with_parsed_sections(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/PMC5059666/fullTextXML").mock(
        return_value=httpx.Response(
            200,
            content=_load_bytes("epmc_fulltext_PMC5059666.xml"),
            headers={"content-type": "application/xml"},
        )
    )

    result = await bio_fetch_paper_fulltext(
        identifier="PMC5059666",
        identifier_type="pmc",
        client=epmc_client,
    )

    assert result["status"] == "found"
    assert result["availability"] == "full_xml"
    assert result["fulltext_unavailable_reason"] is None

    # Metadata from JATS <front>
    assert result["title"].startswith("Impact of feline AIM")
    assert result["year"] == 2016
    assert result["journal"] == "Scientific Reports"
    assert result["doi"] == "10.1038/srep35251"
    assert result["pmid"] == "27731392"
    assert result["pmcid"] == "PMC5059666"
    # 13 authors in this paper -> truncated to 5 with et_al=True.
    assert len(result["authors"]) == 5
    assert result["et_al"] is True
    assert result["authors"][0].startswith("Sugisawa")

    # Abstract pulled from <front><abstract>.
    assert result["abstract"] is not None
    assert "AIM" in result["abstract"]

    # Sections is a flat list with title/level/text. The top-level
    # structural sections (Results, Discussion, Methods, ...) must be
    # present; nested subsections carry higher level numbers.
    section_titles = [s["title"] for s in result["sections"]]
    assert "Results" in section_titles
    assert "Discussion" in section_titles
    assert "Methods" in section_titles

    # Methods is a structural container (19 sub-sections, no direct
    # prose) — its own ``text`` is empty and the prose lives in the
    # flat list at level=2 right after. That's the correct semantic:
    # the container owns its title, children own their prose.
    methods = next(s for s in result["sections"] if s["title"] == "Methods")
    assert methods["level"] == 1
    methods_idx = result["sections"].index(methods)
    first_subsection = result["sections"][methods_idx + 1]
    assert first_subsection["level"] == 2
    assert len(first_subsection["text"]) > 0

    # Figure captions extracted (4 figures in this paper).
    assert len(result["figures"]) >= 1
    first_fig = result["figures"][0]
    assert first_fig["label"].startswith("Figure 1")
    assert "AIM" in first_fig["caption"]


# ---- section filter ---------------------------------------------------


@respx.mock
async def test_sections_filter_restricts_returned_sections(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/PMC5059666/fullTextXML").mock(
        return_value=httpx.Response(
            200, content=_load_bytes("epmc_fulltext_PMC5059666.xml")
        )
    )

    result = await bio_fetch_paper_fulltext(
        identifier="PMC5059666",
        identifier_type="pmc",
        sections=["Methods"],
        client=epmc_client,
    )

    titles = {s["title"] for s in result["sections"]}
    # Only sections matching the requested name should come through.
    # Filter is case-insensitive substring match at level=1.
    assert titles == {"Methods"}


# ---- DOI path: resolves via search then fetches fulltext --------------


@respx.mock
async def test_doi_resolves_via_search_then_fetches_fulltext(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(200, text=_load("epmc_search_by_doi.json"))
    )
    respx.get(f"{EUROPEPMC_BASE_URL}/PMC5059666/fullTextXML").mock(
        return_value=httpx.Response(
            200, content=_load_bytes("epmc_fulltext_PMC5059666.xml")
        )
    )

    result = await bio_fetch_paper_fulltext(
        identifier="10.1038/srep35251",
        identifier_type="doi",
        client=epmc_client,
    )

    assert result["availability"] == "full_xml"
    assert result["pmcid"] == "PMC5059666"
    # Two calls: one search (DOI -> PMC ID) plus one fulltextXML.
    assert len(respx.calls) == 2


# ---- closed-access / no-PMC path -> abstract_only ---------------------


@respx.mock
async def test_doi_not_in_pmc_falls_back_to_abstract_only(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, text=_load("epmc_search_closed_access.json")
        )
    )

    result = await bio_fetch_paper_fulltext(
        identifier="10.1177/1098612x261437907",
        identifier_type="doi",
        client=epmc_client,
    )

    assert result["status"] == "found"
    assert result["availability"] == "abstract_only"
    # fulltext_unavailable_reason must explain WHY, not just that it is
    # unavailable. The closed-access fixture has isOpenAccess=N + no
    # PMC ID — the more informative reason wins ("closed-access paper"
    # over "paper not in PMC", because the former explains the latter).
    assert result["fulltext_unavailable_reason"] == "closed-access paper"
    assert result["pmcid"] is None
    # Closed-access paper still carries metadata + abstract when
    # Europe PMC has indexed it. This fixture has no abstract in the
    # search response, so abstract is None — but metadata still flows.
    assert result["title"].startswith("EXPRESS")
    # Sections list is empty for non-fulltext availability.
    assert result["sections"] == []
    assert result["figures"] == []


# ---- PMC ID given but fullTextXML 404 ---------------------------------


@respx.mock
async def test_pmc_fulltext_404_falls_back_to_abstract_only(
    epmc_client: EuropePMCClient,
) -> None:
    """Some PMC-indexed papers return 404 on /fullTextXML.

    Honest recovery: fetch the search record for metadata + abstract
    and surface ``fulltext_unavailable_reason="PMC ID exists but
    fulltext XML returned 404"``.
    """
    respx.get(f"{EUROPEPMC_BASE_URL}/PMC5059666/fullTextXML").mock(
        return_value=httpx.Response(404, text="")
    )
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(200, text=_load("epmc_search_by_doi.json"))
    )

    result = await bio_fetch_paper_fulltext(
        identifier="PMC5059666",
        identifier_type="pmc",
        client=epmc_client,
    )

    assert result["availability"] == "abstract_only"
    assert (
        result["fulltext_unavailable_reason"]
        == "PMC ID exists but fulltext XML returned 404"
    )
    assert result["pmcid"] == "PMC5059666"


# ---- identifier not resolvable at all ---------------------------------


@respx.mock
async def test_doi_unknown_returns_not_found(
    epmc_client: EuropePMCClient,
) -> None:
    respx.get(f"{EUROPEPMC_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, text='{"hitCount":0,"resultList":{"result":[]}}'
        )
    )

    result = await bio_fetch_paper_fulltext(
        identifier="10.0000/fake-doi",
        identifier_type="doi",
        client=epmc_client,
    )

    assert result["status"] == "not_found"
    assert result["availability"] == "not_found"
    assert "not found" in result["message"].lower()


# ---- integration ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against Europe PMC",
)
async def test_integration_pmc5059666_live() -> None:
    """The proof-of-concept: Sugisawa 2016 retrievable live.

    This is the paper that motivated the entire project. Being able
    to fetch its full text from a real Europe PMC endpoint is the
    single concrete acceptance criterion for the anti-hallucination
    architecture.
    """
    client = EuropePMCClient()
    try:
        result = await bio_fetch_paper_fulltext(
            identifier="PMC5059666",
            identifier_type="pmc",
            client=client,
        )
    finally:
        await client.aclose()

    assert result["status"] == "found"
    assert result["availability"] == "full_xml"
    assert result["pmcid"] == "PMC5059666"
    # Structural sanity: Methods section present with recognisable content.
    assert any(s["title"] == "Methods" for s in result["sections"])
