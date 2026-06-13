"""Tests for the HTML cleaning pipeline, including the children-mutation regression."""

from __future__ import annotations

import logging

import pytest

from clean_mhtml_article import (
    ArticleNotFoundError,
    MHTMLCleaner,
    detect_image_mime,
)


def _clean(html: str, *, download_missing: bool = False) -> str:
    cleaner = MHTMLCleaner(
        mhtml_path="unused",
        download_missing=download_missing,
        downloader=lambda url: url,  # never download in tests
    )
    return cleaner.clean_html(html, images={})


def test_clean_html_preserves_all_top_level_siblings() -> None:
    """Regression: iterating ``article_content.children`` while calling
    ``clean_soup.body.append(element)`` detaches the node from the live
    iterator and silently drops every other sibling. Snapshot the list first.
    """
    html = (
        '<html><body><div class="post-body">'
        '<p>one</p><p>two</p><p>three</p><p>four</p><p>five</p>'
        '</div></body></html>'
    )
    out = _clean(html)
    assert out.count('<p>') == 5
    for expected in ('one', 'two', 'three', 'four', 'five'):
        assert expected in out


def test_clean_html_strips_scripts_and_noscript() -> None:
    html = (
        '<html><body><div class="post-body">'
        '<script>alert(1)</script>'
        '<noscript>nope</noscript>'
        '<p>kept</p>'
        '</div></body></html>'
    )
    out = _clean(html)
    assert 'alert' not in out
    assert 'nope' not in out
    assert 'kept' in out


def test_clean_html_strips_iframes_and_embeds() -> None:
    """Embedded interactive elements cannot render in a standalone offline file
    (their src is a cid: part), so iframe/object/embed are removed wholesale.
    """
    html = (
        '<html><body><div class="post-body">'
        '<p>real text</p>'
        '<iframe src="cid:frame-abc" title="Embedded CTA"></iframe>'
        '<object data="cid:obj"></object>'
        '<embed src="cid:emb">'
        '</div></body></html>'
    )
    out = _clean(html)
    assert 'real text' in out
    assert '<iframe' not in out
    assert '<object' not in out
    assert '<embed' not in out
    assert 'cid:' not in out


def test_clean_html_strips_style_and_link_elements() -> None:
    """Input <style>/<link> must not leak into the output: the cleaner applies
    its own stylesheet and a standalone file should carry no foreign CSS.
    """
    html = (
        '<html><head><link rel="stylesheet" href="x.css"></head><body>'
        '<div class="post-body">'
        '<style>.evil{display:none}</style>'
        '<link rel="stylesheet" href="y.css">'
        '<p>kept</p>'
        '</div></body></html>'
    )
    out = _clean(html)
    assert 'kept' in out
    assert '.evil' not in out
    assert 'y.css' not in out
    # The output's own stylesheet (in <head>) is the only style block.
    assert out.count('<style>') == 1


def test_clean_html_prunes_empty_elements() -> None:
    """Blank <p></p> gaps and containers emptied by earlier removals (e.g. a CTA
    wrapper whose only child was a stripped iframe) are pruned away.
    """
    html = (
        '<html><body><main id="main-content">'
        '<p>keeper</p>'
        '<p>\n   \n</p>'
        '<div class="cta-wrap"><iframe src="cid:x"></iframe></div>'
        '</main></body></html>'
    )
    out = _clean(html)
    assert 'keeper' in out
    # The empty paragraph and the now-empty CTA wrapper div are gone.
    assert out.count('<p>') == 1
    assert '<div>' not in out


def test_clean_html_removes_decorative_separators() -> None:
    """A heading that is only a "- - - -" rule is dropped, and a decorative dash
    run sitting loose beside real heading text is stripped while the text stays.
    """
    html = (
        '<html><body><main id="main-content">'
        '<h1>- - - - - - - - - -</h1>'
        '<h2>Real Heading<br>- - - - - - -</h2>'
        '<span class="x">- - - - -</span>'
        '<p>body</p>'
        '</main></body></html>'
    )
    out = _clean(html)
    assert 'Real Heading' in out
    assert 'body' in out
    # No 4+ dash run survives anywhere in the output body.
    import re as _re
    body = out.split('<body>', 1)[1]
    assert not _re.search(r'(?:[-–—]\s*){4,}', body)


def test_clean_html_keeps_image_only_and_void_elements() -> None:
    """The prune must not remove media-bearing or void elements that hold no
    text: an <img> wrapper, a standalone <hr>, and a single em-dash in prose.
    """
    from tests.fixtures import PNG_BYTES
    html = (
        '<html><body><div class="post-body">'
        '<figure><img src="cid:pic"></figure>'
        '<hr>'
        '<p>a real sentence — with an em dash</p>'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={'pic': PNG_BYTES})
    assert 'data:image/png;base64,' in out  # figure/img survived
    assert '<hr' in out                      # void divider survived
    assert 'em dash' in out                  # lone em-dash prose survived


def test_clean_html_strips_inline_attributes() -> None:
    html = (
        '<html><body><article>'
        '<p style="color:red" class="x" id="p1" data-widget-type="z">hello</p>'
        '</article></body></html>'
    )
    out = _clean(html)
    assert 'style=' not in out
    assert 'class=' not in out
    assert 'id="p1"' not in out
    assert 'data-widget-type' not in out
    assert 'hello' in out


def test_clean_html_uses_title_for_h1() -> None:
    html = (
        '<html><head><title>My Article</title></head><body>'
        '<article><p>body</p></article></body></html>'
    )
    out = _clean(html)
    assert '<title>' in out
    assert '<h1>' in out
    # Prettified output wraps text with whitespace; match on the text itself.
    assert 'My Article' in out


def test_clean_html_falls_back_to_h1_when_no_title() -> None:
    html = (
        '<html><body><h1>Headline Title</h1>'
        '<article><p>body</p></article></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    cleaner.clean_html(html, images={})
    assert cleaner.title == 'Headline Title'


def test_clean_html_raises_when_no_content() -> None:
    html = '<html><body></body></html>'
    # Body has no children, no divs. _find_article_root returns None and
    # clean_html raises instead of silently returning an empty document.
    with pytest.raises(ArticleNotFoundError):
        _clean(html)


def test_clean_html_narrows_to_hs_cos_wrapper() -> None:
    """Regression: the hs_cos_wrapper unwrap must run BEFORE ``class`` is
    stripped, otherwise the class-based lookup never matches and HubSpot
    articles get the outer post-body chrome instead of the article body.

    The wrapper holds the bulk of the text (as on a real blog post), so the
    narrowing fires and the small outer chrome sibling is dropped.
    """
    html = (
        '<html><head><title>HS</title></head><body>'
        '<div class="post-body">'
        '<span class="hs_cos_wrapper">'
        '<p>This is the real article body, long enough to dominate the text.</p>'
        '<p>A second substantial paragraph keeps the wrapper as the majority.</p>'
        '</span>'
        '<p>nav</p>'
        '</div></body></html>'
    )
    out = _clean(html)
    assert 'real article body' in out
    assert 'nav' not in out


def test_clean_html_does_not_narrow_to_cta_only_wrapper() -> None:
    """Regression (Cause Mapping landing page): when the only hs_cos_wrapper
    spans are CTA/widget shells with little or no text, the cleaner must NOT
    narrow to one of them and discard the real article body sitting beside
    them in the root.
    """
    html = (
        '<html><head><title>Landing</title></head><body>'
        '<main id="main-content">'
        '<div class="rich-text">'
        '<p>The genuine article content that must survive cleaning. '
        'It has several sentences so it clearly dominates the page text.</p>'
        '<p>Another paragraph of real article body for good measure.</p>'
        '</div>'
        '<span class="hs_cos_wrapper hs_cos_wrapper_type_cta">'
        '<a href="#">Download</a></span>'
        '</main></body></html>'
    )
    out = _clean(html)
    assert 'genuine article content that must survive' in out
    assert 'Another paragraph of real article body' in out


def test_clean_html_warns_on_unresolved_cid(caplog) -> None:
    html = (
        '<html><body><div class="post-body">'
        '<img src="cid:missing">'
        '<p>body</p>'
        '</div></body></html>'
    )
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = _clean(html)
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert 'unresolved CID image' in messages
    assert "'missing'" in messages
    assert 'cid:missing' in out


def test_external_image_left_alone_without_download_missing(monkeypatch) -> None:
    """By default, the cleaner does not touch the network."""
    called = []

    def boom(url: str) -> str:
        called.append(url)
        raise AssertionError("network must not be used unless --download-missing")

    cleaner = MHTMLCleaner(
        mhtml_path="unused", download_missing=False, downloader=boom,
    )
    html = (
        '<html><body><div class="post-body">'
        '<img src="https://example.invalid/a.png">'
        '</div></body></html>'
    )
    out = cleaner.clean_html(html, images={})
    assert 'https://example.invalid/a.png' in out
    assert called == []


def test_external_image_downloaded_when_opt_in() -> None:
    captured: list[str] = []

    def downloader(url: str) -> str:
        captured.append(url)
        return 'data:image/png;base64,FAKE'

    cleaner = MHTMLCleaner(
        mhtml_path="unused", download_missing=True, downloader=downloader,
    )
    html = (
        '<html><body><div class="post-body">'
        '<img src="https://example.invalid/a.png">'
        '</div></body></html>'
    )
    out = cleaner.clean_html(html, images={})
    assert captured == ['https://example.invalid/a.png']
    assert 'data:image/png;base64,FAKE' in out


def test_cid_image_embedded() -> None:
    from tests.fixtures import PNG_BYTES
    html = (
        '<html><body><div class="post-body">'
        '<img src="cid:abc">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={'abc': PNG_BYTES})
    assert 'data:image/png;base64,' in out
    assert 'cid:abc' not in out


def test_lazy_data_src_resolves_when_src_empty() -> None:
    """An img with an empty src but a lazy ``data-src`` cid must still embed."""
    from tests.fixtures import PNG_BYTES
    html = (
        '<html><body><div class="post-body">'
        '<img src="" data-src="cid:abc">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={'abc': PNG_BYTES})
    assert 'data:image/png;base64,' in out
    # The lazy attribute is consumed and stripped from the output.
    assert 'data-src' not in out


def test_lazy_data_src_overrides_data_uri_placeholder() -> None:
    """A tiny inline ``data:`` GIF placeholder must not block the real image
    referenced by ``data-src``.
    """
    from tests.fixtures import PNG_BYTES
    html = (
        '<html><body><div class="post-body">'
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="'
        ' data-src="cid:real">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={'real': PNG_BYTES})
    assert 'data:image/png;base64,' in out
    assert 'R0lGOD' not in out  # placeholder GIF gone


def test_lazy_srcset_promotes_first_candidate_url() -> None:
    """With no usable src, the first ``srcset`` candidate URL becomes the src
    (kept verbatim here since download is off).
    """
    html = (
        '<html><body><div class="post-body">'
        '<img srcset="https://example.invalid/a.png 1x, '
        'https://example.invalid/a@2x.png 2x">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={})
    assert 'https://example.invalid/a.png' in out
    assert 'srcset' not in out
    assert '2x.png' not in out  # only the first candidate is promoted


def test_real_src_takes_precedence_over_lazy_attrs() -> None:
    """A genuine (non-placeholder) src must not be overridden by data-src."""
    html = (
        '<html><body><div class="post-body">'
        '<img src="https://example.invalid/real.png" '
        'data-src="https://example.invalid/decoy.png">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={})
    assert 'real.png' in out
    assert 'decoy.png' not in out


def test_detect_image_mime_covers_common_formats() -> None:
    assert detect_image_mime(b'\xff\xd8\xff\xe0anything') == 'image/jpeg'
    assert detect_image_mime(b'\x89PNG\r\n\x1a\n...') == 'image/png'
    assert detect_image_mime(b'GIF89a...') == 'image/gif'
    webp = b'RIFF' + b'xxxx' + b'WEBP' + b'...'
    assert detect_image_mime(webp) == 'image/webp'


def test_detect_image_mime_returns_none_for_unknown_bytes() -> None:
    """Previously fell back to image/jpeg — now callers must see None and skip."""
    assert detect_image_mime(b'unknown bytes here') is None
    assert detect_image_mime(b'') is None
    # Almost-WEBP (no WEBP marker) must not match.
    assert detect_image_mime(b'RIFF' + b'xxxx' + b'NOPE' + b'...') is None


def test_detect_image_mime_webp_requires_form_type_at_offset_8() -> None:
    """RIFF form type lives at bytes 8-11. A ``WEBP`` substring appearing at
    any other offset must NOT be mistaken for a WebP image.
    """
    # 'WEBP' placed at offset 16 (inside a RIFF container of some other type).
    fabricated = b'RIFF' + b'\x00' * 4 + b'AVI ' + b'LIST' + b'WEBP'
    assert detect_image_mime(fabricated) is None
    # Canonical form: 'RIFF' + size + 'WEBP' at offset 8 still matches.
    assert detect_image_mime(b'RIFF' + b'xxxx' + b'WEBP' + b'...') == 'image/webp'


def test_cid_image_not_embedded_when_bytes_unrecognized(caplog) -> None:
    """When CID bytes do not match any known signature, keep the cid: src
    and log a warning instead of silently embedding a misattributed jpeg.
    """
    html = (
        '<html><body><div class="post-body">'
        '<img src="cid:mystery">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = cleaner.clean_html(html, images={'mystery': b'garbage-not-an-image'})
    assert 'cid:mystery' in out
    assert 'data:image/jpeg' not in out
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert 'unrecognized image bytes' in messages.lower()


def test_external_image_not_embedded_when_mhtml_bytes_unrecognized(caplog) -> None:
    """Same contract for MHTML-embedded external URLs: skip when unrecognized."""
    html = (
        '<html><body><div class="post-body">'
        '<img src="https://example.invalid/a.png">'
        '</div></body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    with caplog.at_level(logging.WARNING, logger="clean_mhtml_article"):
        out = cleaner.clean_html(
            html, images={'https://example.invalid/a.png': b'not-an-image'},
        )
    assert 'https://example.invalid/a.png' in out
    assert 'data:image/jpeg' not in out
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert 'unrecognized image bytes' in messages.lower()


def test_clean_html_dedupes_h1_with_inline_markup() -> None:
    """An <h1> that contains nested inline tags whose stripped text matches the
    <title> must still be treated as a duplicate and removed.
    """
    html = (
        '<html><head><title>My Important Title</title></head><body>'
        '<article>'
        '<h1>My <em>Important</em> Title</h1>'
        '<p>body</p>'
        '</article></body></html>'
    )
    out = _clean(html)
    assert out.count('<h1>') == 1
    # The surviving H1 is the prepended plain-text one (no <em> inside).
    assert '<em>' not in out or 'Important' in out  # body won't have em here
    assert '<em>' not in out
    assert 'body' in out


def test_clean_html_dedupes_h1_with_trailing_whitespace() -> None:
    """Normalized equality: whitespace differences between title and body H1
    must not defeat dedupe.
    """
    html = (
        '<html><head><title>The Title</title></head><body>'
        '<article>'
        '<h1>  The   Title  </h1>'
        '<p>body</p>'
        '</article></body></html>'
    )
    out = _clean(html)
    assert out.count('<h1>') == 1
    assert 'body' in out


@pytest.mark.parametrize(
    "klass",
    [
        "authoritative-source",
        "authoritative",
        "subscriber-only-content",
        "share-of-voice",
        "co-authored",
        "related-work-citation",
        "commentary-pullquote",
    ],
)
def test_strip_noise_selectors_preserves_legit_content_with_substring(klass: str) -> None:
    """Class names that merely *contain* "author"/"share"/"subscribe"/"comment"
    etc. are real article content and must not be stripped.
    """
    html = (
        f'<html><body><div class="post-body">'
        f'<p class="{klass}">IMPORTANT-CONTENT</p>'
        f'<p>baseline</p>'
        f'</div></body></html>'
    )
    out = _clean(html)
    assert 'IMPORTANT-CONTENT' in out
    assert 'baseline' in out


@pytest.mark.parametrize(
    "klass",
    [
        "comment", "comments", "comment-form", "comment-section",
        "social-share", "share-buttons",
        "related-posts", "related-articles",
        "subscribe", "subscribe-box",
        "author", "author-bio", "post-author", "byline",
    ],
)
def test_strip_noise_selectors_removes_real_chrome(klass: str) -> None:
    html = (
        f'<html><body><div class="post-body">'
        f'<p>keeper</p>'
        f'<aside class="{klass}">REMOVE-ME</aside>'
        f'</div></body></html>'
    )
    out = _clean(html)
    assert 'keeper' in out
    assert 'REMOVE-ME' not in out


def test_strip_noise_selectors_handles_nested_noise() -> None:
    """Regression: a noise element containing another noise element must not
    crash the cleaner. Decomposing the outer tag detaches the inner one while
    it is still in the find_all() snapshot; the loop must skip the detached
    tag instead of calling .get('class') on its now-None attrs.
    """
    html = (
        '<html><head><title>T</title></head><body>'
        '<div class="post-body">'
        '<div class="author-box"><span class="comment">CHROME</span></div>'
        '<p>keeper</p>'
        '</div></body></html>'
    )
    out = _clean(html)
    assert 'keeper' in out
    assert 'CHROME' not in out


def test_clean_html_dedupes_h1_that_matches_title() -> None:
    """When the article body already carries its own <h1> matching the title,
    the cleaner must not emit two copies.
    """
    html = (
        '<html><head><title>The Title</title></head><body>'
        '<article>'
        '<h1>The Title</h1>'
        '<p>body</p>'
        '</article></body></html>'
    )
    out = _clean(html)
    # Ignore <h1> occurrences inside attribute values; count real tag opens.
    assert out.count('<h1>') == 1
    assert 'body' in out


def test_clean_html_keeps_distinct_h1_inside_article() -> None:
    """An <h1> inside the body that is NOT the article title must survive."""
    html = (
        '<html><head><title>Main Title</title></head><body>'
        '<article>'
        '<h1>Section Heading</h1>'
        '<p>body</p>'
        '</article></body></html>'
    )
    out = _clean(html)
    assert 'Main Title' in out
    assert 'Section Heading' in out
    assert out.count('<h1>') == 2


def test_clean_html_dedupes_h1_inside_hs_cos_wrapper() -> None:
    """The narrowing to ``hs_cos_wrapper`` must not defeat title dedupe: when
    the wrapper itself contains an ``<h1>`` that matches the extracted title,
    only one top-level ``<h1>`` should survive in the output.
    """
    html = (
        '<html><head><title>Wrap Title</title></head><body>'
        '<div class="post-body">'
        '<span class="hs_cos_wrapper">'
        '<h1>Wrap Title</h1>'
        '<p>body</p>'
        '</span>'
        '</div></body></html>'
    )
    out = _clean(html)
    assert out.count('<h1>') == 1
    assert 'body' in out


def test_find_article_root_ignores_substring_id_matches() -> None:
    """An id like ``mainstream-nav`` must not be picked as the article root."""
    html = (
        '<html><body>'
        '<div id="mainstream-nav">NAV</div>'
        '<section id="main-content"><p>body</p></section>'
        '</body></html>'
    )
    cleaner = MHTMLCleaner(mhtml_path="unused", downloader=lambda url: url)
    out = cleaner.clean_html(html, images={})
    assert 'body' in out
    # The nav id must not have been selected as the article root.
    assert 'NAV' not in out
