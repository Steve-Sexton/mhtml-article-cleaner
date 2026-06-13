#!/usr/bin/env python3
"""
MHTML Article Cleaner.

Extracts clean article content from MHTML files, removing navigation, headers,
footers, ads, and other extraneous elements while preserving article text,
images, title, and formatting.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import logging
import quopri
import re
import socket
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup, Tag


WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

DOWNLOAD_TIMEOUT_SECONDS = 10
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# RIFF layout: bytes 0-3 are 'RIFF', bytes 4-7 are the chunk size,
# bytes 8-11 are the form type ('WEBP' for WebP).
_WEBP_FORM_TYPE_OFFSET = 8
_WEBP_FORM_TYPE_END = 12

_HTTP_SCHEMES = ('http://', 'https://')

# MIME types the downloader is willing to embed as a data URI. Anything outside
# this set is treated as "not an image we recognise" and the original URL is
# kept, so a server returning text/html or image/tiff cannot slip arbitrary
# content into an <img src="data:..."> attribute.
_ALLOWED_IMAGE_MIMES = frozenset({
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
})

DEFAULT_OUTPUT_FILENAME = 'clean_article.html'

_BOUNDARY_RE = re.compile(rb'boundary=(?:"([^"]+)"|([^\s;]+))', re.IGNORECASE)
_HS_WRAPPER_RE = re.compile(r'hs_cos_wrapper')
# HubSpot reuses ``hs_cos_wrapper`` for CTA/widget embeds, not just the article
# body. Only narrow to a wrapper that still holds at least this fraction of the
# root's text, so a page whose only wrappers are CTA shells is not gutted.
_HS_WRAPPER_MIN_TEXT_RATIO = 0.5

# Embedded interactive/external elements that cannot function in a standalone
# offline document (CTAs, embedded forms, video players). MHTML stores their
# source as a ``cid:`` part, so they render as dead boxes; stripped wholesale
# like scripts.
_EMBED_TAGS = ('iframe', 'object', 'embed')

# Tags that carry content even with no text of their own, so an element that
# wraps one is not "empty". Used by the non-content prune.
_MEDIA_TAGS = ('img', 'picture', 'svg', 'video', 'audio', 'source', 'canvas',
               'hr', 'br')

# A standalone text node that is nothing but a run of 4+ separator characters
# (optionally spaced) is a decorative rule, e.g. the "- - - - -" underline some
# editors place beneath a heading. The lookahead requires at least one real
# separator so pure-whitespace nodes (significant inter-word spacing) are left
# alone.
_DECOR_SEPARATOR_RE = re.compile(r'^(?=.*[-–—_=*•·~])[\s\-–—_=*•·~]{4,}$')
_GENERIC_CONTENT_RE = re.compile(
    r'post-body|blog-content|article-body|entry-content|post-content'
)
# Token-boundary anchored: "main", "main-content", "article-body", "my-content"
# all match, but "mainstream-nav" and "remaining-links" do not.
_MAIN_ID_RE = re.compile(r'(?:^|[-_])(?:main|content|article)(?:[-_]|$)')

# Noise classifier: match whole CSS class tokens only, from a curated allowlist.
# Substring and prefix matches are intentionally NOT used so that legitimate
# content classes like "authoritative-source", "share-of-voice", "co-authored",
# "subscriber-only-content", or "related-work-citation" survive cleaning.
_NOISE_EXACT_TOKENS = frozenset({
    # comments
    'comment', 'comments',
    'comment-form', 'comment-list', 'comment-section',
    'comment-respond', 'comment-wrap', 'comments-area',
    # social / sharing
    'social-share', 'share-buttons', 'share-bar', 'sharing', 'sharing-links',
    # related content
    'related-posts', 'related-articles', 'related-stories', 'related-content',
    # subscribe
    'subscribe', 'subscribe-box', 'subscribe-form', 'newsletter-signup',
    # author / byline
    'author', 'authors',
    'author-bio', 'author-box', 'author-info', 'author-card',
    'post-author', 'entry-author',
    'byline', 'post-byline',
})


logger = logging.getLogger(__name__)


class MHTMLBoundaryError(ValueError):
    """Raised when an MHTML file has no recognizable multipart boundary."""


class ArticleNotFoundError(RuntimeError):
    """Raised when no recognizable article root is found in the HTML part."""


def detect_image_mime(data: bytes) -> Optional[str]:
    """Return image MIME type from magic bytes, or ``None`` when unrecognized.

    Callers must treat ``None`` as "do not embed" and keep the original src,
    since guessing a MIME for unknown bytes is how silently-corrupt data URIs
    get produced.
    """
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data.startswith(b'GIF8'):
        return 'image/gif'
    if (
        data.startswith(b'RIFF')
        and data[_WEBP_FORM_TYPE_OFFSET:_WEBP_FORM_TYPE_END] == b'WEBP'
    ):
        return 'image/webp'
    return None


def mime_from_url_suffix(url: str) -> Optional[str]:
    """Return MIME type implied by the URL's file extension, if recognized.

    Only MIMEs in ``_ALLOWED_IMAGE_MIMES`` are returned here so the download
    path stays symmetric with the MHTML-embedded path, which validates bytes
    through ``detect_image_mime``. SVG is intentionally excluded: it has no
    magic signature (would never embed from MHTML) and can carry script.
    """
    # Strip query/fragment before matching so "image.png?sig=abc" still resolves.
    path = url.split('?', 1)[0].split('#', 1)[0].lower()
    if path.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg'
    if path.endswith('.png'):
        return 'image/png'
    if path.endswith('.gif'):
        return 'image/gif'
    if path.endswith('.webp'):
        return 'image/webp'
    return None


def sanitize_filename(
    title: str, max_length: int = 100, extension: str = '.html'
) -> str:
    """Convert article title to a safe cross-platform filename.

    The result is guaranteed to contain at least one stem character plus the
    ``extension``. When ``max_length`` is smaller than ``len(extension) + 1``,
    the returned filename will exceed ``max_length``; callers that need a hard
    upper bound must pass a ``max_length`` greater than ``len(extension)``.

    Control characters (e.g. ``\\x07``, ``\\x1f``, ``\\x7f``) are stripped:
    they are illegal in Windows filenames and would otherwise surface as an
    ``OSError`` at write time. Whitespace control chars (tab, newline) are kept
    here so the subsequent ``split()`` collapses them to single spaces rather
    than joining adjacent words.
    """
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        title = title.replace(char, '')
    title = ''.join(c for c in title if c.isprintable() or c.isspace())
    title = ' '.join(title.split())
    title = title.strip('. ')

    stem_budget = max(1, max_length - len(extension))
    if len(title) > stem_budget:
        title = title[:stem_budget].strip()

    if not title:
        title = 'article'

    if title.split('.')[0].upper() in WINDOWS_RESERVED_NAMES:
        title = '_' + title
        if len(title) > stem_budget:
            title = title[:stem_budget]

    return title + extension


_STYLESHEET = """
body {
    font-family: Georgia, 'Times New Roman', serif;
    line-height: 1.6;
    max-width: 800px;
    margin: 40px auto;
    padding: 20px;
    color: #333;
}
h1 {
    font-size: 2.5em;
    margin-bottom: 0.5em;
    color: #1a1a1a;
}
h2 {
    font-size: 1.8em;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
    color: #2a2a2a;
}
h3 {
    font-size: 1.4em;
    margin-top: 1.2em;
    margin-bottom: 0.5em;
    color: #3a3a3a;
}
p {
    margin-bottom: 1em;
    text-align: justify;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 20px auto;
}
ul, ol {
    margin: 1em 0;
    padding-left: 2em;
}
li {
    margin-bottom: 0.5em;
}
blockquote {
    border-left: 4px solid #ddd;
    margin: 1.5em 0;
    padding-left: 1em;
    color: #666;
    font-style: italic;
}
"""


def _is_noise_class(tag: Tag) -> bool:
    """Return True iff any of the tag's CSS class tokens is a known noise token.

    Whole-token match only: ``authoritative`` does NOT match ``author``, and
    ``subscriber-only-content`` does NOT match ``subscribe``.
    """
    classes = tag.get('class') or []
    return any(cls in _NOISE_EXACT_TOKENS for cls in classes)


# Attributes lazy-loading scripts use to stash the real image URL while the
# visible ``src`` holds a placeholder (empty, or a tiny inline ``data:`` GIF).
# Checked in order; the first non-empty value wins. ``srcset`` variants are
# parsed for their first candidate URL.
_LAZY_SRC_ATTRS = (
    'data-src', 'data-lazy-src', 'data-original', 'data-srcset', 'srcset',
)


def _first_srcset_url(value: str) -> str:
    """Return the first URL from a ``srcset`` value (drops the width/density
    descriptor and any later candidates)."""
    first_candidate = value.split(',', 1)[0].strip()
    return first_candidate.split()[0] if first_candidate else ''


def _effective_img_src(img: Tag) -> str:
    """Return the best real source URL for ``img``.

    A non-placeholder ``src`` is authoritative. Otherwise (no ``src``, or a
    ``data:`` placeholder left by a lazy-loader) the lazy-loading attributes in
    ``_LAZY_SRC_ATTRS`` are consulted so the actual image still resolves.
    """
    src = (img.get('src') or '').strip()
    if src and not src.startswith('data:'):
        return src
    for attr in _LAZY_SRC_ATTRS:
        value = (img.get(attr) or '').strip()
        if not value:
            continue
        candidate = _first_srcset_url(value) if 'srcset' in attr else value
        if candidate:
            return candidate
    return src


def _strip_img_attributes(img: Tag) -> None:
    """Keep only ``src``/``alt``/``title`` on an ``img`` tag."""
    for attr in list(img.attrs.keys()):
        if attr not in ('src', 'alt', 'title'):
            del img[attr]


class MHTMLCleaner:
    """Clean MHTML files and extract article content."""

    def __init__(
        self,
        mhtml_path: str,
        *,
        download_missing: bool = False,
        downloader: Optional[Callable[[str], str]] = None,
    ):
        self.mhtml_path = Path(mhtml_path)
        self.download_missing = download_missing
        self._downloader = downloader or self.download_and_encode_image
        self.title: str = ''

    def parse_mhtml(self) -> tuple[str, dict[str, bytes]]:
        """Parse MHTML file and extract the largest HTML part and embedded images."""
        with open(self.mhtml_path, 'rb') as f:
            content = f.read()

        match = _BOUNDARY_RE.search(content)
        if not match:
            raise MHTMLBoundaryError("Could not find MHTML boundary marker")
        boundary = match.group(1) or match.group(2)

        parts = content.split(b'--' + boundary)
        html_contents: list[str] = []
        images: dict[str, bytes] = {}

        for part in parts:
            stripped = part.strip()
            if not stripped or stripped == b'--':
                continue

            split = _split_headers_body(part)
            if split is None:
                continue
            headers, body = split
            headers_map = _parse_mime_headers(headers)
            content_type = headers_map.get('content-type', '').lower()
            cte = headers_map.get('content-transfer-encoding', '').lower()

            if content_type.startswith('text/html'):
                if cte == 'quoted-printable':
                    decoded_html = quopri.decodestring(body).decode(
                        'utf-8', errors='ignore'
                    )
                else:
                    decoded_html = body.decode('utf-8', errors='ignore')
                html_contents.append(decoded_html)
                continue

            if content_type.startswith('image/'):
                key = _extract_image_key(headers_map)
                if not key:
                    continue
                if cte == 'base64':
                    body_clean = body.replace(b'\r', b'').replace(b'\n', b'')
                    if not body_clean:
                        logger.warning(
                            "could not decode base64 image %s: empty body", key,
                        )
                        continue
                    try:
                        decoded = base64.b64decode(body_clean, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        logger.warning(
                            "could not decode base64 image %s: %s", key, exc,
                        )
                        continue
                    if not decoded:
                        logger.warning(
                            "could not decode base64 image %s: decoded to "
                            "zero bytes", key,
                        )
                        continue
                    images[key] = decoded
                else:
                    images[key] = body

        html_content = max(html_contents, key=len) if html_contents else ''
        return html_content, images

    def download_and_encode_image(self, url: str) -> str:
        """Download an http(s) image and encode it as a data URI.

        Returns the original URL unchanged in any of these cases:
        (a) the scheme is not http or https;
        (b) the host resolves to a private, loopback, link-local, or otherwise
            non-public address (SSRF guard — e.g. ``localhost``, ``10.x``,
            ``169.254.169.254`` cloud metadata);
        (c) the response exceeds ``MAX_DOWNLOAD_BYTES`` (declared or streamed);
        (d) a network, timeout, or OS error is raised;
        (e) no MIME in ``_ALLOWED_IMAGE_MIMES`` can be established from the
            server header, the URL suffix, or the payload's magic bytes.
        """
        if not url.startswith(_HTTP_SCHEMES):
            return url
        if _url_targets_blocked_host(url):
            logger.warning(
                "Refusing to download %s: host resolves to a private or "
                "reserved address; keeping URL",
                url,
            )
            return url
        fetched = self._fetch_capped(url)
        if fetched is None:
            return url
        image_data, raw_content_type = fetched
        mime = _resolve_image_mime(raw_content_type, url, image_data)
        if mime is None:
            logger.warning(
                "Could not determine image MIME for %s "
                "(header=%r, first bytes=%s); keeping URL",
                url, raw_content_type, image_data[:8].hex(),
            )
            return url
        b64_data = base64.b64encode(image_data).decode('ascii')
        return f'data:{mime};base64,{b64_data}'

    @staticmethod
    def _fetch_capped(url: str) -> Optional[tuple[bytes, str]]:
        """Fetch ``url`` and return ``(bytes, raw_content_type)`` or ``None``.

        ``None`` is returned on any network/OS failure or when the payload
        exceeds ``MAX_DOWNLOAD_BYTES`` (checked against ``Content-Length``
        first, then during streamed reads).
        """
        try:
            request = urllib.request.Request(
                url,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36'
                    )
                },
            )
            with urllib.request.urlopen(
                request, timeout=DOWNLOAD_TIMEOUT_SECONDS
            ) as response:
                declared = response.headers.get('Content-Length', '')
                if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                    logger.warning(
                        "Image %s exceeds max size (%s bytes); keeping URL",
                        url, declared,
                    )
                    return None
                image_data = _read_capped(response, MAX_DOWNLOAD_BYTES)
                if image_data is None:
                    logger.warning(
                        "Image %s exceeded max size during read; keeping URL",
                        url,
                    )
                    return None
                raw_content_type = response.headers.get('Content-Type', '')
                return image_data, raw_content_type
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Could not download image %s: %s", url, exc)
            return None

    def clean_html(self, html: str, images: dict[str, bytes]) -> str:
        """Clean HTML content, removing extraneous elements."""
        soup = BeautifulSoup(html, 'html.parser')
        # Drop executable/embedded/styling elements outright: scripts, the
        # interactive embeds in _EMBED_TAGS, and stylesheets. Input <style>/
        # <link> would otherwise leak raw CSS into the output (and inflate the
        # largest-<div> heuristic); the output gets its own stylesheet instead.
        for tag in soup.find_all(['script', 'noscript', 'style', 'link',
                                  *_EMBED_TAGS]):
            tag.decompose()

        article_content = self._find_article_root(soup)
        if article_content is None:
            raise ArticleNotFoundError(
                "Could not find article content in input file. "
                "Add a platform-specific detector to _find_article_root."
            )

        self._strip_noise_selectors(article_content)
        self._resolve_images(article_content, images)

        # Narrow to the HubSpot wrapper BEFORE stripping class attributes,
        # otherwise the class-based lookup never matches.
        content_span = self._find_hs_content_wrapper(article_content)
        if content_span is not None:
            article_content = content_span

        self._strip_attributes(article_content)
        self._prune_noncontent(article_content)

        title = self._extract_title(soup)
        self.title = title
        return self._build_output_document(article_content, title)

    @staticmethod
    def _prune_noncontent(root: Tag) -> None:
        """Remove empty and purely-decorative elements left after cleaning.

        An element is dropped only when it is genuinely empty: it has no text
        and contains no media (``_MEDIA_TAGS``). Decorative ``- - - -`` rules are
        removed first as text nodes, which empties any heading/span that held
        only them, so those collapse here too. This clears blank ``<p></p>``
        gaps and containers emptied by earlier removals (e.g. a CTA wrapper
        after its ``<iframe>`` was stripped) without touching elements that hold
        real but non-alphanumeric content such as ``<sup>®</sup>`` or
        ``<em>?</em>``. Iterating in reverse document order processes children
        before parents, so a wrapper emptied by pruning its children is itself
        pruned in the same pass.
        """
        # First drop decorative separator text nodes (e.g. a "- - - -" rule
        # sitting loose beside real text inside a heading); a heading reduced to
        # only real text by this is then kept by the element pass below.
        for text_node in list(root.find_all(string=_DECOR_SEPARATOR_RE)):
            text_node.extract()
        for tag in reversed(root.find_all()):
            if tag.decomposed or tag.name in _MEDIA_TAGS:
                continue
            if tag.get_text(strip=True) or tag.find(list(_MEDIA_TAGS)):
                continue
            tag.decompose()

    @staticmethod
    def _find_hs_content_wrapper(root: Tag) -> Optional[Tag]:
        """Return the HubSpot ``hs_cos_wrapper`` holding the article body, else None.

        HubSpot tags its rich-text post body with ``hs_cos_wrapper``, but reuses
        the same class for CTA and other widget embeds (e.g.
        ``hs_cos_wrapper_type_cta``). On a blog post the body wrapper holds
        essentially all of the text, so narrowing to it strips the surrounding
        post chrome. On a landing/marketing page the only wrappers may be CTA
        shells with no article text — narrowing to one of those would discard
        the whole article. Guard against that by narrowing only to the richest
        wrapper, and only when it still holds a majority of the root's text.
        """
        candidates = root.find_all('span', class_=_HS_WRAPPER_RE)
        if not candidates:
            return None
        root_len = len(root.get_text(strip=True))
        if not root_len:
            return None
        best = max(candidates, key=lambda s: len(s.get_text(strip=True)))
        if len(best.get_text(strip=True)) >= root_len * _HS_WRAPPER_MIN_TEXT_RATIO:
            return best
        return None

    @staticmethod
    def _find_article_root(soup: BeautifulSoup) -> Optional[Tag]:
        """Locate the article container, or ``None`` if nothing looks like one.

        Tries platform-specific markers first (HubSpot ``post-body`` /
        ``blog_content``, then a generic ``<article>``, content-class regex, and
        ``<main>``/main-ish id), falling back to the ``<div>`` with the most
        text. ``None`` propagates to an ``ArticleNotFoundError`` in the caller.
        """
        strategies: list[Callable[[], Optional[Tag]]] = [
            lambda: soup.find(class_='post-body'),
            lambda: soup.find(attrs={'data-widget-type': 'blog_content'}),
            lambda: soup.find('article'),
            lambda: soup.find(class_=_GENERIC_CONTENT_RE),
            lambda: soup.find('main') or soup.find(id=_MAIN_ID_RE),
        ]
        for strategy in strategies:
            root = strategy()
            if root is not None:
                return root
        divs = soup.find_all('div')
        if divs:
            return max(divs, key=lambda d: len(d.get_text(strip=True)))
        return None

    @staticmethod
    def _strip_noise_selectors(root: Tag) -> None:
        """Remove descendants whose CSS class is a known chrome token.

        Matching is whole-token against ``_NOISE_EXACT_TOKENS`` (comments,
        sharing, related-posts, subscribe, author/byline blocks).
        """
        for tag in list(root.find_all(class_=True)):
            # Decomposing a noise parent also detaches any noise descendants
            # still sitting in this snapshot; their attrs become None, so skip
            # anything already removed to avoid an AttributeError on .get().
            if tag.decomposed:
                continue
            if _is_noise_class(tag):
                tag.decompose()

    def _resolve_images(self, root: Tag, images: dict[str, bytes]) -> None:
        """Resolve every ``<img>`` in ``root`` to an inline data URI where possible.

        Promotes a lazy-loading URL to ``src``, then embeds ``cid:`` and
        MHTML-known external images, optionally downloading the rest when
        ``download_missing`` is set. Unresolvable sources keep their original
        URL. All non-``src``/``alt``/``title`` attributes are stripped.
        """
        img_tags = root.find_all('img')
        if img_tags:
            logger.info("Processing %d images...", len(img_tags))

        for index, img in enumerate(img_tags, 1):
            # Promote a lazy-loading URL to ``src`` so a placeholder src does
            # not block resolution and the real image survives even when it
            # cannot be embedded (e.g. http without --download-missing).
            src = _effective_img_src(img)
            if src:
                img['src'] = src
            if src.startswith('cid:'):
                self._embed_cid(img, src, images, index)
            elif src.startswith(_HTTP_SCHEMES):
                self._embed_or_fetch_external(img, src, images, index)
            _strip_img_attributes(img)

    @staticmethod
    def _embed_cid(
        img: Tag, src: str, images: dict[str, bytes], index: int
    ) -> bool:
        """Replace a ``cid:`` ``src`` with the embedded image as a data URI.

        Returns ``True`` on success; leaves ``src`` untouched and returns
        ``False`` when the CID is unknown or its bytes are not a known image.
        """
        cid = src[len('cid:'):]
        if cid not in images:
            logger.warning("unresolved CID image %r; src left as-is", cid)
            return False
        data_uri = _build_data_uri(images[cid])
        if data_uri is None:
            logger.warning(
                "unrecognized image bytes for CID %r; src left as-is", cid,
            )
            return False
        img['src'] = data_uri
        logger.info("  [%d] Embedded CID image", index)
        return True

    def _embed_or_fetch_external(
        self, img: Tag, src: str, images: dict[str, bytes], index: int
    ) -> None:
        """Embed an ``http(s)`` image, preferring the MHTML copy over the network.

        Uses the MHTML-embedded bytes when present; otherwise downloads only if
        ``download_missing`` is set, and leaves the URL untouched when the bytes
        are unrecognized or the fetch fails.
        """
        if src in images:
            data_uri = _build_data_uri(images[src])
            if data_uri is None:
                logger.warning(
                    "unrecognized image bytes for %s; src left as-is", src,
                )
                return
            img['src'] = data_uri
            logger.info("  [%d] Embedded from MHTML", index)
            return
        if self.download_missing:
            logger.info("  [%d] Downloading: %s...", index, src[:60])
            data_uri = self._downloader(src)
            img['src'] = data_uri
            if data_uri.startswith('data:'):
                logger.info("  [%d] Successfully embedded", index)
            else:
                logger.info("  [%d] Kept original URL (download failed)", index)
        else:
            logger.info(
                "  [%d] Kept original URL "
                "(not embedded in MHTML; pass --download-missing to fetch)",
                index,
            )

    @staticmethod
    def _strip_attributes(root: Tag) -> None:
        """Strip presentational/platform attributes (``style``/``class``/``id``
        and HubSpot ``data-*``) from every element in ``root``."""
        removable = {
            'style', 'class', 'id',
            'data-hs-cos-general-type', 'data-hs-cos-type',
            'data-widget-type', 'data-x', 'data-w',
        }
        for tag in root.find_all(True):
            for attr in list(tag.attrs.keys()):
                if attr in removable:
                    del tag[attr]

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        """Return the document ``<title>`` text, falling back to the first
        ``<h1>``, or ``''`` when neither carries text."""
        title_tag = soup.find('title')
        if title_tag:
            text = title_tag.get_text(strip=True)
            if text:
                return text
        h1 = soup.find('h1')
        if h1:
            return h1.get_text(strip=True)
        return ''

    @staticmethod
    def _build_output_document(root: Tag, title: str) -> str:
        """Assemble the final standalone HTML document.

        Builds a fresh document with the built-in stylesheet, prepends the
        ``title`` as ``<title>`` and a top-level ``<h1>`` (de-duplicating any
        matching ``<h1>`` already in ``root``), then moves ``root``'s children
        into the body. Returns the prettified HTML string.
        """
        clean = BeautifulSoup(
            '<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body></body></html>',
            'html.parser',
        )
        style = clean.new_tag('style')
        style.string = _STYLESHEET
        clean.head.append(style)

        if title:
            title_tag = clean.new_tag('title')
            title_tag.string = title
            clean.head.append(title_tag)
            h1 = clean.new_tag('h1')
            h1.string = title
            clean.body.append(h1)

            # Drop any descendant <h1> whose normalized text matches the title
            # we just prepended so the output does not double its own headline.
            title_norm = ' '.join(title.split()).strip()
            for existing in list(root.find_all('h1')):
                existing_text = ' '.join(existing.get_text().split()).strip()
                if existing_text == title_norm:
                    existing.decompose()

        # Snapshot children: append() detaches each node from the live `.children`
        # iterator, which would otherwise skip every other sibling.
        for element in list(root.children):
            if getattr(element, 'name', None):
                clean.body.append(element)
            elif isinstance(element, str) and element.strip():
                p = clean.new_tag('p')
                p.string = element.strip()
                clean.body.append(p)

        return clean.prettify()

    def process(
        self,
        output_path: Optional[str] = None,
        *,
        force: bool = False,
    ) -> str:
        """Process MHTML file and write clean HTML to disk."""
        logger.info("Parsing MHTML file...")
        html_content, images = self.parse_mhtml()
        logger.info("Found %d embedded images", len(images))

        logger.info("Cleaning HTML content...")
        cleaned = self.clean_html(html_content, images)

        if not output_path:
            output_path = (
                sanitize_filename(self.title) if self.title
                else DEFAULT_OUTPUT_FILENAME
            )
            logger.info("Using article title for filename: %s", output_path)

        output_file = Path(output_path)
        if output_file.exists() and not force:
            raise FileExistsError(
                f"Output file already exists: {output_file}. "
                f"Pass --force to overwrite."
            )
        output_file.write_text(cleaned, encoding='utf-8')
        logger.info("Clean article saved to: %s", output_file)
        return cleaned


def _read_capped(response, max_bytes: int) -> Optional[bytes]:
    """Read ``response`` in chunks, returning None if the body exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b''.join(chunks)


def _resolve_host_ips(host: str) -> list[str]:
    """Resolve ``host`` to its IP literals, or ``[]`` when resolution fails.

    An empty list is treated by callers as "could not resolve" rather than
    "blocked": if a name does not resolve, no connection can be opened, so
    there is nothing to guard against. Factored out as a seam so tests can
    drive the SSRF guard without real DNS.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def _is_blocked_ip(ip_str: str) -> bool:
    """True if ``ip_str`` is a private/loopback/link-local/reserved address.

    Unparseable values are treated as blocked: if we cannot reason about the
    address, we must not fetch it.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _url_targets_blocked_host(url: str) -> bool:
    """SSRF guard: True if ``url``'s host resolves to a non-public address.

    A URL with no host is blocked. A host that fails to resolve is allowed
    (the connection would fail anyway); a host that resolves to any blocked
    address is blocked, so a public name that maps to ``127.0.0.1`` or the
    ``169.254.169.254`` metadata endpoint cannot be used to reach internal
    services.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return True
    ips = _resolve_host_ips(host)
    if not ips:
        return False
    return any(_is_blocked_ip(ip) for ip in ips)


def _split_headers_body(part: bytes) -> Optional[tuple[bytes, bytes]]:
    """Return (headers, body) for an MIME part, handling CRLF and LF separators."""
    for sep in (b'\r\n\r\n', b'\n\n'):
        idx = part.find(sep)
        if idx != -1:
            return part[:idx], part[idx + len(sep):]
    return None


def _parse_mime_headers(headers_bytes: bytes) -> dict[str, str]:
    """Parse MIME headers into a dict keyed by lower-cased header names.

    RFC 2045 defines header field names as case-insensitive, so exporter-specific
    casing (``content-type:``, ``Content-Transfer-Encoding: Quoted-Printable``,
    etc.) must not change parser behavior. Also unfolds RFC 5322 §2.2.3
    continuation lines (those beginning with a space or tab) so that long
    folded values (e.g., a multi-line ``Content-Location`` URL) are preserved.
    """
    text = headers_bytes.decode('utf-8', errors='ignore')
    logical: list[str] = []
    for line in text.splitlines():
        if line and line[0] in ' \t' and logical:
            logical[-1] = logical[-1] + ' ' + line.strip()
        else:
            logical.append(line)
    result: dict[str, str] = {}
    for line in logical:
        if ':' in line:
            name, _, value = line.partition(':')
            result[name.strip().lower()] = value.strip()
    return result


def _extract_image_key(headers: dict[str, str]) -> Optional[str]:
    """Return the Content-ID or Content-Location used to key an embedded image."""
    cid = headers.get('content-id', '')
    if cid:
        match = re.match(r'<([^>]+)>', cid)
        if match:
            return match.group(1)
        return cid.strip()
    loc = headers.get('content-location', '').strip()
    if loc:
        return loc
    return None


def _build_data_uri(data: bytes) -> Optional[str]:
    """Build a base64 data URI, or ``None`` when the bytes do not match a known
    image signature. Callers must handle ``None`` as "do not embed"."""
    mime = detect_image_mime(data)
    if mime is None:
        return None
    return f'data:{mime};base64,{base64.b64encode(data).decode("ascii")}'


def _normalize_mime(raw: str) -> str:
    """Strip parameters (``; charset=utf-8``) and lowercase a MIME value."""
    return raw.split(';', 1)[0].strip().lower()


def _resolve_image_mime(
    raw_content_type: str, url: str, data: bytes
) -> Optional[str]:
    """Return a MIME from ``_ALLOWED_IMAGE_MIMES`` for this payload, or ``None``.

    The server header is trusted only when it parses to an allowed image MIME;
    parameters such as ``charset=utf-8`` are stripped before comparison so a
    header like ``image/png; charset=utf-8`` resolves correctly, and a header
    like ``text/html`` or ``image/tiff`` falls through to the URL-suffix and
    magic-byte fallbacks just as a missing or ``application/octet-stream``
    header would.
    """
    header_mime = _normalize_mime(raw_content_type)
    if header_mime in _ALLOWED_IMAGE_MIMES:
        return header_mime
    suffix_mime = mime_from_url_suffix(url)
    if suffix_mime in _ALLOWED_IMAGE_MIMES:
        return suffix_mime
    detected = detect_image_mime(data)
    if detected in _ALLOWED_IMAGE_MIMES:
        return detected
    return None


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract clean article content from an MHTML file."
    )
    parser.add_argument('input', help="Path to input .mhtml file")
    parser.add_argument(
        'output', nargs='?', default=None,
        help="Output HTML filename (default: derived from article title)",
    )
    parser.add_argument(
        '--download-missing', action='store_true',
        help="Download and embed images that are not already in the MHTML",
    )
    parser.add_argument(
        '--force', action='store_true',
        help="Overwrite output file if it already exists",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        '--quiet', action='store_true',
        help="Only log warnings and errors",
    )
    verbosity.add_argument(
        '--verbose', action='store_true',
        help="Log debug-level detail",
    )
    return parser.parse_args(argv)


def _configure_logging(*, quiet: bool, verbose: bool) -> None:
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    _configure_logging(quiet=args.quiet, verbose=args.verbose)
    try:
        cleaner = MHTMLCleaner(args.input, download_missing=args.download_missing)
        cleaner.process(args.output, force=args.force)
        logger.info("Successfully cleaned article!")
        logger.info("  Input:  %s", args.input)
        return 0
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except MHTMLBoundaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except ArticleNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
