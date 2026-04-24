"""`bio_scan_domains` — protein domain architecture prediction via EBI InterProScan.

Phase 2. See spec §4.13.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Scan Protein Domains (InterProScan)".

**Partial-results-on-timeout = HARD ERROR (tier-3) — VERIFIED 2026-04-24.**
Tier-1 (/resulttypes during RUNNING) and tier-2 (/result/{jobId}/tsv
during RUNNING) were probed against live EBI via
`scripts/probe_iprscan_partial.py`. Both are negative: EBI returns
HTTP 400 with a fixed XML "not finished" body throughout RUNNING, and
`/resulttypes/{jobId}` raises non-200 until the job FINISHES. There
is no partial-results path to surface, so the tier-3 hard error is
the only correct behaviour. Do not invent a streaming branch.

**Return shape:** plain ``dict[str, Any]`` — same pattern as align.

**Spec errata captured here for session-end memory:** §4.13 defaults
to ``["Pfam", "SMART", "CDD"]`` but EBI's API (verified via
/parameterdetails/appl) has no "Pfam" — the correct identifier is
**"PfamA"**. Other spec names that don't exist on EBI: "PROSITE"
(split into PrositeProfiles / PrositePatterns), "SUPERFAMILY" (is
"SuperFamily"), "Gene3D" (is "Gene3d" lowercase-d). This tool's
input enum uses spec-facing names mapped to EBI-canonical names at
submission time.

**"This is expected" pre-documentation** (not tool errors):
- Empty ``matches`` array is a valid result (no recognised domains).
- Multiple overlapping matches from different databases (Pfam + SMART
  hitting the same region) are normal — different signatures, same
  biology. Caller should group by region to deduplicate for display.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from bioinformatics_mcp.clients.ebi import EBIJobRunner
from bioinformatics_mcp.utils.errors import (
    ExternalServiceDown,
    JobFailed,
    JobTimeoutError,
    RateLimitExceeded,
    error_response,
)
from bioinformatics_mcp.utils.formatting import soft_cap_with_url_fallback

# Large match payloads — unusual but possible for long multi-domain
# proteins hit by all 24 databases. 200 KB matches the Clustal cap
# for consistency.
MATCHES_SOFT_CAP_BYTES = 200 * 1024

# Cap on `max_wait_seconds` — InterProScan legitimately takes 5+ min
# for long sequences across all databases, but 30 min is past the
# point where a synchronous MCP tool call makes sense.
MAX_WAIT_CEILING_SECONDS = 1800

# Spec-facing name → EBI-canonical name. Verified 2026-04-24 against
# /parameterdetails/appl. Spec §4.13 names that differ from canonical:
#   "Pfam" → "PfamA"
#   "PROSITE" → split: PrositeProfiles + PrositePatterns
#   "SUPERFAMILY" → "SuperFamily"
#   "Gene3D" → "Gene3d"
_APPL_CANONICAL_MAP: dict[str, list[str]] = {
    "Pfam": ["PfamA"],
    "SMART": ["SMART"],
    "CDD": ["CDD"],
    "PROSITE": ["PrositeProfiles", "PrositePatterns"],
    "SUPERFAMILY": ["SuperFamily"],
    "Gene3D": ["Gene3d"],
}


class ScanDomainsInput(BaseModel):
    """Input schema for ``bio_scan_domains`` (spec §4.13)."""

    sequence: str = Field(
        ...,
        min_length=20,
        max_length=40000,
        description=(
            "Protein sequence (no FASTA header). Whitespace is stripped; "
            "alphabet is validated by EBI — mismatched DNA will return a "
            "JobFailed with an actionable message."
        ),
    )
    applications: list[
        Literal["Pfam", "SMART", "PROSITE", "CDD", "SUPERFAMILY", "Gene3D"]
    ] = Field(
        default_factory=lambda: ["Pfam", "SMART", "CDD"],
        description=(
            "Signature databases to scan. Mapped to EBI-canonical names at "
            "submission; 'Pfam' → 'PfamA', 'PROSITE' → PrositeProfiles + "
            "PrositePatterns, 'SUPERFAMILY' → 'SuperFamily', 'Gene3D' → 'Gene3d'."
        ),
    )
    max_wait_seconds: int | None = Field(
        default=None,
        ge=60,
        le=MAX_WAIT_CEILING_SECONDS,
        description=(
            "Override the 600 s default wall-clock timeout. Capped at "
            "1800 s (30 min) to prevent pathological holds."
        ),
    )


def _canonical_appl(user_choices: Sequence[str]) -> str:
    """Map user-facing database names to EBI-canonical comma-separated string."""
    canonical: list[str] = []
    for name in user_choices:
        canonical.extend(_APPL_CANONICAL_MAP.get(name, [name]))
    return ",".join(canonical)


def _parse_matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten InterProScan JSON into a per-match list.

    InterProScan JSON shape (top level):
        {"results": [{"sequence": "...", "matches": [...]}, ...]}
    Each match has a signature (id, name, library/signatureLibrary,
    entry info) and locations (start/end). We flatten to one record
    per (match x location) pair so downstream consumers don't have to
    traverse nested structure.
    """
    flattened: list[dict[str, Any]] = []
    for result in payload.get("results") or []:
        for match in result.get("matches") or []:
            sig = match.get("signature") or {}
            library = sig.get("signatureLibraryRelease") or {}
            library_name = library.get("library") or sig.get("signatureLibrary")
            entry = sig.get("entry") or {}
            for loc in match.get("locations") or []:
                flattened.append(
                    {
                        "signature_id": sig.get("accession"),
                        "signature_name": sig.get("name"),
                        "signature_description": sig.get("description"),
                        "signature_library": library_name,
                        "interpro_id": entry.get("accession"),
                        "interpro_name": entry.get("name"),
                        "interpro_description": entry.get("description"),
                        "start": loc.get("start"),
                        "end": loc.get("end"),
                        "e_value": loc.get("evalue") or match.get("evalue"),
                        "score": loc.get("score") or match.get("score"),
                    }
                )
    return flattened


async def bio_scan_domains(
    sequence: str,
    applications: Sequence[str] | None = None,
    max_wait_seconds: int | None = None,
    *,
    runner: EBIJobRunner,
    email: str,
) -> dict[str, Any]:
    """Scan a protein sequence for domain signatures via EBI InterProScan.

    Returns a flattened list of matches (signature + location + InterPro
    cross-reference). Empty matches is a valid result — not every sequence
    hits a recognised signature. Timeout raises an error rather than
    returning partial results (tier-3 fallback; probe for tier-1/tier-2
    support deferred until EBI_EMAIL is available).
    """
    params = ScanDomainsInput.model_validate(
        {
            "sequence": sequence,
            "applications": applications or ["Pfam", "SMART", "CDD"],
            "max_wait_seconds": max_wait_seconds,
        }
    )
    timeout = float(params.max_wait_seconds or 600)
    canonical_appl = _canonical_appl(params.applications)

    submission: dict[str, Any] = {
        "email": email,
        "sequence": params.sequence.strip(),
        "appl": canonical_appl,
        "stype": "p",  # InterProScan only operates on protein
    }

    try:
        raw = await runner.run(
            params=submission,
            result_type="json",
            timeout=timeout,
        )
    except JobTimeoutError as exc:
        return error_response(
            str(exc),
            suggestions=[
                "InterProScan can take 5+ min for long multi-domain proteins. "
                "Retry with max_wait_seconds up to 1800 (30 min).",
                "Partial-results-during-RUNNING is not implemented in this "
                "build (tier-3 fallback per pre-work decision).",
            ],
            job_id=exc.job_id,
            cancelled=exc.cancelled,
        )
    except JobFailed as exc:
        return error_response(
            f"EBI InterProScan job failed: {exc.status}.",
            suggestions=[
                "Common cause: DNA sequence submitted as protein. "
                "InterProScan only operates on protein sequences.",
                "Sequence length under 20 or over 40000 residues is rejected "
                "by the tool's input validator — but EBI may add further limits.",
            ],
            job_id=exc.job_id,
            status=exc.status,
        )
    except RateLimitExceeded:
        return error_response(
            "EBI rate limit exceeded. Retry in a moment.",
            suggestions=["Spec §7.1 caps EBI at 3 concurrent, 0.5 s apart."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"EBI API is unreachable: {exc.reason}.",
            suggestions=[
                "Transient upstream error. Retry in a few minutes.",
                "Check https://www.ebi.ac.uk/Tools/common/status",
            ],
        )

    try:
        import json

        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        return error_response(
            f"InterProScan returned non-JSON output: {exc}.",
            suggestions=[
                "This usually means EBI changed the json result-type shape. "
                "Probe /resulttypes/{jobId} on a fresh submission.",
            ],
        )

    matches = _parse_matches(payload)

    result: dict[str, Any] = {
        "sequence_length": len(params.sequence.strip()),
        "databases_scanned": canonical_appl.split(","),
        "match_count": len(matches),
        "matches": matches if len(matches) <= 200 else matches[:200],
    }
    if len(matches) > 200:
        result["matches_truncated"] = True
        result["matches_full_count"] = len(matches)

    # Cap the full JSON payload inline; massive multi-domain responses get
    # a URL fallback. Fallback URL is advisory — the caller must re-submit
    # to re-fetch (EBI keeps jobs for 7 days by ID).
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode()
    result.update(
        soft_cap_with_url_fallback(
            raw_bytes,
            cap_bytes=MATCHES_SOFT_CAP_BYTES,
            fallback_url=(
                f"{runner.base_url}/result/<job_id>/json "
                "(resubmit if expired; EBI retains results for 7 days)"
            ),
            key_prefix="raw_result",
            format_label="json",
            overage_noun="InterProScan result",
        )
    )
    return result
