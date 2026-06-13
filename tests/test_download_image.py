"""Contract tests for MHTMLCleaner.download_and_encode_image.

These tests use a fake urlopen to avoid any real network I/O. They lock in:
- size cap (Content-Length and streaming)
- MIME fallback chain (header -> URL suffix -> magic bytes)
- graceful failure on URLError/TimeoutError/OSError
- scheme restriction
"""

from __future__ import annotations

import io
import ipaddress
import logging
import urllib.error

import pytest

import clean_mhtml_article as cli
from clean_mhtml_article import MAX_DOWNLOAD_BYTES, MHTMLCleaner
from tests.fixtures import PNG_BYTES


class _FakeResponse:
    """Minimal stand-in for the object returned by urllib.request.urlopen."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body = io.BytesIO(body)
        self.headers = headers

    def read(self, amt: int = -1) -> bytes:
        if amt is None or amt < 0:
            return self._body.read()
        return self._body.read(amt)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake(monkeypatch, response_factory) -> list[urllib.request.Request]:
    calls: list = []

    def fake_urlopen(request, timeout=None):
        calls.append((request, timeout))
        return response_factory(request)

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """Keep the SSRF guard offline and fast: IP literals resolve to themselves
    (so the block tests exercise the real range checks) and bare names resolve
    to a public address by default. Individual tests override as needed.
    """
    def fake_resolve(host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return ["93.184.216.34"]  # public default for names
        return [host]                 # IP literal: itself

    monkeypatch.setattr(cli, "_resolve_host_ips", fake_resolve)


def _cleaner() -> MHTMLCleaner:
    return MHTMLCleaner(mhtml_path="unused")


def test_download_uses_header_content_type(monkeypatch) -> None:
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(PNG_BYTES, {"Content-Type": "image/png"}),
    )
    out = _cleaner().download_and_encode_image("https://host/x.bin")
    assert out.startswith("data:image/png;base64,")


def test_download_falls_back_to_url_suffix_when_octet_stream(monkeypatch) -> None:
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(
            b"\x00\x00\x00bytes", {"Content-Type": "application/octet-stream"}
        ),
    )
    out = _cleaner().download_and_encode_image("https://host/x.png")
    # URL suffix says PNG even though magic bytes do not look like PNG.
    assert out.startswith("data:image/png;base64,")


def test_download_falls_back_to_magic_bytes_when_octet_stream_and_unknown_suffix(
    monkeypatch,
) -> None:
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(PNG_BYTES, {"Content-Type": "application/octet-stream"}),
    )
    out = _cleaner().download_and_encode_image("https://host/x.bin")
    # No suffix hint, so magic bytes decide (PNG_BYTES is a real PNG).
    assert out.startswith("data:image/png;base64,")


def test_download_falls_back_to_magic_bytes_when_no_content_type(monkeypatch) -> None:
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(PNG_BYTES, {}),
    )
    out = _cleaner().download_and_encode_image("https://host/x.bin")
    assert out.startswith("data:image/png;base64,")


def test_download_respects_declared_content_length_cap(monkeypatch, caplog) -> None:
    huge = str(MAX_DOWNLOAD_BYTES + 1)
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(PNG_BYTES, {"Content-Length": huge}),
    )
    url = "https://host/huge.png"
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "exceeds max size" in messages


def test_download_aborts_when_streamed_body_exceeds_cap(monkeypatch, caplog) -> None:
    """No Content-Length is present; the loop must stop after the cap is hit."""
    # Build a body one byte over the cap; no Content-Length header so the
    # function has to detect overflow while reading.
    oversized = b"\x00" * (MAX_DOWNLOAD_BYTES + 1)
    _install_fake(monkeypatch, lambda req: _FakeResponse(oversized, {}))
    url = "https://host/streamed.bin"
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "exceeded max size during read" in messages


def test_download_returns_url_on_network_error(monkeypatch, caplog) -> None:
    def boom(req, timeout=None):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    url = "https://host/broken.png"
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Could not download image" in messages


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://host/x", "data:image/png;base64,FAKE", "x"],
)
def test_download_refuses_non_http_scheme_without_opening(
    monkeypatch, url: str
) -> None:
    """Non-http(s) URLs must be returned unchanged and MUST NOT call urlopen."""

    def must_not_open(*args, **kwargs):
        raise AssertionError("urlopen must not be called for non-http URLs")

    monkeypatch.setattr(cli.urllib.request, "urlopen", must_not_open)
    assert _cleaner().download_and_encode_image(url) == url


def test_download_strips_charset_parameter_from_header(monkeypatch) -> None:
    """``image/png; charset=utf-8`` must resolve to a clean ``image/png`` URI.

    Regression: the header value was previously embedded verbatim, which
    produced a malformed ``data:image/png; charset=utf-8;base64,...`` URI.
    """
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(
            PNG_BYTES, {"Content-Type": "image/png; charset=utf-8"}
        ),
    )
    out = _cleaner().download_and_encode_image("https://host/x.bin")
    assert out.startswith("data:image/png;base64,")
    # No header parameters leaked into the MIME portion of the URI.
    assert "charset" not in out.split(",", 1)[0]


def test_download_rejects_non_image_content_type(monkeypatch, caplog) -> None:
    """A server returning ``text/html`` (e.g. an error page with 200) must not
    be embedded as a data URI. The original URL is kept and a warning logged.
    """
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(
            b"<html>error</html>", {"Content-Type": "text/html"}
        ),
    )
    url = "https://host/x.bin"
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Could not determine image MIME" in messages


def test_download_rejects_unsupported_image_content_type(monkeypatch, caplog) -> None:
    """``image/tiff`` is outside the allowlist; URL suffix and magic bytes
    must also fail to recognise it, so the URL is kept unchanged.
    """
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(b"II*\x00tiff", {"Content-Type": "image/tiff"}),
    )
    url = "https://host/x.tiff"
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url


def test_download_rejects_octet_stream_with_charset_parameter(monkeypatch) -> None:
    """``application/octet-stream; charset=binary`` must trigger the fallback
    chain just like a plain ``application/octet-stream`` header.
    """
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(
            PNG_BYTES,
            {"Content-Type": "application/octet-stream; charset=binary"},
        ),
    )
    out = _cleaner().download_and_encode_image("https://host/x.png")
    assert out.startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://10.0.0.5/x.png",                      # private (RFC 1918)
        "http://192.168.1.1/x.png",                   # private
        "http://127.0.0.1/x.png",                     # loopback
        "http://[::1]/x.png",                         # loopback (IPv6)
    ],
)
def test_download_blocks_ssrf_to_internal_ip_literals(
    monkeypatch, caplog, url: str
) -> None:
    """IP-literal hosts in private/loopback/link-local ranges must be refused
    without ever opening a connection.
    """
    def must_not_open(*args, **kwargs):
        raise AssertionError("urlopen must not be called for a blocked host")

    monkeypatch.setattr(cli.urllib.request, "urlopen", must_not_open)
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "private or reserved address" in messages


def test_download_blocks_ssrf_when_hostname_resolves_to_private(
    monkeypatch,
) -> None:
    """A public-looking hostname that resolves to a private IP must be blocked
    (defends against DNS-rebinding-style internal access).
    """
    monkeypatch.setattr(cli, "_resolve_host_ips", lambda host: ["10.1.2.3"])

    def must_not_open(*args, **kwargs):
        raise AssertionError("urlopen must not be called for a blocked host")

    monkeypatch.setattr(cli.urllib.request, "urlopen", must_not_open)
    out = _cleaner().download_and_encode_image("https://internal.example/x.png")
    assert out == "https://internal.example/x.png"


def test_download_allows_public_resolution(monkeypatch) -> None:
    """A host resolving to a public address proceeds to the normal fetch."""
    monkeypatch.setattr(cli, "_resolve_host_ips", lambda host: ["93.184.216.34"])
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(PNG_BYTES, {"Content-Type": "image/png"}),
    )
    out = _cleaner().download_and_encode_image("https://public.example/x.png")
    assert out.startswith("data:image/png;base64,")


def test_download_allows_unresolvable_host(monkeypatch) -> None:
    """A host that fails DNS resolution is not treated as blocked: the fetch is
    attempted and fails naturally. (Locks in the behavior the other download
    tests rely on, where ``host`` never resolves.)
    """
    monkeypatch.setattr(cli, "_resolve_host_ips", lambda host: [])
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(PNG_BYTES, {"Content-Type": "image/png"}),
    )
    out = _cleaner().download_and_encode_image("https://host/x.png")
    assert out.startswith("data:image/png;base64,")


def test_download_rejects_svg_suffix(monkeypatch, caplog) -> None:
    """SVG has no magic signature and the suffix allowlist no longer covers it,
    so a ``.svg`` URL with no trustworthy header is kept unchanged.
    """
    _install_fake(
        monkeypatch,
        lambda req: _FakeResponse(
            b"<svg xmlns='...'/>", {"Content-Type": "application/octet-stream"}
        ),
    )
    url = "https://host/x.svg"
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _cleaner().download_and_encode_image(url)
    assert out == url
