"""Sketches for docs/architecture/05-building-blocks.md, in "How the broker's rate limit is shared".

Three scenes, each an animated whiteboard GIF in docs/figures, drawn in the
style of the blog's sketch notes with :mod:`ithildincore.whiteboard`.

1. :func:`buckets`, "One instance, two token buckets": what draws on each of an
   execution instance's two buckets, that each refills all the time and holds
   at most the doc's cap of tokens, that a request takes a token, and that an
   empty bucket makes the request wait. The refill, cap and token labels sit
   on the top bucket only, and the bottom bucket shares them.
2. :func:`sharing`, "How the account's 200 a minute are shared": drawn to scale,
   the account's limit, the reserve's quarter of it, and each instance's
   shares, the reserve's rounded down.
3. :func:`alarm`, "When an instance alarms, and when it waits": one instance's
   requests against its share, the alarm at the doc's threshold, and an order
   that would pass the share waiting before the order path takes the
   account's lease.

Scenes 1 and 3 are schematic. Their shapes, their token counts and how far the
gauge's bar fills carry no figures. Every figure a scene does draw, and the
scale of scene 2, comes from :func:`figures`, which reads them from the doc's
own section, so the text and the sketch cannot quote two numbers.

Redraw every GIF from the repository root with::

    uv run python -m docs.figures.sketches.rate_limit

``tests/test_rate_limit_sketches.py`` pins what each scene draws.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ithildincore.palette import ACCENT, INK, INKS, LOST, MUTED
from ithildincore.whiteboard import (
    Label,
    Scene,
    Stroke,
    arrow,
    line,
    paced_timeline,
    stroke,
    write_gif,
)

#: The folder the GIFs go in, and the doc whose section they draw.
FIGURES_DIR = Path(__file__).resolve().parents[1]
DOC = FIGURES_DIR.parent / "architecture" / "05-building-blocks.md"
SECTION = "##### How the broker's rate limit is shared"

_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}


@dataclass(frozen=True)
class Figures:
    """The figures the doc's section states, which the sketches draw."""

    limit: int
    reserve: int
    instances: int
    reserve_each: int
    orders_each: int
    cap: int
    alarm_percent: int


def _find(pattern: str, text: str, what: str) -> re.Match:
    found = re.search(pattern, text)
    if found is None:
        raise ValueError(f"the section no longer states {what}")
    return found


def figures(doc: Path = DOC) -> Figures:
    """Read the figures from the section of ``doc`` the sketches illustrate.

    Raises ``ValueError`` when the section no longer states one of them, or
    when its figures disagree with each other: a reserve that is not a quarter
    of the limit, or a per-instance share that is not the total split evenly
    and rounded down.
    """
    text = doc.read_text()
    start = text.index(SECTION)
    end = text.find("\n#### ", start)
    # An image's alt text quotes the sketches' own labels, so it is dropped:
    # a figure read from it would be the sketch quoting itself.
    section = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text[start : end if end != -1 else None])
    limit = int(_find(r"splits the account's (\d+) requests a minute", section, "the limit")[1])
    instances = _WORDS[_find(r"share, for (\w+) instances", section, "the instances")[1]]
    reserve_row = _find(
        r"\| The reserve \| A quarter, (\d+) a minute \| (\d+) a minute, rounded down \|",
        section,
        "the reserve's row",
    )
    orders_row = _find(
        r"\| Orders and lookups \| The rest \| (\d+) a minute \|", section, "the orders row"
    )
    found = Figures(
        limit=limit,
        reserve=int(reserve_row[1]),
        instances=instances,
        reserve_each=int(reserve_row[2]),
        orders_each=int(orders_row[1]),
        cap=int(_find(r"holds at most (\d+) tokens", section, "the cap")[1]),
        alarm_percent=int(_find(r"pass (\d+)% of its share", section, "the alarm")[1]),
    )
    if found.reserve * 4 != found.limit:
        raise ValueError(f"a reserve of {found.reserve} is not a quarter of {found.limit}")
    if found.reserve // found.instances != found.reserve_each:
        raise ValueError(
            f"{found.reserve_each} is not {found.reserve} split {found.instances} ways"
        )
    if (found.limit - found.reserve) // found.instances != found.orders_each:
        raise ValueError(f"{found.orders_each} is not the rest split {found.instances} ways")
    return found


# --- One instance's two token buckets -----------------------------------------

#: Each bucket's lip and floor, left and right, in board units.
LIP = (4.2, 5.5)
FLOOR = (4.35, 5.35)
#: Each row's bucket floor and lip heights, and the height its outlet runs at.
ROWS = {
    "reserve": (2.62, 3.7, 2.92),
    "orders": (0.62, 1.7, 1.0),
}
#: Where an outlet arrow ends, and where the words after it start.
OUT_TO, WORDS_AT = 7.1, 7.32


def _bucket(floor: float, lip: float) -> tuple:
    return line(
        [(LIP[0], lip), (FLOOR[0], floor), (FLOOR[1], floor), (LIP[1], lip)],
        per_unit=8,
    )


def _tokens(floor: float, count: int) -> tuple:
    xs, ys = [], []
    for i in range(count):
        row, col = divmod(i, 5)
        xs.append(4.55 + 0.17 * col + 0.085 * row)
        ys.append(floor + 0.13 + 0.16 * row)
    return np.array(xs), np.array(ys)


def _row(
    tag: str, words_in: str, tokens: int, words_out: str, name: str, notes: bool, cap_words: str
) -> list[Stroke | Label]:
    """One bucket, what flows into and out of it, and, when ``notes``, the
    labels for its refill, its cap and its outlet, which the other bucket shares."""
    floor, lip, out_y = ROWS[tag]
    cap = lip - 0.2
    steps: list[Stroke | Label] = [
        stroke(f"{tag}-bucket", _bucket(floor, lip), frames=6),
        stroke(f"{tag}-tokens", _tokens(floor, tokens), color=ACCENT, dots=8, frames=3),
        Label(name, sum(LIP) / 2, floor - 0.34, names=f"{tag}-bucket", ha="center", size=12),
        stroke(
            f"{tag}-refill",
            arrow(5.3, lip + 0.2, 5.3, lip + 0.03),
            color=MUTED,
            width=1.6,
            frames=3,
        ),
    ]
    if notes:
        steps.append(
            Label(
                "refills all the time",
                5.66,
                lip + 0.23,
                names=f"{tag}-refill",
                color=MUTED,
                size=11,
            )
        )
    steps.append(
        stroke(
            f"{tag}-cap",
            line([(LIP[0] + 0.05, cap), (LIP[1] - 0.05, cap)]),
            color=MUTED,
            dashed=True,
            width=1.6,
            frames=3,
        )
    )
    if notes:
        steps.append(Label(cap_words, 5.7, cap + 0.06, names=f"{tag}-cap", color=MUTED, size=11))
    steps += [
        stroke(f"{tag}-in", arrow(3.3, out_y + 0.25, 4.15, out_y + 0.25), frames=4),
        Label(words_in, 0.3, out_y + 0.25, names=f"{tag}-in", size=12),
        stroke(f"{tag}-out", arrow(5.5, out_y, OUT_TO, out_y), frames=4),
        Label(words_out, WORDS_AT, out_y, names=f"{tag}-out", size=12),
    ]
    if notes:
        steps.append(
            Label("takes a token", 5.62, out_y - 0.4, names=f"{tag}-out", color=MUTED, size=11)
        )
    return steps


def _empty() -> list[Stroke | Label]:
    x0, x1, y0, y1 = 7.32, 7.72, 1.95, 2.3
    outline = line([(x0, y1), (x0 + 0.06, y0), (x1 - 0.06, y0), (x1, y1)], per_unit=10)
    cross = line(
        [
            (x0 + 0.12, y1 - 0.06),
            (x1 - 0.12, y0 + 0.06),
            (x1 - 0.12, y1 - 0.06),
            (x0 + 0.12, y0 + 0.06),
        ],
        per_unit=10,
    )
    return [
        stroke("empty", outline, color=LOST, frames=3),
        stroke("empty-cross", cross, color=LOST, frames=3),
        Label(
            "empty: the\nrequest waits\nthe time the\nbucket names",
            7.95,
            2.1,
            names="empty",
            color=LOST,
            size=11,
        ),
    ]


def buckets(found: Figures | None = None) -> Scene:
    """One execution instance's two token buckets."""
    cap_words = f"holds at most {(found or figures()).cap} tokens"
    steps = _row(
        "reserve",
        "cancels, and the reads\nand lookups a cancel\nor a sort needs",
        6,
        "goes to the broker",
        "the reserve",
        notes=True,
        cap_words=cap_words,
    )
    steps += _row(
        "orders",
        "new orders and\nother lookups",
        8,
        "an order takes the\naccount's lease only\nnow, then the broker",
        "orders and lookups",
        notes=False,
        cap_words=cap_words,
    )
    steps += _empty()
    return Scene("One instance, two token buckets", "rate_limit_buckets", tuple(steps))


# --- Sharing the account's limit, drawn to scale --------------------------------

#: Where the bar starts, and how wide the whole limit is drawn, in board units.
BAR_X, BAR_WIDTH = 0.5, 9.0
#: The account's bar, and each instance's row, as (bottom, top). The rows
#: are laid out for the three instances the doc's table counts.
TOP_BAR = (3.14, 3.78)
INSTANCE_ROWS = ((2.36, 3.0), (1.63, 2.27), (0.9, 1.54))


def _rect(gid: str, x0: float, x1: float, rows: tuple[float, float], color: str) -> Stroke:
    return stroke(
        gid,
        line(
            [(x0, rows[0]), (x1, rows[0]), (x1, rows[1]), (x0, rows[1]), (x0, rows[0])], per_unit=10
        ),
        color=color,
        frames=4,
    )


def sharing(found: Figures | None = None) -> Scene:
    """The account's limit, split into the reserve and the rest, and each
    instance's share of both, drawn to scale."""
    f = found or figures()
    if f.instances != len(INSTANCE_ROWS):
        raise ValueError(f"the sketch lays out {len(INSTANCE_ROWS)} instances, not {f.instances}")

    def _x(requests: float) -> float:
        return BAR_X + requests * BAR_WIDTH / f.limit

    mid = sum(TOP_BAR) / 2
    steps: list[Stroke | Label] = [
        _rect("reserve", _x(0), _x(f.reserve), TOP_BAR, ACCENT),
        Label(
            f"reserve: {f.reserve}",
            (_x(0) + _x(f.reserve)) / 2,
            mid,
            names="reserve",
            color=ACCENT,
            ha="center",
            size=12,
        ),
        _rect("rest", _x(f.reserve), _x(f.limit), TOP_BAR, INK),
        Label(
            "orders and lookups: the rest",
            (_x(f.reserve) + _x(f.limit)) / 2,
            mid,
            names="rest",
            ha="center",
            size=12,
        ),
    ]
    for i, rows in enumerate(INSTANCE_ROWS):
        r0, o0 = i * f.reserve_each, f.reserve + i * f.orders_each
        row_mid = sum(rows) / 2
        steps += [
            _rect(f"i{i}-reserve", _x(r0), _x(r0 + f.reserve_each), rows, ACCENT),
            Label(
                str(f.reserve_each),
                (_x(r0) + _x(r0 + f.reserve_each)) / 2,
                row_mid,
                names=f"i{i}-reserve",
                color=ACCENT,
                ha="center",
                size=12,
            ),
            _rect(f"i{i}-orders", _x(o0), _x(o0 + f.orders_each), rows, INK),
            Label(
                f"instance {i + 1}: {f.orders_each}",
                (_x(o0) + _x(o0 + f.orders_each)) / 2,
                row_mid,
                names=f"i{i}-orders",
                ha="center",
                size=12,
            ),
        ]
    steps += [
        Label(
            f"Each instance takes {f.reserve_each} of the reserve, rounded down,"
            f" and {f.orders_each} of the rest.",
            BAR_X,
            0.55,
            names="i2-orders",
            color=MUTED,
            size=11,
        ),
        Label(
            "Together the shares stay under the limit, so no counter is shared.",
            BAR_X,
            0.24,
            names="i2-orders",
            color=MUTED,
            size=11,
        ),
    ]
    return Scene(
        f"How the account's {f.limit} a minute are shared", "rate_limit_shares", tuple(steps)
    )


# --- One instance's alarm --------------------------------------------------------

#: The gauge runs from no requests to the instance's share, and the alarm sits
#: at this fraction of it. The level drawn past the alarm is illustrative.
GAUGE = (0.8, 8.8)
GAUGE_Y = 2.2
LEVEL = 0.83


def _at(fraction: float) -> float:
    return GAUGE[0] + fraction * (GAUGE[1] - GAUGE[0])


def alarm(found: Figures | None = None) -> Scene:
    """One instance's requests against its share, and its two thresholds."""
    percent = (found or figures()).alarm_percent
    alarm_x, level_x = _at(percent / 100), _at(LEVEL)
    steps: list[Stroke | Label] = [
        stroke("gauge", line([(GAUGE[0], GAUGE_Y), (GAUGE[1], GAUGE_Y)], per_unit=8), frames=5),
        stroke(
            "zero",
            line([(GAUGE[0], GAUGE_Y - 0.15), (GAUGE[0], GAUGE_Y + 0.15)], per_unit=10),
            frames=2,
        ),
        Label("none", GAUGE[0], GAUGE_Y - 0.47, names="zero", ha="center", size=12),
        stroke(
            "share",
            line([(GAUGE[1], GAUGE_Y - 0.3), (GAUGE[1], GAUGE_Y + 0.3)], per_unit=10),
            width=2.6,
            frames=2,
        ),
        Label("its share", GAUGE[1], GAUGE_Y - 0.64, names="share", ha="center", size=12),
        stroke(
            "level",
            line([(GAUGE[0], GAUGE_Y), (level_x, GAUGE_Y)], per_unit=8),
            color=ACCENT,
            width=9,
            frames=8,
        ),
        Label(
            "its requests in a minute",
            GAUGE[0],
            GAUGE_Y + 0.52,
            names="level",
            color=ACCENT,
            size=12,
        ),
        stroke(
            "alarm",
            line([(alarm_x, GAUGE_Y - 0.55), (alarm_x, GAUGE_Y + 0.75)], per_unit=10),
            color=LOST,
            dashed=True,
            frames=3,
        ),
        Label(
            f"{percent}% of its share:\nthe instance alarms",
            alarm_x,
            GAUGE_Y + 1.3,
            names="alarm",
            color=LOST,
            ha="center",
            size=12,
        ),
        Label(
            "an order that would pass its share\nwaits for the budget, before the\n"
            "order path takes the account's lease",
            9.6,
            0.75,
            names="share",
            ha="right",
            size=12,
        ),
    ]
    return Scene("When an instance alarms, and when it waits", "rate_limit_alarm", tuple(steps))


def scenes(found: Figures | None = None) -> tuple[Scene, ...]:
    """The three scenes, in the order the doc places them."""
    f = found or figures()
    return (sharing(f), buckets(f), alarm(f))


def gif_name(scene: Scene) -> str:
    """The file the doc embeds, such as ``rate-limit-shares.gif``."""
    return scene.slug.replace("_", "-") + ".gif"


def main() -> None:
    """Redraw every GIF, paced, on the colour-blind safe inks."""
    for scene in scenes():
        path = write_gif(scene, paced_timeline(scene), FIGURES_DIR / gif_name(scene), INKS)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
