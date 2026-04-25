"""Unit + integration tests for ``bio_fetch_pdb`` (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.rcsb import (
    RCSB_DATA_BASE_URL,
    RCSB_FILES_BASE_URL,
    RCSBClient,
)
from grounded_bio_mcp.tools.fetch_pdb import (
    COORDINATES_SOFT_CAP_BYTES,
    fetch_pdb,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def rcsb_client():
    client = RCSBClient()
    try:
        yield client
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_pdb_metadata_parses(rcsb_client: RCSBClient) -> None:
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/entry/1crn").mock(
        return_value=httpx.Response(200, text=_load("rcsb_entry_1CRN.json"))
    )
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/polymer_entity/1crn/1").mock(
        return_value=httpx.Response(200, text=_load("rcsb_polymer_entity_1CRN_1.json"))
    )

    out = await fetch_pdb(pdb_id="1CRN", client=rcsb_client)
    assert out["pdb_id"] == "1CRN"
    assert out["experimental_method"] == "X-RAY DIFFRACTION"
    assert out["resolution"] == 1.5
    assert out["space_group"] == "P 1 21 1"
    assert out["r_factors"]["r_work"] == 0.114
    assert out["r_factors"]["r_free"] is None
    assert out["deposit_date"].startswith("1981-04-30")
    assert len(out["chains"]) == 1
    chain_a = out["chains"][0]
    assert chain_a["auth_chains"] == ["A"]
    assert chain_a["sequence"] == "TTCCPSIVARSNFNVCRLPGTPEAICATYTGCIIIPGATCPGDYAN"
    assert chain_a["length"] == 46
    assert chain_a["description"] == "CRAMBIN"


@respx.mock
async def test_fetch_pdb_chain_filter_drops_others(rcsb_client: RCSBClient) -> None:
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/entry/1crn").mock(
        return_value=httpx.Response(200, text=_load("rcsb_entry_1CRN.json"))
    )
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/polymer_entity/1crn/1").mock(
        return_value=httpx.Response(200, text=_load("rcsb_polymer_entity_1CRN_1.json"))
    )

    out = await fetch_pdb(pdb_id="1CRN", chain_filter="Z", client=rcsb_client)
    assert out["chains"] == []


@respx.mock
async def test_fetch_pdb_with_coordinates_inlines_cif(rcsb_client: RCSBClient) -> None:
    cif_text = _load("rcsb_1CRN_minimal.cif")
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/entry/1crn").mock(
        return_value=httpx.Response(200, text=_load("rcsb_entry_1CRN.json"))
    )
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/polymer_entity/1crn/1").mock(
        return_value=httpx.Response(200, text=_load("rcsb_polymer_entity_1CRN_1.json"))
    )
    respx.get(f"{RCSB_FILES_BASE_URL}/download/1crn.cif").mock(
        return_value=httpx.Response(200, text=cif_text)
    )

    out = await fetch_pdb(pdb_id="1CRN", include_coordinates=True, client=rcsb_client)
    assert out["coordinates_format"] == "mmCIF"
    assert out["coordinates"].startswith("data_1CRN")
    assert out["coordinates_size_bytes"] == len(cif_text.encode("utf-8"))


@respx.mock
async def test_fetch_pdb_oversized_coordinates_returns_actionable_url(
    rcsb_client: RCSBClient,
) -> None:
    bloated = "data_XXXX\n" + ("X" * (COORDINATES_SOFT_CAP_BYTES + 64))
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/entry/1crn").mock(
        return_value=httpx.Response(200, text=_load("rcsb_entry_1CRN.json"))
    )
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/polymer_entity/1crn/1").mock(
        return_value=httpx.Response(200, text=_load("rcsb_polymer_entity_1CRN_1.json"))
    )
    respx.get(f"{RCSB_FILES_BASE_URL}/download/1crn.cif").mock(
        return_value=httpx.Response(200, text=bloated)
    )

    out = await fetch_pdb(pdb_id="1CRN", include_coordinates=True, client=rcsb_client)
    assert "coordinates" not in out
    err = out["coordinates_error"]
    assert "too large" in err.lower()
    assert "https://files.rcsb.org/download/1crn.cif" in err


@respx.mock
async def test_fetch_pdb_missing_id_returns_actionable_error(
    rcsb_client: RCSBClient,
) -> None:
    respx.get(f"{RCSB_DATA_BASE_URL}/rest/v1/core/entry/9xxx").mock(
        return_value=httpx.Response(404, text="")
    )

    out = await fetch_pdb(pdb_id="9XXX", client=rcsb_client)
    assert out["error"] is True
    assert "9XXX" in out["message"]
    assert out["suggestions"]


@pytest.mark.integration
async def test_fetch_pdb_integration_1crn() -> None:
    """Live RCSB: 1CRN (crambin) has been in PDB since 1981 — stable."""
    client = RCSBClient()
    try:
        out = await fetch_pdb(pdb_id="1CRN", client=client)
    finally:
        await client.aclose()

    assert out["pdb_id"] == "1CRN"
    # Crambin's original 1982 refinement is 1.5 Å; an ultra-high-resolution
    # re-refinement at 0.54 Å exists as a separate PDB entry (1EJG), not
    # under 1CRN itself. Assert generously.
    assert out["resolution"] is not None
    assert out["resolution"] < 2.5
    assert out["experimental_method"] == "X-RAY DIFFRACTION"
    assert out["chains"], "crambin has at least one polymer chain"
    seq = out["chains"][0]["sequence"]
    assert seq.startswith("TTCC")  # first residues of crambin
