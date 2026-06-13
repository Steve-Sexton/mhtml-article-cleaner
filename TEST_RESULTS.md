# MHTML Article Cleaner - Test Results

This file summarizes the automated test suite and a set of manual smoke tests
against real MHTML exports. It is **not** a production-readiness audit — it
documents what the current tests actually cover.

## Automated test suite

Run with:

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is deterministic, uses no network, and covers (file / focus):

- `tests/test_sanitize_filename.py` — empty, whitespace, invalid chars, Windows
  reserved device names, length/extension budget, degenerate `max_length`, and
  non-whitespace control-character stripping (whitespace controls collapse to a
  single space; words are not fused).
- `tests/test_parse_mhtml.py` — quoted/unquoted boundaries, missing boundary,
  LF-only separators, RFC 2045 case-insensitive headers (Content-Type,
  Content-Transfer-Encoding, MIME values, `Quoted-Printable`, `Base64`),
  Content-ID and Content-Location image extraction, largest-HTML-part
  selection, and graceful skip of malformed base64 parts.
- `tests/test_clean_html.py` — children-iteration regression, script/noscript
  removal, inline attribute stripping, title/h1 extraction and fallback,
  ArticleNotFoundError contract, HubSpot `hs_cos_wrapper` narrowing, unresolved
  CID warning (via `caplog`), offline default (network boom fixture),
  opt-in download path, CID embedding, MIME magic-byte detection, noise-selector
  allowlist (removes real chrome; preserves `authoritative-source`,
  `share-of-voice`, `co-authored`, `subscriber-only-content`,
  `related-work-citation`, `commentary-pullquote`), duplicate-`<h1>`
  deduplication, the `mainstream-nav` substring-id regression, the nested-noise
  crash regression (decomposing a noise parent must not dereference a detached
  noise child), and lazy-load image resolution (`data-src`/`data-lazy-src`/
  `data-original`/`srcset` promoted over an empty or `data:` placeholder `src`).
- `tests/test_cli.py` — happy path with title-derived filename, overwrite
  protection (exit 2), `--force`, missing-boundary exit code (3),
  article-not-found exit code (4), offline default (patched `urlopen`),
  scheme restriction (`file://`, `ftp://`), and end-to-end CID embedding.
- `tests/test_mime_from_url_suffix.py` — every recognized suffix (with and
  without query/fragment), case-insensitivity, and unknown-suffix → `None`.
- `tests/test_download_image.py` — MIME fallback chain
  (header → URL suffix → magic bytes), declared `Content-Length` cap,
  streaming overflow detection when no Content-Length is present,
  `URLError` graceful fallback, non-http(s) scheme refusal without calling
  `urlopen`, content-type allowlist (rejects `text/html`/`image/tiff`/SVG),
  `charset` parameter stripping, and the SSRF guard (private/loopback/
  link-local/reserved hosts refused without opening a connection; a host that
  resolves to a private IP is blocked, an unresolvable host is allowed to fail
  naturally). All tests inject a fake `urlopen` and stub DNS resolution; no real
  network traffic.

## Manual smoke tests

Three real HubSpot blog MHTML exports have been processed end-to-end and
visually inspected. These are **qualitative checks**, not automated assertions.

| Sample | MHTML images | Article images | Output size | Result |
|---|---|---|---|---|
| How Layers of Protection Reduce Risk | 37 | 7 | 140 KB | OK |
| The Cause Mapping® Investigation Template Explained | 33 | 2 | 64 KB | OK |
| Updating the Fishbone Diagram... | 32 | 3 | 140 KB | OK |

Each output was verified to: preserve the article title, embed all referenced
images as base64 data URIs, drop navigation/sharing/subscription chrome, and
render standalone in a browser without network access.

## What this file does NOT claim

- This is not a full production-readiness audit.
- No CI runs this suite; results above were produced on a developer machine.
- No load, soak, fuzzing, or adversarial-input testing has been performed.
- Security is scoped to the design constraints documented in the README
  (offline-by-default, opt-in `--download-missing` with a 20 MB size cap,
  scheme restriction, overwrite protection). It has not been independently
  reviewed.

## Reproducing the suite

```bash
pip install -r requirements-dev.txt
pytest -q
```

All tests pass on Python 3.9+ with `beautifulsoup4>=4.10,<5`.
