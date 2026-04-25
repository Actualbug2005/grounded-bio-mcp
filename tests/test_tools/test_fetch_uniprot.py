"""Unit + integration tests for ``bio_fetch_uniprot`` (spec §4.2).

Unit tests use a canned ``uniprot_P01308.json`` fixture via :mod:`respx`.
The integration test hits the live UniProt REST API and is gated on
``RUN_INTEGRATION=1``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.uniprot import UNIPROT_BASE_URL, UniProtClient
from grounded_bio_mcp.tools.fetch_uniprot import fetch_uniprot

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def uniprot_client():
    client = UniProtClient()
    try:
        yield client
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_uniprot_returns_narrow_model(uniprot_client: UniProtClient) -> None:
    respx.get(f"{UNIPROT_BASE_URL}/uniprotkb/P01308.json").mock(
        return_value=httpx.Response(200, text=_load("uniprot_P01308.json"))
    )

    out = await fetch_uniprot(accession="P01308", client=uniprot_client)
    assert isinstance(out, str)
    payload = json.loads(out)

    assert payload["accession"] == "P01308"
    assert payload["entry_name"] == "INS_HUMAN"
    assert payload["protein_name"] == "Insulin"
    assert payload["length"] == 110
    assert payload["sequence"].startswith("MALWMR")
    assert payload["organism"] == {
        "scientific_name": "Homo sapiens",
        "common_name": "Human",
        "taxon_id": 9606,
    }
    assert payload["entry_version"] == 253
    assert payload["last_sequence_update_date"] == "1986-07-21"


@respx.mock
async def test_fetch_uniprot_features_parsed(uniprot_client: UniProtClient) -> None:
    respx.get(f"{UNIPROT_BASE_URL}/uniprotkb/P01308.json").mock(
        return_value=httpx.Response(200, text=_load("uniprot_P01308.json"))
    )

    out = await fetch_uniprot(
        accession="P01308", include_features=True, client=uniprot_client
    )
    payload = json.loads(out)

    feature_types = {f["type"] for f in payload["features"]}
    assert feature_types == {"Signal", "Chain", "Disulfide bond"}
    signal = next(f for f in payload["features"] if f["type"] == "Signal")
    assert signal["start"] == 1
    assert signal["end"] == 24


@respx.mock
async def test_fetch_uniprot_without_features(uniprot_client: UniProtClient) -> None:
    respx.get(f"{UNIPROT_BASE_URL}/uniprotkb/P01308.json").mock(
        return_value=httpx.Response(200, text=_load("uniprot_P01308.json"))
    )

    out = await fetch_uniprot(
        accession="P01308", include_features=False, client=uniprot_client
    )
    payload = json.loads(out)
    assert "features" not in payload, "features excluded when include_features=False"


@respx.mock
async def test_fetch_uniprot_cross_refs_grouped(uniprot_client: UniProtClient) -> None:
    respx.get(f"{UNIPROT_BASE_URL}/uniprotkb/P01308.json").mock(
        return_value=httpx.Response(200, text=_load("uniprot_P01308.json"))
    )

    out = await fetch_uniprot(accession="P01308", client=uniprot_client)
    payload = json.loads(out)
    xrefs = payload["cross_references"]

    # Priority databases surface as top-level keys — these are the bridges
    # to bio_fetch_pdb, bio_fetch_alphafold, bio_fetch_sequence.
    assert {"PDB", "AlphaFoldDB", "RefSeq", "Pfam", "InterPro"} <= xrefs.keys()
    pdb_ids = {entry["id"] for entry in xrefs["PDB"]}
    assert pdb_ids == {"1A7F", "1AI0"}
    # Property dict round-trip: PDB Method must survive the flatten step.
    assert xrefs["PDB"][0]["properties"]["Method"] in {"NMR", "X-ray"}


@respx.mock
async def test_fetch_uniprot_missing_accession_is_actionable(
    uniprot_client: UniProtClient,
) -> None:
    respx.get(f"{UNIPROT_BASE_URL}/uniprotkb/ZZZZZZ.json").mock(
        return_value=httpx.Response(404, text="")
    )

    out = await fetch_uniprot(accession="ZZZZZZ", client=uniprot_client)
    assert isinstance(out, dict)
    assert out["error"] is True
    assert "ZZZZZZ" in out["message"]
    assert out["suggestions"]


@pytest.mark.integration
async def test_fetch_uniprot_integration_p01308() -> None:
    """Live UniProt: P01308 (human insulin) is stable across releases."""
    client = UniProtClient()
    try:
        out = await fetch_uniprot(accession="P01308", client=client)
    finally:
        await client.aclose()

    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["accession"] == "P01308"
    assert payload["organism"]["taxon_id"] == 9606
    # Insulin preproprotein is 110 aa; this is locked by spec.
    assert payload["length"] == 110
    # AlphaFold cross-ref must be present — it is the bridge to bio_fetch_alphafold.
    assert "AlphaFoldDB" in payload["cross_references"]
