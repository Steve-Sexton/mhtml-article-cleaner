"""Shared MHTML fixture builders for tests."""

from __future__ import annotations

import base64


# Smallest valid PNG (1x1 transparent).
PNG_BYTES = base64.b64decode(
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAj'
    b'CB0C8AAAAASUVORK5CYII='
)


def make_mhtml(
    *,
    boundary: str = '"----=_Part_1"',
    html: str = '<html><body><p>Hello</p></body></html>',
    extra_html: str | None = None,
    images: list[tuple[str, bytes, str]] | None = None,
    sep: bytes = b'\r\n',
    content_type_header: str = 'Content-Type',
    cte_header: str = 'Content-Transfer-Encoding',
    html_mime: str = 'text/html',
    base64_value: str = 'base64',
    quoted_printable_html: bool = False,
) -> bytes:
    """Build a minimal MHTML document.

    images: list of (key_header, raw_bytes, key_kind) where key_kind is
    'cid' or 'loc'.

    Header-case overrides (``content_type_header``, ``cte_header``,
    ``html_mime``, ``base64_value``) let tests exercise RFC 2045
    case-insensitive header parsing.
    """
    raw_boundary = boundary.strip('"')
    parts: list[bytes] = []
    parts.append(
        (f'MIME-Version: 1.0{sep.decode()}'
         f'Content-Type: multipart/related; boundary={boundary}{sep.decode()}'
         f'{sep.decode()}').encode()
    )

    body_parts: list[bytes] = []
    if quoted_printable_html:
        html_body_bytes = html.encode('utf-8')
        html_cte = 'Quoted-Printable'
        html_part = (
            f'{content_type_header}: {html_mime}; charset=utf-8{sep.decode()}'
            f'{cte_header}: {html_cte}{sep.decode()}'
            f'{sep.decode()}'
        ).encode() + html_body_bytes + sep
    else:
        html_part = (
            f'{content_type_header}: {html_mime}; charset=utf-8{sep.decode()}'
            f'{cte_header}: 8bit{sep.decode()}'
            f'{sep.decode()}'
        ).encode() + html.encode('utf-8') + sep
    body_parts.append(html_part)

    if extra_html is not None:
        extra = (
            f'{content_type_header}: {html_mime}; charset=utf-8{sep.decode()}'
            f'{cte_header}: 8bit{sep.decode()}'
            f'{sep.decode()}'
        ).encode() + extra_html.encode('utf-8') + sep
        body_parts.append(extra)

    for key, raw, kind in images or []:
        if kind == 'cid':
            key_header = f'Content-ID: <{key}>'
        else:
            key_header = f'Content-Location: {key}'
        b64 = base64.b64encode(raw).decode('ascii')
        img_part = (
            f'{content_type_header}: image/png{sep.decode()}'
            f'{key_header}{sep.decode()}'
            f'{cte_header}: {base64_value}{sep.decode()}'
            f'{sep.decode()}'
            f'{b64}{sep.decode()}'
        ).encode()
        body_parts.append(img_part)

    out = bytearray()
    out.extend(parts[0])
    for body in body_parts:
        out.extend(f'--{raw_boundary}{sep.decode()}'.encode())
        out.extend(body)
    out.extend(f'--{raw_boundary}--{sep.decode()}'.encode())
    return bytes(out)
