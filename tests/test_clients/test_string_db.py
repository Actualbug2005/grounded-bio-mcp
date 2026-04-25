"""Unit tests for ``StringDBClient`` (spec §4.18, §7.1).

STRING protein-protein interaction network. Two endpoints:

* ``/api/json/interaction_partners`` — top-N partners of the query
  protein. This is the endpoint spec §4.18 needs (not ``/network``,
  which returns the induced subgraph of queried + neighbour proteins
  and produces cluttered results for single-protein queries).
* ``/api/json/get_string_ids`` — resolves free-text identifiers
  (gene symbols, UniProt accessions) to STRING's native ENSP IDs.
  Used optionally for pre-resolving a query to a canonical ID when
  the caller wants to understand disambiguation.

**Score-scale contract, verified live 2026-04-24:**

* Input ``required_score`` is **0-1000** (so ``700`` = 0.7 threshold).
* Output ``score`` and sub-scores (``nscore`` / ``fscore`` / ``pscore``
  / ``ascore`` / ``escore`` / ``dscore`` / ``tscore``) are **0-1**.
* Server-side filter is NOT leaky (30 partners at required_score=900
  all had score ≥ 0.998; 0 below-threshold). Unlike ChEMBL, we trust
  the server-side threshold and do not add a client-side counter;
  a DEBUG-level assertion catches regressions silently.

User-Agent: the STRING docs request a contact email for courtesy.
If ``STRING_USER_EMAIL`` is set, the client appends
``(+mailto:<email>)`` to the User-Agent string. Missing email is a
warning, not an error — unlike EBI, STRING does not enforce the
contact email.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.string_db import (
    STRING_BASE_URL,
    StringDBClient,
)
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def string_client():
    client = StringDBClient(user_email=None)
    try:
        yield client
    finally:
        await client.aclose()


# ---- interaction_partners ----------------------------------------------


@respx.mock
async def test_interaction_partners_passes_all_required_params(
    string_client: StringDBClient,
) -> None:
    route = respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(
            200, text=_load("string_interaction_partners_TP53.json")
        )
    )

    partners = await string_client.interaction_partners(
        identifier="TP53",
        species_taxon=9606,
        required_score=700,
        limit=10,
    )

    assert route.called
    params = dict(route.calls.last.request.url.params)
    # The client must forward the raw 0-1000 required_score — the
    # server expects the input scale, we do not convert.
    assert params["identifiers"] == "TP53"
    assert params["species"] == "9606"
    assert params["required_score"] == "700"
    assert params["limit"] == "10"

    assert len(partners) == 10
    # Every partner has side-A == query (TP53).
    assert {p["preferredName_A"] for p in partners} == {"TP53"}
    # Evidence sub-scores present, on the 0-1 scale.
    first = partners[0]
    for key in ("nscore", "fscore", "pscore", "ascore", "escore", "dscore", "tscore"):
        assert isinstance(first[key], (int, float))
        assert 0.0 <= first[key] <= 1.0


@respx.mock
async def test_interaction_partners_filter_is_not_leaky(
    string_client: StringDBClient,
) -> None:
    """Regression test for the ChEMBL-style leakage pattern.

    Server-side filter at required_score=900 should return ZERO edges
    below score 0.9. Verified live 2026-04-24; this test is the
    offline canary that catches STRING silently regressing.
    """
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(
            200,
            text=_load("string_interaction_partners_TP53_high_threshold.json"),
        )
    )

    partners = await string_client.interaction_partners(
        identifier="TP53",
        species_taxon=9606,
        required_score=900,
        limit=30,
    )

    assert partners
    below = [p for p in partners if p["score"] < 0.9]
    assert not below, (
        f"STRING returned {len(below)} edges below 0.9 at required_score=900 "
        "— the server-side filter is now leaky; add a client-side "
        "below_threshold_excluded counter (see ChEMBL leaky-filter memory)."
    )


# ---- get_string_ids ----------------------------------------------------


@respx.mock
async def test_get_string_ids_resolves_symbol_to_ensp(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/get_string_ids").mock(
        return_value=httpx.Response(200, text=_load("string_get_ids_TP53.json"))
    )

    mappings = await string_client.get_string_ids(
        identifier="TP53", species_taxon=9606, limit=5
    )

    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping["queryItem"] == "TP53"
    assert mapping["stringId"] == "9606.ENSP00000269305"
    assert mapping["preferredName"] == "TP53"


# ---- not found ---------------------------------------------------------


@respx.mock
async def test_interaction_partners_unknown_identifier_raises_not_found(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(
            404, text=_load("string_not_found.json")
        )
    )

    with pytest.raises(AccessionNotFound) as exc:
        await string_client.interaction_partners(
            identifier="TOTALLY_FAKE_GENE_XYZ",
            species_taxon=9606,
            required_score=700,
            limit=10,
        )
    assert exc.value.accession == "TOTALLY_FAKE_GENE_XYZ"
    assert exc.value.database == "STRING"


# ---- user-agent email pattern ------------------------------------------


@respx.mock
async def test_user_email_appended_to_user_agent_when_set() -> None:
    client = StringDBClient(user_email="contact@example.com")
    try:
        respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
            return_value=httpx.Response(200, text="[]")
        )
        await client.interaction_partners(
            identifier="TP53",
            species_taxon=9606,
            required_score=700,
            limit=1,
        )
        ua = respx.calls.last.request.headers["user-agent"]
        # Email must be embedded in the User-Agent for STRING contact.
        assert "contact@example.com" in ua
        assert "mailto:" in ua
    finally:
        await client.aclose()


@respx.mock
async def test_user_agent_omits_email_when_not_set(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(200, text="[]")
    )
    await string_client.interaction_partners(
        identifier="TP53",
        species_taxon=9606,
        required_score=700,
        limit=1,
    )
    ua = respx.calls.last.request.headers["user-agent"]
    assert "mailto:" not in ua
    assert "@" not in ua


# ---- error normalisation ------------------------------------------------


@respx.mock
async def test_503_maps_to_service_down(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(503, text="")
    )
    with pytest.raises(ExternalServiceDown) as exc:
        await string_client.interaction_partners(
            identifier="TP53",
            species_taxon=9606,
            required_score=700,
            limit=1,
        )
    assert exc.value.service == "STRING"


@respx.mock
async def test_429_maps_to_rate_limit(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(429, text="")
    )
    with pytest.raises(RateLimitExceeded) as exc:
        await string_client.interaction_partners(
            identifier="TP53",
            species_taxon=9606,
            required_score=700,
            limit=1,
        )
    assert exc.value.service == "STRING"
