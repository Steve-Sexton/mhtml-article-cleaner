"""Boundary and edge-case tests for sanitize_filename."""

from __future__ import annotations

import pytest

from clean_mhtml_article import WINDOWS_RESERVED_NAMES, sanitize_filename


INVALID_WIN_CHARS = set('<>:"/\\|?*')


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hello World", "Hello World.html"),
        ("", "article.html"),
        ("   ", "article.html"),
        ("...", "article.html"),
        ("<<<>>>", "article.html"),
        ("  leading and trailing  ", "leading and trailing.html"),
        ("a / b \\ c : d", "a b c d.html"),
        ("multi   spaces", "multi spaces.html"),
        ("article\u00ae", "article\u00ae.html"),
    ],
)
def test_sanitize_filename_basic(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Non-whitespace control chars are removed outright.
        ("Bad\x07Name\x01Here", "BadNameHere.html"),
        ("trailing\x7f", "trailing.html"),
        ("\x1fleading", "leading.html"),
        # Whitespace control chars collapse to a single space (words preserved).
        ("Hello\tWorld", "Hello World.html"),
        ("Hello\nWorld", "Hello World.html"),
        # A title made entirely of control chars degrades to the default.
        ("\x00\x01\x02", "article.html"),
    ],
)
def test_sanitize_filename_strips_control_characters(raw: str, expected: str) -> None:
    """Control chars are illegal in Windows filenames; stripping them here
    prevents an OSError at write time. Whitespace control chars are retained
    long enough to collapse to single spaces rather than fusing words.
    """
    result = sanitize_filename(raw)
    assert result == expected
    assert not any(ord(c) < 32 or ord(c) == 127 for c in result)


def test_sanitize_filename_respects_max_length() -> None:
    result = sanitize_filename("a" * 500, max_length=100)
    assert len(result) == 100
    assert result.endswith(".html")


def test_sanitize_filename_strips_all_invalid_chars() -> None:
    result = sanitize_filename('bad<>:"/\\|?*name')
    assert not (INVALID_WIN_CHARS & set(result))


@pytest.mark.parametrize("reserved", ["CON", "con", "prn", "NUL", "COM1", "LPT9"])
def test_sanitize_filename_prefixes_windows_reserved_names(reserved: str) -> None:
    """Windows reserved device names cannot be used as filenames."""
    out = sanitize_filename(reserved)
    stem = out.rsplit(".", 1)[0]
    assert stem.upper() not in WINDOWS_RESERVED_NAMES


def test_sanitize_filename_custom_extension_budget() -> None:
    result = sanitize_filename("x" * 50, max_length=20, extension=".htm")
    assert len(result) == 20
    assert result.endswith(".htm")


def test_sanitize_filename_tiny_max_length_degrades_gracefully() -> None:
    result = sanitize_filename("hello", max_length=1)
    assert result.endswith(".html")
