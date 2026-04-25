"""`bio_design_grna` — CRISPR gRNA design with off-target analysis (CRISPOR wrapper).

Phase 1, MVP — the heaviest tool in the spec, deferred to end of phase 1.
See spec §4.7.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Design CRISPR gRNA (CRISPOR)".

The single most important anti-hallucination tool in the server — real
off-target tables instead of fabricated ones.

Implementation lands in the GREEN commit of the design_grna arc.
This stub exists so the RED-phase test fails on schema, not on
ImportError.
"""

from __future__ import annotations

from typing import Any

from bioinformatics_mcp.clients.crispor import CrisporRunner


async def bio_design_grna(
    target_sequence: str,
    genome: str,
    pam: str = "NGG",
    max_guides: int = 10,
    max_off_target_mismatches: int = 4,
    *,
    runner: CrisporRunner,
) -> dict[str, Any]:
    """Stub — see module docstring."""
    return {}
