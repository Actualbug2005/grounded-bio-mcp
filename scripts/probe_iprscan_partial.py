#!/usr/bin/env python3
"""Probe whether EBI InterProScan exposes partial results while the job is RUNNING.

session-3-ebi-async.md §6.2 defines three tiers:

* tier-1: does /status/{jobId} or /resulttypes/{jobId} expose per-database
  completion signals during RUNNING?
* tier-2: does /result/{jobId}/tsv return usable partial output during
  RUNNING? Four cases to distinguish:
    - 404 / 5xx (clean "not ready")
    - empty 200 body
    - stale 200 (same bytes every poll — looks like streaming but isn't)
    - genuinely-growing 200 (content length increases over time)

This probe submits InterProScan against all six tool-default databases
(PfamA, SMART, PrositeProfiles, PrositePatterns, CDD, SuperFamily,
Gene3d) on the insulin preproprotein and polls /status, /resulttypes,
and /result/{jobId}/tsv every 5 s until FINISHED. It records a row per
poll and prints a summary table so the four-case distinction above can
be decided evidence-first.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

from bioinformatics_mcp.clients.base import RATE_LIMITS
from bioinformatics_mcp.clients.ebi import EBIJobRunner
from bioinformatics_mcp.utils.rate_limit import RateLimitedClient

INSULIN = (
    "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKTRREAEDLQ"
    "VGQVELGGGPGAGSLQPLALEGSLQKRGIVEQCCTSICSLYQLENYCN"
)

# All six spec-facing database names → EBI-canonical names, per
# tools/scan_domains.py::_canonical_appl.
APPLICATIONS = "PfamA,SMART,PrositeProfiles,PrositePatterns,CDD,SuperFamily,Gene3d"


@dataclass
class Poll:
    elapsed_s: float
    status: str
    tsv_http_status: int
    tsv_bytes: int
    tsv_sha256: str
    tsv_head: str
    resulttypes_count: int | None = None
    resulttypes_identifiers: list[str] = field(default_factory=list)


async def _fetch_tsv(
    raw: httpx.AsyncClient, base_url: str, job_id: str
) -> tuple[int, bytes]:
    """Direct GET /result/{jobId}/tsv — bypass EBIJobRunner to see raw status.

    EBIJobRunner.fetch_result raises on non-2xx; we want to observe 404s
    too.
    """
    r = await raw.get(f"{base_url}/result/{job_id}/tsv")
    return r.status_code, r.content


async def _main() -> int:
    email = os.environ.get("EBI_EMAIL")
    if not email:
        print("EBI_EMAIL not set", file=sys.stderr)
        return 2

    rl = RATE_LIMITS["ebi"]
    rlclient = RateLimitedClient(
        max_concurrent=rl.max_concurrent,
        min_interval_s=rl.min_interval_s,
        timeout=60.0,
        headers={"User-Agent": "bioinformatics-mcp/0.2 (+probe)"},
    )
    # Separate raw client for the speculative /result/{jobId}/tsv polls so
    # we can observe 404s without EBIJobRunner's "raise on error" layer.
    raw = httpx.AsyncClient(
        timeout=60.0,
        headers={"User-Agent": "bioinformatics-mcp/0.2 (+probe-raw)"},
    )

    try:
        runner = EBIJobRunner("iprscan5", rlclient)
        params = {
            "stype": "p",
            "appl": APPLICATIONS,
            "sequence": INSULIN,
            "email": email,
        }
        job_id = await runner.submit(params)
        print(f"job_id={job_id} appl={APPLICATIONS}")
        base_url = runner.base_url
        polls: list[Poll] = []
        start = time.monotonic()
        for _ in range(360):  # up to 30 min
            elapsed = time.monotonic() - start
            status = await runner.get_status(job_id)
            http_status, body = await _fetch_tsv(raw, base_url, job_id)
            sha = hashlib.sha256(body).hexdigest()[:12]
            head = body.decode("utf-8", errors="replace").splitlines()[:3]
            head_str = " || ".join(head)[:200]
            rt_count: int | None = None
            rt_idents: list[str] = []
            try:
                rts = await runner.list_result_types(job_id)
                rt_count = len(rts)
                rt_idents = [t.get("identifier", "?") for t in rts]
            except Exception as exc:  # noqa: BLE001
                head_str = head_str + f" | resulttypes-err: {exc}"
            polls.append(
                Poll(
                    elapsed_s=elapsed,
                    status=status,
                    tsv_http_status=http_status,
                    tsv_bytes=len(body),
                    tsv_sha256=sha,
                    tsv_head=head_str,
                    resulttypes_count=rt_count,
                    resulttypes_identifiers=rt_idents,
                )
            )
            print(
                f"t={elapsed:6.1f}s status={status:<10} "
                f"tsv=HTTP{http_status} {len(body):>7}B sha={sha} "
                f"rtypes={rt_count} "
                f"head={head_str[:80]!r}"
            )
            if status in {"FINISHED", "DONE"}:
                break
            if status in {"FAILURE", "FAILED", "ERROR", "NOT_FOUND"}:
                print(f"terminated: {status}")
                break
            await asyncio.sleep(5.0)
        else:
            print("probe hit its own 30-min poll ceiling")

        print("\n=== summary ===")
        print(f"polls={len(polls)}  final_status={polls[-1].status}")
        running = [p for p in polls if p.status == "RUNNING"]
        if running:
            print(
                f"RUNNING polls: {len(running)}, "
                f"first={running[0].elapsed_s:.1f}s, "
                f"last={running[-1].elapsed_s:.1f}s"
            )
            # Distinguish the four cases during RUNNING.
            running_http = {p.tsv_http_status for p in running}
            running_bytes = {p.tsv_bytes for p in running}
            running_sha = {p.tsv_sha256 for p in running}
            print(f"  HTTP statuses seen while RUNNING: {sorted(running_http)}")
            print(f"  tsv byte-lengths seen while RUNNING: {sorted(running_bytes)}")
            print(f"  distinct tsv sha256 prefixes while RUNNING: {len(running_sha)}")
            if running_http == {200} and len(running_sha) > 1:
                print("  → genuinely-growing stream (tier-2 SUCCESS)")
            elif running_http == {200} and len(running_sha) == 1 and 0 not in running_bytes:
                print("  → stale-buffered 200 (identical content every poll)")
            elif running_http == {200} and running_bytes == {0}:
                print("  → empty 200 during RUNNING")
            elif all(h >= 400 for h in running_http):
                print("  → clean not-ready (HTTP 4xx/5xx) — tier-2 NEGATIVE")
            else:
                print("  → mixed / indeterminate")

            # Tier-1: resulttypes during RUNNING.
            rt_counts_running = {p.resulttypes_count for p in running}
            print(f"  resulttypes counts during RUNNING: {sorted(c for c in rt_counts_running if c is not None)}")
            first_fin = next((p for p in polls if p.status in {"FINISHED", "DONE"}), None)
            if first_fin:
                print(
                    f"  final resulttypes count: {first_fin.resulttypes_count}, "
                    f"identifiers={first_fin.resulttypes_identifiers}"
                )
        else:
            print("no RUNNING polls captured — job completed too fast to probe")

        return 0
    finally:
        await rlclient.aclose()
        await raw.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
