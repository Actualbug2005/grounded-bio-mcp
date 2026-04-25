"""Unit + integration tests for ``bio_fetch_sequence`` (spec §4.1).

The unit tests mock NCBI E-utilities with :mod:`respx`; they run in the
default offline pytest invocation. The single integration test is gated
behind ``RUN_INTEGRATION=1`` (spec §10.2) and hits NCBI with the stable
test accession ``NM_001301717``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.ncbi import EUTILS_BASE_URL, NCBIClient
from grounded_bio_mcp.config import Settings
from grounded_bio_mcp.tools.fetch_sequence import fetch_sequence

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _settings() -> Settings:
    # No API key, no email — pytest-fixture isolation from developer env.
    return Settings(NCBI_API_KEY=None, EBI_EMAIL=None)


@pytest.fixture
async def ncbi_client():
    client = NCBIClient(_settings())
    try:
        yield client
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_sequence_fasta_returns_parsed_record(ncbi_client: NCBIClient) -> None:
    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=_load("ncbi_fetch_sequence_fasta.txt"))
    )

    out = await fetch_sequence(
        accession="NM_000001",
        database="nucleotide",
        rettype="fasta",
        client=ncbi_client,
    )

    assert isinstance(out, str), "response_format='json' default returns serialised JSON text"
    payload = json.loads(out)
    assert payload["accession"] == "NM_000001.1"
    assert payload["database"] == "nucleotide"
    assert payload["rettype"] == "fasta"
    assert payload["length"] == 180
    # Spot-check the first codon and that the sequence round-trips intact.
    assert payload["sequence"].startswith("ATGGCGACCC")
    assert len(payload["sequence"]) == payload["length"]


@respx.mock
async def test_fetch_sequence_genbank_parses_feature_table(ncbi_client: NCBIClient) -> None:
    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=_load("ncbi_fetch_sequence_genbank.txt"))
    )

    out = await fetch_sequence(
        accession="NM_000001",
        database="nucleotide",
        rettype="gb",
        client=ncbi_client,
    )
    payload = json.loads(out)

    assert payload["organism"] == "Homo sapiens"
    assert payload["rettype"] == "gb"
    feature_types = [f["type"] for f in payload["features"]]
    assert feature_types == ["source", "gene", "CDS"]
    cds = payload["features"][2]
    assert cds["qualifiers"]["gene"] == ["TEST1"]
    assert cds["qualifiers"]["protein_id"] == ["NP_000001.1"]


@respx.mock
async def test_fetch_sequence_missing_accession_returns_actionable_error(
    ncbi_client: NCBIClient,
) -> None:
    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(
        return_value=httpx.Response(400, text="Error: Accession not found")
    )

    out = await fetch_sequence(
        accession="ZZ_DOES_NOT_EXIST",
        database="nucleotide",
        rettype="fasta",
        client=ncbi_client,
    )

    assert isinstance(out, dict), "error responses stay as dicts so the model can branch on 'error'"
    assert out["error"] is True
    assert "ZZ_DOES_NOT_EXIST" in out["message"]
    assert out["suggestions"], "error must include at least one suggestion (spec §8)"


@respx.mock
async def test_fetch_sequence_includes_api_key_when_configured() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, text=_load("ncbi_fetch_sequence_fasta.txt"))

    respx.get(f"{EUTILS_BASE_URL}/efetch.fcgi").mock(side_effect=handler)

    client = NCBIClient(Settings(NCBI_API_KEY="test-key-xyz", EBI_EMAIL="me@example.test"))
    try:
        await fetch_sequence(
            accession="NM_000001",
            database="nucleotide",
            rettype="fasta",
            client=client,
        )
    finally:
        await client.aclose()

    # Spec §2.4 / §7.1: sending api_key unlocks the 10 req/s tier.
    assert captured.get("api_key") == "test-key-xyz"
    assert captured.get("email") == "me@example.test"
    assert captured.get("db") == "nucleotide"


@pytest.mark.integration
async def test_fetch_sequence_integration_nm_001301717() -> None:
    """Live NCBI: the test accession is stable across RefSeq releases (spec §10.2).

    We deliberately do not assert on what this accession *encodes* (the spec
    §10.2 label appears to be out of date); what matters for a regression
    test is that the fetch succeeds, the parser cleanly produces a DNA
    sequence, and the length is substantial.
    """
    client = NCBIClient(Settings())
    try:
        out = await fetch_sequence(
            accession="NM_001301717",
            database="nucleotide",
            rettype="fasta",
            client=client,
        )
    finally:
        await client.aclose()

    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["accession"].startswith("NM_001301717")
    # Any real RefSeq mRNA is well over 500 nt — cheap smoke assertion
    # that also catches "empty body returned 200" regressions.
    assert payload["length"] > 500
    assert set(payload["sequence"]).issubset({"A", "C", "G", "T", "N"})
