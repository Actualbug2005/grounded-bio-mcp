# ruff: noqa: RUF001  # intentional non-ASCII in multibyte test below
"""Tests for utils/formatting helpers — spec §4 output shape."""

from __future__ import annotations

from grounded_bio_mcp.utils.formatting import soft_cap_with_url_fallback


def test_under_cap_returns_inlined_payload() -> None:
    out = soft_cap_with_url_fallback(
        "HELLO",
        cap_bytes=100,
        fallback_url="https://example.org/f.txt",
        key_prefix="coordinates",
        format_label="mmCIF",
        overage_noun="Structure",
    )
    assert out == {
        "coordinates": "HELLO",
        "coordinates_format": "mmCIF",
        "coordinates_size_bytes": 5,
    }


def test_over_cap_returns_error_with_url() -> None:
    out = soft_cap_with_url_fallback(
        "A" * 200,
        cap_bytes=50,
        fallback_url="https://example.org/big.cif",
        key_prefix="coordinates",
        format_label="mmCIF",
        overage_noun="Structure",
    )
    assert set(out.keys()) == {"coordinates_error"}
    msg = out["coordinates_error"]
    assert "Structure" in msg
    assert "https://example.org/big.cif" in msg
    # Reports both observed size and cap so the caller can act without guessing.
    assert "50" in msg  # cap KB line or cap bytes line


def test_bytes_input_uses_raw_length_not_reencoded() -> None:
    # Non-ASCII bytes: ensures we don't double-encode via str roundtrip.
    blob = b"\xff\xfe\xfd\xfc"
    out = soft_cap_with_url_fallback(
        blob,
        cap_bytes=100,
        fallback_url="http://x/",
        key_prefix="data",
        format_label="bin",
    )
    assert out["data_size_bytes"] == 4
    assert out["data"] == blob


def test_string_with_multibyte_utf8_counts_bytes_not_chars() -> None:
    # Deliberately non-ASCII to catch a bug where we'd accidentally
    # count characters instead of bytes. Encodes to 7 UTF-8 bytes.
    s = "Aα\U0001f600"  # A + Greek alpha (2 bytes) + grinning face (4 bytes)
    out = soft_cap_with_url_fallback(
        s,
        cap_bytes=100,
        fallback_url="http://x/",
        key_prefix="p",
        format_label="txt",
    )
    assert out["p_size_bytes"] == len(s.encode("utf-8"))
