"""Shared Pydantic input/output models — spec §5.

Per-tool input schemas live in their own tool modules (e.g.
`FetchSequenceInput` in `tools/fetch_sequence.py`). This file hosts only
the cross-tool primitives reused across multiple schemas, so that changes
to a shared shape don't require touching every tool.

Populated incrementally as tools are implemented. For now it contains the
`response_format` alias used by every tool's input model.
"""

from __future__ import annotations

from typing import Literal

ResponseFormat = Literal["json", "markdown"]
"""Every tool accepts this to pick its output rendering. See utils/formatting.py."""
