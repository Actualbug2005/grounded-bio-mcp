"""Unit tests for ``PubChemClient`` (spec §4.9, §7.1).

PubChem PUG REST is stateless and unauthenticated. The client exposes
three methods the compound tool needs:

* ``get_properties(cid)`` — structured chemical data via
  ``/compound/cid/{cid}/property/{fields}/JSON``.
* ``get_synonyms(cid, limit)`` — capped synonym list via
  ``/compound/cid/{cid}/synonyms/JSON`` (PubChem returns hundreds;
  we cap and preserve rank).
* ``resolve_to_cids(namespace, identifier)`` — identifier-to-CID
  resolution via ``/compound/{namespace}/{identifier}/cids/JSON``.

Quirks verified by probe on 2026-04-24 (see fixtures):

* PubChem's current property names are ``SMILES`` and
  ``ConnectivitySMILES`` (the old ``CanonicalSMILES`` /
  ``IsomericSMILES`` names no longer appear).
* ``MolecularWeight`` comes back as a STRING, not a float.
* Valid-format-but-unknown CID (e.g. ``999999999``) returns HTTP
  200 with a Properties entry containing ONLY the ``CID`` field —
  no molecular data. This is a semantic null that must be detected
  explicitly.
* Malformed identifier (e.g. ``99999999999``) returns HTTP 400
  with ``{"Fault": {...}}``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.pubchem import (
    PUBCHEM_BASE_URL,
    PubChemClient,
    PubChemCompoundNotFound,
)
from grounded_bio_mcp.utils.errors import (
    ExternalServiceDown,
    RateLimitExceeded,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
async def pubchem_client():
    client = PubChemClient()
    try:
        yield client
    finally:
        await client.aclose()


# ---- properties ---------------------------------------------------------


@respx.mock
async def test_get_properties_returns_aspirin_record(
    pubchem_client: PubChemClient,
) -> None:
    route = respx.get(url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/2244/property").mock(
        return_value=httpx.Response(200, text=_load("pubchem_properties_2244.json"))
    )

    props = await pubchem_client.get_properties(2244)

    assert route.called
    assert props["CID"] == 2244
    assert props["MolecularFormula"] == "C9H8O4"
    # PubChem serialises MW as a string-encoded decimal.
    assert props["MolecularWeight"] == "180.16"
    assert props["InChIKey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    # Current property name is "SMILES" (2025+ rename; "CanonicalSMILES"
    # is no longer returned).
    assert "SMILES" in props
    assert props["SMILES"].startswith("CC(=O)")


@respx.mock
async def test_get_properties_for_unknown_cid_raises_not_found(
    pubchem_client: PubChemClient,
) -> None:
    """PubChem returns 200 with a Properties-entry-containing-only-CID
    when the CID is valid-format but not present. The client must
    detect this 'ghost' record and raise a not-found error rather
    than handing back a half-empty dict.
    """
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/999999999/property"
    ).mock(
        return_value=httpx.Response(200, text=_load("pubchem_unknown_cid_ghost.json"))
    )
    with pytest.raises(PubChemCompoundNotFound):
        await pubchem_client.get_properties(999999999)


@respx.mock
async def test_get_properties_malformed_cid_400_raises_not_found(
    pubchem_client: PubChemClient,
) -> None:
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/99999999999/property"
    ).mock(
        return_value=httpx.Response(400, text=_load("pubchem_malformed_cid_400.json"))
    )
    with pytest.raises(PubChemCompoundNotFound):
        await pubchem_client.get_properties(99999999999)


@respx.mock
async def test_get_properties_503_raises_service_down(
    pubchem_client: PubChemClient,
) -> None:
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/2244/property"
    ).mock(return_value=httpx.Response(503, text=""))
    with pytest.raises(ExternalServiceDown):
        await pubchem_client.get_properties(2244)


@respx.mock
async def test_get_properties_429_raises_rate_limit(
    pubchem_client: PubChemClient,
) -> None:
    respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/cid/2244/property"
    ).mock(return_value=httpx.Response(429, text=""))
    with pytest.raises(RateLimitExceeded):
        await pubchem_client.get_properties(2244)


# ---- synonyms -----------------------------------------------------------


@respx.mock
async def test_get_synonyms_returns_capped_ranked_list(
    pubchem_client: PubChemClient,
) -> None:
    respx.get(f"{PUBCHEM_BASE_URL}/compound/cid/2244/synonyms/JSON").mock(
        return_value=httpx.Response(200, text=_load("pubchem_synonyms_2244.json"))
    )

    syns = await pubchem_client.get_synonyms(2244, limit=10)

    # PubChem sorts by frequency/authority; aspirin is always first.
    assert syns[0].lower() == "aspirin"
    assert len(syns) == 10
    # Should cover the commonly-recognised aliases (within top 10).
    top_lower = [s.lower() for s in syns]
    assert "acetylsalicylic acid" in top_lower


@respx.mock
async def test_get_synonyms_unknown_cid_returns_empty(
    pubchem_client: PubChemClient,
) -> None:
    respx.get(f"{PUBCHEM_BASE_URL}/compound/cid/999999999/synonyms/JSON").mock(
        return_value=httpx.Response(200, json={"InformationList": {"Information": []}})
    )
    syns = await pubchem_client.get_synonyms(999999999, limit=10)
    assert syns == []


# ---- identifier resolution ----------------------------------------------


@respx.mock
async def test_resolve_to_cids_by_name_returns_single(
    pubchem_client: PubChemClient,
) -> None:
    respx.get(f"{PUBCHEM_BASE_URL}/compound/name/aspirin/cids/JSON").mock(
        return_value=httpx.Response(200, text=_load("pubchem_name_aspirin_cids.json"))
    )
    cids = await pubchem_client.resolve_to_cids("name", "aspirin")
    assert cids == [2244]


@respx.mock
async def test_resolve_to_cids_no_match_raises_not_found(
    pubchem_client: PubChemClient,
) -> None:
    """PubChem's /cids endpoint returns 404 when the name does not
    resolve to anything — that's the 'no match' signal, not a 200
    with empty list.
    """
    respx.get(
        f"{PUBCHEM_BASE_URL}/compound/name/zzzznotareal/cids/JSON"
    ).mock(return_value=httpx.Response(404, text=""))
    with pytest.raises(PubChemCompoundNotFound):
        await pubchem_client.resolve_to_cids("name", "zzzznotareal")


@respx.mock
async def test_resolve_to_cids_ambiguous_name_returns_all(
    pubchem_client: PubChemClient,
) -> None:
    """If a name maps to multiple CIDs (stereoisomers / salts), return
    them all in PubChem's ranking order — the tool layer surfaces them
    as ``candidate_cids`` so the model can disambiguate.
    """
    respx.get(
        f"{PUBCHEM_BASE_URL}/compound/name/cortisone/cids/JSON"
    ).mock(
        return_value=httpx.Response(
            200, json={"IdentifierList": {"CID": [222786, 636417, 446562]}}
        )
    )
    cids = await pubchem_client.resolve_to_cids("name", "cortisone")
    assert cids == [222786, 636417, 446562]


@respx.mock
async def test_resolve_to_cids_by_smiles_uses_smiles_namespace(
    pubchem_client: PubChemClient,
) -> None:
    route = respx.get(
        url__startswith=f"{PUBCHEM_BASE_URL}/compound/smiles/"
    ).mock(
        return_value=httpx.Response(
            200, json={"IdentifierList": {"CID": [2244]}}
        )
    )
    cids = await pubchem_client.resolve_to_cids("smiles", "CC(=O)Oc1ccccc1C(=O)O")
    assert cids == [2244]
    assert route.called
