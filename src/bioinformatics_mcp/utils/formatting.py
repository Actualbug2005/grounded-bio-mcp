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
