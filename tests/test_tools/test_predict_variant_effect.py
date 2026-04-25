"""Unit + integration tests for ``bio_predict_variant_effect`` (spec §4.12).

The tool dispatches between VEP's HGVS endpoint and its region endpoint
based on ``input_format``. When ``input_format="auto"`` (default), a
narrow regex matches the region shape (``chr:pos:ref:alt``) — every
HGVS notation fails that regex because HGVS always includes a transcript
or genomic prefix with a dot (``:c.``, ``:p.``, ``:g.``), so the
classifier is unambiguous.

The output wraps VEP's per-variant record into three parallel
consequence lists so callers don't have to special-case variants that
fall in regulatory regions versus transcripts versus intergenic space.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.ensembl import (
    ENSEMBL_REST_BASE_URL,
    EnsemblClient,
)
from grounded_bio_mcp.tools.predict_variant_effect import (
    bio_predict_variant_effect,
)

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


# ---- HGVS happy path -----------------------------------------------------


@respx.mock
async def test_hgvs_p_notation_routes_to_hgvs_endpoint(
    ensembl_client: EnsemblClient,
) -> None:
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/hgvs/ENSP00000252486.4:p.Cys130Arg"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_vep_hgvs_apoe.json")
        )
    )

    result = await bio_predict_variant_effect(
        variant="ENSP00000252486.4:p.Cys130Arg",
        species="human",
        client=ensembl_client,
    )

    assert route.called
    assert result["status"] == "predicted"
    assert result["assembly_used"] == "GRCh38"
    assert result["input_format_used"] == "hgvs"
    # The tool surfaces three parallel consequence lists so callers
    # don't have to special-case intergenic / regulatory hits.
    assert "transcript_consequences" in result
    assert "regulatory_feature_consequences" in result
    assert "intergenic_consequences" in result
    # APOE ε4 most severe consequence must propagate to the top.
    assert result["most_severe_consequence"] == "missense_variant"


@respx.mock
async def test_hgvs_c_notation_also_routes_to_hgvs_endpoint(
    ensembl_client: EnsemblClient,
) -> None:
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/hgvs/ENST00000252486.9:c.388T>C"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_vep_hgvs_apoe.json")
        )
    )

    result = await bio_predict_variant_effect(
        variant="ENST00000252486.9:c.388T>C",
        species="human",
        client=ensembl_client,
    )
    assert route.called
    assert result["input_format_used"] == "hgvs"


# ---- region (chr:pos:ref:alt) happy path --------------------------------


@respx.mock
async def test_coord_snp_routes_to_region_endpoint(
    ensembl_client: EnsemblClient,
) -> None:
    """For a SNP REF/ALT of equal length 1, end == start."""
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/region/19:44908684-44908684:1/C"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_vep_region_apoe.json")
        )
    )
    result = await bio_predict_variant_effect(
        variant="19:44908684:T:C", species="human", client=ensembl_client
    )

    assert route.called
    assert result["status"] == "predicted"
    assert result["input_format_used"] == "region"
    # Transcript consequences include APOE missense.
    terms = {
        t
        for tc in result["transcript_consequences"]
        for t in tc.get("consequence_terms", [])
    }
    assert "missense_variant" in terms
    # SIFT / PolyPhen scores must carry through when Ensembl provides them.
    assert any("sift_score" in tc for tc in result["transcript_consequences"])


@respx.mock
async def test_coord_deletion_uses_ref_length_for_end_position(
    ensembl_client: EnsemblClient,
) -> None:
    """A REF of length 3 must yield end == start + 2 in the region URL."""
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/region/1:100-102:1/A"
    ).mock(return_value=httpx.Response(200, json=[]))

    await bio_predict_variant_effect(
        variant="1:100:TGC:A", species="human", client=ensembl_client
    )
    assert route.called


@respx.mock
async def test_coord_insertion_uses_ref_length_for_end_position(
    ensembl_client: EnsemblClient,
) -> None:
    """A REF of length 1 with ALT longer stays single-base (end == start)."""
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/region/1:100-100:1/AGCT"
    ).mock(return_value=httpx.Response(200, json=[]))

    await bio_predict_variant_effect(
        variant="1:100:A:AGCT", species="human", client=ensembl_client
    )
    assert route.called


# ---- empty-consequence REF-mismatch hint ---------------------------------


@respx.mock
async def test_empty_coord_response_surfaces_ref_mismatch_hint(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/region/19:44908684-44908684:1/C"
    ).mock(return_value=httpx.Response(200, json=[]))

    result = await bio_predict_variant_effect(
        variant="19:44908684:G:C",  # bogus REF
        species="human",
        client=ensembl_client,
    )

    assert result["status"] == "empty"
    # Hint must mention REF mismatch so the caller can debug.
    hint = " ".join(result.get("hints", []))
    assert "REF" in hint


# ---- explicit input_format overrides auto-detection ----------------------


@respx.mock
async def test_explicit_region_bypasses_autodetection(
    ensembl_client: EnsemblClient,
) -> None:
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/region/19:44908684-44908684:1/C"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_vep_region_apoe.json")
        )
    )
    await bio_predict_variant_effect(
        variant="19:44908684:T:C",
        species="human",
        input_format="region",
        client=ensembl_client,
    )
    assert route.called


@respx.mock
async def test_invalid_input_returns_error(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/hgvs/bogus_notation"
    ).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "Unable to parse HGVS notation 'bogus_notation'"
            },
        )
    )
    result = await bio_predict_variant_effect(
        variant="bogus_notation", species="human", client=ensembl_client
    )
    assert result.get("error") is True


# ---- integration ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against Ensembl",
)
async def test_integration_apoe_hgvs_p_live() -> None:
    client = EnsemblClient()
    try:
        result = await bio_predict_variant_effect(
            variant="ENSP00000252486.4:p.Cys130Arg",
            species="human",
            client=client,
        )
    finally:
        await client.aclose()

    assert result["status"] == "predicted"
    assert result["most_severe_consequence"] == "missense_variant"
    # Live Ensembl must surface APOE as the affected gene.
    genes = {
        tc.get("gene_symbol")
        for tc in result["transcript_consequences"]
        if tc.get("gene_symbol")
    }
    assert "APOE" in genes
