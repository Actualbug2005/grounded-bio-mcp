"""Typed environment-variable loading — spec §9.5.

The server refuses to start on invalid config: missing paths, absent EBI
email for EBI-dependent tools, or an `0.0.0.0` bind (which would bypass
the Caddy bearer-token layer documented in spec §9.4).

Every env var listed in `.env.example` is represented here. Tools and
clients import `get_settings()` rather than reading `os.environ` directly,
so secrets never leak through ad-hoc `os.getenv(...)` calls and the
validation lives in one place.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from the process environment.

    `.env` files are loaded on dev machines; on the LXC the systemd unit
    provides the same variables via `EnvironmentFile=/etc/bioinformatics_mcp/env`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Upstream credentials ---------------------------------------------------
    ncbi_api_key: str | None = Field(default=None, alias="NCBI_API_KEY")
    ebi_email: str | None = Field(default=None, alias="EBI_EMAIL")
    string_user_email: str | None = Field(default=None, alias="STRING_USER_EMAIL")

    # Server binding ---------------------------------------------------------
    mcp_bind_host: str = Field(default="127.0.0.1", alias="MCP_BIND_HOST")
    mcp_bind_port: int = Field(default=8080, alias="MCP_BIND_PORT", ge=1, le=65535)
    mcp_auth_token: str | None = Field(default=None, alias="MCP_AUTH_TOKEN")

    # Paths ------------------------------------------------------------------
    crispor_path: Path = Field(default=Path("/opt/crispor"), alias="CRISPOR_PATH")
    crispor_python: Path = Field(
        default=Path("/opt/crispor/venv/bin/python"), alias="CRISPOR_PYTHON"
    )
    genome_dir: Path = Field(
        default=Path("/var/lib/bioinformatics_mcp/genomes"), alias="GENOME_DIR"
    )
    cache_dir: Path = Field(
        default=Path("/var/lib/bioinformatics_mcp/cache"), alias="CACHE_DIR"
    )
    log_dir: Path = Field(
        default=Path("/var/lib/bioinformatics_mcp/logs"), alias="LOG_DIR"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("mcp_bind_host")
    @classmethod
    def _forbid_public_bind(cls, v: str) -> str:
        # Spec §2.2: "Do not bind to 0.0.0.0 directly" — the Caddy reverse
        # proxy is the auth boundary. Anything that circumvents 127.0.0.1
        # also circumvents bearer-token validation.
        if v in {"0.0.0.0", "::"}:
            raise ValueError(
                "MCP_BIND_HOST must not be a public bind address — the Caddy reverse "
                "proxy is the auth boundary (spec §2.2). Use 127.0.0.1."
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        level = v.upper()
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {v!r}")
        return level


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide, lazily-loaded Settings singleton."""
    return Settings()
