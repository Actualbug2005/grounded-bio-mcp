"""`bio_fetch_bioactivity` — measured drug-target binding/activity data from ChEMBL.

Phase 1, MVP. See spec §4.10.

Annotations: readOnlyHint=True, destructiveHint=False, openWorldHint=True,
idempotentHint=False, title="Fetch ChEMBL Bioactivity".

idempotentHint=False because ChEMBL accepts new assay submissions — the
same query may gain additional rows over time.

**Critical anti-hallucination design (approved 2026-04-24):**

* Default ``min_confidence=7`` — ChEMBL's "direct single protein target
  assigned" threshold. Lowering this lets in assays where the
  assay-to-target mapping is weak; the field is the single most
  important protection against fabricating target attributions.
* **Null-confidence records are ALWAYS excluded**, regardless of
  ``min_confidence`` value. We cannot verify an unscored mapping meets
  any quality bar. The excluded count surfaces in the output as
  ``null_confidence_excluded`` so callers can see how many records
  were dropped. This is a deliberate recall-for-correctness trade-off.

**ChEMBL schema note:** activity records do NOT include
``confidence_score`` — it lives on the joined assay row. The tool
batches a second ChEMBL query against ``/assay.json`` to enrich each
returned activity with its assay's confidence score + description.
Same pattern for target enrichment (``target_components.accession``
for UniProt is only accessible via ``/target.json``).

**ChEMBL server-side filter is leaky — verified 2026-04-24.** Passing
``confidence_score__gte=7`` on the activity query should restrict to
assays with cs ≥ 7, but ChEMBL has returned activities whose joined
assay has cs=0 in observed responses. Client-side re-filtering is
therefore mandatory, not belt-and-braces. The tool re-enforces
``min_confidence`` after enrichment and surfaces the below-threshold
drop count as ``below_threshold_excluded`` alongside
``null_confidence_excluded``.

**Query direction:**

* ``query_type='compound'``: pass a molecule ChEMBL ID; returns its
  targets (each with UniProt accession) at the configured confidence.
* ``query_type='target'``: pass a target ChEMBL ID *or* a UniProt
  accession; UniProt is resolved to the single-protein target_chembl_id
  via ``/target.json?target_components__accession=…`` (first SINGLE
  PROTEIN hit wins), then the activity query runs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from grounded_bio_mcp.clients.chembl import ChEMBLClient
from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    BioMCPError,
    error_response,
)

logger = logging.getLogger(__name__)

ActivityType = Literal["IC50", "Ki", "Kd", "EC50", "AC50", "Potency"]
QueryType = Literal["compound", "target"]

_DEFAULT_ACTIVITY_TYPES: tuple[str, ...] = ("IC50", "Ki", "Kd")

_CHEMBL_ID_RE = re.compile(r"^CHEMBL\d+$")


class FetchBioactivityInput(BaseModel):
    """Input schema — spec §4.10."""

    query_type: QueryType
    identifier: str = Field(..., min_length=1, max_length=50)
    activity_types: list[ActivityType] = Field(
        default_factory=lambda: list(_DEFAULT_ACTIVITY_TYPES)
    )
    max_results: int = Field(default=50, ge=1, le=500)
    min_confidence: int = Field(
        default=7,
        ge=0,
        le=9,
        description=(
            "Minimum ChEMBL confidence score for the assay-to-target "
            "mapping. 7 = 'direct single protein target assigned'. "
            "Lowering this admits assays where the target mapping is "
            "weak; 0 admits everything with a score. Null-confidence "
            "records are ALWAYS excluded regardless of this value."
        ),
    )
    offset: int = Field(default=0, ge=0)


async def _resolve_target(
    client: ChEMBLClient, identifier: str
) -> str | None:
    """Resolve a target identifier (CHEMBL or UniProt) to a target_chembl_id."""
    if _CHEMBL_ID_RE.match(identifier):
        return identifier
    # Treat anything else as a UniProt accession.
    results = await client._batch_fetch(  # pylint: disable=protected-access
        "/target.json",
        param_name="target_components__accession",
        ids=[identifier],
        collection_key="targets",
    )
    # Prefer the SINGLE PROTEIN target — it's the mapping users mean.
    for t in results:
        if t.get("target_type") == "SINGLE PROTEIN":
            return t.get("target_chembl_id")
    if results:
        return results[0].get("target_chembl_id")
    return None


_NULL_CONFIDENCE = "null"
_BELOW_THRESHOLD = "below"
_INCLUDED = "ok"


def _merge_activity_row(
    activity: dict[str, Any],
    assay_by_id: dict[str, dict[str, Any]],
    target_by_id: dict[str, dict[str, Any]],
    min_confidence: int,
) -> tuple[str, dict[str, Any] | None]:
    """Project one activity + joined assay + joined target into the tool's
    flat output row.

    Returns a ``(status, row_or_none)`` pair where ``status`` is one of
    ``_NULL_CONFIDENCE``, ``_BELOW_THRESHOLD``, or ``_INCLUDED``. The
    first two are the anti-hallucination exclusions; the caller uses
    the status to count drops by reason.
    """
    assay_id = activity.get("assay_chembl_id", "")
    assay = assay_by_id.get(assay_id) or {}
    confidence = assay.get("confidence_score")
    if confidence is None:
        return _NULL_CONFIDENCE, None
    confidence_int = int(confidence)
    if confidence_int < min_confidence:
        return _BELOW_THRESHOLD, None
    target_id = activity.get("target_chembl_id", "")
    target = target_by_id.get(target_id) or {}
    components = target.get("target_components") or []
    uniprot = next(
        (c.get("accession") for c in components if c.get("accession")), None
    )
    row = {
        "compound_chembl_id": activity.get("molecule_chembl_id"),
        "compound_pref_name": activity.get("molecule_pref_name"),
        "target_chembl_id": activity.get("target_chembl_id"),
        "target_pref_name": activity.get("target_pref_name"),
        "target_organism": activity.get("target_organism"),
        "target_uniprot_accession": uniprot,
        "activity_type": activity.get("standard_type"),
        "standard_value": activity.get("standard_value"),
        "standard_units": activity.get("standard_units"),
        "relation": activity.get("standard_relation"),
        "assay_chembl_id": activity.get("assay_chembl_id"),
        "assay_description": activity.get("assay_description"),
        "assay_type": activity.get("assay_type"),
        "confidence_score": confidence_int,
        "confidence_description": assay.get("confidence_description"),
        "document_chembl_id": activity.get("document_chembl_id"),
        "document_year": activity.get("document_year"),
    }
    return _INCLUDED, row


async def bio_fetch_bioactivity(
    *,
    query_type: str,
    identifier: str,
    activity_types: list[str] | None,
    max_results: int,
    min_confidence: int,
    offset: int,
    chembl: ChEMBLClient,
) -> dict[str, Any]:
    """Fetch measured bioactivity data from ChEMBL — spec §4.10."""
    try:
        params = FetchBioactivityInput.model_validate(
            {
                "query_type": query_type,
                "identifier": identifier,
                "activity_types": activity_types
                or list(_DEFAULT_ACTIVITY_TYPES),
                "max_results": max_results,
                "min_confidence": min_confidence,
                "offset": offset,
            }
        )
    except ValidationError as exc:
        return error_response(
            f"Invalid input: {exc.errors()[0]['msg']}",
            suggestions=[
                "query_type must be 'compound' or 'target'.",
                "max_results must be between 1 and 500.",
                "min_confidence must be between 0 and 9.",
            ],
        )

    # Resolve target identifier if needed.
    resolved_target: str | None = None
    molecule_chembl_id: str | None = None
    if params.query_type == "compound":
        if not _CHEMBL_ID_RE.match(params.identifier):
            return error_response(
                f"Compound identifier {params.identifier!r} is not a ChEMBL ID.",
                suggestions=[
                    "Use bio_fetch_compound first to obtain the ChEMBL ID "
                    "from a name, SMILES, InChI, or PubChem CID.",
                ],
            )
        molecule_chembl_id = params.identifier
    else:
        try:
            resolved_target = await _resolve_target(chembl, params.identifier)
        except BioMCPError as exc:
            return error_response(str(exc))
        if resolved_target is None:
            return error_response(
                f"Could not resolve {params.identifier!r} to a ChEMBL target.",
                suggestions=[
                    "For UniProt accessions, verify the protein is in "
                    "ChEMBL's target dictionary (not every UniProt entry "
                    "has an assigned target_chembl_id).",
                    "Pass a ChEMBL target ID directly (e.g. 'CHEMBL204' "
                    "for thrombin).",
                ],
            )

    # Primary activity query.
    try:
        page = await chembl.list_activities(
            molecule_chembl_id=molecule_chembl_id,
            target_chembl_id=resolved_target,
            activity_types=tuple(params.activity_types),
            min_confidence=params.min_confidence,
            limit=params.max_results,
            offset=params.offset,
        )
    except AccessionNotFound as exc:
        return error_response(
            f"ChEMBL has no record for {exc.accession!r}.",
        )
    except BioMCPError as exc:
        return error_response(str(exc))

    activities_raw: list[dict[str, Any]] = list(page.get("activities") or [])
    page_meta = page.get("page_meta") or {}

    # Batch-enrich with assay confidence + target UniProt.
    assay_ids = sorted({
        a.get("assay_chembl_id", "")
        for a in activities_raw
        if a.get("assay_chembl_id")
    })
    target_ids = sorted({
        a.get("target_chembl_id", "")
        for a in activities_raw
        if a.get("target_chembl_id")
    })

    try:
        assays = await chembl.list_assays_by_ids(assay_ids)
        targets = await chembl.list_targets_by_ids(target_ids)
    except BioMCPError as exc:
        return error_response(str(exc))

    assay_by_id = {a.get("assay_chembl_id", ""): a for a in assays}
    target_by_id = {t.get("target_chembl_id", ""): t for t in targets}

    enriched: list[dict[str, Any]] = []
    null_confidence_excluded = 0
    below_threshold_excluded = 0
    for a in activities_raw:
        status, row = _merge_activity_row(
            a, assay_by_id, target_by_id, params.min_confidence
        )
        if status == _NULL_CONFIDENCE:
            null_confidence_excluded += 1
            continue
        if status == _BELOW_THRESHOLD:
            below_threshold_excluded += 1
            continue
        assert row is not None  # noqa: S101 — exhaustive status handling
        enriched.append(row)

    total_count = int(page_meta.get("total_count") or 0)
    returned = len(enriched) + null_confidence_excluded
    next_offset = (
        params.offset + returned
        if params.offset + returned < total_count
        else None
    )

    output: dict[str, Any] = {
        "query_type": params.query_type,
        "identifier": params.identifier,
        "activities": enriched,
        "page_meta": {
            "total_count": total_count,
            "returned_count": len(enriched),
            "limit": params.max_results,
            "offset": params.offset,
            "truncated": next_offset is not None,
            "next_offset": next_offset,
        },
        "min_confidence_applied": params.min_confidence,
        "activity_types_applied": list(params.activity_types),
        "null_confidence_excluded": null_confidence_excluded,
        "below_threshold_excluded": below_threshold_excluded,
        "see_also": (
            "For compound structural properties (SMILES, InChI, MW, LogP, "
            "clinical phase), use bio_fetch_compound."
        ),
    }
    if resolved_target and resolved_target != params.identifier:
        output["resolved_target_chembl_id"] = resolved_target
    if resolved_target:
        output["resolved_target_chembl_id"] = resolved_target

    return output
