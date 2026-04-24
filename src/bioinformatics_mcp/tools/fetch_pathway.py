"""`bio_fetch_pathway` — Reactome pathway lookup. Spec §4.17.

Three input modes, one trimmed output shape:

* ``identifier_type='pathway_id'`` — direct ``/data/query/{stId}``.
  Returns the full record projected onto ``{stable_id, name,
  species, summary, go_biological_process, literature_references,
  figures, release_date}``.
* ``identifier_type='uniprot'`` — species-filtered
  ``/data/mapping/UniProt/{acc}/pathways`` — returns a list of
  ``pathways: [{stable_id, name, species}]`` for that protein.
* ``identifier_type='gene_symbol'`` — ``/search/query`` with strict
  species filtering by default. ``cross_species=True`` relaxes the
  filter and surfaces ``candidate_pathways`` with species context
  for caller disambiguation (same pattern as ``bio_fetch_gene``
  and ``bio_fetch_compound``).

Gene-symbol search returns pathways whose ``species`` list includes
the requested species (Reactome stamps each entry with a species
list, not a single value — some pathways are multi-species by
projection). Match is case-insensitive; ``Homo sapiens`` ≠ ``homo
sapiens`` at Reactome but we normalise.

Literature-reference trimming: each entry becomes ``{pmid, title,
journal, year, url}`` — just enough that a follow-up
``bio_fetch_paper_fulltext`` call can resolve the citation.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from bioinformatics_mcp.clients.reactome import ReactomeClient
from bioinformatics_mcp.utils.errors import (
    AccessionNotFound,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)


async def bio_fetch_pathway(
    identifier: str,
    identifier_type: Literal["pathway_id", "gene_symbol", "uniprot"],
    *,
    species: str = "Homo sapiens",
    cross_species: bool = False,
    client: ReactomeClient,
) -> dict[str, Any]:
    """Look up Reactome pathway data, honestly."""
    if not identifier or not identifier.strip():
        return error_response(
            "identifier is required.",
            suggestions=[
                "Pass a Reactome stable ID (e.g. 'R-HSA-109581'),",
                "a gene symbol (e.g. 'TP53'),",
                "or a UniProt accession (e.g. 'P04637').",
            ],
        )

    ident = identifier.strip()

    try:
        if identifier_type == "pathway_id":
            return await _lookup_by_pathway_id(ident, client=client)
        if identifier_type == "uniprot":
            return await _lookup_by_uniprot(
                ident, species=species, client=client
            )
        if identifier_type == "gene_symbol":
            return await _lookup_by_gene_symbol(
                ident,
                species=species,
                cross_species=cross_species,
                client=client,
            )
    except RateLimitExceeded:
        return error_response(
            "Reactome rate limit exceeded.",
            suggestions=["Retry after a short delay."],
        )
    except ExternalServiceDown as exc:
        return error_response(
            f"Reactome is unreachable: {exc.reason}.",
            suggestions=[f"Check service status at {exc.status_url}."],
        )

    return error_response(
        f"identifier_type must be 'pathway_id', 'gene_symbol', or 'uniprot'; "
        f"got {identifier_type!r}."
    )


# ---- pathway_id path ---------------------------------------------------


async def _lookup_by_pathway_id(
    stable_id: str, *, client: ReactomeClient
) -> dict[str, Any]:
    try:
        record = await client.query_pathway(stable_id)
    except AccessionNotFound:
        return {
            "status": "not_found",
            "message": f"Pathway '{stable_id}' not found in Reactome.",
        }
    return {"status": "found", "pathway": _trim_pathway_record(record)}


def _trim_pathway_record(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse a full Reactome /data/query record into tool output shape."""
    go = record.get("goBiologicalProcess") or {}
    literature = [_trim_reference(r) for r in (record.get("literatureReference") or [])]
    figures = [
        {
            "display_name": fig.get("displayName"),
            "url": fig.get("url"),
        }
        for fig in (record.get("figure") or [])
    ]
    return {
        "stable_id": record.get("stId"),
        "name": record.get("displayName") or (record.get("name") or [None])[0],
        "species": record.get("speciesName"),
        "summary": record.get("summation") or None,
        "go_biological_process": (
            {
                "accession": go.get("accession"),
                "name": go.get("name"),
                "definition": go.get("definition"),
            }
            if go
            else None
        ),
        "literature_references": literature,
        "figures": figures,
        "release_date": record.get("releaseDate"),
    }


def _trim_reference(ref: dict[str, Any]) -> dict[str, Any]:
    pmid = ref.get("pubMedIdentifier")
    return {
        "pmid": str(pmid) if pmid is not None else None,
        "title": ref.get("title"),
        "journal": ref.get("journal"),
        "year": ref.get("year"),
        "url": ref.get("url"),
    }


# ---- uniprot path ------------------------------------------------------


async def _lookup_by_uniprot(
    accession: str, *, species: str, client: ReactomeClient
) -> dict[str, Any]:
    taxon = _species_to_taxon(species)
    try:
        pathways = await client.mapping_to_pathways(
            resource="UniProt", identifier=accession, species_taxon=taxon
        )
    except AccessionNotFound:
        return {
            "status": "not_found",
            "message": (
                f"UniProt accession '{accession}' has no Reactome pathways "
                f"for species '{species}'."
            ),
        }
    trimmed = [_trim_pathway_summary(p) for p in pathways]
    return {
        "status": "found",
        "count": len(trimmed),
        "pathways": trimmed,
    }


def _trim_pathway_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "stable_id": p.get("stId"),
        "name": p.get("displayName") or (p.get("name") or [None])[0],
        "species": p.get("speciesName"),
    }


# ---- gene_symbol path --------------------------------------------------


async def _lookup_by_gene_symbol(
    symbol: str,
    *,
    species: str,
    cross_species: bool,
    client: ReactomeClient,
) -> dict[str, Any]:
    groups = await client.search_pathways(
        query=symbol, species=None if cross_species else species
    )
    all_entries: list[dict[str, Any]] = []
    for g in groups:
        all_entries.extend(g.get("entries") or [])
    if not all_entries:
        return {
            "status": "not_found",
            "message": (
                f"No Reactome pathways found for gene symbol '{symbol}'"
                + ("." if cross_species else f" in species '{species}'.")
            ),
        }

    if cross_species:
        # Collect distinct species — if more than one, return candidates
        # for disambiguation (gene-tool pattern). If only one species,
        # behave as if cross_species had been False.
        species_seen = {
            s
            for e in all_entries
            for s in (e.get("species") or [])
        }
        if len(species_seen) > 1:
            return {
                "status": "ambiguous",
                "candidate_pathways": [
                    _trim_search_entry(e) for e in all_entries
                ],
                "disambiguation_hint": (
                    "Multiple species carry this gene symbol in Reactome. "
                    "Re-query with identifier_type='pathway_id' and a specific "
                    "stable_id, or pass species='<scientific name>' to filter."
                ),
            }

    # Strict mode (or cross_species with only one species): filter to
    # matching species.
    wanted = species.lower()
    filtered = [
        e
        for e in all_entries
        if any(s.lower() == wanted for s in (e.get("species") or []))
    ]
    if not filtered:
        return {
            "status": "not_found",
            "message": (
                f"No Reactome pathways found for gene symbol '{symbol}' "
                f"in species '{species}'."
            ),
        }
    return {
        "status": "found",
        "count": len(filtered),
        "pathways": [_trim_search_entry(e) for e in filtered],
    }


def _trim_search_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # ``name`` from /search comes HTML-highlighted; strip the <span>.
    name = entry.get("name", "")
    name_clean = _strip_html_tags(name)
    return {
        "stable_id": entry.get("stId"),
        "name": name_clean,
        "species": entry.get("species") or [],
    }


# ---- helpers -----------------------------------------------------------


def _strip_html_tags(s: str) -> str:
    """Remove Reactome's search-result highlighting ``<span>`` wrappers."""
    return re.sub(r"<[^>]+>", "", s)


_SPECIES_TAXON_MAP: dict[str, int] = {
    "homo sapiens": 9606,
    "human": 9606,
    "mus musculus": 10090,
    "mouse": 10090,
    "rattus norvegicus": 10116,
    "rat": 10116,
    "felis catus": 9685,
    "cat": 9685,
}


def _species_to_taxon(species: str) -> int | None:
    return _SPECIES_TAXON_MAP.get(species.strip().lower())
