"""`bio_fetch_gene` — NCBI Gene record with full genomic context.

Phase 2. See spec §4.16.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=True, title="Fetch NCBI Gene".

Gene records are essentially stable in their genomic location and exon
structure — ``idempotentHint=True`` is the right annotation even though
annotation detail (GO curation, RefSeq transcript additions) accumulates
over time. A retry of the same query will still describe the same gene.

Design decisions (approved 2026-04-24):

* **Two-call NCBI strategy.** ``esearch`` resolves a symbol to a Gene
  ID, ``esummary`` gives the compact metadata view (chromosome, exon
  count, organism, OMIM), and ``efetch rettype=xml`` gives the rich
  content (RefSeq transcripts, GO annotations, UniProt/Ensembl
  cross-references). ``esummary`` alone doesn't expose the rich
  content; ``efetch`` alone omits the compact fields. Both are needed
  for a useful response.
* **Gene-symbol disambiguation via candidate list.** When ``esearch``
  returns more than one ID after the organism filter, the tool fans
  out ``esummary`` on all hits (capped at 10) and returns them as
  ``candidate_gene_ids`` with ``{gene_id, symbol, description,
  organism, chromosome, map_location}`` so the caller can re-query with
  a disambiguated identifier.
* **GO-list soft-cap.** BRCA1 has hundreds of GO annotations. The
  200 KB cap from ``soft_cap_with_url_fallback`` (matching alignment /
  domain-scan convention) trims the serialised GO list into a fallback
  URL when it blows the budget.
* **No raw gene_table.** NCBI's ``rettype=gene_table`` is a plain-text
  exon table that isn't structurally parseable — including it would
  give callers bytes they can't reason over. Omitted by design; if a
  future caller needs raw exon coordinates verbatim, the XML parser
  already extracts them.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from grounded_bio_mcp.clients.ncbi import NCBIClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)
from grounded_bio_mcp.utils.formatting import soft_cap_with_url_fallback

_GENE_DB = "gene"
_CANDIDATE_CAP = 10
_GO_CAP_BYTES = 200 * 1024
_GO_FALLBACK_URL = "https://www.ncbi.nlm.nih.gov/gene/"
_DISAMBIGUATION_HINT = (
    "NCBI Gene returned more than one record for this symbol. This usually "
    "means the symbol is used in multiple species or has historical "
    "aliases across organisms. Re-query with a specific NCBI Gene ID "
    "(e.g. '672' for human BRCA1) or narrow by organism."
)


async def bio_fetch_gene(
    identifier: str,
    organism: str = "Homo sapiens",
    *,
    client: NCBIClient,
) -> dict[str, Any]:
    """Fetch an NCBI Gene record by symbol or numeric Gene ID.

    Returns a structured record with genomic location, RefSeq
    transcripts, GO annotations, and UniProt/Ensembl cross-refs on
    unique resolution, or a ``candidate_gene_ids`` list for ambiguous
    symbol lookups.
    """
    if not identifier or not identifier.strip():
        return error_response(
            "A gene symbol ('BRCA1') or NCBI Gene ID ('672') is required.",
            suggestions=[
                "Pass a symbol like 'BRCA1' with organism='Homo sapiens'.",
                "Or pass a numeric Gene ID if you already have one.",
            ],
        )

    identifier = identifier.strip()
    try:
        gene_ids = await _resolve_gene_ids(identifier, organism, client=client)
    except AccessionNotFound as exc:
        return error_response(
            f"NCBI Gene query for '{exc.accession}' returned no results.",
            suggestions=[
                "Check the gene symbol spelling — NCBI is case-insensitive but exact.",
                "Try without the organism filter to see if the symbol exists for a different species.",
            ],
        )
    except (RateLimitExceeded, ExternalServiceDown) as exc:
        return error_response(
            f"NCBI is unavailable ({exc}).",
            suggestions=["Transient upstream error — retry in a few minutes."],
        )

    if not gene_ids:
        return error_response(
            f"No NCBI Gene record found for '{identifier}'"
            + (f" in {organism}." if organism else "."),
            suggestions=[
                "Check the symbol spelling.",
                "Omit the organism parameter to search across species.",
                "Use a known Gene ID directly if you have one.",
            ],
        )

    try:
        summary_result = await client.esummary(
            db=_GENE_DB, ids=gene_ids[:_CANDIDATE_CAP]
        )
    except (AccessionNotFound, RateLimitExceeded, ExternalServiceDown) as exc:
        return error_response(f"NCBI esummary failed: {exc}.")

    if len(gene_ids) > 1:
        return _build_ambiguous_response(gene_ids, summary_result)

    sole_id = gene_ids[0]
    summary = summary_result.get(sole_id, {})
    try:
        xml_text = await client.efetch_xml(db=_GENE_DB, uid=sole_id)
    except (AccessionNotFound, RateLimitExceeded, ExternalServiceDown) as exc:
        return error_response(f"NCBI efetch XML failed for Gene ID {sole_id}: {exc}.")

    rich = _parse_gene_xml(xml_text)
    return _compose_found_response(sole_id, summary, rich)


async def _resolve_gene_ids(
    identifier: str, organism: str, *, client: NCBIClient
) -> list[str]:
    if identifier.isdigit():
        return [identifier]
    term_parts = [f"{identifier}[Gene]"]
    if organism:
        term_parts.append(f'"{organism}"[Organism]')
    term = " AND ".join(term_parts)
    return await client.esearch(db=_GENE_DB, term=term, retmax=_CANDIDATE_CAP)


def _build_ambiguous_response(
    gene_ids: list[str], summary_result: dict[str, Any]
) -> dict[str, Any]:
    candidates = []
    for gid in gene_ids[:_CANDIDATE_CAP]:
        record = summary_result.get(gid, {})
        if not record:
            continue
        candidates.append(
            {
                "gene_id": gid,
                "symbol": record.get("name") or record.get("nomenclaturesymbol"),
                "description": record.get("description"),
                "organism": (record.get("organism") or {}).get("scientificname"),
                "chromosome": record.get("chromosome"),
                "map_location": record.get("maplocation"),
            }
        )
    return {
        "status": "ambiguous",
        "candidate_gene_ids": candidates,
        "disambiguation_hint": _DISAMBIGUATION_HINT,
    }


def _compose_found_response(
    gene_id: str,
    summary: dict[str, Any],
    rich: dict[str, Any],
) -> dict[str, Any]:
    genomic_info = (summary.get("genomicinfo") or [{}])[0]
    gene: dict[str, Any] = {
        "gene_id": gene_id,
        "symbol": summary.get("name") or rich.get("symbol"),
        "description": summary.get("description") or rich.get("description"),
        "type": rich.get("type"),
        "map_location": summary.get("maplocation") or rich.get("map_location"),
        "chromosome": summary.get("chromosome") or rich.get("chromosome"),
        "synonyms": _split_aliases(summary.get("otheraliases"))
        or rich.get("synonyms", []),
        "alternative_names": _split_pipe(summary.get("otherdesignations")),
        "organism": summary.get("organism") or rich.get("organism"),
        "mim_ids": list(summary.get("mim") or []),
        "genomic_location": {
            "assembly_accession": genomic_info.get("chraccver"),
            "start": genomic_info.get("chrstart"),
            "stop": genomic_info.get("chrstop"),
            "exon_count": genomic_info.get("exoncount"),
        },
        "refseq_transcripts": rich.get("refseq_transcripts", []),
        "cross_references": rich.get("cross_references", {}),
        "refseq_summary": summary.get("summary"),
    }
    go_list = rich.get("go_annotations", [])
    capped = soft_cap_with_url_fallback(
        _serialise_go(go_list),
        cap_bytes=_GO_CAP_BYTES,
        fallback_url=f"{_GO_FALLBACK_URL}{gene_id}",
        key_prefix="go_annotations",
        format_label="json_inline",
        overage_noun="GO annotation list",
    )
    if "go_annotations_error" in capped:
        gene["go_annotations"] = []
        gene["go_annotations_error"] = capped["go_annotations_error"]
    else:
        gene["go_annotations"] = go_list
    return {"status": "found", "gene": gene}


def _split_aliases(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in value.split(",") if a.strip()]


def _split_pipe(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in value.split("|") if a.strip()]


def _serialise_go(go_list: list[dict[str, Any]]) -> str:
    """Return a stable serialised form for cap measurement."""
    import json

    return json.dumps(go_list, ensure_ascii=False)


# ---- XML parsing ---------------------------------------------------------

_CROSS_REF_DBS = {
    "UniProtKB/Swiss-Prot",
    "UniProtKB/TrEMBL",
    "Ensembl",
    "HGNC",
    "MGI",
    "MIM",
    "HPRD",
}


def _parse_gene_xml(xml_text: str) -> dict[str, Any]:
    """Extract RefSeq transcripts, GO annotations, and cross-refs from the XML."""
    try:
        root = etree.fromstring(
            xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text,
            etree.XMLParser(load_dtd=False, resolve_entities=False, no_network=True),
        )
    except etree.XMLSyntaxError:
        return {
            "refseq_transcripts": [],
            "go_annotations": [],
            "cross_references": {},
        }

    rich: dict[str, Any] = {
        "symbol": _find_text(root, ".//Gene-ref_locus"),
        "description": _find_text(root, ".//Gene-ref_desc"),
        "map_location": _find_text(root, ".//Gene-ref_maploc"),
        "synonyms": [e.text for e in root.iter("Gene-ref_syn_E") if e.text],
        "type": _get_attr(root, ".//Entrezgene_type", "value"),
        "organism": {
            "scientificname": _find_text(root, ".//Org-ref_taxname"),
            "commonname": _find_text(root, ".//Org-ref_common"),
        },
        "refseq_transcripts": _extract_refseq_transcripts(root),
        "go_annotations": _extract_go_annotations(root),
        "cross_references": _extract_cross_references(root),
    }
    return rich


def _find_text(root: Any, xpath: str) -> str | None:
    elem = root.find(xpath)
    return elem.text if elem is not None else None


def _get_attr(root: Any, xpath: str, attr: str) -> str | None:
    elem = root.find(xpath)
    return elem.get(attr) if elem is not None else None


def _extract_refseq_transcripts(root: Any) -> list[dict[str, Any]]:
    """Collect RefSeq transcript/protein accessions from Gene-commentary entries."""
    transcripts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for acc_elem in root.iter("Gene-commentary_accession"):
        acc = (acc_elem.text or "").strip()
        if not acc or acc in seen:
            continue
        prefix = acc.split("_", 1)[0] if "_" in acc else ""
        if prefix not in {"NM", "NR", "NP", "XM", "XR", "XP"}:
            continue
        seen.add(acc)
        parent = acc_elem.getparent()
        version_elem = parent.find("Gene-commentary_version")
        type_elem = parent.find("Gene-commentary_type")
        transcripts.append(
            {
                "accession": acc,
                "version": version_elem.text if version_elem is not None else None,
                "type": type_elem.get("value") if type_elem is not None else None,
            }
        )
    return transcripts


def _extract_go_annotations(root: Any) -> list[dict[str, Any]]:
    """Collect GO terms from Dbtag elements with db=GO."""
    go: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dbtag in root.iter("Dbtag"):
        db = dbtag.findtext("Dbtag_db")
        if db != "GO":
            continue
        go_id = dbtag.findtext("Dbtag_tag/Object-id/Object-id_id")
        if not go_id or go_id in seen:
            continue
        seen.add(go_id)
        # Anchor (term name) lives in the parent Other-source_anchor.
        other_source = dbtag.getparent()
        anchor = (
            other_source.getparent().findtext("Other-source_anchor")
            if other_source is not None and other_source.getparent() is not None
            else None
        )
        go.append(
            {
                "id": f"GO:{int(go_id):07d}" if go_id.isdigit() else go_id,
                "term": anchor,
            }
        )
    return go


def _extract_cross_references(root: Any) -> dict[str, list[str]]:
    """Collect cross-references to UniProt, Ensembl, HGNC, MGI, MIM, HPRD."""
    xrefs: dict[str, set[str]] = {}
    for dbtag in root.iter("Dbtag"):
        db = dbtag.findtext("Dbtag_db")
        if db not in _CROSS_REF_DBS:
            continue
        acc = dbtag.findtext(
            "Dbtag_tag/Object-id/Object-id_str"
        ) or dbtag.findtext("Dbtag_tag/Object-id/Object-id_id")
        if not acc:
            continue
        xrefs.setdefault(db, set()).add(acc)
    return {db: sorted(accs) for db, accs in xrefs.items()}
