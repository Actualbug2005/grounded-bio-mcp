#!/usr/bin/env python3
"""Probe EBI Job Dispatcher /resulttypes/{jobId} for Clustal Omega and InterProScan.

Session 3 shipped with an unverified result-type identifier map
(`_OUTPUT_FORMAT_MAP` in `tools/align_sequences.py` and the hard-coded
"tsv"/"json" identifiers in `tools/scan_domains.py`). This probe submits
small jobs to both services, waits for completion, then lists every
result-type identifier EBI advertises for each job — so the map can be
verified rather than guessed.

Run with ``EBI_EMAIL`` set::

    EBI_EMAIL=you@example.org .venv/bin/python scripts/probe_ebi_resulttypes.py

The script prints the full identifier list for each service and exits 0
on success. It does NOT modify code; any drift must be applied by hand.
"""

from __future__ import annotations

import asyncio
import os
import sys

from grounded_bio_mcp.clients.base import RATE_LIMITS
from grounded_bio_mcp.clients.ebi import EBIJobRunner
from grounded_bio_mcp.utils.rate_limit import RateLimitedClient

# Three tiny insulin orthologue snippets — Clustal aligns them in ~10 s.
CLUSTAL_PARAMS = {
    "stype": "protein",
    "outfmt": "clustal_num",
    "sequence": (
        ">human\nMALWMRLLPLLALLALWGPDPAAA\n"
        ">mouse\nMALWMRFLPLLALLVLWEPKPAQA\n"
        ">bovine\nMALWTRLRPLLALLALWPPPPARA\n"
    ),
}

# Insulin preproprotein — InterProScan against Pfam completes in ~1-2 min.
IPRSCAN_PARAMS = {
    "stype": "p",
    "appl": "PfamA",
    "sequence": (
        "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKTRREAEDLQ"
        "VGQVELGGGPGAGSLQPLALEGSLQKRGIVEQCCTSICSLYQLENYCN"
    ),
}


async def _probe(service: str, params: dict[str, str], email: str) -> None:
    rl = RATE_LIMITS["ebi"]
    client = RateLimitedClient(
        max_concurrent=rl.max_concurrent,
        min_interval_s=rl.min_interval_s,
        timeout=60.0,
        headers={"User-Agent": "grounded-bio-mcp/0.2 (+probe)"},
    )
    try:
        runner = EBIJobRunner(service, client)
        submission = dict(params, email=email)
        print(f"\n=== {service} ===")
        job_id = await runner.submit(submission)
        print(f"job_id = {job_id}")
        # Reuse the runner's built-in wait_until_done shape by polling manually.
        for _ in range(720):  # up to ~12 min for InterProScan
            status = await runner.get_status(job_id)
            if status in {"FINISHED", "DONE"}:
                break
            if status in {"FAILURE", "FAILED", "ERROR", "NOT_FOUND"}:
                raise RuntimeError(f"{service} job terminated with {status}")
            await asyncio.sleep(2.0)
        else:
            raise RuntimeError(f"{service} job {job_id} did not complete in time")
        print(f"status   = {status}")
        types = await runner.list_result_types(job_id)
        print(f"result_types ({len(types)}):")
        for t in types:
            ident = t.get("identifier")
            label = t.get("label") or t.get("description") or ""
            mtype = t.get("mediaType") or ""
            print(f"  - {ident!r:30} {mtype:25} {label}")
    finally:
        await client.aclose()


async def _main() -> int:
    email = os.environ.get("EBI_EMAIL")
    if not email:
        print("EBI_EMAIL not set; cannot probe live EBI.", file=sys.stderr)
        return 2
    await _probe("clustalo", CLUSTAL_PARAMS, email)
    await _probe("iprscan5", IPRSCAN_PARAMS, email)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
