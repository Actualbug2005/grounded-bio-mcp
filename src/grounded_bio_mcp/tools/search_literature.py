"""`bio_search_literature` — Europe PMC literature search. Spec §4.14.

Anti-hallucination: this is the search half of the literature surface
that answers "does paper X exist, and what does it actually say?"
Chains with ``bio_fetch_paper_fulltext`` — hits surface a
``fulltext_available`` flag so callers know which ones are worth
following up on.

The tool is deliberately thin on top of ``EuropePMCClient``: the client
already composes the query string (OPEN_ACCESS / PUB_YEAR filters
encoded into the Lucene-style query), so this layer just trims each
result down to the fields the model cares about and surfaces the
availability flags.

Author truncation: the spec asks for "first 5 + et al." — we emit
``authors`` as a list of at most 5 names and an explicit ``et_al``
boolean so callers do not have to infer truncation from list length.

Output shape::

    {
      "status": "found",
      "query": "<echoed query>",
      "hit_count": 27731392,   # Europe PMC's total hits, NOT len(papers)
      "papers": [
        {
          "title": "...",
          "authors": ["Sugisawa R", ...],  # up to 5
          "et_al": True,
          "journal": "Scientific Reports",
          "year": 2016,
          "doi": "10.1038/srep35251",
          "pmid": "27731392",
          "pmcid": "PMC5059666",     # may be None
          "abstract": "...",
          "fulltext_available": True,
          "open_access": True
        },
        ...
      ]
    }

``hit_count`` is Europe PMC's ``hitCount`` so callers can tell whether
they got the full result set or a truncated window; ``max_results``
caps the list but not the counter.
"""

from __future__ import annotations

from typing import Any

from grounded_bio_mcp.clients.europepmc import EuropePMCClient
from grounded_bio_mcp.utils.errors import (
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)

_MAX_AUTHORS = 5


async def bio_search_literature(
    query: str,
    *,
    max_results: int = 20,
    open_access_only: bool = False,
    year_from: int | None = None,
    year_to: int | None = None,
    client: EuropePMCClient,
) -> dict[str, Any]:
    """Search Europe PMC for papers matching ``query``.

    Returns ``{status, query, hit_count, papers}`` on success or an
    error payload for malformed inputs / upstream outages. Zero hits
    is a valid ``status="found"`` outcome with an empty ``papers``
    list — the anti-hallucination principle is that "no matches" must
    never be conflated with "lookup failed".
    """
    if not query or len(query.strip()) < 3:
        return error_response(
            "query must be at least 3 characters (spec §4.14 min_length=3).",
            suggestions=[
                "Use a more specific query, e.g. 'Sugisawa 2016 AIM feline'.",
                "To look up by DOI, pass 'DOI:<doi>' as the query.",
            ],
        )

    try:
        payload = await client.search(
            query.strip(),
            max_results=max_results,
            open_access_only=open_access_only,
            year_from=year_from,
            year_to=year_to,
        )
    except RateLimitExceeded:
        return error_response(
            "Europe PMC rate limit exceeded.",
            suggestions=["Retry after a short delay."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"Europe PMC is unreachable: {exc.reason}.",
            suggestions=[f"Check service status at {exc.status_url}."],
        )

    hit_count = int(payload.get("hitCount", 0))
    raw_results = payload.get("resultList", {}).get("result", [])
    papers = [_trim_paper(r) for r in raw_results]
    return {
        "status": "found",
        "query": query.strip(),
        "hit_count": hit_count,
        "papers": papers,
    }


def _trim_paper(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse a Europe PMC ``core`` result into the tool's output shape."""
    authors, et_al = _truncate_authors(record.get("authorList") or {})
    pmcid = record.get("pmcid") or None
    in_pmc = record.get("inPMC") == "Y"
    has_fulltext_id = bool(
        record.get("fullTextIdList", {}).get("fullTextId") if record.get("fullTextIdList") else False
    )
    # Only claim fulltext_available when Europe PMC itself confirms the
    # fulltext is indexed. A PMC ID is necessary but not sufficient —
    # ``inPMC=Y`` + a non-empty ``fullTextIdList`` is the honest signal.
    fulltext_available = bool(pmcid) and in_pmc and has_fulltext_id
    year = record.get("pubYear")
    journal = (record.get("journalInfo", {}) or {}).get("journal", {}) or {}
    return {
        "title": record.get("title", "").rstrip("."),
        "authors": authors,
        "et_al": et_al,
        "journal": journal.get("title"),
        "year": int(year) if year else None,
        "doi": record.get("doi"),
        "pmid": record.get("pmid"),
        "pmcid": pmcid,
        "abstract": record.get("abstractText"),
        "fulltext_available": fulltext_available,
        "open_access": record.get("isOpenAccess") == "Y",
    }


def _truncate_authors(author_list: dict[str, Any]) -> tuple[list[str], bool]:
    """Return ``(up_to_five_names, et_al_flag)`` from a Europe PMC authorList."""
    authors = (author_list or {}).get("author") or []
    names = [a.get("fullName", "") for a in authors if a.get("fullName")]
    if len(names) > _MAX_AUTHORS:
        return names[:_MAX_AUTHORS], True
    return names, False
