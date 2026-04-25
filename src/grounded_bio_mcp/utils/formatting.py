"""JSON ↔ Markdown response formatting stubs — spec §4, §5.

Every tool takes a `response_format: Literal["json", "markdown"]` parameter.
This module is the shared dispatcher: tools hand it a structured payload
plus a format choice, and get back text ready for an MCP `content[].text`
block.

The Markdown renderers live here rather than inside each tool module so
that shared primitives (tables, sequence blocks, conservation views) can
be reused across related tools. Individual renderers are added lazily as
tool implementations come online.
"""

from __future__ import annotations

import json
from typing import Any, Literal

ResponseFormat = Literal["json", "markdown"]


def format_json(payload: Any) -> str:
    """Pretty-print a Python structure as stable JSON.

    `indent=2` matches the output shape recommended in the mcp-server-dev
    plugin's tool-design guidance — parseable by the model, still readable
    if it renders the text verbatim.
    """
    return json.dumps(payload, indent=2, ensure_ascii=False, default=_fallback)


def format_markdown(payload: Any) -> str:  # pragma: no cover — stub
    """Render `payload` as Markdown. Tool-specific renderers override this.

    The base implementation wraps the JSON in a fenced block so a tool can
    return something useful *before* a dedicated renderer exists. Each
    tool module that opts into Markdown replaces this by passing a renderer
    function to `format_response(...)`.
    """
    return f"```json\n{format_json(payload)}\n```"


def format_response(
    payload: Any,
    response_format: ResponseFormat,
    markdown_renderer: "_MarkdownRenderer | None" = None,
) -> str:
    """Dispatch a payload to JSON or a tool-specific Markdown renderer."""
    if response_format == "json":
        return format_json(payload)
    if markdown_renderer is not None:
        return markdown_renderer(payload)
    return format_markdown(payload)


# Typing helper kept private to avoid enlarging the public surface.
from typing import Callable  # noqa: E402

_MarkdownRenderer = Callable[[Any], str]


def soft_cap_with_url_fallback(
    content: str | bytes,
    *,
    cap_bytes: int,
    fallback_url: str,
    key_prefix: str,
    format_label: str,
    overage_noun: str = "Content",
) -> dict[str, Any]:
    """Inline `content` if its byte size fits `cap_bytes`; otherwise return a
    URL-fallback error fragment.

    Shared across tools that can legitimately produce oversized output:
    PDB coordinates (coordinates), Clustal alignments (alignment),
    InterProScan matches (matches). Caller merges the returned dict into
    its output payload.

    Size is measured in **bytes**:
    - str inputs are encoded UTF-8 for measurement, so multi-byte chars
      are counted correctly
    - bytes inputs are measured directly (no roundtrip, preserves
      non-UTF-8 payloads like raw binary structure files)

    On overflow the error message names the `overage_noun` (so callers
    get "Structure too large…" vs "Alignment too large…"), reports both
    observed size and cap in KB, and points to `fallback_url`.
    """
    size = len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))
    if size > cap_bytes:
        return {
            f"{key_prefix}_error": (
                f"{overage_noun} too large to inline ({_fmt_size(size)}, "
                f"cap {_fmt_size(cap_bytes)}). Fetch directly from {fallback_url}"
            )
        }
    return {
        key_prefix: content,
        f"{key_prefix}_format": format_label,
        f"{key_prefix}_size_bytes": size,
    }


def _fmt_size(n: int) -> str:
    """Human-ish size string; bytes for small values, KB for larger."""
    if n >= 1024:
        return f"{n // 1024} KB ({n} bytes)"
    return f"{n} bytes"


def _fallback(obj: Any) -> Any:
    """json.dumps `default=` — coerce Biopython/Pydantic types to JSON-safe."""
    # Pydantic v2 models expose model_dump(); call it when present.
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    # Sets and similar — convert to sorted list where possible for stability.
    if isinstance(obj, (set, frozenset)):
        try:
            return sorted(obj)
        except TypeError:
            return list(obj)
    return str(obj)
