"""`bio_fetch_paper_fulltext` — Europe PMC full-text fetch. Spec §4.15.

The most load-bearing tool in the project. Citation verification —
answering "does paper X actually say Y?" — is the concrete thing this
tool exists to do, and "does it exist" is the failure mode that started
the whole project (the ChatGPT transcript attributing findings to
"Miyazaki/Nakata work" without a verifiable DOI is the canonical
example of the anti-hallucination failure we reject).

**Four-state ``availability`` enum — honest reporting over uniform shape:**

* ``full_xml`` — /fullTextXML returned JATS 1.4; sections + figure
  captions parsed from the document.
* ``abstract_only`` — search record found but no fulltext available.
  ``fulltext_unavailable_reason`` explains *why*:
    - ``"paper not in PMC"`` — search record has ``pmcid=None`` /
      ``inPMC=N``. Closed-access, not in PMC, publisher doesn't deposit.
    - ``"PMC ID exists but fulltext XML returned 404"`` — PMC has the
      record but the fulltext XML is unavailable (retracted paper,
      embargoed, PMC-side issue).
    - ``"closed-access paper"`` — ``isOpenAccess=N`` with no PMC ID.
      Sometimes coincides with "paper not in PMC"; we prefer this
      reason when Europe PMC explicitly says closed-access because
      it is more informative.
* ``metadata_only`` — search record exists but has no abstract and
  no fulltext. Rare but possible for very old or ill-indexed papers.
* ``not_found`` — identifier did not resolve in Europe PMC at all.

Every non-full-xml branch **returns the metadata it has, not
fabricated prose**. The callee's job is citation-grounding, not
synthesis — when we only know the title and authors, that's what we
return.

Section shape: flat list of ``{title, level, text}`` where ``level=1``
is a top-level section, ``level=2`` is a subsection, and so on. Flat
structure keeps ``sections`` filter simple (case-insensitive substring
match on ``title`` at ``level=1``) and matches how the model is likely
to chain follow-up questions.

Figure shape: ``{label, id, caption}`` — label is the paper's own
"Figure 1" / "Fig. 2" string; id is the JATS ``@id`` attribute so
callers can reference it unambiguously.

Soft-cap fallback: if the combined section text exceeds 300 KB, the
``sections`` key is replaced with ``sections_error`` pointing to the
Europe PMC web URL for the paper. The helper is shared with the PDB
and InterProScan tools (``utils.formatting.soft_cap_with_url_fallback``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from lxml import etree

from grounded_bio_mcp.clients.europepmc import EuropePMCClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)
from grounded_bio_mcp.utils.formatting import soft_cap_with_url_fallback

logger = logging.getLogger(__name__)

_MAX_AUTHORS = 5
_SECTION_SOFT_CAP_BYTES = 300 * 1024  # 300 KB — ~10x PMC5059666

AvailabilityLiteral = Literal["full_xml", "abstract_only", "metadata_only", "not_found"]


async def bio_fetch_paper_fulltext(
    identifier: str,
    identifier_type: Literal["pmc", "doi"],
    *,
    sections: list[str] | None = None,
    client: EuropePMCClient,
) -> dict[str, Any]:
    """Fetch JATS fulltext for an open-access paper, honestly.

    Returns the uniform ``availability`` shape described in the module
    docstring. Never fabricates content for unavailable papers.
    """
    if not identifier or not identifier.strip():
        return error_response(
            "identifier is required (PMC ID or DOI).",
            suggestions=[
                "Pass a PMC ID like 'PMC5059666' with identifier_type='pmc'.",
                "Or a DOI like '10.1038/srep35251' with identifier_type='doi'.",
            ],
        )

    ident = identifier.strip()

    try:
        if identifier_type == "pmc":
            return await _fetch_via_pmc(ident, sections=sections, client=client)
        if identifier_type == "doi":
            return await _fetch_via_doi(ident, sections=sections, client=client)
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

    return error_response(
        f"identifier_type must be 'pmc' or 'doi', got {identifier_type!r}.",
    )


# ---- PMC-ID path --------------------------------------------------------


async def _fetch_via_pmc(
    pmc_id: str,
    *,
    sections: list[str] | None,
    client: EuropePMCClient,
) -> dict[str, Any]:
    try:
        xml = await client.fetch_fulltext_xml(pmc_id)
    except AccessionNotFound:
        # PMC ID didn't produce fulltext XML; try the search endpoint
        # for metadata + abstract as a graceful abstract_only fallback.
        return await _abstract_only_via_search(
            query=f"PMC:{pmc_id.replace('PMC', '')}",
            supplied_pmcid=pmc_id if pmc_id.upper().startswith("PMC") else f"PMC{pmc_id}",
            fulltext_unavailable_reason=(
                "PMC ID exists but fulltext XML returned 404"
            ),
            client=client,
        )

    metadata = _parse_jats_metadata(xml)
    parsed_sections = _parse_jats_sections(xml)
    figures = _parse_jats_figures(xml)
    if sections:
        parsed_sections = _filter_sections(parsed_sections, requested=sections)

    out: dict[str, Any] = {
        "status": "found",
        "availability": "full_xml",
        "fulltext_unavailable_reason": None,
        **metadata,
        "figures": figures,
    }
    # Apply the shared soft-cap helper to the serialised section list.
    section_blob = "\n\n".join(
        f"# {s['title']}\n{s['text']}" for s in parsed_sections
    )
    cap = soft_cap_with_url_fallback(
        section_blob,
        cap_bytes=_SECTION_SOFT_CAP_BYTES,
        fallback_url=f"https://europepmc.org/article/PMC/{_strip_pmc_prefix(pmc_id)}",
        key_prefix="sections",
        format_label="structured",
        overage_noun="Fulltext",
    )
    if "sections_error" in cap:
        out["sections"] = []
        out["sections_error"] = cap["sections_error"]
    else:
        # Under-cap: we can safely inline the structured list the model
        # actually wants, rather than the serialised blob the helper
        # returns (the helper is designed for monolithic payloads like
        # mmCIF files). Keep the structured form.
        out["sections"] = parsed_sections
    return out


# ---- DOI path -----------------------------------------------------------


async def _fetch_via_doi(
    doi: str,
    *,
    sections: list[str] | None,
    client: EuropePMCClient,
) -> dict[str, Any]:
    search = await client.search(f"DOI:{doi}", max_results=1)
    results = (search.get("resultList") or {}).get("result") or []
    if not results:
        return {
            "status": "not_found",
            "availability": "not_found",
            "message": f"DOI '{doi}' not found in Europe PMC.",
        }
    record = results[0]
    pmcid = record.get("pmcid")
    in_pmc = record.get("inPMC") == "Y"
    full_text_ids = ((record.get("fullTextIdList") or {}).get("fullTextId")) or []
    if pmcid and in_pmc and full_text_ids:
        # PMC fulltext should be reachable — try and fall back if not.
        try:
            xml = await client.fetch_fulltext_xml(pmcid)
        except AccessionNotFound:
            return _abstract_only_from_search_record(
                record,
                reason="PMC ID exists but fulltext XML returned 404",
            )
        parsed_sections = _parse_jats_sections(xml)
        figures = _parse_jats_figures(xml)
        if sections:
            parsed_sections = _filter_sections(parsed_sections, requested=sections)
        metadata = _merge_metadata_from_jats_and_search(xml, record)
        return {
            "status": "found",
            "availability": "full_xml",
            "fulltext_unavailable_reason": None,
            **metadata,
            "sections": parsed_sections,
            "figures": figures,
        }
    reason = _explain_no_fulltext(record)
    return _abstract_only_from_search_record(record, reason=reason)


# ---- abstract-only helpers ---------------------------------------------


async def _abstract_only_via_search(
    *,
    query: str,
    supplied_pmcid: str | None,
    fulltext_unavailable_reason: str,
    client: EuropePMCClient,
) -> dict[str, Any]:
    search = await client.search(query, max_results=1)
    results = (search.get("resultList") or {}).get("result") or []
    if not results:
        # PMC ID gave 404 on fulltext AND the search didn't find metadata
        # either. The identifier itself is effectively unresolvable.
        return {
            "status": "not_found",
            "availability": "not_found",
            "message": f"Identifier not found in Europe PMC ({query}).",
        }
    return _abstract_only_from_search_record(
        results[0],
        reason=fulltext_unavailable_reason,
        fallback_pmcid=supplied_pmcid,
    )


def _abstract_only_from_search_record(
    record: dict[str, Any],
    *,
    reason: str,
    fallback_pmcid: str | None = None,
) -> dict[str, Any]:
    pmcid = record.get("pmcid") or fallback_pmcid
    abstract = record.get("abstractText")
    authors, et_al = _truncate_search_authors(record.get("authorList") or {})
    year = record.get("pubYear")
    journal = (record.get("journalInfo", {}) or {}).get("journal", {}) or {}
    availability: AvailabilityLiteral = (
        "abstract_only" if abstract else "metadata_only"
    )
    return {
        "status": "found",
        "availability": availability,
        "fulltext_unavailable_reason": reason,
        "title": (record.get("title") or "").rstrip("."),
        "authors": authors,
        "et_al": et_al,
        "journal": journal.get("title"),
        "year": int(year) if year else None,
        "doi": record.get("doi"),
        "pmid": record.get("pmid"),
        "pmcid": pmcid,
        "abstract": abstract,
        "sections": [],
        "figures": [],
    }


def _explain_no_fulltext(record: dict[str, Any]) -> str:
    if record.get("isOpenAccess") == "N" and not record.get("pmcid"):
        return "closed-access paper"
    if not record.get("pmcid") or record.get("inPMC") != "Y":
        return "paper not in PMC"
    return "PMC ID exists but fulltext XML returned 404"


# ---- JATS parsing ------------------------------------------------------


def _parse_jats_root(xml: bytes) -> etree._Element:
    # ``resolve_entities=False`` — defensive against XXE even on a JATS
    # response we trust, because the parser may accept DOCTYPE hints
    # that reference external entities. ``lxml`` is the right tool here;
    # Python's stdlib ``xml.etree`` doesn't expose section structure as
    # conveniently.
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
    return etree.fromstring(xml, parser)


def _parse_jats_metadata(xml: bytes) -> dict[str, Any]:
    root = _parse_jats_root(xml)
    front = root.find("front")
    article_meta = root.find(".//front/article-meta")

    def _front_text(xpath: str) -> str | None:
        if front is None:
            return None
        el = front.find(xpath)
        if el is None:
            return None
        text = etree.tostring(el, method="text", encoding="unicode").strip()
        return text or None

    title = _front_text(".//article-title")
    journal = _front_text(".//journal-title")
    doi = _front_text('.//article-id[@pub-id-type="doi"]')
    pmid = _front_text('.//article-id[@pub-id-type="pmid"]')
    pmcid = _front_text('.//article-id[@pub-id-type="pmcid"]')
    abstract_el = article_meta.find("abstract") if article_meta is not None else None
    abstract = (
        etree.tostring(abstract_el, method="text", encoding="unicode").strip()
        if abstract_el is not None
        else None
    )
    year = _parse_year(front)

    authors_all = _parse_authors(front)
    authors = authors_all[:_MAX_AUTHORS]
    et_al = len(authors_all) > _MAX_AUTHORS

    return {
        "title": (title or "").rstrip("."),
        "authors": authors,
        "et_al": et_al,
        "journal": journal,
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "abstract": abstract,
    }


def _merge_metadata_from_jats_and_search(
    xml: bytes, record: dict[str, Any]
) -> dict[str, Any]:
    """Prefer JATS-derived metadata; fill gaps from the search record."""
    meta = _parse_jats_metadata(xml)
    meta["doi"] = meta.get("doi") or record.get("doi")
    meta["pmid"] = meta.get("pmid") or record.get("pmid")
    meta["pmcid"] = meta.get("pmcid") or record.get("pmcid")
    if not meta.get("abstract"):
        meta["abstract"] = record.get("abstractText")
    return meta


def _parse_year(front: etree._Element | None) -> int | None:
    if front is None:
        return None
    for xpath in (
        './/pub-date[@pub-type="epub"]/year',
        './/pub-date[@pub-type="ppub"]/year',
        ".//pub-date/year",
    ):
        el = front.find(xpath)
        if el is not None and el.text and el.text.strip().isdigit():
            return int(el.text.strip())
    return None


def _parse_authors(front: etree._Element | None) -> list[str]:
    if front is None:
        return []
    names: list[str] = []
    for contrib in front.iterfind('.//contrib[@contrib-type="author"]'):
        surname = contrib.find(".//surname")
        given = contrib.find(".//given-names")
        if surname is None:
            continue
        parts = [surname.text or ""]
        if given is not None and given.text:
            parts.append(given.text)
        full = " ".join(p for p in parts if p).strip()
        if full:
            names.append(full)
    return names


def _parse_jats_sections(xml: bytes) -> list[dict[str, Any]]:
    root = _parse_jats_root(xml)
    body = root.find("body")
    if body is None:
        return []
    out: list[dict[str, Any]] = []
    for sec in body.findall("sec"):
        _walk_section(sec, level=1, out=out)
    return out


def _walk_section(
    sec: etree._Element, *, level: int, out: list[dict[str, Any]]
) -> None:
    title_el = sec.find("title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    # Text of this section excludes nested <sec> children so each level
    # owns its own prose. Collect immediate-child text elements.
    text_parts: list[str] = []
    for child in sec:
        if child.tag == "sec":
            continue
        if child.tag == "title":
            continue
        text = etree.tostring(child, method="text", encoding="unicode").strip()
        if text:
            text_parts.append(text)
    text = "\n\n".join(text_parts)
    out.append({"title": title, "level": level, "text": text})
    for child in sec.findall("sec"):
        _walk_section(child, level=level + 1, out=out)


def _parse_jats_figures(xml: bytes) -> list[dict[str, Any]]:
    root = _parse_jats_root(xml)
    figures: list[dict[str, Any]] = []
    for fig in root.iter("fig"):
        label_el = fig.find("label")
        caption_el = fig.find("caption")
        label = (
            etree.tostring(label_el, method="text", encoding="unicode").strip()
            if label_el is not None
            else ""
        )
        caption = (
            etree.tostring(caption_el, method="text", encoding="unicode").strip()
            if caption_el is not None
            else ""
        )
        figures.append(
            {
                "id": fig.get("id") or "",
                "label": label,
                "caption": caption,
            }
        )
    return figures


def _filter_sections(
    parsed: list[dict[str, Any]], *, requested: list[str]
) -> list[dict[str, Any]]:
    """Keep top-level sections whose title matches any requested name.

    Matching is case-insensitive substring on level=1 titles. Subsections
    are dropped because callers asking for "Methods" rarely want the 19
    nested method sub-sections alongside.
    """
    wanted = {name.lower() for name in requested}
    return [
        s
        for s in parsed
        if s["level"] == 1
        and any(w in s["title"].lower() for w in wanted)
    ]


# ---- misc helpers -------------------------------------------------------


def _truncate_search_authors(author_list: dict[str, Any]) -> tuple[list[str], bool]:
    authors = (author_list or {}).get("author") or []
    names = [a.get("fullName", "") for a in authors if a.get("fullName")]
    if len(names) > _MAX_AUTHORS:
        return names[:_MAX_AUTHORS], True
    return names, False


def _strip_pmc_prefix(pmc_id: str) -> str:
    return re.sub(r"^PMC", "", pmc_id.strip(), flags=re.IGNORECASE)
