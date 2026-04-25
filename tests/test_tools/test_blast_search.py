"""Unit + integration tests for ``bio_blast_search`` (spec §4.6).

Tests are organised in three layers:

1. Pure parsers (QBlastInfo → RID/RTOE/Status) — no I/O, no mocks.
2. ``NCBIClient`` BLAST methods — respx mocks the Blast.cgi endpoint and
   sequences submit / poll / fetch responses with fast intervals so the
   polling loop runs in milliseconds.
3. ``bio_blast_search`` tool — exercises the full pipeline through the
   tool boundary with a stub client.

The integration test at the end is gated on ``RUN_INTEGRATION=1`` and
hits the real NCBI BLAST URL API. Those queries can take several minutes
during peak hours; the test sets ``max_wait_seconds=900`` to give NCBI
enough head-room.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from grounded_bio_mcp.clients.ncbi import (
    BLAST_BASE_URL,
    NCBIClient,
    _parse_qblast_info,
)
from grounded_bio_mcp.config import Settings
from grounded_bio_mcp.tools.blast_search import bio_blast_search
from grounded_bio_mcp.utils.errors import JobFailed, JobTimeoutError

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
async def ncbi():
    """A real NCBIClient — respx intercepts its HTTP at the transport layer."""
    settings = Settings()
    client = NCBIClient(settings)
    try:
        yield client
    finally:
        await client.aclose()


# ---- pure parsers --------------------------------------------------------


SUBMIT_RESPONSE_HTML = """
<html>
  <body>
    <p>... irrelevant page chrome ...</p>
    <!--QBlastInfoBegin
        RID = YT52BZW4014
        RTOE = 15
    QBlastInfoEnd
    -->
  </body>
</html>
"""


STATUS_WAITING_HTML = """
<html><body>
                QBlastInfoBegin
\t                Status=WAITING
                QBlastInfoEnd
                -->
</body></html>
"""

STATUS_READY_HTML = """
<html><body>
<!--
                QBlastInfoBegin
\t                Status=READY
                QBlastInfoEnd
-->
</body></html>
"""


def test_parse_qblast_info_extracts_rid_and_rtoe() -> None:
    """Submit-response parser must pick RID + RTOE out of the comment block
    even when the surrounding HTML is noisy. RID is the canonical job id;
    RTOE is the server's estimated time of execution in seconds."""
    info = _parse_qblast_info(SUBMIT_RESPONSE_HTML)
    assert info.get("RID") == "YT52BZW4014", f"missing RID; got {info!r}"
    assert info.get("RTOE") == "15", f"missing RTOE; got {info!r}"


def test_parse_qblast_info_extracts_status_under_messy_whitespace() -> None:
    """Status responses use mixed tabs / spaces around 'Status=...' and
    sometimes drop the surrounding HTML comment markers — the parser
    must tolerate both."""
    waiting = _parse_qblast_info(STATUS_WAITING_HTML)
    assert waiting.get("Status") == "WAITING", f"WAITING not parsed; got {waiting!r}"
    ready = _parse_qblast_info(STATUS_READY_HTML)
    assert ready.get("Status") == "READY", f"READY not parsed; got {ready!r}"


def test_parse_qblast_info_returns_empty_when_no_block_present() -> None:
    """Defensive — pages without a QBlastInfo block (e.g. NCBI maintenance
    page) yield an empty dict, never raise. Callers handle the missing
    keys."""
    assert _parse_qblast_info("<html>NCBI is undergoing maintenance.</html>") == {}


# ---- NCBIClient BLAST methods -------------------------------------------


@respx.mock
async def test_blast_submit_returns_rid_and_rtoe(ncbi) -> None:
    """``blast_submit`` posts a ``CMD=Put`` form to /Blast.cgi and returns
    the (RID, RTOE) tuple parsed from the response's QBlastInfo block."""
    respx.post(f"{BLAST_BASE_URL}/Blast.cgi").mock(
        return_value=httpx.Response(200, text=SUBMIT_RESPONSE_HTML)
    )
    rid, rtoe = await ncbi.blast_submit(
        program="blastp",
        database="swissprot",
        query="MFVNQHLCG",
        max_hits=5,
        e_value=10.0,
    )
    assert rid == "YT52BZW4014"
    assert rtoe == 15


@respx.mock
async def test_blast_submit_raises_when_qblast_info_missing(ncbi) -> None:
    """If NCBI returns a 200 page that doesn't carry the QBlastInfo block
    (e.g. a maintenance notice or unexpected error page), the client must
    fail loud rather than return an empty RID — callers depend on this
    invariant for downstream polling."""
    respx.post(f"{BLAST_BASE_URL}/Blast.cgi").mock(
        return_value=httpx.Response(200, text="<html>down for maintenance</html>")
    )
    with pytest.raises(JobFailed):
        await ncbi.blast_submit(
            program="blastp",
            database="swissprot",
            query="MFVNQHLCG",
            max_hits=5,
            e_value=10.0,
        )


@respx.mock
async def test_blast_status_returns_each_state(ncbi) -> None:
    """The status endpoint can return WAITING, READY, UNKNOWN, or FAILED.
    The client returns each verbatim — interpretation is deferred to the
    polling loop and the tool layer."""
    respx.get(f"{BLAST_BASE_URL}/Blast.cgi").mock(
        side_effect=[
            httpx.Response(200, text=STATUS_WAITING_HTML),
            httpx.Response(200, text=STATUS_READY_HTML),
        ]
    )
    assert await ncbi.blast_status("YT52BZW4014") == "WAITING"
    assert await ncbi.blast_status("YT52BZW4014") == "READY"


@respx.mock
async def test_blast_fetch_result_returns_parsed_json2_dict(ncbi) -> None:
    """``blast_fetch_result`` GETs CMD=Get with FORMAT_TYPE=JSON2_S and
    returns the decoded JSON dict (BlastOutput2 wrapper). Callers extract
    hits from ``BlastOutput2[0].report.results.search.hits``."""
    sample = {
        "BlastOutput2": [{"report": {"results": {"search": {"hits": []}}}}]
    }
    respx.get(f"{BLAST_BASE_URL}/Blast.cgi").mock(
        return_value=httpx.Response(
            200,
            json=sample,
            headers={"content-type": "application/json"},
        )
    )
    result = await ncbi.blast_fetch_result("YT52BZW4014")
    assert "BlastOutput2" in result
    assert result["BlastOutput2"][0]["report"]["results"]["search"]["hits"] == []


@respx.mock
async def test_blast_run_composite_submit_poll_fetch(ncbi) -> None:
    """End-to-end through ``blast_run``: submit → WAITING → READY → fetch
    JSON. Polling uses tiny intervals so the test runs in milliseconds."""
    sample = {"BlastOutput2": [{"report": {"results": {"search": {"hits": []}}}}]}

    blast_route = respx.route(
        method__in=["GET", "POST"],
        host="blast.ncbi.nlm.nih.gov",
    )
    blast_route.mock(
        side_effect=[
            httpx.Response(200, text=SUBMIT_RESPONSE_HTML),  # POST submit
            httpx.Response(200, text=STATUS_WAITING_HTML),  # GET status #1
            httpx.Response(200, text=STATUS_READY_HTML),    # GET status #2
            httpx.Response(200, json=sample),                # GET result
        ]
    )

    out = await ncbi.blast_run(
        program="blastp",
        database="swissprot",
        query="MFVNQHLCG",
        max_hits=5,
        e_value=10.0,
        initial_interval=0.001,
        max_interval=0.001,
        max_wait_seconds=5.0,
    )
    assert out["BlastOutput2"][0]["report"]["results"]["search"]["hits"] == []


@respx.mock
async def test_blast_run_raises_on_failed_status(ncbi) -> None:
    """A FAILED status from NCBI surfaces as JobFailed so the tool layer
    can render an error_response with actionable suggestions."""
    failed_html = "<html>QBlastInfoBegin Status=FAILED QBlastInfoEnd</html>"
    blast_route = respx.route(
        method__in=["GET", "POST"], host="blast.ncbi.nlm.nih.gov"
    )
    blast_route.mock(
        side_effect=[
            httpx.Response(200, text=SUBMIT_RESPONSE_HTML),
            httpx.Response(200, text=failed_html),
        ]
    )
    with pytest.raises(JobFailed):
        await ncbi.blast_run(
            program="blastp",
            database="swissprot",
            query="MFVNQHLCG",
            max_hits=5,
            e_value=10.0,
            initial_interval=0.001,
            max_interval=0.001,
            max_wait_seconds=5.0,
        )


# ---- bio_blast_search tool layer ----------------------------------------


def _make_hit(*, num: int, accession: str, sciname: str, evalue: float,
              extra_descriptions: int = 0) -> dict:
    """Build a synthetic BLAST hit dict matching the JSON2_S shape."""
    descriptions = [{
        "id": f"sp|{accession}.1|",
        "accession": accession,
        "title": f"Synthetic protein from {sciname}",
        "taxid": 9606 + num,
        "sciname": sciname,
    }]
    for i in range(extra_descriptions):
        descriptions.append({
            "id": f"tr|FAKE{accession}{i}|",
            "accession": f"FAKE{accession}{i}",
            "title": f"Identical sequence {i} from {sciname}",
            "taxid": 9606 + num,
            "sciname": sciname,
        })
    return {
        "num": num,
        "description": descriptions,
        "len": 100,
        "hsps": [{
            "num": 1,
            "bit_score": 100.0 - num * 5,
            "score": 200,
            "evalue": evalue,
            "identity": 30,
            "positive": 32,
            "query_from": 1,
            "query_to": 31,
            "hit_from": 10,
            "hit_to": 40,
            "align_len": 31,
            "gaps": 0,
            "qseq": "MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
            "hseq": "MFVNQHLCGSHLVEALYLVCGERGFFYTPK_",
            "midline": "||||||||||||||||||||||||||||||| ",
        }],
    }


def _make_blast_result(hits: list[dict], query_id: str = "Q1", query_len: int = 31) -> dict:
    return {
        "BlastOutput2": [{
            "report": {
                "program": "blastp",
                "version": "BLASTP 2.17.0+",
                "search_target": {"db": "swissprot"},
                "params": {},
                "results": {
                    "search": {
                        "query_id": query_id,
                        "query_title": "test query",
                        "query_len": query_len,
                        "hits": hits,
                        "stat": {},
                    }
                },
            }
        }]
    }


class _FakeNCBIClient:
    """Stand-in that records inputs and returns a canned blast_run output."""

    def __init__(self, canned_result: dict) -> None:
        self.canned_result = canned_result
        self.calls: list[dict] = []

    async def blast_run(self, **kwargs):
        self.calls.append(kwargs)
        return self.canned_result


async def test_bio_blast_search_formats_hits_per_spec() -> None:
    """Tool reshapes the JSON2_S structure into the spec §4.6 hit shape:
    each hit carries accession / description / organism / e_value /
    bit_score / identity_pct / query_coverage_pct / alignment positions.
    Top-5 hits include the alignment strings; the rest don't."""
    hits = [
        _make_hit(num=i, accession=f"P0000{i}", sciname=f"Species {i}", evalue=10**-i)
        for i in range(1, 8)
    ]
    fake = _FakeNCBIClient(_make_blast_result(hits))
    out = await bio_blast_search(
        query_sequence="MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
        program="blastp",
        database="swissprot",
        organism_filter=None,
        max_hits=10,
        e_value=10.0,
        max_wait_seconds=None,
        client=fake,
    )
    assert out["hit_count"] == 7
    assert out["query_length"] == 31
    assert out["program"] == "blastp"
    assert out["database"] == "swissprot"
    assert isinstance(out["hits"], list)
    assert len(out["hits"]) == 7

    first = out["hits"][0]
    expected_keys = {
        "accession", "description", "organism", "taxid",
        "e_value", "bit_score", "identity_pct", "query_coverage_pct",
        "query_from", "query_to", "hit_from", "hit_to", "align_length",
        "identical_sequence_count",
    }
    assert expected_keys.issubset(first.keys()), (
        f"missing keys: {expected_keys - first.keys()}"
    )
    assert first["accession"] == "P00001"
    assert first["organism"] == "Species 1"
    assert first["identity_pct"] == pytest.approx(96.77, abs=0.01)  # 30/31
    assert first["query_coverage_pct"] == pytest.approx(100.0, abs=0.01)

    # Top 5 hits inline alignment strings; hits 6 + 7 do not.
    for i in range(5):
        assert "qseq" in out["hits"][i], f"hit #{i} should have qseq"
    for i in range(5, 7):
        assert "qseq" not in out["hits"][i], f"hit #{i} should NOT have qseq"


async def test_bio_blast_search_identical_sequence_count_from_description_list() -> None:
    """Multiple description records on a single hit means that the hit's
    sequence is identical across N database entries. The tool surfaces
    that count so callers don't have to count description list length
    themselves."""
    hits = [
        _make_hit(num=1, accession="P00001", sciname="Homo sapiens",
                  evalue=1e-10, extra_descriptions=2),  # canonical + 2 identicals
    ]
    fake = _FakeNCBIClient(_make_blast_result(hits))
    out = await bio_blast_search(
        query_sequence="MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
        program="blastp",
        database="swissprot",
        organism_filter=None,
        max_hits=10,
        e_value=10.0,
        max_wait_seconds=None,
        client=fake,
    )
    assert out["hits"][0]["identical_sequence_count"] == 3


async def test_bio_blast_search_empty_hits_is_valid() -> None:
    """A truly novel sequence with no homologues legitimately returns
    zero hits. The tool must return a successful empty result, not an
    error — empty hits are signal, not failure."""
    fake = _FakeNCBIClient(_make_blast_result([]))
    out = await bio_blast_search(
        query_sequence="MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
        program="blastp",
        database="swissprot",
        organism_filter=None,
        max_hits=10,
        e_value=10.0,
        max_wait_seconds=None,
        client=fake,
    )
    assert out["hit_count"] == 0
    assert out["hits"] == []
    assert "error" not in out


async def test_bio_blast_search_max_wait_seconds_passes_through() -> None:
    """The caller's max_wait_seconds (or default 600) flows to
    client.blast_run; the cap of 1800s is enforced at the schema layer.
    """
    fake = _FakeNCBIClient(_make_blast_result([]))
    await bio_blast_search(
        query_sequence="MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
        program="blastp",
        database="swissprot",
        organism_filter=None,
        max_hits=10,
        e_value=10.0,
        max_wait_seconds=900,
        client=fake,
    )
    assert fake.calls[0]["max_wait_seconds"] == 900
    # Default falls through when None.
    fake2 = _FakeNCBIClient(_make_blast_result([]))
    await bio_blast_search(
        query_sequence="MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
        program="blastp",
        database="swissprot",
        organism_filter=None,
        max_hits=10,
        e_value=10.0,
        max_wait_seconds=None,
        client=fake2,
    )
    assert fake2.calls[0]["max_wait_seconds"] == 600


async def test_bio_blast_search_handles_unknown_status_with_actionable_error() -> None:
    """UNKNOWN status from NCBI is genuinely ambiguous (RID expired vs
    never existed). The tool surfaces both interpretations in the
    error_response so the caller gets actionable guidance."""

    class _FailingClient:
        async def blast_run(self, **_kwargs):
            raise JobFailed(service="BLAST", job_id="EXPIRED123", status="UNKNOWN")

    out = await bio_blast_search(
        query_sequence="MFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
        program="blastp",
        database="swissprot",
        organism_filter=None,
        max_hits=10,
        e_value=10.0,
        max_wait_seconds=None,
        client=_FailingClient(),
    )
    assert "error" in out, f"expected error_response, got {out!r}"
    suggestions_text = " ".join(out.get("suggestions", []))
    # Both interpretations should be mentioned so the caller knows what
    # action to take.
    assert "expired" in suggestions_text.lower(), (
        f"expected 'expired' in suggestions: {suggestions_text!r}"
    )


@respx.mock
async def test_blast_run_raises_timeout_when_max_wait_exceeded(ncbi) -> None:
    """Wall-clock timeout maps to JobTimeoutError. The status URL is
    surfaced in the exception so callers can give users a way to
    reconnect with the orphaned job."""
    blast_route = respx.route(
        method__in=["GET", "POST"], host="blast.ncbi.nlm.nih.gov"
    )
    # Respond with submit, then keep returning WAITING forever.
    blast_route.mock(
        side_effect=[httpx.Response(200, text=SUBMIT_RESPONSE_HTML)]
        + [httpx.Response(200, text=STATUS_WAITING_HTML)] * 50
    )
    with pytest.raises(JobTimeoutError):
        await ncbi.blast_run(
            program="blastp",
            database="swissprot",
            query="MFVNQHLCG",
            max_hits=5,
            e_value=10.0,
            initial_interval=0.001,
            max_interval=0.001,
            max_wait_seconds=0.05,
        )
