"""Sketches for docs/architecture.md, in "How the broker's rate limit is shared".

Three scenes, each an animated whiteboard GIF in docs/figures, drawn in the
style of the blog's sketch notes.

1. ``scene``, "One instance, two token buckets": what draws on each of an
   execution instance's two buckets, that each refills all the time and holds at
   most 10 tokens, that a request takes a token, and that an empty bucket makes
   the request wait. The refill, cap and token labels sit on the top bucket only,
   and the bottom bucket shares them.
2. ``sharing``, "How the account's 200 a minute are shared": drawn to scale from
   the figures in the doc's table, the account's limit, the reserve's quarter,
   and each of three instances' shares, the reserve's rounded down.
3. ``alarm``, "When an instance alarms, and when it waits": one instance's
   requests against its share, the alarm at 75% of it, and an order that would
   pass the share waiting before the order path takes the account's lease. The
   level drawn is illustrative.

Scenes 1 and 3 are schematic: their shapes and token counts carry no figures.

The scenes use the whiteboard helpers of l3a0/quantitative-trading's ``chan``
package, the ones its ``sketch-notes`` skill draws with, until
l3a0/ithildin-core#24 moves them into ithildin-core for both repositories. Until
then this module runs from a quantitative-trading checkout, and no test here
pins the GIFs. Redraw each scene, paced, with that skill's draft script::

    PYTHONPATH=<this folder> uv run python .claude/skills/sketch-notes/draft.py \
        rate_limit:scene --out <scratch> --paced --palette

and copy the GIF into docs/figures under the name the doc embeds.
"""

from __future__ import annotations

import numpy as np
from chan.palette import ACCENT, INK, LOST
from chan.regime_figure import MUTED
from chan.usdcad_tests_whiteboard import Label, Scene, Stroke, _arrow, _line, _stroke

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
    return _line(
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
    tag: str, words_in: str, tokens: int, words_out: str, name: str, notes: bool
) -> list[Stroke | Label]:
    """One bucket, what flows into and out of it, and, when ``notes``, the
    labels for its refill, its cap and its outlet, which the other bucket shares."""
    floor, lip, out_y = ROWS[tag]
    cap = lip - 0.2
    steps: list[Stroke | Label] = [
        _stroke(f"{tag}-bucket", _bucket(floor, lip), frames=6),
        _stroke(f"{tag}-tokens", _tokens(floor, tokens), color=ACCENT, dots=8, frames=3),
        Label(name, sum(LIP) / 2, floor - 0.34, names=f"{tag}-bucket", ha="center", size=12),
        _stroke(
            f"{tag}-refill",
            _arrow(5.3, lip + 0.2, 5.3, lip + 0.03),
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
        _stroke(
            f"{tag}-cap",
            _line([(LIP[0] + 0.05, cap), (LIP[1] - 0.05, cap)]),
            color=MUTED,
            dashed=True,
            width=1.6,
            frames=3,
        )
    )
    if notes:
        steps.append(
            Label(
                "holds at most 10 tokens", 5.7, cap + 0.06, names=f"{tag}-cap", color=MUTED, size=11
            )
        )
    steps += [
        _stroke(f"{tag}-in", _arrow(3.3, out_y + 0.25, 4.15, out_y + 0.25), frames=4),
        Label(words_in, 0.3, out_y + 0.25, names=f"{tag}-in", size=12),
        _stroke(f"{tag}-out", _arrow(5.5, out_y, OUT_TO, out_y), frames=4),
        Label(words_out, WORDS_AT, out_y, names=f"{tag}-out", size=12),
    ]
    if notes:
        steps.append(
            Label("takes a token", 5.62, out_y - 0.4, names=f"{tag}-out", color=MUTED, size=11)
        )
    return steps


def _empty() -> list[Stroke | Label]:
    x0, x1, y0, y1 = 7.32, 7.72, 1.95, 2.3
    outline = _line([(x0, y1), (x0 + 0.06, y0), (x1 - 0.06, y0), (x1, y1)], per_unit=10)
    cross = _line(
        [
            (x0 + 0.12, y1 - 0.06),
            (x1 - 0.12, y0 + 0.06),
            (x1 - 0.12, y1 - 0.06),
            (x0 + 0.12, y0 + 0.06),
        ],
        per_unit=10,
    )
    return [
        _stroke("empty", outline, color=LOST, frames=3),
        _stroke("empty-cross", cross, color=LOST, frames=3),
        Label(
            "empty: the\nrequest waits\nthe time the\nbucket names",
            7.95,
            2.1,
            names="empty",
            color=LOST,
            size=11,
        ),
    ]


def scene() -> Scene:
    steps = _row(
        "reserve",
        "cancels, and the reads\nand lookups a cancel\nor a sort needs",
        6,
        "goes to the broker",
        "the reserve",
        notes=True,
    )
    steps += _row(
        "orders",
        "new orders and\nother lookups",
        8,
        "an order takes the\naccount's lease only\nnow, then the broker",
        "orders and lookups",
        notes=False,
    )
    steps += _empty()
    return Scene("One instance, two token buckets", "rate_limit_buckets", tuple(steps))


# --- Sharing the account's limit, drawn to scale --------------------------------

#: The doc's figures: the account's limit, the reserve's quarter of it, and
#: each of three instances' shares, the reserve's rounded down.
LIMIT, RESERVE, INSTANCES = 200, 50, 3
RESERVE_EACH, ORDERS_EACH = RESERVE // INSTANCES, (LIMIT - RESERVE) // INSTANCES
#: Where the bar starts, and board units per request a minute.
BAR_X, PER = 0.5, 9.0 / LIMIT
#: The account's bar, and each instance's row, as (bottom, top).
TOP_BAR = (3.14, 3.78)
INSTANCE_ROWS = ((2.36, 3.0), (1.63, 2.27), (0.9, 1.54))


def _rect(gid: str, x0: float, x1: float, rows: tuple[float, float], color: str) -> Stroke:
    return _stroke(
        gid,
        _line(
            [(x0, rows[0]), (x1, rows[0]), (x1, rows[1]), (x0, rows[1]), (x0, rows[0])], per_unit=10
        ),
        color=color,
        frames=4,
    )


def _x(requests: float) -> float:
    return BAR_X + requests * PER


def sharing() -> Scene:
    mid = sum(TOP_BAR) / 2
    steps: list[Stroke | Label] = [
        _rect("reserve", _x(0), _x(RESERVE), TOP_BAR, ACCENT),
        Label(
            f"reserve: {RESERVE}",
            (_x(0) + _x(RESERVE)) / 2,
            mid,
            names="reserve",
            color=ACCENT,
            ha="center",
            size=12,
        ),
        _rect("rest", _x(RESERVE), _x(LIMIT), TOP_BAR, INK),
        Label(
            "orders and lookups: the rest",
            (_x(RESERVE) + _x(LIMIT)) / 2,
            mid,
            names="rest",
            ha="center",
            size=12,
        ),
    ]
    for i, rows in enumerate(INSTANCE_ROWS):
        r0, o0 = i * RESERVE_EACH, RESERVE + i * ORDERS_EACH
        row_mid = sum(rows) / 2
        steps += [
            _rect(f"i{i}-reserve", _x(r0), _x(r0 + RESERVE_EACH), rows, ACCENT),
            Label(
                str(RESERVE_EACH),
                (_x(r0) + _x(r0 + RESERVE_EACH)) / 2,
                row_mid,
                names=f"i{i}-reserve",
                color=ACCENT,
                ha="center",
                size=12,
            ),
            _rect(f"i{i}-orders", _x(o0), _x(o0 + ORDERS_EACH), rows, INK),
            Label(
                f"instance {i + 1}: {ORDERS_EACH}",
                (_x(o0) + _x(o0 + ORDERS_EACH)) / 2,
                row_mid,
                names=f"i{i}-orders",
                ha="center",
                size=12,
            ),
        ]
    steps += [
        Label(
            f"Each instance takes {RESERVE_EACH} of the reserve, rounded down,"
            f" and {ORDERS_EACH} of the rest.",
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
        f"How the account's {LIMIT} a minute are shared", "rate_limit_shares", tuple(steps)
    )


# --- One instance's alarm --------------------------------------------------------

#: The gauge runs from no requests to the instance's share, and the alarm sits
#: at this fraction of it. The level drawn past the alarm is illustrative.
GAUGE = (0.8, 8.8)
GAUGE_Y = 2.2
ALARM_AT = 0.75
LEVEL = 0.83


def _at(fraction: float) -> float:
    return GAUGE[0] + fraction * (GAUGE[1] - GAUGE[0])


def alarm() -> Scene:
    alarm_x, level_x = _at(ALARM_AT), _at(LEVEL)
    steps: list[Stroke | Label] = [
        _stroke("gauge", _line([(GAUGE[0], GAUGE_Y), (GAUGE[1], GAUGE_Y)], per_unit=8), frames=5),
        _stroke(
            "zero",
            _line([(GAUGE[0], GAUGE_Y - 0.15), (GAUGE[0], GAUGE_Y + 0.15)], per_unit=10),
            frames=2,
        ),
        Label("none", GAUGE[0], GAUGE_Y - 0.47, names="zero", ha="center", size=12),
        _stroke(
            "share",
            _line([(GAUGE[1], GAUGE_Y - 0.3), (GAUGE[1], GAUGE_Y + 0.3)], per_unit=10),
            width=2.6,
            frames=2,
        ),
        Label("its share", GAUGE[1], GAUGE_Y - 0.64, names="share", ha="center", size=12),
        _stroke(
            "level",
            _line([(GAUGE[0], GAUGE_Y), (level_x, GAUGE_Y)], per_unit=8),
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
        _stroke(
            "alarm",
            _line([(alarm_x, GAUGE_Y - 0.55), (alarm_x, GAUGE_Y + 0.75)], per_unit=10),
            color=LOST,
            dashed=True,
            frames=3,
        ),
        Label(
            f"{round(ALARM_AT * 100)}% of its share:\nthe instance alarms",
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


def all_three() -> tuple[Scene, ...]:
    return (scene(), sharing(), alarm())
