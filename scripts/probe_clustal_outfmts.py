#!/usr/bin/env python3
"""Submit Clustal Omega with each supported outfmt and list returned result-types.

Supplements scripts/probe_ebi_resulttypes.py (which probes one outfmt).
This probe checks whether the result-type identifier set varies per
outfmt — critical because tools/align_sequences.py::_OUTPUT_FORMAT_MAP
encodes a (outfmt, result_type) pairing per user-facing format choice.
"""

from __future__ import annotations

import asyncio
import os
import sys

from grounded_bio_mcp.clients.base import RATE_LIMITS
from grounded_bio_mcp.clients.ebi import EBIJobRunner
from grounded_bio_mcp.utils.rate_limit import RateLimitedClient

SEQUENCE = (
    ">human\nMALWMRLLPLLALLALWGPDPAAA\n"
    ">mouse\nMALWMRFLPLLALLVLWEPKPAQA\n"
    ">bovine\nMALWTRLRPLLALLALWPPPPARA\n"
)


async def _probe_outfmt(outfmt: str, email: str) -> list[str | None]:
    rl = RATE_LIMITS["ebi"]
    client = RateLimitedClient(
        max_concurrent=rl.max_concurrent,
        min_interval_s=rl.min_interval_s,
        timeout=60.0,
        headers={"User-Agent": "grounded-bio-mcp/0.2 (+probe)"},
    )
    try:
        runner = EBIJobRunner("clustalo", client)
        params = {
            "stype": "protein",
            "outfmt": outfmt,
            "sequence": SEQUENCE,
            "email": email,
        }
        print(f"\n--- outfmt={outfmt!r} ---")
        job_id = await runner.submit(params)
        print(f"job_id = {job_id}")
        for _ in range(120):
            status = await runner.get_status(job_id)
            if status in {"FINISHED", "DONE"}:
                break
            if status in {"FAILURE", "FAILED", "ERROR", "NOT_FOUND"}:
                raise RuntimeError(f"job {job_id} terminated with {status}")
            await asyncio.sleep(2.0)
        else:
            raise RuntimeError(f"job {job_id} did not complete in time")
        types = await runner.list_result_types(job_id)
        idents = [t.get("identifier") for t in types]
        for t in types:
            ident = t.get("identifier")
            label = t.get("label") or t.get("description") or ""
            mtype = t.get("mediaType") or ""
            print(f"  {ident!r:30} {mtype:25} {label}")
        return idents
    finally:
        await client.aclose()


async def _main() -> int:
    email = os.environ.get("EBI_EMAIL")
    if not email:
        print("EBI_EMAIL not set", file=sys.stderr)
        return 2
    seen: dict[str, list[str | None]] = {}
    for outfmt in ("clustal_num", "fa", "msf"):
        seen[outfmt] = await _probe_outfmt(outfmt, email)
    print("\n=== summary ===")
    for outfmt, idents in seen.items():
        aln_ids = sorted([i for i in idents if i and i.startswith("aln-") or i in {"fa", "msf"}])
        print(f"outfmt={outfmt!r:12} alignment-ish identifiers: {aln_ids}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
