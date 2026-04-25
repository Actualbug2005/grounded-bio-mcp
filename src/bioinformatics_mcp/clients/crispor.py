"""`CrisporRunner` — subprocess wrapper around CRISPOR's command-line entrypoint.

Not a stateless API client like the other entries under ``clients/`` — CRISPOR
is a local subprocess invocation, not an HTTP service. The runner sits here
because the project's separation of concerns puts external-tool wrapping
in ``clients/`` (one file per upstream service / tool) and per-tool business
logic in ``tools/``. ``bio_design_grna`` consumes the runner's TSV output;
the runner doesn't know about the spec output shape.

Implementation lands in the GREEN commit of the design_grna arc.
This stub exists so the RED-phase test fails on schema, not on
ImportError.
"""

from __future__ import annotations

from pathlib import Path


class CrisporRunner:
    """Stub — see module docstring."""

    def __init__(
        self,
        crispor_python: Path,
        crispor_path: Path,
        genomes_dir: Path,
        timeout_s: float = 300.0,
    ) -> None:
        self.crispor_python = crispor_python
        self.crispor_path = crispor_path
        self.genomes_dir = genomes_dir
        self.timeout_s = timeout_s

    async def run(
        self,
        genome: str,
        target_sequence: str,
        pam: str = "NGG",
        max_off_target_mismatches: int = 4,
    ) -> tuple[str, str]:
        """Stub: returns ``(guides_tsv, offtargets_tsv)`` once implemented."""
        raise NotImplementedError(
            "CrisporRunner.run is a stub — implementation lands in the next commit."
        )
