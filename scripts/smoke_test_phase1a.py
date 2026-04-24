#!/usr/bin/env python3
"""Phase-1a/1b end-to-end smoke test.

Calls every phase-1a/1b tool (``bio_fetch_sequence``, ``bio_fetch_uniprot``,
``bio_fetch_pdb``, ``bio_fetch_alphafold``, ``bio_align_sequences``,
``bio_scan_domains``) through the in-process FastMCP client — i.e. the
same handshake path Claude would use — against real upstream APIs, with
the spec §10.2 test accessions. Prints a pass/fail summary and exits
non-zero on any failure.

The two EBI async tools are gated on EBI_EMAIL: without it, they
exercise the graceful "EBI_EMAIL required" error path rather than
skipping entirely, so the smoke test still validates wiring.

Run locally::

    .venv/bin/python scripts/smoke_test_phase1a.py

The in-process ``fastmcp.Client`` is the programmatic equivalent of MCP
Inspector — it exercises tool registration, schema validation, argument
marshalling, and return-value serialisation in one pass without needing
a Node tool-chain for Inspector.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any

from fastmcp import Client

from bioinformatics_mcp.server import mcp


def _first_text_payload(result: Any) -> Any:
    """Return the tool's payload as a Python object.

    FastMCP wraps plain-scalar return values (e.g. a JSON string returned
    by tools that go through ``format_response``) as
    ``structured_content = {"result": <value>}``; when the tool returns
    a dict directly, the dict itself is the structured content. We
    normalise both shapes here so callers see a homogeneous dict/object.
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        # FastMCP's {"result": <scalar>} wrapper.
        if set(structured.keys()) == {"result"}:
            inner = structured["result"]
            if isinstance(inner, str):
                try:
                    return json.loads(inner)
                except json.JSONDecodeError:
                    return inner
            return inner
        return structured
    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


async def _check(
    client: Client,
    tool: str,
    args: dict[str, Any],
    assertion: Any,
) -> tuple[bool, str]:
    try:
        result = await client.call_tool(tool, args)
    except Exception as exc:  # noqa: BLE001 — aggregating for summary
        return False, f"raised {type(exc).__name__}: {exc}"
    payload = _first_text_payload(result)
    try:
        detail = assertion(payload)
    except AssertionError as exc:
        return False, f"assertion failed: {exc}"
    return True, detail


def _build_cases() -> list[tuple[str, dict[str, Any], Any]]:
    import os as _os

    has_email = bool(_os.environ.get("EBI_EMAIL"))

    # Shared input for the async EBI tools.
    insulin_orthologues = [
        {"id": "human", "sequence": "MALWMRLLPLLALLALWGPDPAAA"},
        {"id": "mouse", "sequence": "MALWMRFLPLLALLVLWEPKPAQA"},
        {"id": "bovine", "sequence": "MALWTRLRPLLALLALWPPPPARA"},
    ]
    insulin_protein = (
        "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKTRREAEDLQ"
        "VGQVELGGGPGAGSLQPLALEGSLQKRGIVEQCCTSICSLYQLENYCN"
    )

    if has_email:
        async_align_assertion = lambda p: (  # noqa: E731
            _assert(
                p.get("error") is not True,
                f"align errored: {p.get('message')}",
            ),
            _assert(p["sequence_count"] == 3, f"count={p.get('sequence_count')}"),
            _assert(
                p["alignment_statistics"]["conserved_columns_count"] > 0,
                "no conserved columns",
            ),
            f"conserved={p['alignment_statistics']['conserved_columns_count']} "
            f"strict={p['alignment_statistics']['strict_identity_pct']}%",
        )[-1]
        async_scan_assertion = lambda p: (  # noqa: E731
            _assert(
                p.get("error") is not True,
                f"scan errored: {p.get('message')}",
            ),
            _assert(p["match_count"] > 0, "no Pfam/SMART hits on insulin"),
            f"matches={p['match_count']} dbs={p['databases_scanned']}",
        )[-1]
    else:
        # Without EBI_EMAIL, assert the graceful error rather than a pass.
        async_align_assertion = lambda p: (  # noqa: E731
            _assert(p.get("error") is True, "expected error for missing EBI_EMAIL"),
            _assert(
                "EBI_EMAIL" in p["message"],
                f"expected EBI_EMAIL message, got: {p.get('message')}",
            ),
            "graceful EBI_EMAIL-missing error (set EBI_EMAIL for live align)",
        )[-1]
        async_scan_assertion = lambda p: (  # noqa: E731
            _assert(p.get("error") is True, "expected error for missing EBI_EMAIL"),
            _assert(
                "EBI_EMAIL" in p["message"],
                f"expected EBI_EMAIL message, got: {p.get('message')}",
            ),
            "graceful EBI_EMAIL-missing error (set EBI_EMAIL for live scan)",
        )[-1]

    return [
        (
            "bio_align_sequences",
            {
                "sequences": insulin_orthologues,
                "sequence_type": "protein",
                "output_format": "clustal",
            },
            async_align_assertion,
        ),
        (
            "bio_scan_domains",
            {
                "sequence": insulin_protein,
                "applications": ["Pfam", "SMART", "CDD"],
            },
            async_scan_assertion,
        ),
    ]


async def run() -> int:
    # Tool-name → (args, assertion) maps. Each assertion returns a short
    # human-readable summary on success, raises AssertionError on failure.
    cases = [
        (
            "bio_fetch_sequence",
            {"accession": "NM_001301717", "database": "nucleotide", "rettype": "fasta"},
            lambda p: (
                _assert(
                    p["accession"].startswith("NM_001301717"), f"accession={p['accession']}"
                ),
                _assert(p["length"] > 500, f"length={p['length']}"),
                f"accession={p['accession']} length={p['length']}",
            )[-1],
        ),
        (
            "bio_fetch_uniprot",
            {"accession": "P01308"},
            lambda p: (
                _assert(p["accession"] == "P01308", f"accession={p['accession']}"),
                _assert(p["length"] == 110, f"length={p['length']}"),
                _assert(
                    "AlphaFoldDB" in p["cross_references"],
                    "expected AlphaFoldDB xref",
                ),
                f"length={p['length']} xrefs={list(p['cross_references'].keys())}",
            )[-1],
        ),
        (
            "bio_fetch_pdb",
            {"pdb_id": "1CRN"},
            lambda p: (
                _assert(p["pdb_id"] == "1CRN", f"pdb_id={p['pdb_id']}"),
                _assert(
                    p["experimental_method"] == "X-RAY DIFFRACTION",
                    f"method={p['experimental_method']}",
                ),
                _assert(p["resolution"] is not None, "resolution missing"),
                f"resolution={p['resolution']}Å method={p['experimental_method']}",
            )[-1],
        ),
        (
            "bio_fetch_alphafold",
            {"uniprot_accession": "P01308"},
            lambda p: (
                _assert(p["uniprot_accession"] == "P01308", "accession mismatch"),
                _assert(
                    p["plddt_summary"]["residue_count"] == 110,
                    f"count={p['plddt_summary']['residue_count']}",
                ),
                _assert(
                    p["plddt_summary"]["mean_plddt"] is not None, "mean missing"
                ),
                f"mean_pLDDT={p['plddt_summary']['mean_plddt']:.1f} "
                f"residues={p['plddt_summary']['residue_count']}",
            )[-1],
        ),
    ]

    cases.extend(_build_cases())

    async with Client(mcp) as client:
        results: list[tuple[str, bool, str]] = []
        tools = await client.list_tools()
        names = {t.name for t in tools}
        expected = {
            "bio_fetch_sequence",
            "bio_fetch_uniprot",
            "bio_fetch_pdb",
            "bio_fetch_alphafold",
            "bio_align_sequences",
            "bio_scan_domains",
        }
        missing = expected - names
        if missing:
            print(f"✗ server is missing tools: {missing}")
            return 1

        for tool, args, assertion in cases:
            ok, detail = await _check(client, tool, args, assertion)
            results.append((tool, ok, detail))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nPhase-1a/1b smoke test  —  {passed}/{total} passed\n")
    for name, ok, detail in results:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name:24}  {detail}")
    return 0 if passed == total else 1


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
