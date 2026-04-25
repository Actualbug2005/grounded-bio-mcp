#!/usr/bin/env python3
"""Fetch missing codon usage tables from Kazusa and bundle them locally.

Three of the six organisms named in spec §4.19 are *not* shipped with
``python-codon-tables`` 0.1.18: ``p_pastoris``, ``cho``, ``sf9``. Rather
than calling Kazusa from inside the codon optimiser tool at runtime — which
would silently flip ``openWorldHint`` from ``False`` to ``True`` — this
script downloads the tables once at packaging time, persists them as CSV
files matching the library's own format, and writes a ``provenance.json``
sidecar so the tables remain auditable.

Run::

    .venv/bin/python scripts/fetch_codon_tables.py

The codon optimiser's loader (``tools/codon_optimise.py``) reads from
``src/bioinformatics_mcp/data/codon_tables/`` and falls through to
``python_codon_tables.get_codons_table`` for the three ``ecoli_k12`` /
``h_sapiens`` / ``s_cerevisiae`` aliases that the library *does* ship.

To refresh the bundled tables (e.g. if Kazusa updates its data), re-run
this script and commit the diff. The provenance file's SHA256 will change
when content changes and the timestamp gives an audit trail.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "src" / "bioinformatics_mcp" / "data" / "codon_tables"

KAZUSA_URL = "http://www.kazusa.or.jp/codon/cgi-bin/showcodon.cgi?aa=1&style=N&species={taxid}"

# Mirror python-codon-tables' parsing regex exactly so the bundled CSVs
# match the library's expected schema byte-for-byte.
CODON_REGEX = re.compile(r"([ATGCU]{3}) ([A-Z]|\*) (\d\.\d+)")

USER_AGENT = (
    "bioinformatics-mcp/0.2 (codon-table-fetch; "
    "+mailto:zgqr6xbt6f@privaterelay.appleid.com)"
)


# Spec organism alias → fetch plan. Pichia carries two candidate Kazusa
# taxids (renamed K. phaffii first, legacy P. pastoris fallback).
ORGANISMS: list[dict[str, Any]] = [
    {
        "spec_alias": "p_pastoris",
        "scientific_name": "Komagataella phaffii",
        "common_name": "Pichia pastoris (renamed; yeast expression host)",
        "ncbi_taxon_id": 460519,  # K. phaffii (parent of strain GS115 = 644223)
        "kazusa_candidates": [644223, 4922],  # GS115 first, then P. pastoris legacy
    },
    {
        "spec_alias": "cho",
        "scientific_name": "Cricetulus griseus",
        "common_name": "Chinese hamster (CHO cell host)",
        "ncbi_taxon_id": 10029,
        "kazusa_candidates": [10029],
    },
    {
        "spec_alias": "sf9",
        "scientific_name": "Spodoptera frugiperda",
        "common_name": "Fall armyworm (Sf9 cell host)",
        "ncbi_taxon_id": 7108,
        "kazusa_candidates": [7108],
    },
]


def _fetch_kazusa_html(taxid: int, *, timeout: float = 30.0) -> str:
    """GET the Kazusa codon usage page for ``taxid`` with courtesy UA.

    Uses ``httpx`` (project-standard HTTP client) rather than ``urllib`` —
    httpx restricts to http(s) schemes by default, sidestepping the
    ``file://`` SSRF surface that semgrep rightly flags on urllib.
    """
    url = KAZUSA_URL.format(taxid=taxid)
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.text.replace("\n", " ")


def _html_to_csv(html: str) -> str | None:
    """Parse Kazusa HTML into the library's CSV schema, or None if empty."""
    if "<title>not found</title>" in html.lower():
        return None
    rows = CODON_REGEX.findall(html)
    if not rows:
        return None
    sorted_rows = sorted(f"{aa},{codon},{usage}" for codon, aa, usage in rows)
    return "\n".join(["amino_acid,codon,relative_frequency", *sorted_rows]) + "\n"


def _fetch_one(plan: dict[str, Any]) -> dict[str, Any]:
    """Try each candidate taxid in order; return provenance for the winner."""
    last_error: str | None = None
    for taxid in plan["kazusa_candidates"]:
        url = KAZUSA_URL.format(taxid=taxid)
        print(f"  GET {url}", file=sys.stderr)
        try:
            html = _fetch_kazusa_html(taxid)
        except Exception as exc:  # noqa: BLE001 — we re-raise after exhausting candidates
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"    network error: {last_error}", file=sys.stderr)
            continue
        csv = _html_to_csv(html)
        if csv is None:
            last_error = f"taxid={taxid} returned no codon rows (Kazusa empty / not-found)"
            print(f"    {last_error}", file=sys.stderr)
            continue

        filename = f"{plan['spec_alias']}_{taxid}.csv"
        path = DATA_DIR / filename
        path.write_text(csv)
        sha = hashlib.sha256(csv.encode("utf-8")).hexdigest()
        print(f"    saved {filename} ({len(csv)} bytes, sha256={sha[:12]}…)", file=sys.stderr)
        return {
            "spec_alias": plan["spec_alias"],
            "filename": filename,
            "kazusa_taxid_used": taxid,
            "kazusa_taxid_candidates": plan["kazusa_candidates"],
            "kazusa_url": url,
            "scientific_name": plan["scientific_name"],
            "common_name": plan["common_name"],
            "ncbi_taxon_id": plan["ncbi_taxon_id"],
            "sha256": sha,
            "downloaded_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "downloader": "scripts/fetch_codon_tables.py",
            "library_format": "python-codon-tables CSV (amino_acid,codon,relative_frequency)",
        }
    raise RuntimeError(
        f"Could not fetch codon table for {plan['spec_alias']!r}; "
        f"tried {plan['kazusa_candidates']}; last error: {last_error}"
    )


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = [_fetch_one(plan) for plan in ORGANISMS]
    provenance_path = DATA_DIR / "provenance.json"
    provenance = {
        "schema_version": 1,
        "source": "Kazusa Codon Usage Database (http://www.kazusa.or.jp/codon/)",
        "tables": entries,
    }
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"wrote {provenance_path} ({len(entries)} tables)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
