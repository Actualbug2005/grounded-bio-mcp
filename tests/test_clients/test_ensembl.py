"""Unit tests for ``EnsemblClient`` (spec §4.11, §4.12, §7.1).

The client wraps Ensembl REST. It has to route between the GRCh38
server (``rest.ensembl.org``, the default) and the legacy GRCh37 server
(``grch37.rest.ensembl.org``) based on the caller's ``assembly`` argument,
surface the actual assembly used, and normalise errors into the
project-wide ``BioMCPError`` taxonomy.

Fixtures captured live on 2026-04-24 against Ensembl release 114
(GRCh38) / release 113 (GRCh37).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bioinformatics_mcp.clients.ensembl import (
    ENSEMBL_GRCH37_BASE_URL,
    ENSEMBL_REST_BASE_URL,
    EnsemblClient,
)
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
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


# ---- variation lookup -----------------------------------------------------


@respx.mock
async def test_lookup_variation_returns_full_record(
    ensembl_client: EnsemblClient,
) -> None:
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_variation_rs429358.json")
        )
    )

    record = await ensembl_client.lookup_variation("human", "rs429358")

    assert route.called
    # pops=1 is requested by default so population frequencies are
    # harvested in one round-trip (design decision approved 2026-04-24).
    call_url = str(route.calls.last.request.url)
    assert "pops=1" in call_url

    assert record["name"] == "rs429358"
    # Heavily-annotated variant: these top-level fields should be present.
    assert record["most_severe_consequence"] == "missense_variant"
    assert record["mappings"][0]["assembly_name"] == "GRCh38"
    # With pops=1 the populations array must be surfaced so the tool layer
    # can derive MAF from gnomADe.
    assert any(
        p.get("population", "").startswith("gnomADe")
        for p in record["populations"]
    )


@respx.mock
async def test_lookup_variation_grch37_routes_to_legacy_server(
    ensembl_client: EnsemblClient,
) -> None:
    grch38_route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358"
    ).mock(return_value=httpx.Response(500, text="should not be called"))
    grch37_route = respx.get(
        f"{ENSEMBL_GRCH37_BASE_URL}/variation/human/rs429358"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_variation_rs429358_grch37.json")
        )
    )

    record = await ensembl_client.lookup_variation(
        "human", "rs429358", assembly="GRCh37"
    )

    assert grch37_route.called
    assert not grch38_route.called
    assert record["mappings"][0]["assembly_name"] == "GRCh37"


@respx.mock
async def test_lookup_variation_not_found_raises(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs999999999999"
    ).mock(
        return_value=httpx.Response(
            400, text=_load("ensembl_variation_not_found.json")
        )
    )
    with pytest.raises(AccessionNotFound) as exc:
        await ensembl_client.lookup_variation("human", "rs999999999999")
    assert exc.value.accession == "rs999999999999"
    assert "Ensembl" in exc.value.database


@respx.mock
async def test_lookup_variation_429_raises_rate_limit(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358"
    ).mock(return_value=httpx.Response(429, text=""))
    with pytest.raises(RateLimitExceeded):
        await ensembl_client.lookup_variation("human", "rs429358")


@respx.mock
async def test_lookup_variation_503_raises_service_down(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358"
    ).mock(return_value=httpx.Response(503, text=""))
    with pytest.raises(ExternalServiceDown):
        await ensembl_client.lookup_variation("human", "rs429358")


@respx.mock
async def test_lookup_variation_without_populations_flag_omits_pops(
    ensembl_client: EnsemblClient,
) -> None:
    """Caller can opt out of the pops=1 default for a lighter payload."""
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/variation/human/rs429358"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_variation_rs429358.json")
        )
    )
    await ensembl_client.lookup_variation(
        "human", "rs429358", include_populations=False
    )
    call_url = str(route.calls.last.request.url)
    assert "pops=" not in call_url


# ---- VEP HGVS ------------------------------------------------------------


@respx.mock
async def test_vep_hgvs_returns_list_of_consequences(
    ensembl_client: EnsemblClient,
) -> None:
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/hgvs/ENSP00000252486.4:p.Cys130Arg"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_vep_hgvs_apoe.json")
        )
    )

    results = await ensembl_client.vep_hgvs(
        "human", "ENSP00000252486.4:p.Cys130Arg"
    )

    # VEP always returns a list, even for single-variant input.
    assert isinstance(results, list)
    assert len(results) == 1
    # The input is echoed back in the id field, so callers can correlate
    # results with inputs in batch queries.
    assert results[0]["id"] == "ENSP00000252486.4:p.Cys130Arg"


@respx.mock
async def test_vep_hgvs_400_raises_accession_not_found(
    ensembl_client: EnsemblClient,
) -> None:
    """Invalid HGVS notation comes back as a 400 with an 'Unable to parse' error."""
    respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/hgvs/bogus_notation"
    ).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "Unable to parse HGVS notation 'bogus_notation': Could not parse the HGVS notation bogus_notation"
            },
        )
    )
    with pytest.raises(AccessionNotFound) as exc:
        await ensembl_client.vep_hgvs("human", "bogus_notation")
    assert exc.value.accession == "bogus_notation"


# ---- VEP region ----------------------------------------------------------


@respx.mock
async def test_vep_region_builds_url_with_strand_and_allele(
    ensembl_client: EnsemblClient,
) -> None:
    route = respx.get(
        f"{ENSEMBL_REST_BASE_URL}/vep/human/region/19:44908684-44908684:1/C"
    ).mock(
        return_value=httpx.Response(
            200, text=_load("ensembl_vep_region_apoe.json")
        )
    )

    results = await ensembl_client.vep_region(
        "human", region="19:44908684-44908684", strand=1, allele="C"
    )

    assert route.called
    assert isinstance(results, list)
    # APOE ε4 missense consequence must be present in the transcript list.
    consequences = {
        term
        for r in results
        for tc in r.get("transcript_consequences", [])
        for term in tc.get("consequence_terms", [])
    }
    assert "missense_variant" in consequences


@respx.mock
async def test_vep_region_grch37_routes_to_legacy_server(
    ensembl_client: EnsemblClient,
) -> None:
    grch37_route = respx.get(
        f"{ENSEMBL_GRCH37_BASE_URL}/vep/human/region/19:45411941-45411941:1/C"
    ).mock(return_value=httpx.Response(200, json=[]))

    await ensembl_client.vep_region(
        "human",
        region="19:45411941-45411941",
        strand=1,
        allele="C",
        assembly="GRCh37",
    )
    assert grch37_route.called
