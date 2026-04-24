"""Unit + integration tests for ``bio_fetch_compound`` (spec §4.9).

Covers the identifier-resolution matrix (5 types × 3 source modes),
dual-source merge with explicit provenance, ambiguity surfacing, and
graceful one-side-miss behaviour.

Unit tests mock the underlying ``ChEMBLClient`` and ``PubChemClient``
via respx — the real HTTP layer is exercised end-to-end per-client
in ``tests/test_clients/``, so the tool tests focus on merge/resolve
logic rather than transport.

Integration test at the bottom hits live ChEMBL + PubChem (aspirin);
gated on ``RUN_INTEGRATION=1``.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from bioinformatics_mcp.clients.chembl import CHEMBL_BASE_URL, ChEMBLClient
from bioinformatics_mcp.clients.pubchem import PUBCHEM_BASE_URL, PubChemClient
from bioinformatics_mcp.tools.fetch_compound import bio_fetch_compound

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


@pytest.fixture
async def pubchem_client():
    c = PubChemClient()
    try:
        yield c
    finally:
        await c.aclose()


# ---- helpers to register fixture-backed responses ------------------------


def _mock_chembl_aspirin() -> None:
    respx.get(f"{CHEMBL_BASE_URL}/molecule/CHEMBL25.json").mock(
        return_value=httpx.Response(200, text=_load("chembl_molecule_CHEMBL25.json"))
    )


def _mock_pubchem_aspirin() -> None:
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/2244/property"
    ).mock(
        return_value=httpx.Response(200, text=_load("pubchem_properties_2244.json"))
    )
    respx.get(f"{PUBCHEM_BASE_URL}/compound/cid/2244/synonyms/JSON").mock(
        return_value=httpx.Response(200, text=_load("pubchem_synonyms_2244.json"))
    )


def _mock_pubchem_inchikey_to_cid_aspirin() -> None:
    respx.get(
        f"{PUBCHEM_BASE_URL}/compound/inchikey/"
        "BSYNRYMUTXBXSQ-UHFFFAOYSA-N/cids/JSON"
    ).mock(
        return_value=httpx.Response(
            200, json={"IdentifierList": {"CID": [2244]}}
        )
    )


# ---- happy path ----------------------------------------------------------


@respx.mock
async def test_chembl_id_both_sources_merges_with_provenance(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    """``CHEMBL25`` + source=both merges ChEMBL + PubChem via InChIKey bridge."""
    _mock_chembl_aspirin()
    _mock_pubchem_inchikey_to_cid_aspirin()
    _mock_pubchem_aspirin()

    out = await bio_fetch_compound(
        identifier="CHEMBL25",
        identifier_type="chembl_id",
        source="both",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )

    assert out.get("error") is not True, out
    assert out["chembl_id"] == "CHEMBL25"
    assert out["pubchem_cid"] == 2244
    assert out["pref_name"] == "ASPIRIN"
    assert out["structure"]["inchi_key"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert out["properties"]["molecular_formula"] == "C9H8O4"
    assert out["clinical_phase"] == 4
    assert "B01AC06" in out["atc_classifications"]
    # Synonyms are drawn from PubChem (broader coverage).
    assert any(s.lower() == "aspirin" for s in out["synonyms"])
    # Sources + provenance must be filled.
    assert out["sources_queried"] == ["chembl", "pubchem"]
    assert set(out["sources_found"]) == {"chembl", "pubchem"}
    # ChEMBL wins on structural fields where both are present.
    assert out["sources"]["smiles"] == "chembl"
    assert out["sources"]["inchi_key"] == "chembl"


@respx.mock
async def test_chembl_only_source(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    _mock_chembl_aspirin()
    out = await bio_fetch_compound(
        identifier="CHEMBL25",
        identifier_type="chembl_id",
        source="chembl",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["sources_queried"] == ["chembl"]
    assert out["sources_found"] == ["chembl"]
    assert out["chembl_id"] == "CHEMBL25"
    assert out["pubchem_cid"] is None


@respx.mock
async def test_pubchem_only_source(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    _mock_pubchem_aspirin()
    out = await bio_fetch_compound(
        identifier="2244",
        identifier_type="pubchem_cid",
        source="pubchem",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["sources_queried"] == ["pubchem"]
    assert out["sources_found"] == ["pubchem"]
    assert out["pubchem_cid"] == 2244
    assert out["chembl_id"] is None
    assert out["properties"]["molecular_formula"] == "C9H8O4"


# ---- name lookup / ambiguity --------------------------------------------


@respx.mock
async def test_name_lookup_parallel_both_sources(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    """Name resolution queries both sources in parallel, no InChIKey bridge."""
    respx.get(f"{CHEMBL_BASE_URL}/molecule/search").mock(
        return_value=httpx.Response(200, text=_load("chembl_search_aspirin.json"))
    )
    _mock_chembl_aspirin()
    respx.get(f"{PUBCHEM_BASE_URL}/compound/name/aspirin/cids/JSON").mock(
        return_value=httpx.Response(200, json={"IdentifierList": {"CID": [2244]}})
    )
    _mock_pubchem_aspirin()

    out = await bio_fetch_compound(
        identifier="aspirin",
        identifier_type="name",
        source="both",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["chembl_id"] == "CHEMBL25"
    assert out["pubchem_cid"] == 2244


@respx.mock
async def test_name_with_multiple_pubchem_cids_surfaces_candidates(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    """Ambiguous PubChem resolution surfaces ``candidate_pubchem_cids`` +
    a ``disambiguation_hint`` the model can act on.
    """
    # PubChem returns three candidate CIDs for cortisone-like name.
    respx.get(f"{PUBCHEM_BASE_URL}/compound/name/cortisone/cids/JSON").mock(
        return_value=httpx.Response(
            200, json={"IdentifierList": {"CID": [222786, 636417, 446562]}}
        )
    )
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/222786/property"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "PropertyTable": {
                    "Properties": [
                        {
                            "CID": 222786,
                            "SMILES": "C1CC2(C(=O)C=CC3(C2CC4C3(CCC4)C)C)C1",
                            "InChI": "InChI=test",
                            "InChIKey": "TEST-KEY",
                            "MolecularFormula": "C21H28O5",
                            "MolecularWeight": "360.44",
                            "XLogP": 1.5,
                            "HBondDonorCount": 2,
                            "HBondAcceptorCount": 5,
                            "RotatableBondCount": 2,
                            "IUPACName": "cortisone",
                        }
                    ]
                }
            },
        )
    )
    respx.get(f"{PUBCHEM_BASE_URL}/compound/cid/222786/synonyms/JSON").mock(
        return_value=httpx.Response(
            200,
            json={
                "InformationList": {
                    "Information": [{"CID": 222786, "Synonym": ["cortisone"]}]
                }
            },
        )
    )

    out = await bio_fetch_compound(
        identifier="cortisone",
        identifier_type="name",
        source="pubchem",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["pubchem_cid"] == 222786
    assert out["candidate_pubchem_cids"] == [222786, 636417, 446562]
    assert "stereoisomer" in out["disambiguation_hint"].lower() or "salt" in out["disambiguation_hint"].lower()


# ---- one-side miss & both-miss ------------------------------------------


@respx.mock
async def test_one_side_miss_returns_whatever_was_found(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    """PubChem hits, ChEMBL does not — tool returns PubChem data without error."""
    respx.get(f"{PUBCHEM_BASE_URL}/compound/name/novel_substance/cids/JSON").mock(
        return_value=httpx.Response(200, json={"IdentifierList": {"CID": [88888]}})
    )
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/88888/property"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "PropertyTable": {
                    "Properties": [
                        {
                            "CID": 88888,
                            "SMILES": "CC",
                            "InChI": "InChI=1S/CC",
                            "InChIKey": "XXX-YYY-ZZZ",
                            "MolecularFormula": "C2H6",
                            "MolecularWeight": "30.07",
                            "XLogP": 0.0,
                            "HBondDonorCount": 0,
                            "HBondAcceptorCount": 0,
                            "RotatableBondCount": 0,
                            "IUPACName": "ethane",
                        }
                    ]
                }
            },
        )
    )
    respx.get(f"{PUBCHEM_BASE_URL}/compound/cid/88888/synonyms/JSON").mock(
        return_value=httpx.Response(
            200,
            json={"InformationList": {"Information": [{"Synonym": ["ethane"]}]}},
        )
    )
    # ChEMBL returns empty search.
    respx.get(f"{CHEMBL_BASE_URL}/molecule/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "molecules": [],
                "page_meta": {"total_count": 0, "limit": 5, "offset": 0},
            },
        )
    )

    out = await bio_fetch_compound(
        identifier="novel_substance",
        identifier_type="name",
        source="both",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out.get("error") is not True
    assert out["sources_found"] == ["pubchem"]
    assert "chembl" in out["sources_queried"]
    assert out["chembl_id"] is None
    assert out["pubchem_cid"] == 88888


@respx.mock
async def test_both_sources_miss_returns_error(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
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
    respx.get(f"{PUBCHEM_BASE_URL}/compound/name/zzzznotareal/cids/JSON").mock(
        return_value=httpx.Response(404, text="")
    )

    out = await bio_fetch_compound(
        identifier="zzzznotareal",
        identifier_type="name",
        source="both",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["error"] is True
    assert "not found" in out["message"].lower()


# ---- input validation ----------------------------------------------------


async def test_rejects_invalid_identifier_type(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    out = await bio_fetch_compound(
        identifier="anything",
        identifier_type="not_a_real_type",  # type: ignore[arg-type]
        source="both",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["error"] is True


async def test_rejects_non_integer_pubchem_cid(
    chembl_client: ChEMBLClient,
    pubchem_client: PubChemClient,
) -> None:
    out = await bio_fetch_compound(
        identifier="abc",
        identifier_type="pubchem_cid",
        source="pubchem",
        chembl=chembl_client,
        pubchem=pubchem_client,
    )
    assert out["error"] is True


# ---- integration ---------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test; set RUN_INTEGRATION=1 to run against real APIs",
)
async def test_integration_aspirin_chembl_id_both_sources() -> None:
    c = ChEMBLClient()
    p = PubChemClient()
    try:
        out = await bio_fetch_compound(
            identifier="CHEMBL25",
            identifier_type="chembl_id",
            source="both",
            chembl=c,
            pubchem=p,
        )
    finally:
        await c.aclose()
        await p.aclose()
    assert out.get("error") is not True
    assert out["chembl_id"] == "CHEMBL25"
    assert out["pubchem_cid"] == 2244
    assert out["pref_name"] == "ASPIRIN"
    assert set(out["sources_found"]) == {"chembl", "pubchem"}
    assert "aspirin" in [s.lower() for s in out["synonyms"]]
