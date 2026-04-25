"""`bio_fold_sequence` — RNA / DNA secondary structure prediction (ViennaRNA).

Phase 1, MVP. See spec §4.8.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=False,
idempotentHint=True, title="Fold RNA/DNA Sequence".

openWorldHint=False because the computation is purely local (ViennaRNA
bindings) — no upstream API touched.

# Implementation notes

We use the ViennaRNA Python bindings (``import RNA``) restricted to a
narrow, non-GLPK API surface — ``RNA.md``, ``RNA.fold_compound``,
``fc.mfe``, ``fc.pf``, ``fc.bpp``, and the ``params_load_*`` family. This
keeps the licence boundary clean (full attribution lands in NOTICE in
Session 8.5) without the friction of a subprocess wrapper around the
``RNAfold`` CLI, which would require a system-level ViennaRNA install
that isn't available via Homebrew on the dev machine.

ViennaRNA's energy parameters live in C-library global state.
``params_load_DNA_Mathews2004`` and ``params_load_RNA_Turner2004`` mutate
that global; two concurrent fold calls of different ``sequence_type``
would race. Serialise every fold behind a single asyncio lock — folds
of short sequences run in well under 100 ms, so contention is not a
practical concern. The actual compute runs on a worker thread via
``asyncio.to_thread`` so it does not block the event loop, and the
lock guards the worker dispatch so only one thread ever sees the
parameter table mid-flight. RNA defaults are restored on the way out
of every call (including failure paths) so DNA mode cannot leak across
invocations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import RNA
from pydantic import BaseModel, Field, ValidationError

from bioinformatics_mcp import __version__
from bioinformatics_mcp.utils.errors import error_response

_FOLD_LOCK = asyncio.Lock()
"""Serialises all fold calls so DNA/RNA param mutations never overlap.
Module-level asyncio locks are loop-agnostic in Python 3.10+; this binds
to the first event loop that awaits it."""


class FoldSequenceInput(BaseModel):
    """Spec §4.8 input schema. Alphabet check is done at the tool layer
    rather than via Pydantic regex because the valid alphabet depends on
    the sibling ``sequence_type`` field — Pydantic field validators can do
    that, but a tool-layer check produces a more actionable error message
    that names both the offending character and the expected alphabet."""

    sequence: str = Field(
        ...,
        min_length=10,
        max_length=5000,
    )
    sequence_type: Literal["rna", "dna"]
    temperature: float = Field(default=37.0, ge=0.0, le=100.0)


def _fold_sync(
    sequence: str, sequence_type: str, temperature: float
) -> dict[str, Any]:
    """Run a single ViennaRNA fold synchronously. Caller must hold ``_FOLD_LOCK``.

    Returns a dict with ``structure`` (dot-bracket), ``mfe`` (kcal/mol),
    ``per_position`` (per-base pairing probability list, 0-indexed), and
    ``mean_pair_prob`` (mean of per_position).
    """
    try:
        if sequence_type == "dna":
            RNA.params_load_DNA_Mathews2004()
        else:
            RNA.params_load_RNA_Turner2004()

        md = RNA.md()
        md.temperature = temperature
        fc = RNA.fold_compound(sequence, md)
        structure, mfe = fc.mfe()

        # Partition function gives the equilibrium ensemble; bpp[i][j] is
        # the probability that 1-indexed bases i and j pair (j > i). To
        # collapse this into per-position pairing probability we sum each
        # row's upper triangle into both endpoints — the probability that
        # position k is paired with anything is the sum over all partners.
        fc.pf()
        bpp = fc.bpp()
        n = len(sequence)
        per_position = [0.0] * n
        for i in range(1, n + 1):
            for j in range(i + 1, n + 1):
                p = bpp[i][j]
                per_position[i - 1] += p
                per_position[j - 1] += p
        mean_pair_prob = sum(per_position) / n if n else 0.0
    finally:
        # Always restore RNA defaults — a crash mid-fold in DNA mode would
        # otherwise leak the DNA parameter table into the next RNA call and
        # silently produce wrong MFEs.
        RNA.params_load_RNA_Turner2004()

    return {
        "structure": structure,
        "mfe": mfe,
        "per_position": per_position,
        "mean_pair_prob": mean_pair_prob,
    }


async def bio_fold_sequence(
    sequence: str,
    sequence_type: str,
    temperature: float = 37.0,
) -> dict[str, Any]:
    """RNA / DNA secondary structure prediction via ViennaRNA Python bindings.

    Returns the spec §4.8 output: ``structure`` (MFE dot-bracket),
    ``mfe_kcal_per_mol`` (the Gibbs free-energy of the MFE structure under
    the temperature-adjusted Turner 2004 / Mathews 2004 parameter set),
    plus ``base_pair_probabilities`` (mean pair probability and per-position
    pair probability list from the equilibrium partition function).

    Determinism: ViennaRNA's MFE algorithm is fully deterministic for fixed
    ``sequence``, ``sequence_type``, and ``temperature``. ``provenance`` is
    populated with the ViennaRNA version so the result can be reproduced
    exactly. ``confidence.level`` drops to ``medium`` for sequences over
    1000 nt where MFE alone increasingly underdetermines the true ensemble
    structure.
    """
    try:
        params = FoldSequenceInput.model_validate(
            {
                "sequence": sequence,
                "sequence_type": sequence_type,
                "temperature": temperature,
            }
        )
    except ValidationError as exc:
        return error_response(
            f"Invalid input to bio_fold_sequence: {exc.errors()[0]['msg']}",
            suggestions=[
                "sequence: 10-5000 nt; alphabet ACGU for RNA or ACGT for DNA.",
                "sequence_type: 'rna' or 'dna'.",
                "temperature: 0-100 °C; default 37.",
            ],
        )

    seq_upper = params.sequence.upper()
    expected_alphabet = "ACGT" if params.sequence_type == "dna" else "ACGU"
    bad = sorted({ch for ch in seq_upper if ch not in expected_alphabet})
    if bad:
        return error_response(
            f"sequence contains character(s) {bad} outside the "
            f"{params.sequence_type.upper()} alphabet ({expected_alphabet}).",
            suggestions=[
                "Set sequence_type='rna' for ACGU sequences; "
                "sequence_type='dna' for ACGT.",
                "Convert U↔T as needed before calling.",
            ],
        )

    async with _FOLD_LOCK:
        raw = await asyncio.to_thread(
            _fold_sync, seq_upper, params.sequence_type, params.temperature
        )

    parameter_set = (
        "Mathews 2004 (DNA)" if params.sequence_type == "dna" else "Turner 2004 (RNA)"
    )
    return {
        "sequence": seq_upper,
        "sequence_type": params.sequence_type,
        "temperature_celsius": params.temperature,
        "length": len(seq_upper),
        "structure": raw["structure"],
        "mfe_kcal_per_mol": round(raw["mfe"], 2),
        "base_pair_probabilities": {
            "mean_pair_probability": round(raw["mean_pair_prob"], 4),
            "per_position": [round(p, 4) for p in raw["per_position"]],
        },
        "provenance": {
            "source": "ViennaRNA",
            "tool_version": __version__,
            "viennarna_version": getattr(RNA, "__version__", "unknown"),
            "parameter_set": parameter_set,
        },
        "confidence": {
            "level": "high" if len(seq_upper) <= 1000 else "medium",
            "basis": (
                f"MFE structure from ViennaRNA {parameter_set}; "
                "deterministic for fixed inputs"
            ),
            "interpretation": (
                "Single-structure MFE; ensemble diversity beyond the partition "
                "function summary is not reported. Suboptimal structures within "
                "~1 kcal/mol of MFE are common and not exposed by this tool. "
                "For sequences over 1000 nt MFE quality degrades; consider "
                "sliding-window approaches for whole transcripts."
            ),
        },
    }
