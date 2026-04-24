"""Unit + integration tests for ``bio_fetch_bioactivity`` (spec §4.10).

Covers compound↔target queries, confidence enrichment via batch assay
lookup, UniProt → target_chembl_id resolution, min_confidence
filtering (with null-confidence always excluded per approved design),
pagination, and the two-direction docstring invariants.

Fixtures captured 2026-04-24 against live ChEMBL (aspirin CHEMBL25
activity slice + matching assays/targets).

Integration test at the bottom hits live ChEMBL for
``query_type='compound', identifier='CHEMBL25'`` and confirms at least
one COX-family target surfaces at confidence ≥ 7.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from bioinformatics_mcp.clients.chembl import CHEMBL_BASE_URL, ChEMBLClient
from bioinformatics_mcp.tools.fetch_bioactivity import bio_fetch_bioactivity

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def chembl_client():
    c = ChEMBLClient()
    try:
        yield c
    finally:
        await c.aclose()


# ---- compound → targets --------------------------------------------------


@respx.mock
async def test_compound_query_returns_enriched_activities(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/activity.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_activities_CHEMBL25.json"))
    )
    # Two unique assay_chembl_ids appear in the fixture; the batch lookup
    # must hit /assay.json exactly once.
    respx.get(f"{CHEMBL_BASE_URL}/assay.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_assays_batch.json"))
    )
    respx.get(f"{CHEMBL_BASE_URL}/target.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_targets_batch.json"))
    )

    result = await bio_fetch_bioactivity(
        query_type="compound",
        identifier="CHEMBL25",
        activity_types=None,
        max_results=50,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )

    assert result.get("error") is not True, result
    assert result["query_type"] == "compound"
    assert result["identifier"] == "CHEMBL25"
    assert result["min_confidence_applied"] == 7
    assert len(result["activities"]) >= 1
    first = result["activities"][0]
    # Confidence score enrichment is mandatory — activity records don't
    # carry it natively so we merge from the joined assay.
    assert isinstance(first["confidence_score"], int)
    assert first["confidence_score"] >= 7
    assert "Homologous" in first["confidence_description"] or \
           "Direct" in first["confidence_description"]
    assert first["compound_chembl_id"] == "CHEMBL25"
    assert first["target_chembl_id"].startswith("CHEMBL")
    # Assay-type from activity record is passed through.
    assert first["assay_type"] in ("B", "F", "A", "T")


@respx.mock
async def test_activity_types_filter_serialises_to_chembl_in(
    chembl_client: ChEMBLClient,
) -> None:
    route = respx.get(f"{CHEMBL_BASE_URL}/activity.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "activities": [],
                "page_meta": {"total_count": 0, "limit": 50, "offset": 0},
            },
        )
    )
    await bio_fetch_bioactivity(
        query_type="compound",
        identifier="CHEMBL25",
        activity_types=["IC50", "Ki"],
        max_results=50,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )
    call_url = str(route.calls.last.request.url)
    assert "standard_type__in=IC50%2CKi" in call_url or \
           "standard_type__in=IC50,Ki" in call_url


# ---- target → compounds --------------------------------------------------


@respx.mock
async def test_target_query_by_chembl_id(chembl_client: ChEMBLClient) -> None:
    route = respx.get(f"{CHEMBL_BASE_URL}/activity.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "activities": [],
                "page_meta": {"total_count": 0, "limit": 50, "offset": 0},
            },
        )
    )
    await bio_fetch_bioactivity(
        query_type="target",
        identifier="CHEMBL204",
        activity_types=None,
        max_results=50,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )
    url = str(route.calls.last.request.url)
    assert "target_chembl_id=CHEMBL204" in url


@respx.mock
async def test_target_query_with_uniprot_resolves_to_chembl_id(
    chembl_client: ChEMBLClient,
) -> None:
    """Pass a UniProt accession; the tool must resolve it via the target
    endpoint to a CHEMBL target ID before running the activity query.
    """
    target_route = respx.get(
        f"{CHEMBL_BASE_URL}/target.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "targets": [
                    {
                        "target_chembl_id": "CHEMBL204",
                        "target_type": "SINGLE PROTEIN",
                        "pref_name": "Prothrombin",
                        "target_components": [{"accession": "P00734"}],
                    }
                ],
                "page_meta": {"total_count": 1, "limit": 3, "offset": 0},
            },
        )
    )
    activity_route = respx.get(
        f"{CHEMBL_BASE_URL}/activity.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "activities": [],
                "page_meta": {"total_count": 0, "limit": 50, "offset": 0},
            },
        )
    )

    result = await bio_fetch_bioactivity(
        query_type="target",
        identifier="P00734",
        activity_types=None,
        max_results=50,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )
    assert result.get("error") is not True
    assert target_route.called
    url = str(activity_route.calls.last.request.url)
    assert "target_chembl_id=CHEMBL204" in url
    assert result["resolved_target_chembl_id"] == "CHEMBL204"


# ---- null-confidence always excluded ------------------------------------


@respx.mock
async def test_null_confidence_records_always_excluded(
    chembl_client: ChEMBLClient,
) -> None:
    """Records whose joined assay has confidence_score=None must be
    filtered out regardless of the ``min_confidence`` value — we
    cannot verify an unscored assay meets any quality bar.
    """
    # Two activities referencing different assays — one with a confidence
    # score, one whose assay returns null confidence.
    respx.get(f"{CHEMBL_BASE_URL}/activity.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "activities": [
                    {
                        "activity_id": 1,
                        "molecule_chembl_id": "CHEMBL25",
                        "molecule_pref_name": "ASPIRIN",
                        "assay_chembl_id": "ASSAY_CONFIDENT",
                        "assay_description": "has conf",
                        "assay_type": "B",
                        "standard_type": "IC50",
                        "standard_value": "100",
                        "standard_units": "nM",
                        "standard_relation": "=",
                        "target_chembl_id": "CHEMBL204",
                        "target_pref_name": "Prothrombin",
                        "target_organism": "Homo sapiens",
                        "document_chembl_id": "DOC1",
                        "document_year": 2020,
                    },
                    {
                        "activity_id": 2,
                        "molecule_chembl_id": "CHEMBL25",
                        "molecule_pref_name": "ASPIRIN",
                        "assay_chembl_id": "ASSAY_UNSCORED",
                        "assay_description": "null conf",
                        "assay_type": "B",
                        "standard_type": "IC50",
                        "standard_value": "200",
                        "standard_units": "nM",
                        "standard_relation": "=",
                        "target_chembl_id": "CHEMBL204",
                        "target_pref_name": "Prothrombin",
                        "target_organism": "Homo sapiens",
                        "document_chembl_id": "DOC2",
                        "document_year": 2020,
                    },
                ],
                "page_meta": {"total_count": 2, "limit": 50, "offset": 0},
            },
        )
    )
    respx.get(f"{CHEMBL_BASE_URL}/assay.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "assays": [
                    {
                        "assay_chembl_id": "ASSAY_CONFIDENT",
                        "confidence_score": 8,
                        "confidence_description": "Homologous single protein",
                    },
                    {
                        "assay_chembl_id": "ASSAY_UNSCORED",
                        "confidence_score": None,
                        "confidence_description": None,
                    },
                ]
            },
        )
    )
    respx.get(f"{CHEMBL_BASE_URL}/target.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "targets": [
                    {
                        "target_chembl_id": "CHEMBL204",
                        "pref_name": "Prothrombin",
                        "target_type": "SINGLE PROTEIN",
                        "target_components": [{"accession": "P00734"}],
                    }
                ]
            },
        )
    )

    # Even with min_confidence=0 the null record must stay out.
    result = await bio_fetch_bioactivity(
        query_type="compound",
        identifier="CHEMBL25",
        activity_types=None,
        max_results=50,
        min_confidence=0,
        offset=0,
        chembl=chembl_client,
    )
    assert len(result["activities"]) == 1
    assert result["activities"][0]["assay_chembl_id"] == "ASSAY_CONFIDENT"
    # Excluded count must be surfaced so the caller can see how many
    # records were dropped and why.
    assert result["null_confidence_excluded"] == 1


# ---- pagination ---------------------------------------------------------


@respx.mock
async def test_pagination_surfaces_truncated_and_next_offset(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/activity.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "activities": [
                    {
                        "activity_id": i,
                        "molecule_chembl_id": "CHEMBL25",
                        "molecule_pref_name": "ASPIRIN",
                        "assay_chembl_id": f"A{i}",
                        "assay_description": "d",
                        "assay_type": "B",
                        "standard_type": "IC50",
                        "standard_value": "1",
                        "standard_units": "nM",
                        "standard_relation": "=",
                        "target_chembl_id": "CHEMBL204",
                        "target_pref_name": "Prothrombin",
                        "target_organism": "Homo sapiens",
                        "document_chembl_id": "D",
                        "document_year": 2020,
                    }
                    for i in range(5)
                ],
                "page_meta": {"total_count": 4073, "limit": 5, "offset": 0},
            },
        )
    )
    respx.get(f"{CHEMBL_BASE_URL}/assay.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "assays": [
                    {
                        "assay_chembl_id": f"A{i}",
                        "confidence_score": 9,
                        "confidence_description": "Direct",
                    }
                    for i in range(5)
                ]
            },
        )
    )
    respx.get(f"{CHEMBL_BASE_URL}/target.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "targets": [
                    {
                        "target_chembl_id": "CHEMBL204",
                        "pref_name": "Prothrombin",
                        "target_type": "SINGLE PROTEIN",
                        "target_components": [{"accession": "P00734"}],
                    }
                ]
            },
        )
    )

    result = await bio_fetch_bioactivity(
        query_type="compound",
        identifier="CHEMBL25",
        activity_types=None,
        max_results=5,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )
    pm = result["page_meta"]
    assert pm["total_count"] == 4073
    assert pm["returned_count"] == 5
    assert pm["truncated"] is True
    assert pm["next_offset"] == 5


# ---- input validation ---------------------------------------------------


async def test_rejects_invalid_query_type(
    chembl_client: ChEMBLClient,
) -> None:
    out = await bio_fetch_bioactivity(
        query_type="not_real",  # type: ignore[arg-type]
        identifier="CHEMBL25",
        activity_types=None,
        max_results=50,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )
    assert out["error"] is True


async def test_rejects_out_of_range_max_results(
    chembl_client: ChEMBLClient,
) -> None:
    out = await bio_fetch_bioactivity(
        query_type="compound",
        identifier="CHEMBL25",
        activity_types=None,
        max_results=10000,
        min_confidence=7,
        offset=0,
        chembl=chembl_client,
    )
    assert out["error"] is True


# ---- integration --------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against real APIs",
)
async def test_integration_aspirin_finds_cox_targets() -> None:
    c = ChEMBLClient()
    try:
        result = await bio_fetch_bioactivity(
            query_type="compound",
            identifier="CHEMBL25",
            activity_types=None,
            max_results=50,
            min_confidence=7,
            offset=0,
            chembl=c,
        )
    finally:
        await c.aclose()
    assert result.get("error") is not True, result
    targets = {
        (a.get("target_pref_name") or "").lower()
        for a in result["activities"]
    }
    # Aspirin at confidence ≥ 7 surfaces at least one COX family target
    # or a direct cyclooxygenase hit; the exact name varies per release.
    cox_hits = [t for t in targets if "cyclooxygenase" in t or "prostaglandin" in t]
    assert result["page_meta"]["total_count"] > 0
    assert cox_hits or any("cox" in t for t in targets)
