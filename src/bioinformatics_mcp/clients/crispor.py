"""`CrisporRunner` — subprocess wrapper around CRISPOR's command-line entrypoint.

Not a stateless API client like the other entries under ``clients/`` — CRISPOR
is a local subprocess invocation, not an HTTP service. The runner sits here
because the project's separation of concerns puts external-tool wrapping in
``clients/`` (one file per upstream service / tool) and per-tool business
logic in ``tools/``. ``bio_design_grna`` consumes the runner's TSV output;
the runner doesn't know about the spec output shape.

Three classes of failure surface as typed exceptions, all subclasses of
``CrisporError`` (which itself subclasses ``BioMCPError``):

  - ``GenomeIndexNotFound`` — the genome subdirectory under
    ``genomes_dir`` is missing or incomplete. The runner walks
    ``_REQUIRED_GENOME_FILES`` up front and reports every missing path
    in one shot rather than letting CRISPOR's subprocess die with a
    cryptic stderr.
  - ``CrisporTimeout`` — the subprocess exceeded the configured wall
    clock budget. The runner kills the child process before raising.
  - ``CrisporRunFailed`` — non-zero exit code; stderr is captured and
    truncated for the error message (full text on ``.stderr``).

The bundled sacCer3 genome (under ``genomes.sample/sacCer3/`` in the
upstream CRISPOR repo) ships with all required layout files; copy or
symlink it into the configured ``genomes_dir`` before invoking the
runner against ``genome="sacCer3"``.

Subprocess invocation uses ``asyncio.create_subprocess_exec`` with an
argv list — no shell interpolation, no command-injection surface. User
inputs (genome name, target sequence, PAM motif) flow through
positional argv slots that CRISPOR consumes verbatim; sequence content
goes into a temp FASTA file rather than being passed on the command
line, so even a sequence containing shell metacharacters cannot escape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from bioinformatics_mcp.utils.errors import BioMCPError


class CrisporError(BioMCPError):
    """Base class for CRISPOR subprocess failures."""


class GenomeIndexNotFound(CrisporError):
    """Configured genome directory is missing or incomplete."""

    def __init__(
        self, genome: str, expected_path: Path, missing: list[str]
    ) -> None:
        super().__init__(
            f"CRISPOR genome '{genome}' index not found under "
            f"{expected_path}; missing files: {missing}"
        )
        self.genome = genome
        self.expected_path = expected_path
        self.missing = missing


class CrisporTimeout(CrisporError):
    """CRISPOR subprocess exceeded the configured wall-clock timeout."""

    def __init__(self, genome: str, timeout_s: float) -> None:
        super().__init__(
            f"CRISPOR run for genome '{genome}' did not complete in "
            f"{timeout_s:.0f}s"
        )
        self.genome = genome
        self.timeout_s = timeout_s


class CrisporRunFailed(CrisporError):
    """CRISPOR subprocess exited with a non-zero return code."""

    def __init__(self, genome: str, returncode: int, stderr: str) -> None:
        super().__init__(
            f"CRISPOR run for genome '{genome}' exited with code "
            f"{returncode}: {stderr[:500]}"
        )
        self.genome = genome
        self.returncode = returncode
        self.stderr = stderr


_REQUIRED_GENOME_FILES: tuple[str, ...] = (
    "{genome}.2bit",
    "{genome}.fa.amb",
    "{genome}.fa.ann",
    "{genome}.fa.bwt",
    "{genome}.fa.pac",
    "{genome}.fa.sa",
    "{genome}.sizes",
    "{genome}.segments.bed",
    "genomeInfo.tab",
)
"""Files CRISPOR needs to run an off-target search. Walking this list up
front means a missing index reports as a single clean error rather than
a confusing subprocess failure deep inside crispor.py."""


class CrisporRunner:
    """Subprocess wrapper around CRISPOR's ``crispor.py``.

    Stateless — every ``run`` call materialises its own temp directory
    for input + output files, invokes the subprocess, reads the two TSVs
    back, and lets the temp dir be cleaned up. One instance per server
    process is fine; concurrent calls are independent because each gets
    its own temp dir.
    """

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

    def _check_genome_layout(self, genome: str) -> None:
        genome_dir = self.genomes_dir / genome
        if not genome_dir.is_dir():
            raise GenomeIndexNotFound(
                genome=genome,
                expected_path=genome_dir,
                missing=["(directory does not exist)"],
            )
        missing: list[str] = []
        for template in _REQUIRED_GENOME_FILES:
            filename = template.format(genome=genome)
            if not (genome_dir / filename).exists():
                missing.append(filename)
        if missing:
            raise GenomeIndexNotFound(genome, genome_dir, missing)

    async def run(
        self,
        genome: str,
        target_sequence: str,
        pam: str = "NGG",
        max_off_target_mismatches: int = 4,
    ) -> tuple[str, str]:
        """Run CRISPOR end-to-end and return ``(guides_tsv, offtargets_tsv)``.

        Validates the genome layout up front, materialises a temp dir
        with the input FASTA and output paths, invokes ``crispor.py``,
        and reads both TSVs back. Caller gets raw text content; parsing
        happens in ``bio_design_grna``.
        """
        self._check_genome_layout(genome)

        crispor_py = self.crispor_path / "crispor.py"
        with TemporaryDirectory(prefix="crispor_") as tmpdir:
            tmp_path = Path(tmpdir)
            input_fa = tmp_path / "input.fa"
            guides_out = tmp_path / "guides.tsv"
            offtargets_out = tmp_path / "offtargets.tsv"

            # Single-record FASTA. Header text is internal to the run;
            # a stable id keeps output reproducible.
            input_fa.write_text(f">target\n{target_sequence}\n")

            argv = [
                str(self.crispor_python),
                str(crispor_py),
                "-p",
                pam,
                "--mm",
                str(max_off_target_mismatches),
                "--genomeDir",
                str(self.genomes_dir),
                "-o",
                str(offtargets_out),
                genome,
                str(input_fa),
                str(guides_out),
            ]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.crispor_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_s
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise CrisporTimeout(genome, self.timeout_s) from exc

            if proc.returncode != 0:
                raise CrisporRunFailed(
                    genome=genome,
                    returncode=proc.returncode or -1,
                    stderr=(stderr or b"").decode("utf-8", errors="replace"),
                )

            guides_tsv = (
                guides_out.read_text() if guides_out.exists() else ""
            )
            offtargets_tsv = (
                offtargets_out.read_text() if offtargets_out.exists() else ""
            )

        return guides_tsv, offtargets_tsv
