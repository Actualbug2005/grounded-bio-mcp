"""Unit + integration tests for ``bio_fetch_interactions`` (spec §4.18).

STRING protein-protein interaction partners. Returns a trimmed per-edge
record including the seven evidence sub-scores so callers can
distinguish directly-observed experimental interactions from
text-mining co-occurrences.

**Score scale reminder** (belt-and-braces; see StringDBClient module
docstring and the tool's field descriptions):

* Input ``min_score`` is **0-1000** (700 = 0.7 threshold).
* Output ``score`` / sub-scores are **0-1**.

Integration pins on TP53 / taxon 9606 at min_score=700 — a well-studied
protein that returns a rich neighbourhood with diverse evidence types,
so a passing integration test exercises the evidence breakdown.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.string_db import (
    STRING_BASE_URL,
    StringDBClient,
)
from grounded_bio_mcp.tools.fetch_interactions import bio_fetch_interactions

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


# ---- happy path: TP53 / 9606 -------------------------------------------


@respx.mock
async def test_tp53_returns_partners_with_evidence_breakdown(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(
            200, text=_load("string_interaction_partners_TP53.json")
        )
    )

    result = await bio_fetch_interactions(
        identifier="TP53",
        species_taxon=9606,
        min_score=700,
        max_partners=10,
        client=string_client,
    )

    assert result["status"] == "found"
    # Scale contract surfaces in every response — belt-and-braces
    # documentation so a caller reading structured output sees the
    # 0-1000 vs 0-1 distinction without reading the docstring.
    assert result["score_scale"] == {
        "input_min_score": "0-1000",
        "output_scores": "0-1",
    }
    assert result["query"]["identifier"] == "TP53"
    assert result["query"]["species_taxon"] == 9606
    assert result["query"]["min_score"] == 700

    partners = result["partners"]
    assert len(partners) == 10
    first = partners[0]
    # Per-edge record has partner metadata + combined + evidence.
    assert first["partner_name"] == "SFN"
    assert 0.0 <= first["combined_score"] <= 1.0
    assert first["partner_string_id"] == "9606.ENSP00000340989"
    # Evidence breakdown dict — seven explicit channels, zeros kept
    # (presence of 0 means "STRING knows this channel does not
    # contribute"; absence would mean "we do not know").
    assert set(first["evidence"].keys()) == {
        "neighbourhood",
        "fusion",
        "co_occurrence",
        "coexpression",
        "experimental",
        "database",
        "textmining",
    }
    # For TP53-SFN the experimental channel is high (~0.981 in fixture).
    assert first["evidence"]["experimental"] > 0.9


# ---- score scale belt-and-braces --------------------------------------


@respx.mock
async def test_every_edge_scores_in_0_to_1_range(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(
            200, text=_load("string_interaction_partners_TP53.json")
        )
    )
    result = await bio_fetch_interactions(
        identifier="TP53",
        species_taxon=9606,
        min_score=700,
        max_partners=10,
        client=string_client,
    )
    for p in result["partners"]:
        assert 0.0 <= p["combined_score"] <= 1.0
        for k, v in p["evidence"].items():
            assert 0.0 <= v <= 1.0, f"sub-score {k}={v} out of [0,1] range"


# ---- not found --------------------------------------------------------


@respx.mock
async def test_unknown_identifier_returns_not_found(
    string_client: StringDBClient,
) -> None:
    respx.get(f"{STRING_BASE_URL}/api/json/interaction_partners").mock(
        return_value=httpx.Response(
            404, text=_load("string_not_found.json")
        )
    )

    result = await bio_fetch_interactions(
        identifier="TOTALLY_FAKE_GENE_XYZ",
        species_taxon=9606,
        min_score=700,
        max_partners=10,
        client=string_client,
    )

    assert result["status"] == "not_found"
    assert "TOTALLY_FAKE_GENE_XYZ" in result["message"]


# ---- input validation -------------------------------------------------


async def test_missing_identifier_returns_error(
    string_client: StringDBClient,
) -> None:
    result = await bio_fetch_interactions(
        identifier="",
        species_taxon=9606,
        min_score=700,
        max_partners=10,
        client=string_client,
    )
    assert result.get("error") is True


async def test_min_score_below_zero_returns_error(
    string_client: StringDBClient,
) -> None:
    # Spec §4.18 clamps 150-1000; below 150 STRING would return edges
    # with negligible evidence. The tool rejects obviously-malformed
    # scale confusion (sub-1 thresholds == someone passed the output
    # scale) rather than trusting and sending junk to STRING.
    result = await bio_fetch_interactions(
        identifier="TP53",
        species_taxon=9606,
        min_score=0,
        max_partners=10,
        client=string_client,
    )
    assert result.get("error") is True
    assert "0-1000" in result["message"] or "150" in result["message"]


# ---- integration ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against STRING",
)
async def test_integration_tp53_live() -> None:
    client = StringDBClient(user_email=os.environ.get("STRING_USER_EMAIL"))
    try:
        result = await bio_fetch_interactions(
            identifier="TP53",
            species_taxon=9606,
            min_score=700,
            max_partners=10,
            client=client,
        )
    finally:
        await client.aclose()

    assert result["status"] == "found"
    assert result["partners"]
    # Every partner must satisfy the server-side threshold — this is
    # the live canary for STRING's server-side filter staying honest.
    for p in result["partners"]:
        assert p["combined_score"] >= 0.7
        # Evidence breakdown is populated.
        assert "evidence" in p
