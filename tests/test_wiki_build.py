"""The wiki converter: names, links, the sidebar, and the files it writes."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import wiki_build as wb  # noqa: E402


def make_wiki(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


BASE = {
    "Home.md": "# Home\n",
    "Connect-to-Plex.md": "# Connect to Plex\n",
    "Install.md": "# Install\n",
}


def pages_of(tmp_path, files=None):
    return wb.read_pages(make_wiki(tmp_path, files or BASE))


def test_key_ignores_case_spaces_and_hyphens():
    assert wb.key("Connect-to-Plex") == wb.key("connect to plex") == wb.key("Connect  to_Plex")


def test_slug():
    assert wb.slug("Connect-to-Plex") == "connect-to-plex"
    assert wb.slug("What's new?") == "what-s-new"


def test_home_becomes_index(tmp_path):
    outs = {p.name: p.out for p in pages_of(tmp_path)}
    assert outs["Home"] == "index.md"
    assert outs["Connect-to-Plex"] == "connect-to-plex.md"


def test_no_home_is_an_error(tmp_path):
    with pytest.raises(wb.BuildError):
        wb.read_pages(make_wiki(tmp_path, {"Install.md": "x"}))


def test_special_files_are_not_pages(tmp_path):
    files = {**BASE, "_Sidebar.md": "- [[Home]]", "_Footer.md": "f"}
    assert {p.name for p in pages_of(tmp_path, files)} == {"Home", "Connect-to-Plex", "Install"}


def test_two_names_one_file_is_an_error(tmp_path):
    files = {**BASE, "install.md": "x", "Install-.md": "y"}
    with pytest.raises(wb.BuildError):
        pages_of(tmp_path, files)


def test_plain_wikilink(tmp_path):
    pages = pages_of(tmp_path)
    assert wb.convert_links("See [[Install]].", pages) == "See [Install](install.md)."


def test_wikilink_with_text(tmp_path):
    pages = pages_of(tmp_path)
    out = wb.convert_links("[[Set up Plex|Connect-to-Plex]]", pages)
    assert out == "[Set up Plex](connect-to-plex.md)"


def test_wikilink_spelled_with_spaces_and_case(tmp_path):
    pages = pages_of(tmp_path)
    assert wb.convert_links("[[connect to plex]]", pages) == "[connect to plex](connect-to-plex.md)"


def test_wikilink_to_home_and_anchor(tmp_path):
    pages = pages_of(tmp_path)
    assert wb.convert_links("[[Home]]", pages) == "[Home](index.md)"
    assert wb.convert_links("[[Folders|Install#first-run]]", pages) == "[Folders](install.md#first-run)"


def test_unknown_page_keeps_its_text_and_warns(tmp_path):
    pages = pages_of(tmp_path)
    warns = []
    assert wb.convert_links("[[Nope]] and [[Say|Nope]]", pages, warns, "Home.md") == "Nope and Say"
    assert len(warns) == 2 and "Nope" in warns[0]


def test_markdown_link_to_page_name(tmp_path):
    pages = pages_of(tmp_path)
    assert wb.convert_links("[go](Connect-to-Plex)", pages) == "[go](connect-to-plex.md)"
    assert wb.convert_links("[go](Install.md#x)", pages) == "[go](install.md#x)"


def test_other_markdown_links_are_left_alone(tmp_path):
    pages = pages_of(tmp_path)
    text = ("[a](https://example.org/Install) [b](#top) [c](/Install) "
            "[d](pic.png) [e](mailto:x@y.z) ![i](Install)")
    assert wb.convert_links(text, pages) == text


def test_code_is_left_alone(tmp_path):
    pages = pages_of(tmp_path)
    text = "Type `[[Install]]` now.\n\n```\n[[Install]]\n[x](Install)\n```\n\n[[Install]]\n"
    out = wb.convert_links(text, pages)
    assert "`[[Install]]`" in out
    assert "```\n[[Install]]\n[x](Install)\n```" in out
    assert out.rstrip().endswith("[Install](install.md)")


def test_edit_url_points_at_the_wiki_page(tmp_path):
    page = next(p for p in pages_of(tmp_path) if p.name == "Connect-to-Plex")
    assert page.edit_url == "https://github.com/beamflash/couchelephant/wiki/Connect-to-Plex/_edit"


def test_sidebar_nav_flat():
    pages = [wb.Page(wb.Path(n)) for n in ("Home.md", "Install.md", "Connect-to-Plex.md")]
    nav = wb.build_nav("- [[Home]]\n- [[Install]]\n- [[Plex|Connect-to-Plex]]\n", pages)
    assert nav == [{"Home": "index.md"}, {"Install": "install.md"}, {"Plex": "connect-to-plex.md"}]


def test_sidebar_nav_groups_and_home_added():
    pages = [wb.Page(wb.Path(n)) for n in ("Home.md", "Install.md", "Connect-to-Plex.md")]
    nav = wb.build_nav("- Setup\n  - [[Install]]\n  - [[Connect-to-Plex]]\n", pages)
    assert nav == [{"Home": "index.md"},
                   {"Setup": [{"Install": "install.md"}, {"Connect-to-Plex": "connect-to-plex.md"}]}]


def test_sidebar_unknown_page_is_dropped_with_a_warning():
    pages = [wb.Page(wb.Path("Home.md"))]
    warns = []
    nav = wb.build_nav("- [[Home]]\n- [[Ghost]]\n", pages, warns)
    assert nav == [{"Home": "index.md"}]
    assert warns and "Ghost" in warns[0]


def test_prepare_writes_docs_config_and_assets(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    make_wiki(wiki, {
        **BASE,
        "Home.md": "# Home\n\n[[Install]]\n![shot](shot.png)\n",
        "Orphan.md": "# Orphan\n",
        "_Sidebar.md": "- [[Home]]\n- [[Install]]\n",
        "_Footer.md": "Footer [[Install]]\n",
    })
    (wiki / "shot.png").write_bytes(b"png")
    (wiki / ".git").mkdir()
    (wiki / ".git" / "config").write_text("x")
    work = tmp_path / "work"
    warns = wb.prepare(wiki, work)
    assert warns == []
    home = (work / "docs" / "index.md").read_text()
    assert home.startswith('---\nedit_url: "https://github.com/beamflash/couchelephant/wiki/Home/_edit"')
    assert "[Install](install.md)" in home
    assert home.rstrip().endswith("Footer [Install](install.md)")
    assert (work / "docs" / "shot.png").read_bytes() == b"png"
    assert not (work / "docs" / ".git").exists()
    assert not (work / "docs" / "_Sidebar.md").exists()
    cfg = (work / "mkdocs.yml").read_text()
    assert '- "Install": install.md' in cfg
    # A page the sidebar forgot is still reachable.
    assert '- "Orphan": orphan.md' in cfg
    assert "name: material" in cfg and "- search" in cfg
    assert (work / "hooks.py").exists()


def test_front_matter_in_a_page_is_replaced(tmp_path):
    wiki = tmp_path / "w"
    wiki.mkdir()
    make_wiki(wiki, {**BASE, "Home.md": "---\ntitle: x\n---\n# Home\n"})
    wb.prepare(wiki, tmp_path / "work")
    text = (tmp_path / "work" / "docs" / "index.md").read_text()
    assert text.count("---\n") == 2 and "title: x" not in text
