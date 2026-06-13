"""End-to-end CLI tests: overwrite protection, exit codes, network opt-in."""

from __future__ import annotations

import clean_mhtml_article as cli
from clean_mhtml_article import MHTMLCleaner, main
from tests.fixtures import PNG_BYTES, make_mhtml


def test_cli_happy_path(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mhtml"
    src.write_bytes(make_mhtml(
        html='<html><head><title>My Piece</title></head>'
             '<body><div class="post-body"><p>Hi</p></div></body></html>',
    ))
    monkeypatch.chdir(tmp_path)
    rc = main([str(src)])
    assert rc == 0
    out = tmp_path / "My Piece.html"
    assert out.exists()
    assert '<h1>' in out.read_text(encoding='utf-8')


def test_cli_refuses_to_overwrite_existing_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mhtml"
    src.write_bytes(make_mhtml(
        html='<html><head><title>Dup</title></head>'
             '<body><article><p>a</p></article></body></html>',
    ))
    monkeypatch.chdir(tmp_path)
    rc1 = main([str(src)])
    assert rc1 == 0
    rc2 = main([str(src)])
    assert rc2 == 2  # FileExistsError exit code
    assert (tmp_path / "Dup.html").exists()


def test_cli_force_overwrites(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mhtml"
    src.write_bytes(make_mhtml(
        html='<html><head><title>Force</title></head>'
             '<body><article><p>a</p></article></body></html>',
    ))
    monkeypatch.chdir(tmp_path)
    assert main([str(src)]) == 0
    assert main([str(src), "--force"]) == 0


def test_cli_missing_boundary_returns_nonzero(tmp_path) -> None:
    bad = tmp_path / "bad.mhtml"
    bad.write_bytes(b"not an mhtml")
    out = tmp_path / "out.html"
    rc = main([str(bad), str(out)])
    assert rc == 3
    assert not out.exists()


def test_cli_article_not_found_returns_nonzero_and_writes_nothing(
    tmp_path, monkeypatch, capsys
) -> None:
    """When no article root is detected, the CLI must not create an output file
    and must exit with a distinct non-zero code.
    """
    src = tmp_path / "in.mhtml"
    src.write_bytes(make_mhtml(
        html='<html><head><title>Empty</title></head><body></body></html>',
    ))
    monkeypatch.chdir(tmp_path)
    rc = main([str(src)])
    assert rc == 4
    # No output file should have been created.
    assert not (tmp_path / "Empty.html").exists()
    assert not (tmp_path / "clean_article.html").exists()
    err = capsys.readouterr().err
    assert 'article content' in err.lower()


def test_cli_default_does_not_touch_network(tmp_path, monkeypatch) -> None:
    """Without --download-missing, the cleaner must not reach the network."""
    def boom(*args, **kwargs):
        raise AssertionError("network must not be used")
    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)

    src = tmp_path / "in.mhtml"
    src.write_bytes(make_mhtml(
        html=(
            '<html><head><title>NoNet</title></head>'
            '<body><div class="post-body">'
            '<img src="https://example.invalid/a.png">'
            '<p>body</p>'
            '</div></body></html>'
        ),
    ))
    monkeypatch.chdir(tmp_path)
    assert main([str(src)]) == 0


def test_downloader_refuses_non_http_scheme(tmp_path) -> None:
    """Scheme restriction: file:// and ftp:// must be rejected without raising."""
    cleaner = MHTMLCleaner(mhtml_path="unused")
    # Should return the URL untouched (no urlopen attempt).
    assert cleaner.download_and_encode_image("file:///etc/passwd") == "file:///etc/passwd"
    assert cleaner.download_and_encode_image("ftp://example.invalid/x") == "ftp://example.invalid/x"


def test_cli_embeds_cid_image_end_to_end(tmp_path, monkeypatch) -> None:
    html = (
        '<html><head><title>Cid</title></head>'
        '<body><div class="post-body">'
        '<img src="cid:pic1">'
        '<p>body</p>'
        '</div></body></html>'
    )
    src = tmp_path / "in.mhtml"
    src.write_bytes(make_mhtml(
        html=html, images=[('pic1', PNG_BYTES, 'cid')],
    ))
    monkeypatch.chdir(tmp_path)
    assert main([str(src)]) == 0
    out_text = (tmp_path / "Cid.html").read_text(encoding='utf-8')
    assert 'data:image/png;base64,' in out_text
    assert 'cid:pic1' not in out_text
