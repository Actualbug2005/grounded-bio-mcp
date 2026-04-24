"""Unit + integration tests for ``bio_align_sequences`` (spec §4.5).

Offline tests use a tiny pre-baked Clustal fixture; the EBIJobRunner is
stubbed so no network I/O happens. Integration test runs a real EBI
Clustal job — gated on RUN_INTEGRATION=1 and EBI_EMAIL.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from Bio import AlignIO

from bioinformatics_mcp.tools.align_sequences import (
    AlignSequencesInput,
    _build_multifasta,
    _compute_stats,
    bio_align_sequences,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
TINY_ALN = (FIXTURE_DIR / "clustalo_tiny_insulin.aln").read_text()


# ---- stats --------------------------------------------------------------


def test_compute_stats_against_known_alignment() -> None:
    """Hand-verified: 3 insulin signal peptides, 20 cols, 18 conserved,
    0 gaps, pairwise 95/95/90 → mean 93.33."""
    alignment = AlignIO.read(io.StringIO(TINY_ALN), "clustal")
    stats = _compute_stats(alignment)
    assert stats["alignment_length"] == 20
    assert stats["conserved_columns_count"] == 18
    assert stats["gap_pct"] == 0.0
    assert stats["strict_identity_pct"] == 90.0  # 18/20 * 100
    assert stats["mean_pairwise_identity_pct"] == pytest.approx(93.33, abs=0.01)


def test_compute_stats_with_gaps() -> None:
    """A hand-built alignment with known gap columns and identity."""
    # Columns:
    # 0: A A A   → all-same, no gap → conserved
    # 1: C - C   → has gap → gap col
    # 2: G G T   → all non-gap but not all-same → non-gap, not conserved
    # 3: - - -   → has gap (all gaps) → gap col
    text = (
        "CLUSTAL\n\n"
        "s1   ACG-\n"
        "s2   A-G-\n"
        "s3   ACT-\n"
    )
    alignment = AlignIO.read(io.StringIO(text), "clustal")
    stats = _compute_stats(alignment)
    assert stats["alignment_length"] == 4
    # conserved: only col 0 (all A, no gaps)
    assert stats["conserved_columns_count"] == 1
    # gap cols: 1, 3 → 2 cols with a gap → 50%
    assert stats["gap_pct"] == 50.0
    # non-gap cols: 0 and 2 → 1 conserved of 2 → 50%
    assert stats["strict_identity_pct"] == 50.0


def test_compute_stats_handles_divergent_all_different() -> None:
    """Twilight-zone case — zero identity must be a valid output, not an error."""
    text = (
        "CLUSTAL\n\n"
        "s1   ACGT\n"
        "s2   TGCA\n"
    )
    alignment = AlignIO.read(io.StringIO(text), "clustal")
    stats = _compute_stats(alignment)
    assert stats["conserved_columns_count"] == 0
    assert stats["strict_identity_pct"] == 0.0
    assert stats["mean_pairwise_identity_pct"] == 0.0


# ---- input validation ---------------------------------------------------


def test_input_requires_at_least_two_sequences() -> None:
    with pytest.raises(ValueError):  # pydantic's ValidationError subclasses ValueError
        AlignSequencesInput.model_validate(
            {
                "sequences": [{"id": "solo", "sequence": "ACGT"}],
                "sequence_type": "dna",
            }
        )


def test_input_rejects_id_with_whitespace() -> None:
    # EBI's FASTA parser rejects whitespace in IDs — catch it client-side.
    with pytest.raises(ValueError):
        AlignSequencesInput.model_validate(
            {
                "sequences": [
                    {"id": "bad id", "sequence": "AAAA"},
                    {"id": "ok", "sequence": "AAAA"},
                ],
                "sequence_type": "dna",
            }
        )


def test_build_multifasta_strips_whitespace_in_sequence() -> None:
    records = AlignSequencesInput.model_validate(
        {
            "sequences": [
                {"id": "a", "sequence": "AC GT"},  # spaces inside
                {"id": "b", "sequence": "ACGT\n\nACGT"},  # newlines inside
            ],
            "sequence_type": "dna",
        }
    ).sequences
    out = _build_multifasta(records)
    assert out == ">a\nACGT\n>b\nACGTACGT\n"


# ---- tool path ----------------------------------------------------------


@pytest.fixture
def fake_runner() -> MagicMock:
    runner = MagicMock()
    runner.base_url = "https://www.ebi.ac.uk/Tools/services/rest/clustalo"
    runner.run = AsyncMock()
    return runner


async def test_bio_align_sequences_happy_path(fake_runner: MagicMock) -> None:
    fake_runner.run.return_value = TINY_ALN.encode()
    out = await bio_align_sequences(
        sequences=[
            {"id": "a", "sequence": "MKWVTFISLLFLFSSAYSRG"},
            {"id": "b", "sequence": "MKWVTFLSLLFLFSSAYSRG"},
            {"id": "c", "sequence": "MKWITFISLLFLFSSAYSRG"},
        ],
        sequence_type="protein",
        output_format="clustal",
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["sequence_count"] == 3
    assert out["output_format"] == "clustal"
    assert out["alignment_statistics"]["alignment_length"] == 20
    assert out["alignment_statistics"]["conserved_columns_count"] == 18
    assert out["alignment_format"] == "clustal"
    assert "CLUSTAL" in out["alignment"]
    # Submission used the right Clustal params.
    assert fake_runner.run.call_args.kwargs["result_type"] == "aln-clustal_num"
    submitted = fake_runner.run.call_args.kwargs["params"]
    assert submitted["email"] == "t@example.org"
    assert submitted["stype"] == "protein"
    assert submitted["outfmt"] == "clustal_num"
    assert ">a\nMKWVTFISLLFLFSSAYSRG" in submitted["sequence"]


async def test_bio_align_sequences_truncates_oversized_alignment(
    fake_runner: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the soft cap so the tiny fixture trips the oversize path.
    monkeypatch.setattr(
        "bioinformatics_mcp.tools.align_sequences.ALIGNMENT_SOFT_CAP_BYTES", 10
    )
    fake_runner.run.return_value = TINY_ALN.encode()
    out = await bio_align_sequences(
        sequences=[
            {"id": "a", "sequence": "MKWVTFISLLFLFSSAYSRG"},
            {"id": "b", "sequence": "MKWVTFLSLLFLFSSAYSRG"},
        ],
        sequence_type="protein",
        runner=fake_runner,
        email="t@example.org",
    )
    # alignment body should NOT be inlined; the error key is present with URL.
    assert "alignment" not in out
    assert "alignment_error" in out
    assert "too large" in out["alignment_error"].lower()
    # Stats are still computed even when body is too large to inline.
    assert out["alignment_statistics"]["alignment_length"] == 20


async def test_bio_align_sequences_jobfailed_returns_actionable_error(
    fake_runner: MagicMock,
) -> None:
    from bioinformatics_mcp.utils.errors import JobFailed

    fake_runner.run.side_effect = JobFailed(
        service="clustalo", job_id="J-BAD", status="FAILED"
    )
    out = await bio_align_sequences(
        sequences=[
            {"id": "a", "sequence": "ACGT"},
            {"id": "b", "sequence": "ACGT"},
        ],
        sequence_type="dna",
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["error"] is True
    assert "FAILED" in out["message"]
    assert out["job_id"] == "J-BAD"
    assert any("sequence_type" in s for s in out["suggestions"])


async def test_bio_align_sequences_timeout_returns_actionable_error(
    fake_runner: MagicMock,
) -> None:
    from bioinformatics_mcp.utils.errors import JobTimeoutError

    fake_runner.run.side_effect = JobTimeoutError(
        service="clustalo",
        job_id="J-SLOW",
        timeout_s=300.0,
        status_url="https://www.ebi.ac.uk/Tools/services/rest/clustalo/status/J-SLOW",
        cancelled=True,
    )
    out = await bio_align_sequences(
        sequences=[
            {"id": "a", "sequence": "ACGT"},
            {"id": "b", "sequence": "ACGT"},
        ],
        sequence_type="dna",
        runner=fake_runner,
        email="t@example.org",
    )
    assert out["error"] is True
    assert out["job_id"] == "J-SLOW"
    assert out["cancelled"] is True


# ---- integration (gated) -----------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("EBI_EMAIL"),
    reason="integration test needs EBI_EMAIL",
)
async def test_integration_align_three_insulin_orthologues() -> None:
    """Real EBI Clustal run — 3 short insulin orthologue signal peptides."""
    from bioinformatics_mcp.clients.ebi import EBIJobRunner
    from bioinformatics_mcp.utils.rate_limit import RateLimitedClient

    email = os.environ["EBI_EMAIL"]
    rlc = RateLimitedClient(max_concurrent=3, min_interval_s=0.5, timeout=60.0)
    runner = EBIJobRunner("clustalo", rlc)
    try:
        out = await bio_align_sequences(
            sequences=[
                {"id": "human", "sequence": "MALWMRLLPLLALLALWGPDPAAA"},
                {"id": "mouse", "sequence": "MALWMRFLPLLALLVLWEPKPAQA"},
                {"id": "bovine", "sequence": "MALWTRLRPLLALLALWPPPPARA"},
            ],
            sequence_type="protein",
            output_format="clustal",
            runner=runner,
            email=email,
            timeout=180.0,
        )
    finally:
        await rlc.aclose()

    assert out.get("error") is not True, out
    assert out["sequence_count"] == 3
    # All three IDs present in the alignment body.
    assert "human" in out["alignment"]
    assert "mouse" in out["alignment"]
    assert "bovine" in out["alignment"]
    # Conserved count > 0 for real orthologues.
    assert out["alignment_statistics"]["conserved_columns_count"] > 0


