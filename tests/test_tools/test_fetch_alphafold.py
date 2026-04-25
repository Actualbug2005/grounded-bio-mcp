"""Unit + integration tests for ``bio_fetch_alphafold`` (spec §4.4)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.alphafold import (
    ALPHAFOLD_BASE_URL,
    AlphaFoldClient,
)
from grounded_bio_mcp.tools.fetch_alphafold import fetch_alphafold

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def af_client():
    client = AlphaFoldClient()
    try:
        yield client
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_alphafold_summary_always_includes_plddt(af_client: AlphaFoldClient) -> None:
    respx.get(f"{ALPHAFOLD_BASE_URL}/api/prediction/P01308").mock(
        return_value=httpx.Response(200, text=_load("alphafold_prediction_P01308.json"))
    )
    respx.get(
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v4.pdb"
    ).mock(
        return_value=httpx.Response(200, text=_load("alphafold_AF-P01308_F1_plddt.pdb"))
    )

    out = await fetch_alphafold(uniprot_accession="P01308", client=af_client)
    assert out["uniprot_accession"] == "P01308"
    assert out["uniprot_id"] == "INS_HUMAN"
    assert out["confidence_category"] == "VERY_HIGH"
    assert out["has_pae"] is True

    summary = out["plddt_summary"]
    assert summary["residue_count"] == 9
    # Fixture: N-term=30 (3 residues), middle=90 (3), C-term=50 (3). Mean ≈ 56.67.
    assert summary["mean_plddt"] == pytest.approx(56.67, abs=0.01)
    regions = summary["per_region"]
    assert regions["n_term"]["mean_plddt"] == 30.0
    assert regions["middle"]["mean_plddt"] == 90.0
    assert regions["c_term"]["mean_plddt"] == 50.0
    assert regions["n_term"]["residue_range"] == [1, 3]
    assert regions["middle"]["residue_range"] == [4, 6]
    assert regions["c_term"]["residue_range"] == [7, 9]

    # summary format does not inline the structure.
    assert "structure" not in out


@respx.mock
async def test_fetch_alphafold_uses_pdb_url_from_metadata(af_client: AlphaFoldClient) -> None:
    """We must route through ``pdbUrl`` not a locally-constructed URL.

    This test proves that behaviour by serving a prediction whose pdbUrl
    points at an unusual path — if we were constructing URLs ourselves we'd
    hit the canonical ``_v4.pdb`` path and the test would miss.
    """
    import json as _json

    payload = _json.loads(_load("alphafold_prediction_P01308.json"))
    payload[0]["pdbUrl"] = (
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v999.pdb"
    )
    respx.get(f"{ALPHAFOLD_BASE_URL}/api/prediction/P01308").mock(
        return_value=httpx.Response(200, json=payload)
    )
    called = respx.get(
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v999.pdb"
    ).mock(
        return_value=httpx.Response(200, text=_load("alphafold_AF-P01308_F1_plddt.pdb"))
    )

    out = await fetch_alphafold(uniprot_accession="P01308", client=af_client)
    assert called.called, "client must GET the pdbUrl returned by metadata, not a guessed URL"
    assert out["pdb_url"].endswith("_v999.pdb")


@respx.mock
async def test_fetch_alphafold_format_pdb_inlines_structure(af_client: AlphaFoldClient) -> None:
    respx.get(f"{ALPHAFOLD_BASE_URL}/api/prediction/P01308").mock(
        return_value=httpx.Response(200, text=_load("alphafold_prediction_P01308.json"))
    )
    respx.get(
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v4.pdb"
    ).mock(
        return_value=httpx.Response(200, text=_load("alphafold_AF-P01308_F1_plddt.pdb"))
    )

    out = await fetch_alphafold(
        uniprot_accession="P01308", format="pdb", client=af_client
    )
    assert out["structure_format"] == "pdb"
    assert out["structure"].startswith("ATOM")
    # pLDDT summary still present — anti-hallucination requirement (spec §4.4).
    assert out["plddt_summary"]["mean_plddt"] == pytest.approx(56.67, abs=0.01)


@respx.mock
async def test_fetch_alphafold_format_cif_downloads_cif(af_client: AlphaFoldClient) -> None:
    respx.get(f"{ALPHAFOLD_BASE_URL}/api/prediction/P01308").mock(
        return_value=httpx.Response(200, text=_load("alphafold_prediction_P01308.json"))
    )
    respx.get(
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v4.pdb"
    ).mock(
        return_value=httpx.Response(200, text=_load("alphafold_AF-P01308_F1_plddt.pdb"))
    )
    respx.get(
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v4.cif"
    ).mock(return_value=httpx.Response(200, text="data_AF-P01308\n# minimal CIF"))

    out = await fetch_alphafold(
        uniprot_accession="P01308", format="cif", client=af_client
    )
    assert out["structure_format"] == "cif"
    assert "data_AF-P01308" in out["structure"]
    # Both pdb and cif were downloaded — pdb for pLDDT summary, cif for payload.
    assert out["plddt_summary"]["mean_plddt"] is not None


@respx.mock
async def test_fetch_alphafold_low_confidence_warning_surfaces(
    af_client: AlphaFoldClient,
) -> None:
    """Synthesise a prediction where every CA has pLDDT 40 to trip the warning."""
    low_pdb_lines = []
    for serial in range(1, 7):
        low_pdb_lines.append(
            f"ATOM  {serial:>5d}  CA  ALA A{serial:>4d}    "
            f"{10.0:8.3f}{10.0:8.3f}{0.0:8.3f}"
            f"{1.00:6.2f}{40.0:6.2f}          C  "
        )
    low_pdb = "\n".join(low_pdb_lines) + "\nEND\n"

    respx.get(f"{ALPHAFOLD_BASE_URL}/api/prediction/P01308").mock(
        return_value=httpx.Response(200, text=_load("alphafold_prediction_P01308.json"))
    )
    respx.get(
        "https://alphafold.ebi.ac.uk/files/AF-P01308-F1-model_v4.pdb"
    ).mock(return_value=httpx.Response(200, text=low_pdb))

    out = await fetch_alphafold(uniprot_accession="P01308", client=af_client)
    assert out["plddt_summary"]["mean_plddt"] == 40.0
    assert out["plddt_summary"]["low_confidence_warning"] is not None
    assert "pLDDT" in out["plddt_summary"]["low_confidence_warning"]


@respx.mock
async def test_fetch_alphafold_missing_returns_actionable_error(
    af_client: AlphaFoldClient,
) -> None:
    respx.get(f"{ALPHAFOLD_BASE_URL}/api/prediction/ZZZZZZ").mock(
        return_value=httpx.Response(404, text="")
    )

    out = await fetch_alphafold(uniprot_accession="ZZZZZZ", client=af_client)
    assert out["error"] is True
    assert "ZZZZZZ" in out["message"]
    assert out["suggestions"]


@pytest.mark.integration
async def test_fetch_alphafold_integration_p01308() -> None:
    """Live AlphaFold: P01308 has a predicted structure (same test accession as UniProt)."""
    client = AlphaFoldClient()
    try:
        out = await fetch_alphafold(uniprot_accession="P01308", client=client)
    finally:
        await client.aclose()

    assert out["uniprot_accession"] == "P01308"
    summary = out["plddt_summary"]
    assert summary["residue_count"] == 110, "insulin preproprotein is 110 aa"
    assert 0 < summary["mean_plddt"] <= 100
    for region in ("n_term", "middle", "c_term"):
        mean = summary["per_region"][region]["mean_plddt"]
        assert 0 < mean <= 100
