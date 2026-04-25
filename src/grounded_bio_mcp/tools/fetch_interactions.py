"""`bio_fetch_interactions` — STRING protein-protein interactions. Spec §4.18.

Returns a trimmed per-edge list with the seven STRING evidence sub-scores
so callers can distinguish direct experimental evidence from text-mining
co-occurrence. Every response echoes the score-scale contract so callers
reading structured output see the 0-1000 vs 0-1 distinction without
having to read the docstring (belt-and-braces documentation per user
guidance — silent wrong-answer bugs in this surface would be a factor
of 1000 off).

**Score scale:**

* Input ``min_score``: 0-1000 (700 = 0.7 threshold).
* Output ``combined_score`` + ``evidence.*``: 0-1.

The tool enforces ``min_score >= 150`` per spec §4.18 — a caller who
passes 0 or 1 has almost certainly confused the scales, so we reject
rather than submit a junk query to STRING.

**Trust but verify:** STRING's server-side ``required_score`` filter
was verified non-leaky during session-6 pre-work (30 partners at
required_score=900 → every score ≥ 0.998). Unlike ChEMBL, we do not
re-enforce client-side with a counter. A DEBUG-level assertion catches
drift silently.

**Evidence channel renaming:** STRING's JSON uses terse keys
(``nscore, fscore, pscore, ascore, escore, dscore, tscore``). The tool
renames to human-readable names
(``neighbourhood, fusion, co_occurrence, coexpression, experimental,
database, textmining``) because callers of the MCP will be LLMs
picking meaning from structured fields, not humans memorising the
STRING letter code.

Output shape::

    {
      "status": "found",
      "score_scale": {"input_min_score": "0-1000", "output_scores": "0-1"},
      "query": {"identifier": "TP53", "species_taxon": 9606,
                "min_score": 700, "max_partners": 20},
      "partners": [
        {
          "partner_name": "SFN",
          "partner_string_id": "9606.ENSP00000340989",
          "combined_score": 0.999,
          "evidence": {
            "neighbourhood": 0.0, "fusion": 0.0, "co_occurrence": 0.0,
            "coexpression": 0.0, "experimental": 0.981,
            "database": 0.75, "textmining": 0.859
          }
        },
        ...
      ]
    }
"""

from __future__ import annotations

import logging
from typing import Any

from grounded_bio_mcp.clients.string_db import StringDBClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)

logger = logging.getLogger(__name__)

_MIN_SCORE_FLOOR = 150
_MIN_SCORE_CEILING = 1000

_EVIDENCE_KEY_MAP: dict[str, str] = {
    "nscore": "neighbourhood",
    "fscore": "fusion",
    "pscore": "co_occurrence",
    "ascore": "coexpression",
    "escore": "experimental",
    "dscore": "database",
    "tscore": "textmining",
}


async def bio_fetch_interactions(
    identifier: str,
    *,
    species_taxon: int = 9606,
    min_score: int = 700,
    max_partners: int = 20,
    client: StringDBClient,
) -> dict[str, Any]:
    """Fetch top interaction partners for ``identifier``.

    ``min_score`` is on the 0-1000 input scale; output scores are 0-1.
    A caller who passes e.g. ``min_score=0.7`` (output scale) instead
    of ``700`` (input scale) is rejected at the input-validation gate.
    """
    if not identifier or not identifier.strip():
        return error_response(
            "identifier is required (gene symbol, UniProt accession, or STRING ID).",
            suggestions=[
                "Example: 'TP53', 'P04637', or '9606.ENSP00000269305'.",
            ],
        )
    if not _MIN_SCORE_FLOOR <= min_score <= _MIN_SCORE_CEILING:
        return error_response(
            (
                f"min_score must be on the 0-1000 input scale "
                f"(got {min_score}); spec §4.18 clamps to 150-1000. "
                f"Did you pass the output scale (0-1)? 700 on input "
                f"= 0.7 threshold in output scores."
            ),
            suggestions=[
                "Default 700 = high confidence; 900 = highest; 400 = medium.",
            ],
        )

    try:
        raw_partners = await client.interaction_partners(
            identifier=identifier.strip(),
            species_taxon=species_taxon,
            required_score=min_score,
            limit=max_partners,
        )
    except AccessionNotFound:
        return {
            "status": "not_found",
            "message": (
                f"Identifier '{identifier}' not found in STRING for "
                f"species taxon {species_taxon}. Check the gene symbol "
                f"and species match."
            ),
        }
    except RateLimitExceeded:
        return error_response(
            "STRING rate limit exceeded.",
            suggestions=["Retry after a short delay."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"STRING is unreachable: {exc.reason}.",
            suggestions=[f"Check service status at {exc.status_url}."],
        )

    # DEBUG-level canary: any edge below the server-side threshold
    # indicates STRING's filter has become leaky. We do NOT add a
    # user-visible counter because the offline unit test is the
    # canary of record (see test_interaction_partners_filter_is_not_leaky).
    threshold = min_score / 1000.0
    leaky = [p for p in raw_partners if p.get("score", 0) < threshold]
    if leaky:
        logger.debug(
            "STRING returned %d/%d edges below required_score=%d threshold "
            "— server-side filter may be regressing",
            len(leaky),
            len(raw_partners),
            min_score,
        )

    partners = [_trim_edge(p) for p in raw_partners]
    return {
        "status": "found",
        "score_scale": {
            "input_min_score": "0-1000",
            "output_scores": "0-1",
        },
        "query": {
            "identifier": identifier.strip(),
            "species_taxon": species_taxon,
            "min_score": min_score,
            "max_partners": max_partners,
        },
        "partners": partners,
    }


def _trim_edge(edge: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        human: float(edge.get(terse, 0) or 0)
        for terse, human in _EVIDENCE_KEY_MAP.items()
    }
    return {
        "partner_name": edge.get("preferredName_B"),
        "partner_string_id": edge.get("stringId_B"),
        "combined_score": float(edge.get("score", 0) or 0),
        "evidence": evidence,
    }
