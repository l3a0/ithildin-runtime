"""Find every section of a design page that names a price.

`docs/architecture/11-risks.md` promises to list each section that holds a
sentence starting "The price", so a reader can review every price the design
accepts in one place. A list kept by hand drifts as soon as a section gains a
price, and on 2026-11-10 a review found seven sections missing from it. This
module finds the sections, and `tests/test_risks_page.py` holds the list to
them.

A price is a sentence that starts "The price is", "The price of" or "The
price has", in prose or in a heading. A table cell such as "The price and its
source" names a field, not a price, so it does not count. Fenced blocks and
backtick spans are blanked first, as in the other prose sweeps.

A price belongs to the nearest heading above it. A link on the risks page to
that heading or to any heading above it, other than the page's title, covers
it, because the list links a section and its subsections are read with it.
"""

from __future__ import annotations

import re

from tests.support.markdown_links import _FENCE, _HEADING, anchor
from tests.support.markdown_sweep import blank_code

_PRICE = re.compile(r"\bThe price (?:is|of|has)\b")


def priced_sections(text: str) -> list[tuple[int, list[str]]]:
    """Each price's line number and the anchors of its heading chain, outermost first.

    The chain leaves out the page's title, so a link to the title never
    covers a price. Anchors are numbered the way GitHub numbers a repeated
    heading, matching `anchors()`.
    """
    prose = blank_code(text)
    found: set[str] = set()
    seen: dict[str, int] = {}
    chain: list[tuple[int, str]] = []
    prices: list[tuple[int, list[str]]] = []
    fence: str | None = None
    for number, (line, prose_line) in enumerate(
        zip(text.split("\n"), prose.split("\n"), strict=True), start=1
    ):
        opening = _FENCE.match(line)
        if opening:
            run = opening.group(1)
            if fence is None:
                fence = run[0] * len(run)
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            base = anchor(heading.group(2))
            slug = base
            while slug in found:
                seen[base] = seen.get(base, 0) + 1
                slug = f"{base}-{seen[base]}"
            found.add(slug)
            chain = [(lvl, a) for lvl, a in chain if lvl < level]
            chain.append((level, slug))
        if _PRICE.search(prose_line):
            prices.append((number, [a for lvl, a in chain if lvl > 1]))
    return prices
