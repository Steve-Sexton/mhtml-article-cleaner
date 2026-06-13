"""Contract tests for MHTML boundary handling and part extraction."""

from __future__ import annotations

import base64
import hashlib
import logging

import pytest

from clean_mhtml_article import MHTMLBoundaryError, MHTMLCleaner
from tests.fixtures import PNG_BYTES, make_mhtml


PNG_BYTES_B64 = base64.b64encode(PNG_BYTES)


def _write_and_parse(tmp_path, data: bytes):
    mhtml_path = tmp_path / "in.mhtml"
    mhtml_path.write_bytes(data)
    cleaner = MHTMLCleaner(str(mhtml_path))
    return cleaner.parse_mhtml()


def test_quoted_boundary_parses(tmp_path) -> None:
    data = make_mhtml(boundary='"----=_Part_1"', html='<p>Quoted</p>')
    html, images = _write_and_parse(tmp_path, data)
    assert '<p>Quoted</p>' in html
    assert images == {}


def test_unquoted_boundary_parses(tmp_path) -> None:
    """RFC 2046 permits unquoted boundaries; older regex rejected them."""
    data = make_mhtml(boundary='----=_Part_Unquoted', html='<p>Unquoted</p>')
    html, _ = _write_and_parse(tmp_path, data)
    assert '<p>Unquoted</p>' in html


def test_missing_boundary_raises(tmp_path) -> None:
    mhtml_path = tmp_path / "bad.mhtml"
    mhtml_path.write_bytes(b"This has no boundary at all.\n")
    cleaner = MHTMLCleaner(str(mhtml_path))
    with pytest.raises(MHTMLBoundaryError):
        cleaner.parse_mhtml()


def test_largest_html_part_wins(tmp_path) -> None:
    data = make_mhtml(
        html='<p>small</p>',
        extra_html='<p>' + 'LARGER ' * 100 + '</p>',
    )
    html, _ = _write_and_parse(tmp_path, data)
    assert 'LARGER' in html
    assert '<p>small</p>' not in html


def test_cid_image_extraction(tmp_path) -> None:
    data = make_mhtml(
        html='<p>hi</p>',
        images=[('abc@example', PNG_BYTES, 'cid')],
    )
    _, images = _write_and_parse(tmp_path, data)
    assert 'abc@example' in images
    assert hashlib.sha256(images['abc@example']).hexdigest() == (
        hashlib.sha256(PNG_BYTES).hexdigest()
    )


def test_content_location_image_extraction(tmp_path) -> None:
    data = make_mhtml(
        html='<p>hi</p>',
        images=[('https://example.invalid/a.png', PNG_BYTES, 'loc')],
    )
    _, images = _write_and_parse(tmp_path, data)
    assert 'https://example.invalid/a.png' in images
    assert images['https://example.invalid/a.png'] == PNG_BYTES


def test_lf_only_separators_parse(tmp_path) -> None:
    """Some exporters emit LF-only line endings; parser must handle both."""
    data = make_mhtml(html='<p>LF only</p>', sep=b'\n')
    html, _ = _write_and_parse(tmp_path, data)
    assert '<p>LF only</p>' in html


@pytest.mark.parametrize(
    "kwargs",
    [
        # Lowercase Content-Type header name on HTML + image parts.
        dict(content_type_header='content-type'),
        # Mixed casing on Content-Transfer-Encoding header name.
        dict(cte_header='content-transfer-encoding'),
        # Upper/mixed casing on the MIME value itself.
        dict(html_mime='TEXT/HTML'),
        # Quoted-Printable with non-canonical casing.
        dict(quoted_printable_html=True),
        # Base64 CTE value with non-canonical casing.
        dict(base64_value='Base64'),
    ],
)
def test_mime_headers_parsed_case_insensitively(tmp_path, kwargs) -> None:
    """RFC 2045 header fields are case-insensitive; parser must not care."""
    data = make_mhtml(
        html='<p>ok</p>',
        images=[('abc@example', PNG_BYTES, 'cid')],
        **kwargs,
    )
    html, images = _write_and_parse(tmp_path, data)
    assert '<p>ok</p>' in html
    assert 'abc@example' in images
    assert images['abc@example'] == PNG_BYTES


@pytest.mark.parametrize(
    "bad_body",
    [
        b'@@@not-base64@@@',     # non-alphabet chars mixed with alphabet chars
        b'@@@@',                 # only non-alphabet chars (previously decoded to b'')
        b'####',                 # only non-alphabet chars (second case)
        b'',                     # empty body
        b'\r\n\r\n',             # only whitespace that strips to empty
    ],
)
def test_malformed_base64_image_is_skipped_not_raised(
    tmp_path, caplog, bad_body
) -> None:
    """A single bad base64 part must not abort the whole parse, must not store
    an empty/zero-byte image, and must log a warning.
    """
    raw = (
        b'MIME-Version: 1.0\r\n'
        b'Content-Type: multipart/related; boundary="b"\r\n\r\n'
        b'--b\r\n'
        b'Content-Type: text/html\r\n\r\n'
        b'<p>ok</p>\r\n'
        b'--b\r\n'
        b'Content-Type: image/png\r\n'
        b'Content-Location: bad.png\r\n'
        b'Content-Transfer-Encoding: base64\r\n\r\n'
        + bad_body + b'\r\n'
        + b'--b--\r\n'
    )
    mhtml_path = tmp_path / "bad-image.mhtml"
    mhtml_path.write_bytes(raw)
    cleaner = MHTMLCleaner(str(mhtml_path))
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        html, images = cleaner.parse_mhtml()
    assert '<p>ok</p>' in html
    assert 'bad.png' not in images
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert 'could not decode base64' in messages


def test_duplicate_image_key_last_successful_decode_wins(tmp_path) -> None:
    """When two parts share the same Content-Location, the second successfully
    decoded payload wins. This locks in the current last-wins semantics so a
    future change cannot silently flip behavior without a test update.
    """
    alt_png = (
        b'\x89PNG\r\n\x1a\n'                         # PNG signature
        b'\x00\x00\x00\rIHDR'                        # IHDR chunk length + name
        b'\x00\x00\x00\x02\x00\x00\x00\x02\x08\x06\x00\x00\x00'  # 2x2 RGBA
        b'DIFFERENT-PAYLOAD'                         # body bytes (not a valid PNG past IHDR, but unique)
    )
    data = make_mhtml(
        html='<p>hi</p>',
        images=[
            ('https://example.invalid/a.png', PNG_BYTES, 'loc'),
            ('https://example.invalid/a.png', alt_png, 'loc'),
        ],
    )
    _, images = _write_and_parse(tmp_path, data)
    assert images['https://example.invalid/a.png'] == alt_png
    # Only one entry for the shared key.
    assert len(images) == 1


def test_duplicate_image_key_bad_second_decode_does_not_clobber(
    tmp_path, caplog
) -> None:
    """A malformed second part sharing a key with a valid first part must not
    evict the good decode. The warning for the skipped part must still fire.
    """
    raw = (
        b'MIME-Version: 1.0\r\n'
        b'Content-Type: multipart/related; boundary="b"\r\n\r\n'
        b'--b\r\n'
        b'Content-Type: text/html\r\n\r\n'
        b'<p>ok</p>\r\n'
        b'--b\r\n'
        b'Content-Type: image/png\r\n'
        b'Content-Location: shared.png\r\n'
        b'Content-Transfer-Encoding: base64\r\n\r\n'
        + PNG_BYTES_B64 + b'\r\n'
        b'--b\r\n'
        b'Content-Type: image/png\r\n'
        b'Content-Location: shared.png\r\n'
        b'Content-Transfer-Encoding: base64\r\n\r\n'
        b'@@@not-base64@@@\r\n'
        b'--b--\r\n'
    )
    mhtml_path = tmp_path / "dup.mhtml"
    mhtml_path.write_bytes(raw)
    cleaner = MHTMLCleaner(str(mhtml_path))
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        _, images = cleaner.parse_mhtml()
    # The valid first decode must still be present.
    assert 'shared.png' in images
    assert images['shared.png'] == PNG_BYTES
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert 'could not decode base64' in messages


def test_folded_content_location_header_is_preserved(tmp_path) -> None:
    """RFC 5322 §2.2.3: a Content-Location split across lines with a leading
    space/tab on the continuation must be joined, not truncated.
    """
    raw = (
        b'MIME-Version: 1.0\r\n'
        b'Content-Type: multipart/related; boundary="b"\r\n\r\n'
        b'--b\r\n'
        b'Content-Type: text/html\r\n\r\n'
        b'<p>ok</p>\r\n'
        b'--b\r\n'
        b'Content-Type: image/png\r\n'
        b'Content-Location: https://example.com/path/\r\n'
        b'\tcontinued-and-more.png\r\n'
        b'Content-Transfer-Encoding: base64\r\n\r\n'
        + PNG_BYTES_B64 + b'\r\n'
        + b'--b--\r\n'
    )
    mhtml_path = tmp_path / "folded.mhtml"
    mhtml_path.write_bytes(raw)
    cleaner = MHTMLCleaner(str(mhtml_path))
    _, images = cleaner.parse_mhtml()
    # The folded continuation must have been joined to the original value.
    assert 'https://example.com/path/ continued-and-more.png' in images
    # The truncated key must NOT be present.
    assert 'https://example.com/path/' not in images
