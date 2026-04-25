"""Shared client plumbing — spec §5, §7.1, §7.2.

Every service client (ncbi.py, uniprot.py, etc.) is built on top of
`RateLimitedClient` from `utils/rate_limit`. This module re-exports it
and pins per-service rate-limit parameters from spec §7.1 so that
individual client modules don't need to re-type the numbers (and risk
drifting from the table).

Tenacity-based retry on transient 429/503 is layered here rather than
inside `utils/rate_limit` so the pure rate-limit primitive stays testable
in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from grounded_bio_mcp.utils.errors import (
    AccessionNotFound,
    BioMCPError,
    ExternalServiceDown,
    RateLimitExceeded,
    error_response,
)
from grounded_bio_mcp.utils.rate_limit import RateLimitedClient

__all__ = [
    "AccessionNotFound",
    "BioMCPError",
    "ExternalServiceDown",
    "RATE_LIMITS",
    "RateLimitExceeded",
    "RateLimitedClient",
    "ServiceRateLimit",
    "error_response",
]


@dataclass(frozen=True, slots=True)
class ServiceRateLimit:
    """Pair of concurrency cap and minimum inter-request interval (seconds).

    Values mirror the table in spec §7.1. Per-service client modules look
    up their own entry in `RATE_LIMITS` rather than hardcoding numbers.
    """

    max_concurrent: int
    min_interval_s: float


# Spec §7.1. Keep keys stable — downstream modules use these as service IDs.
# NCBI gets two entries because the limit depends on whether NCBI_API_KEY is
# present; `ncbi.py` will pick the right one at construction time.
RATE_LIMITS: dict[str, ServiceRateLimit] = {
    "ncbi_with_key": ServiceRateLimit(max_concurrent=10, min_interval_s=0.1),
    "ncbi_no_key": ServiceRateLimit(max_concurrent=3, min_interval_s=0.34),
    "uniprot": ServiceRateLimit(max_concurrent=5, min_interval_s=0.2),
    "ebi": ServiceRateLimit(max_concurrent=3, min_interval_s=0.5),
    "ensembl": ServiceRateLimit(max_concurrent=15, min_interval_s=0.07),
    "alphafold": ServiceRateLimit(max_concurrent=5, min_interval_s=0.2),
    "rcsb": ServiceRateLimit(max_concurrent=10, min_interval_s=0.1),
    "chembl": ServiceRateLimit(max_concurrent=3, min_interval_s=0.34),
    "pubchem": ServiceRateLimit(max_concurrent=5, min_interval_s=0.2),
    "europepmc": ServiceRateLimit(max_concurrent=10, min_interval_s=0.1),
    "reactome": ServiceRateLimit(max_concurrent=5, min_interval_s=0.2),
    "string": ServiceRateLimit(max_concurrent=3, min_interval_s=1.0),
}
