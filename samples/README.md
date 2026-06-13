# Samples

Real-world examples used to exercise and demonstrate the cleaner.

- **`before/`** — the original `.mhtml` exports (full saved web pages, including
  navigation, sharing widgets, ads, and every embedded asset). These are the
  inputs.
- **`after/`** — the corresponding cleaned, standalone `.html` produced by
  running the tool on each `before/` file: just the article title, body, and
  inline images, with site chrome removed.

Regenerate every `after/` file from the sources:

```bash
# from the repository root
for f in samples/before/*.mhtml; do
    python clean_mhtml_article.py --force "$f" "samples/after/$(basename "${f%.mhtml}").html"
done
```

> The `before/` exports are third-party ThinkReliability / HubSpot blog articles,
> kept only as realistic test fixtures. They are not part of the licensed
> software.
