"""Every link and image in the documentation resolves to something real.

This exists because of a bug that shipped: the README embedded two screenshots
that the export deliberately leaves behind, so the published repository would
have opened on two broken images. Nothing caught it. The test suite passed, the
linter passed, and the export's own leak scan passed, because none of them read
prose.

The walk starts at the two entry points a reader starts at and follows every
relative Markdown link it finds. That gives it exactly the reachable
documentation and nothing else, so it needs no list to maintain: a new page
linked from the index is checked automatically, and a page nobody links to is
correctly ignored.

Free, offline, no model calls, and it takes milliseconds.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ("README.md", "docs/README.md")

# [text](target) and <img src="target">, which is how the theme-aware figures
# are embedded. srcset is the dark half of the same <picture> element.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
HTML_SRC = re.compile(r"<(?:img|source)\b[^>]*?(?:src|srcset)=\"([^\"]+)\"")
SKIP_SCHEMES = ("http://", "https://", "mailto:", "#")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def prose(text: str) -> str:
    """Everything outside a fenced code block.

    A page that documents how to embed a figure contains an example `<img>`
    tag pointing at a path that is correct for the page being documented and
    wrong relative to itself. That is a code sample, not a link, and reading it
    as one is how a documentation check earns a reputation for crying wolf."""
    return FENCE.sub("", text)


def _targets(text: str) -> list[str]:
    body = prose(text)
    return [m for m in (*LINK.findall(body), *HTML_SRC.findall(body))
            if not m.startswith(SKIP_SCHEMES)]


def _walk() -> tuple[dict[Path, list[tuple[str, Path]]], list[str]]:
    """Breadth-first over the documentation. Returns every (page -> targets)
    pair found, plus any entry point that is itself missing."""
    found: dict[Path, list[tuple[str, Path]]] = {}
    missing_entries = [e for e in ENTRY_POINTS if not (ROOT / e).exists()]
    queue = [ROOT / e for e in ENTRY_POINTS if (ROOT / e).exists()]
    while queue:
        page = queue.pop(0)
        if page in found:
            continue
        rels: list[tuple[str, Path]] = []
        for raw in _targets(page.read_text(encoding="utf-8")):
            # Strip the fragment: anchors are checked separately below.
            target = (page.parent / unquote(raw.split("#", 1)[0])).resolve()
            rels.append((raw, target))
            if target.suffix == ".md" and target.exists() and target not in found:
                queue.append(target)
        found[page] = rels
    return found, missing_entries


PAGES, MISSING_ENTRIES = _walk()


def test_the_entry_points_exist() -> None:
    assert not MISSING_ENTRIES, f"missing documentation entry point: {MISSING_ENTRIES}"


def test_the_walk_actually_found_pages() -> None:
    """Guards the guard. A regex that silently stops matching would make every
    other test here pass by finding nothing to check."""
    assert len(PAGES) >= 5, f"only reached {len(PAGES)} pages, the walk is broken"
    assert sum(len(v) for v in PAGES.values()) >= 40


@pytest.mark.parametrize("page", sorted(PAGES, key=lambda p: p.as_posix()),
                         ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_link_resolves(page: Path) -> None:
    broken = [raw for raw, target in PAGES[page] if not target.exists()]
    assert not broken, (
        f"{page.relative_to(ROOT)} points at {len(broken)} thing(s) that do not "
        f"exist: {broken}")


@pytest.mark.parametrize("page", sorted(PAGES, key=lambda p: p.as_posix()),
                         ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_figure_has_alt_text(page: Path) -> None:
    """A diagram with no alt text is invisible to a screen reader and to anyone
    whose images failed to load, which on GitHub is anyone behind a proxy."""
    text = prose(page.read_text(encoding="utf-8"))
    bad = [m.group(0)[:70] for m in re.finditer(r"<img\b[^>]*>", text)
           if not re.search(r'alt="[^"]{10,}"', m.group(0))]
    bad += [m.group(0)[:70] for m in re.finditer(r"!\[\]\([^)]*\)", text)]
    assert not bad, f"{page.relative_to(ROOT)} has figures without alt text: {bad}"


def test_no_custom_heading_ids() -> None:
    """`## Heading {#id}` is a MyST and Pandoc extension. GitHub renders the
    braces literally and the link that pointed at them does not work, which is
    the kind of thing that only shows up after publishing."""
    offenders = []
    for page in PAGES:
        for lineno, line in enumerate(prose(page.read_text(encoding="utf-8")).splitlines(), 1):
            if line.startswith("#") and re.search(r"\{#[\w-]+\}\s*$", line):
                offenders.append(f"{page.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "use <a id=\"...\"></a> on its own line instead: " + ", ".join(offenders))


def test_anchor_links_point_at_a_real_anchor() -> None:
    """A link to `page.md#thing` where nothing declares `thing` is a link that
    silently lands at the top of the page."""
    dangling = []
    for page, rels in PAGES.items():
        for raw, target in rels:
            if "#" not in raw or not target.exists() or target.suffix != ".md":
                continue
            frag = raw.split("#", 1)[1]
            body = prose(target.read_text(encoding="utf-8"))
            if f'id="{frag}"' in body:
                continue
            # Otherwise it has to match a heading through GitHub's own slug
            # rules: lowercase, drop anything that is not a word character,
            # space or hyphen, then spaces to hyphens.
            slugs = set()
            for line in body.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    slug = re.sub(r"[^\w\s-]", "", title.lower()).strip()
                    slugs.add(re.sub(r"\s", "-", slug))
            if frag not in slugs:
                dangling.append(f"{page.relative_to(ROOT)} -> {raw}")
    assert not dangling, f"links to anchors that do not exist: {dangling}"
