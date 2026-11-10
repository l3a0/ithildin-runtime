"""The risks page lists every section of the design that names a price.

`docs/architecture/11-risks.md` says it lists each section holding a sentence
that starts "The price". The pages it covers are the ones the architecture's
index maps: every page under `docs/architecture/`, `docs/design.md`,
`docs/what-a-person-runs.md` and `docs/halts-and-drills.md`. The charter
describes the team rather than the design, so it is not covered.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.support.price_sections import priced_sections

REPO_ROOT = Path(__file__).resolve().parents[1]
RISKS = REPO_ROOT / "docs" / "architecture" / "11-risks.md"
_LINK = re.compile(r"\]\(([^)#\s]+)#([^)\s]+)\)")


def _covered_pages() -> list[Path]:
    docs = REPO_ROOT / "docs"
    pages = sorted((docs / "architecture").glob("*.md"))
    pages += [docs / "design.md", docs / "what-a-person-runs.md", docs / "halts-and-drills.md"]
    return [page for page in pages if page != RISKS]


def _listed() -> set[tuple[Path, str]]:
    text = RISKS.read_text(encoding="utf-8")
    return {
        ((RISKS.parent / target).resolve(), fragment) for target, fragment in _LINK.findall(text)
    }


def test_a_price_counts_under_its_nearest_heading_and_skips_a_field_name() -> None:
    text = (
        "# Page\n\n## A\n\nThe price is one.\n\n### B\n\nThe price of two.\n\n"
        "| Field | The price and its source |\n\n```\nThe price is fenced.\n```\n"
    )
    assert priced_sections(text) == [(5, ["a"]), (9, ["a", "b"])]


def test_every_priced_section_is_listed_on_the_risks_page() -> None:
    listed = _listed()
    missing = []
    for page in _covered_pages():
        for number, chain in priced_sections(page.read_text(encoding="utf-8")):
            if not any((page.resolve(), slug) in listed for slug in chain):
                where = page.relative_to(REPO_ROOT)
                missing.append(f"{where}:{number}: {chain[-1] if chain else '(no heading)'}")
    assert missing == [], "add these sections to 11-risks.md:\n" + "\n".join(missing)
