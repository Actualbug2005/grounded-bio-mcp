"""Unit and integration tests for ``bio_fetch_variant`` (spec §4.11).

The tool accepts either an rsID (``rs429358``) or a coordinate string
(``19:44908684:T:C`` for GRCh38) and returns a structured record with
the variant's genomic mapping, alleles, population frequencies, and
clinical significance.

Ensembl returns HTTP 400 with an identical "not found" error for both
real-but-sparse variants and entirely fabricated rsIDs. The tool
therefore exposes two outcomes — ``found`` and ``not_found`` — plus an
``annotation_richness`` object that surfaces presence flags for
clinical_significance, population_frequencies, and consequences so the
caller can see which fields Ensembl supplied without us inventing a
pseudo-"found-empty" branch the upstream can't justify.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.ensembl import (
    ENSEMBL_GRCH37_BASE_URL,
    ENSEMBL_REST_BASE_URL,
    EnsemblClient,
)
from grounded_bio_mcp.tools.fetch_variant import bio_fetch_variant

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def ensembl_client():
    client = EnsemblClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- rsID happy path -----------------------------------------------------


@respx.mock
async def test_rsid_lookup_returns_structured_record(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358").mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_variation_rs429358.json")
        )
    )

    result = await bio_fetch_variant(
        identifier="rs429358", species="human", client=ensembl_client
    )

    assert result["status"] == "found"
    assert result["assembly_used"] == "GRCh38"
    variant = result["variant"]
    assert variant["id"] == "rs429358"
    assert variant["most_severe_consequence"] == "missense_variant"
    # Mappings are surfaced with chromosome/start/end/strand/assembly_name.
    mapping = variant["mappings"][0]
    assert mapping["seq_region_name"] == "19"
    assert mapping["start"] == 44908684
    assert mapping["assembly_name"] == "GRCh38"
    # Clinical significance list must come through for anti-hallucination.
    assert "pathogenic" in variant["clinical_significance"]
    # MAF derivation must prefer gnomADe over 1000G and surface the
    # source so callers see provenance.
    assert variant["maf"]["source"].startswith("gnomADe")
    assert 0 < variant["maf"]["value"] < 1
    # annotation_richness cues — all three flags true for APOE ε4.
    rich = result["annotation_richness"]
    assert rich["has_clinical_significance"] is True
    assert rich["has_population_frequencies"] is True
    assert rich["has_consequences"] is True


@respx.mock
async def test_rsid_lookup_grch37_routes_and_echoes_assembly(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(f"{ENSEMBL_GRCH37_BASE_URL}/variation/human/rs429358").mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_variation_rs429358_grch37.json")
        )
    )

    result = await bio_fetch_variant(
        identifier="rs429358",
        species="human",
        assembly="GRCh37",
        client=ensembl_client,
    )

    assert result["status"] == "found"
    assert result["assembly_used"] == "GRCh37"
    assert result["variant"]["mappings"][0]["assembly_name"] == "GRCh37"


# ---- not found (HTTP 400) -----------------------------------------------


@respx.mock
async def test_unknown_rsid_returns_not_found_error(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs999999999999"
    ).mock(
        return_value=httpx.Response(
            400, text=_load("ensembl_variation_not_found.json")
        )
    )

    result = await bio_fetch_variant(
        identifier="rs999999999999", species="human", client=ensembl_client
    )

    assert result.get("error") is True
    assert "not found" in result["message"].lower()
    # Provide the caller with a hint that Ensembl does not distinguish
    # "found-empty" from "not-found" — the result is honestly
    # unrecoverable information, not pseudo-empty data.
    assert any("fabric" in s.lower() or "verify" in s.lower() for s in result.get("suggestions", []))


# ---- coordinate lookup ---------------------------------------------------


@respx.mock
async def test_coordinate_lookup_finds_matching_variant(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/overlap/region/human/19:44908684-44908684"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_overlap_variation_apoe.json")
        )
    )
    # Once overlap returns the matching rs-ID, the tool enriches via
    # /variation to pick up populations and synonyms that /overlap omits.
    respx.get(f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358").mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_variation_rs429358.json")
        )
    )

    result = await bio_fetch_variant(
        identifier="19:44908684:T:C",
        species="human",
        client=ensembl_client,
    )

    assert result["status"] == "found"
    assert result["assembly_used"] == "GRCh38"
    # The tool must return the rs-ID-matched variant at that coord/allele.
    assert result["variant"]["id"] == "rs429358"
    # Populations came from the enrichment call.
    assert any(
        p.get("population", "").startswith("gnomADe")
        for p in result["variant"]["populations"]
    )


# ---- input validation ---------------------------------------------------


async def test_empty_identifier_is_rejected(
    ensembl_client: EnsemblClient,
) -> None:
    result = await bio_fetch_variant(
        identifier="", species="human", client=ensembl_client
    )
    assert result.get("error") is True


async def test_malformed_identifier_is_rejected(
    ensembl_client: EnsemblClient,
) -> None:
    """Neither rsID nor coord shape — must surface a clear error."""
    result = await bio_fetch_variant(
        identifier="not_an_rsid_or_coord",
        species="human",
        client=ensembl_client,
    )
    assert result.get("error") is True
    # Guidance should name both acceptable input forms.
    joined = " ".join(result.get("suggestions", []))
    assert "rs" in joined.lower()
    assert "coord" in joined.lower() or ":" in joined


# ---- integration --------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against Ensembl",
)
async def test_integration_rs429358_live() -> None:
    client = EnsemblClient()
    try:
        result = await bio_fetch_variant(
            identifier="rs429358", species="human", client=client
        )
    finally:
        await client.aclose()

    assert result["status"] == "found"
    assert result["variant"]["id"] == "rs429358"
    # APOE ε4 is heavily annotated — all three richness flags must be True
    # against live Ensembl.
    rich = result["annotation_richness"]
    assert rich["has_clinical_significance"] is True
    assert rich["has_population_frequencies"] is True
    assert rich["has_consequences"] is True
    # MAF should be derived from gnomADe with a plausible allele frequency.
    assert 0 < result["variant"]["maf"]["value"] < 1
