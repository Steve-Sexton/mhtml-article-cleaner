"""Boundary tests for mime_from_url_suffix."""

from __future__ import annotations

import pytest

from clean_mhtml_article import mime_from_url_suffix


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://host/x.jpg", "image/jpeg"),
        ("https://host/x.jpeg", "image/jpeg"),
        ("https://host/x.JPG", "image/jpeg"),
        ("https://host/x.png", "image/png"),
        ("https://host/x.PNG", "image/png"),
        ("https://host/x.gif", "image/gif"),
        ("https://host/x.webp", "image/webp"),
        ("https://host/x.png?sig=abc&ts=1", "image/png"),
        ("https://host/x.jpg#fragment", "image/jpeg"),
    ],
)
def test_recognized_suffixes(url: str, expected: str) -> None:
    assert mime_from_url_suffix(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://host/x",
        "https://host/x.bin",
        "https://host/x.tiff",
        "https://host/x.svg",
        "https://host/",
        "https://host/path/no-extension-here",
    ],
)
def test_unknown_suffix_returns_none(url: str) -> None:
    assert mime_from_url_suffix(url) is None
