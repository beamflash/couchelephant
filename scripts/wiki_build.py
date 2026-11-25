#!/usr/bin/env python3
"""Turn the GitHub wiki into a MkDocs Material site.

The wiki repo is the only source. This script reads a clone of it, makes a
folder MkDocs can build, and (unless told not to) runs the build.

    python scripts/wiki_build.py --wiki wiki-src --out site/wiki

What it changes:

* Page file names ("Connect-to-Plex.md") become site names ("connect-to-plex.md").
  "Home" becomes the front page.
* `[[Page]]`, `[[Text|Page]]` and `[Text](Page-Name)` become links MkDocs can check.
* `_Sidebar.md` becomes the navigation. `_Footer.md` goes under every page.
* Every page gets an "Edit this page" link to its page in the GitHub wiki.
* Pictures and other files in the wiki are copied as they are.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

WIKI_URL = "https://github.com/beamflash/couchelephant/wiki"
SITE_URL = "https://couchelephant.app/wiki/"
SITE_NAME = "CouchElephant Help"
HOME_URL = "https://couchelephant.app"
SPECIAL = {"_sidebar", "_footer", "_header"}
MD_SUFFIXES = (".md", ".markdown")

WIKILINK = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
MDLINK = re.compile(r"(?<!\!)\[([^\[\]\n]*)\]\(([^()\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")


class BuildError(Exception):
    pass


def key(name: str) -> str:
    """The form two spellings of one page name share."""
    return re.sub(r"[\s\-_]+", " ", name).strip().lower()


def slug(name: str) -> str:
    """The file name a page gets on the site."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "page"


class Page:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem              # "Connect-to-Plex", the wiki's own name
        self.key = key(self.name)
        self.out = "index.md" if self.key == "home" else slug(self.name) + ".md"
        self.title = self.name.replace("-", " ")

    @property
    def edit_url(self) -> str:
        return f"{WIKI_URL}/{quote(self.name, safe='')}/_edit"


def is_plain_file(f: Path, wiki: Path) -> bool:
    """A real file inside the wiki folder. A link could point anywhere."""
    if f.is_symlink() or not f.is_file():
        return False
    try:
        f.resolve().relative_to(wiki.resolve())
    except ValueError:
        return False
    return True


def read_pages(wiki: Path) -> list[Page]:
    pages = []
    for p in sorted(wiki.iterdir()):
        if (p.suffix.lower() in MD_SUFFIXES and p.stem.lower() not in SPECIAL
                and is_plain_file(p, wiki)):
            pages.append(Page(p))
    if not any(p.out == "index.md" for p in pages):
        raise BuildError("The wiki has no Home page.")
    seen = {}
    for p in pages:
        if p.out in seen:
            raise BuildError(f"{p.path.name} and {seen[p.out]} would both become {p.out}.")
        seen[p.out] = p.path.name
    return pages


def _split_code(text: str):
    """Yield (is_code, chunk) so links inside code are left alone."""
    out = []
    infence = False
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        if FENCE.match(line):
            if not infence:
                if buf:
                    out.append((False, "".join(buf)))
                buf = [line]
                infence = True
            else:
                buf.append(line)
                out.append((True, "".join(buf)))
                buf = []
                infence = False
            continue
        buf.append(line)
    if buf:
        out.append((infence, "".join(buf)))
    return out


def convert_links(text: str, pages: list[Page], warnings: list[str] | None = None,
                  where: str = "") -> str:
    """Rewrite wiki links in `text` as links to the site's own files."""
    by_key = {p.key: p for p in pages}

    def target(name: str):
        name, _, anchor = name.partition("#")
        page = by_key.get(key(name))
        if page is None:
            return None
        return page.out + (f"#{anchor}" if anchor else "")

    def wiki(m):
        body = m.group(1)
        if "|" in body:
            label, name = (s.strip() for s in body.split("|", 1))
        else:
            label = name = body.strip()
        dest = target(name)
        if dest is None:
            if warnings is not None:
                warnings.append(f"{where}: no page named '{name}'")
            return label
        return f"[{label}]({dest})"

    def md(m):
        label, href = m.group(1), m.group(2)
        # Only a bare page name is a wiki link: no scheme, no folder, no extension.
        if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I) or href.startswith(("#", "/", ".", "?")):
            return m.group(0)
        path, hash_, anchor = href.partition("#")
        if path.lower().endswith(MD_SUFFIXES):
            path = path.rsplit(".", 1)[0]
        dest = target(path + hash_ + anchor)
        if dest is None:
            return m.group(0)
        return f"[{label}]({dest})"

    out = []
    for is_code, chunk in _split_code(text):
        if is_code:
            out.append(chunk)
            continue
        # Inline code spans stay as they are.
        parts = re.split(r"(`[^`\n]*`)", chunk)
        for i, part in enumerate(parts):
            if i % 2 == 0:
                part = WIKILINK.sub(wiki, part)
                part = MDLINK.sub(md, part)
            out.append(part)
    return "".join(out)


# A MkDocs hook. MkDocs 1.6 ignores `edit_url` in a page's front matter, so
# this copies it onto the page before the theme draws the "Edit this page" link.
HOOK = """\
def on_page_context(context, page, config, nav):
    url = page.meta.get("edit_url")
    if url:
        page.edit_url = url
    return context
"""


def _front_matter(page: Page) -> str:
    # JSON quoting is valid YAML, so no character in the address can break it.
    return f"---\nedit_url: {json.dumps(page.edit_url)}\n---\n\n"


def strip_front_matter(text: str) -> str:
    """A wiki page has none, but a pasted one would clash with ours."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:].lstrip("\n")
    return text


def _items(sidebar: str):
    """Parse the sidebar into (indent, label, link-or-None)."""
    items = []
    for line in sidebar.splitlines():
        m = re.match(r"^(\s*)[-*+]\s+(.*\S)\s*$", line)
        if not m:
            continue
        indent = len(m.group(1).replace("\t", "    "))
        body = m.group(2)
        link = re.search(r"\[\[([^\[\]]+)\]\]", body)
        mdl = re.search(r"\[([^\]]*)\]\(([^)\s]+)\)", body)
        if link:
            inner = link.group(1)
            label, name = (s.strip() for s in inner.split("|", 1)) if "|" in inner \
                else (inner.strip(), inner.strip())
            items.append((indent, label, name))
        elif mdl:
            items.append((indent, mdl.group(1), mdl.group(2)))
        else:
            items.append((indent, re.sub(r"[*_`]", "", body).strip(), None))
    return items


def build_nav(sidebar: str, pages: list[Page], warnings: list[str] | None = None):
    """The MkDocs nav list, from the sidebar's bullets. Home is always first."""
    by_key = {p.key: p for p in pages}

    def file_of(name):
        page = by_key.get(key(name.split("#")[0]))
        return page.out if page else None

    items = _items(sidebar)

    def parse(i, indent):
        nav = []
        while i < len(items):
            ind, label, name = items[i]
            if ind < indent:
                break
            i += 1
            kids = []
            if i < len(items) and items[i][0] > ind:
                kids, i = parse(i, items[i][0])
            dest = file_of(name) if name else None
            if name and dest is None and warnings is not None:
                warnings.append(f"_Sidebar: no page named '{name}'")
            if kids:
                if dest:
                    kids.insert(0, {label: dest})
                nav.append({label: kids})
            elif dest:
                nav.append({label: dest})
        return nav, i

    nav, _ = parse(0, items[0][0]) if items else ([], 0)
    flat = _flatten(nav)
    if "index.md" not in flat:
        nav.insert(0, {"Home": "index.md"})
    return nav


def _flatten(nav):
    out = []
    for n in nav:
        for v in n.values():
            out += _flatten(v) if isinstance(v, list) else [v]
    return out


def _yaml(v, indent=0):
    pad = "  " * indent
    lines = []
    for n in v:
        for label, val in n.items():
            q = json.dumps(label)
            if isinstance(val, list):
                lines.append(f"{pad}- {q}:")
                lines += _yaml(val, indent + 1)
            else:
                lines.append(f"{pad}- {q}: {val}")
    return lines


def mkdocs_config(nav, has_logo: bool) -> str:
    lines = [
        f"site_name: {SITE_NAME}",
        f"site_url: {SITE_URL}",
        "docs_dir: docs",
        # MkDocs shows no edit link unless this is set. Each page then replaces
        # it with its own address (see _front_matter), since a site file name
        # is not always the wiki page name.
        f'edit_uri_template: "{WIKI_URL}/{{path_noext}}/_edit"',
        "use_directory_urls: true",
        "theme:",
        "  name: material",
        "  language: en",
        "  font: false",
        "  features:",
        "    - navigation.top",
        "    - search.highlight",
        "    - content.action.edit",
        "  palette:",
        "    - media: \"(prefers-color-scheme: light)\"",
        "      scheme: default",
        "      primary: deep orange",
        "      accent: deep orange",
        "    - media: \"(prefers-color-scheme: dark)\"",
        "      scheme: slate",
        "      primary: deep orange",
        "      accent: deep orange",
    ]
    if has_logo:
        lines += ["  logo: assets/mark.png", "  favicon: assets/mark.png"]
    lines += [
        "hooks:",
        "  - hooks.py",
        "extra:",
        f"  homepage: {HOME_URL}",
        "  generator: false",
        "plugins:",
        "  - search",
        "markdown_extensions:",
        "  - tables",
        "  - admonition",
        "  - toc:",
        "      permalink: true",
        "nav:",
    ]
    lines += _yaml(nav, 1)
    return "\n".join(lines) + "\n"


def prepare(wiki: Path, work: Path, logo: Path | None = None):
    """Write `work/docs` and `work/mkdocs.yml`. Returns the warnings."""
    pages = read_pages(wiki)
    docs = work / "docs"
    if docs.exists():
        shutil.rmtree(docs)
    docs.mkdir(parents=True)
    warnings: list[str] = []

    footer = ""
    for f in wiki.iterdir():
        if (f.stem.lower() == "_footer" and f.suffix.lower() in MD_SUFFIXES
                and is_plain_file(f, wiki)):
            footer = convert_links(f.read_text(encoding="utf-8"), pages, warnings, f.name)

    for p in pages:
        text = strip_front_matter(p.path.read_text(encoding="utf-8"))
        body = convert_links(text, pages, warnings, p.path.name)
        if footer.strip():
            body = body.rstrip() + "\n\n---\n\n" + footer.strip() + "\n"
        (docs / p.out).write_text(_front_matter(p) + body, encoding="utf-8")

    # Pictures and files the pages use.
    for f in wiki.rglob("*"):
        rel = f.relative_to(wiki)
        if ".git" in rel.parts or not is_plain_file(f, wiki):
            continue
        if f.suffix.lower() in MD_SUFFIXES or f.name.startswith("_"):
            continue
        dest = docs / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)

    has_logo = False
    if logo and logo.is_file():
        (docs / "assets").mkdir(exist_ok=True)
        shutil.copy2(logo, docs / "assets" / "mark.png")
        has_logo = True

    sidebar = ""
    for f in wiki.iterdir():
        if (f.stem.lower() == "_sidebar" and f.suffix.lower() in MD_SUFFIXES
                and is_plain_file(f, wiki)):
            sidebar = f.read_text(encoding="utf-8")
    nav = build_nav(sidebar, pages, warnings)
    # A page the sidebar forgot would still build, but nobody could find it.
    listed = set(_flatten(nav))
    nav.extend({p.title: p.out} for p in pages if p.out not in listed)
    (work / "hooks.py").write_text(HOOK, encoding="utf-8")
    (work / "mkdocs.yml").write_text(mkdocs_config(nav, has_logo), encoding="utf-8")
    return warnings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wiki", required=True, type=Path, help="a clone of the wiki repo")
    ap.add_argument("--out", required=True, type=Path, help="where the built site goes")
    ap.add_argument("--work", type=Path, default=Path(".wiki-work"),
                    help="scratch folder for the MkDocs input")
    ap.add_argument("--logo", type=Path, default=Path("site/mark.png"))
    ap.add_argument("--prepare-only", action="store_true", help="do not run MkDocs")
    args = ap.parse_args(argv)

    try:
        warnings = prepare(args.wiki, args.work, args.logo)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.prepare_only:
        return 0
    cmd = [sys.executable, "-m", "mkdocs", "build", "--strict", "-q",
           "-f", str(args.work / "mkdocs.yml"), "-d", str(args.out.resolve())]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
