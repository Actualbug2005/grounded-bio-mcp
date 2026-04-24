"""Pytest config — integration-test opt-in gate (spec §10.2).

Tests marked ``@pytest.mark.integration`` hit real upstream APIs (NCBI,
UniProt, RCSB, EBI AlphaFold, etc.). They are skipped by default so the
normal ``pytest`` run stays fast, offline, and deterministic. Enable them
by setting ``RUN_INTEGRATION=1`` in the environment:

    RUN_INTEGRATION=1 pytest -m integration

The ``integration`` marker itself is registered in ``pyproject.toml``; this
module only controls whether marked tests are collected-then-run, or
collected-then-skipped.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001 — part of pytest hook signature
    items: list[pytest.Item],
) -> None:
    """Skip ``integration``-marked tests unless RUN_INTEGRATION=1 is set."""
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip_marker = pytest.mark.skip(
        reason="integration test; set RUN_INTEGRATION=1 to run against real APIs",
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)
