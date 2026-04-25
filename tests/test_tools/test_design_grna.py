"""Unit tests for ``bio_design_grna`` (spec §4.7).

CRISPOR is invoked via subprocess at runtime; tests stub the ``CrisporRunner``
to return hand-crafted TSV fixtures so the wrapper's parse + transform logic
is fully exercised without touching the bundled CRISPOR install. The
``CRISPOR_LIVE=1`` integration test (see test_design_grna_live.py once it
lands) exercises the live subprocess path against the bundled sacCer3
genome — but only on machines where Rosetta or native arm64 binaries make
the bundled x86_64 binaries runnable. On Apple Silicon dev machines
without Rosetta the live path is skipped; the LXC in Session 8b is the
canonical live-exec environment.

Fixtures under ``tests/fixtures/crispor_*.tsv`` are hand-crafted
derivatives of CRISPOR's output format (see ``docs/crispor_output_format.md``)
rather than copies of CRISPOR's bundled sample TSVs — the licence
boundary stays clean (CRISPOR is academic-free / commercial-paid;
this project is Apache-2.0 from Session 8.5 onward).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bioinformatics_mcp.clients.crispor import CrisporRunner
from bioinformatics_mcp.tools.design_grna import bio_design_grna

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FakeRunner(CrisporRunner):
    """Test double that returns pre-baked TSVs without running a subprocess.

    Inherits from CrisporRunner so the type contract matches the production
    runner exactly; only ``run`` is overridden. Test code passes the same
    argument shape it would to a real runner — ``genome``, ``target_sequence``,
    ``pam``, ``max_off_target_mismatches`` — and gets back the constructor's
    canned ``(guides_tsv, offtargets_tsv)`` regardless. Last-call args are
    captured on ``self.last_call`` so individual tests can assert command
    construction.
    """

    def __init__(self, guides_tsv: str, offtargets_tsv: str) -> None:
        # Path values are placeholders — the FakeRunner never invokes a
        # subprocess so they are never read. Inheriting from CrisporRunner
        # without calling super().__init__ would skip type contract; we
        # call it with dummy paths so dataclass-like attribute access keeps
        # working if production code grows readers.
        super().__init__(
            crispor_python=Path("/dev/null"),
            crispor_path=Path("/dev/null"),
            genomes_dir=Path("/dev/null"),
            timeout_s=300.0,
        )
        self._guides_tsv = guides_tsv
        self._offtargets_tsv = offtargets_tsv
        self.last_call: dict[str, object] | None = None

    async def run(
        self,
        genome: str,
        target_sequence: str,
        pam: str = "NGG",
        max_off_target_mismatches: int = 4,
    ) -> tuple[str, str]:
        self.last_call = {
            "genome": genome,
            "target_sequence": target_sequence,
            "pam": pam,
            "max_off_target_mismatches": max_off_target_mismatches,
        }
        return self._guides_tsv, self._offtargets_tsv


def _happy_runner() -> FakeRunner:
    """Three-guide sacCer3-style result with mixed locusDesc + full scoring."""
    return FakeRunner(
        guides_tsv=(FIXTURES / "crispor_guides_happy.tsv").read_text(),
        offtargets_tsv=(FIXTURES / "crispor_offtargets_happy.tsv").read_text(),
    )


# ---- spec-shape happy path ----------------------------------------------


async def test_bio_design_grna_returns_spec_fields_for_sacCer3_target() -> None:
    """End-to-end through the tool boundary: a sacCer3 target with three
    guide hits must return a dict carrying every spec §4.7 output field —
    ``guides``, ``candidate_guides_count``, ``returned_guides_count``,
    ``provenance``, ``confidence``, ``genome``, ``pam`` — with the top
    guide ranked first by MIT specificity score and PAM split correctly
    from the 23 nt targetSeq.
    """
    out = await bio_design_grna(
        target_sequence="A" * 500,
        genome="sacCer3",
        pam="NGG",
        max_guides=10,
        runner=_happy_runner(),
    )
    required_keys = {
        "guides",
        "candidate_guides_count",
        "returned_guides_count",
        "provenance",
        "confidence",
        "genome",
        "pam",
    }
    assert required_keys.issubset(out.keys()), (
        f"Missing keys: {sorted(required_keys - out.keys())}"
    )
    assert out["genome"] == "sacCer3"
    assert out["pam"] == "NGG"
    assert isinstance(out["guides"], list)
    assert len(out["guides"]) == 3, f"expected 3 guides, got {len(out['guides'])}"

    top = out["guides"][0]
    assert top["sequence"] == "GCAGGCATGTACGTACGTAC", (
        f"top guide spacer should be 20 nt with PAM stripped; got {top.get('sequence')!r}"
    )
    assert top["pam"] == "AGG"
    assert top["specificity_score"] == 95
    assert top["strand"] == "+"
    assert isinstance(top.get("efficiency_scores"), dict)
    assert top["efficiency_scores"].get("doench16") == 68
    assert isinstance(top.get("off_targets"), list)
    assert top.get("off_target_summary", {}).get("0_mm", -1) >= 0


# ---- placeholder for broader coverage (lands in next commit) ------------


@pytest.mark.skip(reason="placeholder — broader edge-case tests land in the next commit")
async def test_placeholder_for_edge_cases() -> None:
    pass
