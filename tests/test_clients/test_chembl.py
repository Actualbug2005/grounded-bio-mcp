"""Unit tests for ``ChEMBLClient`` (spec §4.9, §4.10, §7.1).

The client exposes a small set of methods that the compound and
bioactivity tools need: molecule lookup by ID, molecule search by
free-text name, activity listing with confidence filter + pagination,
and batch enrichment of assays (for confidence_score) and targets
(for UniProt accessions). Fixtures captured on 2026-04-24 against
live ChEMBL via scripts/probe_ebi_resulttypes.py-style one-off probes.

Error-normalisation tests verify that 404 → AccessionNotFound, 429 →
RateLimitExceeded, 5xx → ExternalServiceDown.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.chembl import CHEMBL_BASE_URL, ChEMBLClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def chembl_client():
    client = ChEMBLClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- molecule lookup -----------------------------------------------------


@respx.mock
async def test_get_molecule_returns_full_record(chembl_client: ChEMBLClient) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/CHEMBL25.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_molecule_CHEMBL25.json"))
    )

    record = await chembl_client.get_molecule("CHEMBL25")

    assert record["molecule_chembl_id"] == "CHEMBL25"
    assert record["pref_name"] == "ASPIRIN"
    # ChEMBL serialises max_phase as a string-encoded decimal ("4.0"
    # = approved). The tool layer coerces this to a number for the
    # output schema; the client passes it through unchanged.
    assert record["max_phase"] == "4.0"
    # Structural data lives under molecule_structures.
    assert record["molecule_structures"]["standard_inchi_key"] == (
        "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    )


@respx.mock
async def test_get_molecule_404_raises_accession_not_found(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/CHEMBL99999999.json").mock(
        return_value=httpx.Response(404, text="")
    )
    with pytest.raises(AccessionNotFound) as exc:
        await chembl_client.get_molecule("CHEMBL99999999")
    assert exc.value.accession == "CHEMBL99999999"
    assert "ChEMBL" in exc.value.database


@respx.mock
async def test_get_molecule_429_raises_rate_limit(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/CHEMBL25.json").mock(
        return_value=httpx.Response(429, text="")
    )
    with pytest.raises(RateLimitExceeded):
        await chembl_client.get_molecule("CHEMBL25")


@respx.mock
async def test_get_molecule_503_raises_service_down(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/CHEMBL25.json").mock(
        return_value=httpx.Response(503, text="")
    )
    with pytest.raises(ExternalServiceDown):
        await chembl_client.get_molecule("CHEMBL25")


# ---- molecule search -----------------------------------------------------


@respx.mock
async def test_search_molecules_returns_ranked_hits(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/search").mock(
        return_value=httpx.Response(200, text=_load("chembl_search_aspirin.json"))
    )

    hits = await chembl_client.search_molecules("aspirin", limit=3)

    assert [m["molecule_chembl_id"] for m in hits][:2] == [
        "CHEMBL25",
        "CHEMBL5314595",
    ]
    # Relevance score must come through so the tool can distinguish strong
    # from weak matches when surfacing candidates.
    assert hits[0]["score"] == 36.0


@respx.mock
async def test_search_molecules_empty_returns_empty_list(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "molecules": [],
                "page_meta": {"total_count": 0, "limit": 5, "offset": 0},
            },
        )
    )
    hits = await chembl_client.search_molecules("zzzznotareal", limit=5)
    assert hits == []


# ---- activity listing ----------------------------------------------------


@respx.mock
async def test_list_activities_for_molecule_returns_page(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/activity.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_activities_CHEMBL25.json"))
    )

    page = await chembl_client.list_activities(
        molecule_chembl_id="CHEMBL25",
        target_chembl_id=None,
        activity_types=("IC50", "Ki", "Kd"),
        min_confidence=7,
        limit=3,
        offset=0,
    )

    assert "activities" in page
    assert "page_meta" in page
    # Fixture captured with confidence_score__gte=7 — every activity's
    # joined assay must satisfy that. We can't verify from the activity
    # record alone (no confidence_score on it), so we check the meta.
    assert page["page_meta"]["total_count"] > 0
    # At least the molecule_chembl_id / assay_chembl_id / standard_type
    # fields must carry through for the bioactivity tool.
    first = page["activities"][0]
    assert first["molecule_chembl_id"] == "CHEMBL25"
    assert first["assay_chembl_id"].startswith("CHEMBL")
    assert first["standard_type"] in {"IC50", "Ki", "Kd", "Kd apparent", "Log K'"}


@respx.mock
async def test_list_activities_for_target_uses_target_filter(
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
    await chembl_client.list_activities(
        molecule_chembl_id=None,
        target_chembl_id="CHEMBL204",
        activity_types=("IC50",),
        min_confidence=7,
        limit=50,
        offset=0,
    )

    assert route.called
    call_url = str(route.calls.last.request.url)
    assert "target_chembl_id=CHEMBL204" in call_url
    assert "confidence_score__gte=7" in call_url
    # activity_types must serialise as ChEMBL's __in filter syntax.
    assert "standard_type__in=IC50" in call_url


@respx.mock
async def test_list_activities_rejects_both_sides_null(
    chembl_client: ChEMBLClient,
) -> None:
    with pytest.raises(ValueError):
        await chembl_client.list_activities(
            molecule_chembl_id=None,
            target_chembl_id=None,
            activity_types=("IC50",),
            min_confidence=7,
            limit=50,
            offset=0,
        )


# ---- batch assay / target enrichment -------------------------------------


@respx.mock
async def test_list_assays_by_ids_returns_confidence_scores(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/assay.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_assays_batch.json"))
    )

    # Fixture contains the three assays joined to the activities fixture
    # — two with cs=0 (target unknown), one with cs=8 (COX-2 homologous).
    assays = await chembl_client.list_assays_by_ids(
        ["CHEMBL762032", "CHEMBL762033", "CHEMBL760085"]
    )

    by_id = {a["assay_chembl_id"]: a for a in assays}
    assert by_id["CHEMBL760085"]["confidence_score"] == 8
    assert "Homologous" in by_id["CHEMBL760085"]["confidence_description"]
    # ChEMBL can return cs=0 "Default value - Target unknown" even when
    # the activity query was filtered with confidence_score__gte=7 — the
    # server-side filter is leaky, verified 2026-04-24.
    assert by_id["CHEMBL762032"]["confidence_score"] == 0


@respx.mock
async def test_list_assays_by_ids_empty_input_skips_call(
    chembl_client: ChEMBLClient,
) -> None:
    route = respx.get(f"{CHEMBL_BASE_URL}/assay.json").mock(
        return_value=httpx.Response(200, json={"assays": []})
    )
    assays = await chembl_client.list_assays_by_ids([])
    assert assays == []
    assert not route.called


@respx.mock
async def test_list_targets_by_ids_returns_uniprot_components(
    chembl_client: ChEMBLClient,
) -> None:
    respx.get(f"{CHEMBL_BASE_URL}/target.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_targets_batch.json"))
    )

    targets = await chembl_client.list_targets_by_ids(
        ["CHEMBL230", "CHEMBL612545"]
    )

    by_id = {t["target_chembl_id"]: t for t in targets}
    # CHEMBL230 is COX-2 (Prostaglandin G/H synthase 2) — its
    # target_components list contains UniProt P35354.
    comps = by_id["CHEMBL230"]["target_components"]
    accessions = {c.get("accession") for c in comps}
    assert "P35354" in accessions
