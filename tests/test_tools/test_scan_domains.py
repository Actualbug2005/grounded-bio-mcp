"""Unit + integration tests for ``bio_scan_domains`` (spec §4.13).

Partial-results-during-RUNNING path is **deliberately not tested** —
session 3 shipped the tier-3 hard-error fallback because EBI_EMAIL
wasn't available at pre-work time to run the required probe.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bioinformatics_mcp.tools.scan_domains import (
    ScanDomainsInput,
    _canonical_appl,
    _parse_matches,
    bio_scan_domains,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
INSULIN_MATCH_JSON = (FIXTURE_DIR / "iprscan5_insulin_match.json").read_bytes()


# ---- applications mapping (Pfam → PfamA errata) -------------------------


def test_canonical_appl_maps_pfam_to_pfama() -> None:
    """Spec §4.13 default 'Pfam' does not exist in EBI's API — must map to 'PfamA'."""
    assert _canonical_appl(["Pfam", "SMART", "CDD"]) == "PfamA,SMART,CDD"


def test_canonical_appl_expands_prosite_to_two_databases() -> None:
    """EBI splits PROSITE into PrositeProfiles + PrositePatterns."""
    assert _canonical_appl(["PROSITE"]) == "PrositeProfiles,PrositePatterns"


def test_canonical_appl_fixes_casing() -> None:
    """'SUPERFAMILY' → 'SuperFamily', 'Gene3D' → 'Gene3d'."""
    assert _canonical_appl(["SUPERFAMILY", "Gene3D"]) == "SuperFamily,Gene3d"


# ---- match parsing ------------------------------------------------------


def test_parse_matches_flattens_per_signature_location() -> None:
    import json

    payload = json.loads(INSULIN_MATCH_JSON)
    matches = _parse_matches(payload)
    # Two signatures (Pfam Insulin + SMART IlGF), one location each → 2 matches.
    assert len(matches) == 2
    pfam_hit = next(m for m in matches if m["signature_library"] == "PFAM")
    assert pfam_hit["signature_id"] == "PF00049"
    assert pfam_hit["interpro_id"] == "IPR004825"
    assert pfam_hit["start"] == 25
    assert pfam_hit["end"] == 108
    assert pfam_hit["e_value"] == 3.2e-30


def test_parse_matches_on_empty_results_returns_empty_list() -> None:
    """Empty matches must be a valid result, not a parse error."""
    assert _parse_matches({"results": [{"matches": []}]}) == []
    assert _parse_matches({"results": []}) == []
    assert _parse_matches({}) == []


# ---- input validation ---------------------------------------------------


def test_input_rejects_sequence_too_short() -> None:
    with pytest.raises(ValueError):
        ScanDomainsInput.model_validate(
            {"sequence": "ACGT"}  # <20 residues
        )


def test_max_wait_seconds_capped_at_ceiling() -> None:
    """Pathological holds are prevented — 1800 s ceiling per user decision 7."""
    with pytest.raises(ValueError):
        ScanDomainsInput.model_validate(
            {"sequence": "A" * 50, "max_wait_seconds": 5000}
        )


# ---- tool path ----------------------------------------------------------


@pytest.fixture
def fake_runner() -> MagicMock:
    runner = MagicMock()
    runner.base_url = "https://www.ebi.ac.uk/Tools/services/rest/iprscan5"
    runner.run = AsyncMock()
    return runner


async def test_bio_scan_domains_happy_path(fake_runner: MagicMock) -> None:
    fake_runner.run.return_value = INSULIN_MATCH_JSON
    out = await bio_scan_domains(
        sequence="M" + "A" * 100,
        applications=["Pfam", "SMART", "CDD"],
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["match_count"] == 2
    assert out["databases_scanned"] == ["PfamA", "SMART", "CDD"]
    # Submission used the PfamA canonical name, not "Pfam".
    submitted = fake_runner.run.call_args.kwargs["params"]
    assert submitted["appl"] == "PfamA,SMART,CDD"
    assert submitted["stype"] == "p"


async def test_bio_scan_domains_empty_matches_is_valid_result(
    fake_runner: MagicMock,
) -> None:
    """Spec: empty matches must not be treated as an error."""
    fake_runner.run.return_value = b'{"results": [{"matches": []}]}'
    out = await bio_scan_domains(
        sequence="A" * 50,
        runner=fake_runner,
        email="t@example.org",
    )
    assert out.get("error") is not True
    assert out["match_count"] == 0
    assert out["matches"] == []


async def test_bio_scan_domains_timeout_returns_tier3_error(
    fake_runner: MagicMock,
) -> None:
    """Tier-3 fallback: timeout = hard error, no partial results."""
    from bioinformatics_mcp.utils.errors import JobTimeoutError

    fake_runner.run.side_effect = JobTimeoutError(
        service="iprscan5",
        job_id="J-SLOW",
        timeout_s=600.0,
        status_url="https://www.ebi.ac.uk/Tools/services/rest/iprscan5/status/J-SLOW",
        cancelled=True,
    )
    out = await bio_scan_domains(
        sequence="A" * 50,
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["error"] is True
    assert out["job_id"] == "J-SLOW"
    # Suggestion must point users at max_wait_seconds, per user decision 7.
    assert any("max_wait_seconds" in s for s in out["suggestions"])
    # Partial-results language must appear so callers know why we didn't give partial data.
    assert any("tier-3" in s.lower() for s in out["suggestions"])


async def test_bio_scan_domains_jobfailed_suggests_dna_misclassification(
    fake_runner: MagicMock,
) -> None:
    from bioinformatics_mcp.utils.errors import JobFailed

    fake_runner.run.side_effect = JobFailed(
        service="iprscan5", job_id="J-BAD", status="FAILED"
    )
    out = await bio_scan_domains(
        sequence="A" * 50,
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["error"] is True
    assert any("DNA" in s for s in out["suggestions"])


async def test_bio_scan_domains_invalid_json_response_returns_actionable(
    fake_runner: MagicMock,
) -> None:
    fake_runner.run.return_value = b"<html>service down</html>"
    out = await bio_scan_domains(
        sequence="A" * 50,
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["error"] is True
    assert "non-JSON" in out["message"]


# ---- integration (gated) -----------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("EBI_EMAIL"),
    reason="integration test needs EBI_EMAIL",
)
async def test_integration_insulin_finds_pfam_signature() -> None:
    """Real EBI InterProScan run — human insulin must match a Pfam signature."""
    from bioinformatics_mcp.clients.ebi import EBIJobRunner
    from bioinformatics_mcp.utils.rate_limit import RateLimitedClient

    email = os.environ["EBI_EMAIL"]
    rlc = RateLimitedClient(max_concurrent=3, min_interval_s=0.5, timeout=60.0)
    runner = EBIJobRunner("iprscan5", rlc)
    try:
        out = await bio_scan_domains(
            sequence=(
                "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKTRREAEDLQ"
                "VGQVELGGGPGAGSLQPLALEGSLQKRGIVEQCCTSICSLYQLENYCN"
            ),
            applications=["Pfam", "SMART", "CDD"],
            runner=runner,
            email=email,
            max_wait_seconds=900,
        )
    finally:
        await rlc.aclose()

    assert out.get("error") is not True, out
    assert out["match_count"] > 0
    libraries = {m["signature_library"] for m in out["matches"]}
    assert "PFAM" in libraries or "PfamA" in libraries
