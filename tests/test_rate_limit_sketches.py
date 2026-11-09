"""What the rate limit section's three sketches draw, and where the doc places them.

The scenes live in ``docs/figures/sketches/rate_limit.py`` and draw with
:mod:`ithildincore.whiteboard`. These tests pin each scene's words, colours,
figures and geometry, the layout rules every sketch shares, each GIF's place
and alt text in ``docs/architecture/05-building-blocks.md``, and that each committed GIF is a
fresh draw.
"""

from __future__ import annotations

import platform
import re
from pathlib import Path

import pytest
from docs.figures.sketches import rate_limit as r
from ithildincore.palette import ACCENT, INK, INKS, LOST, MUTED
from ithildincore.whiteboard import (
    HEIGHT,
    WIDTH,
    Label,
    Stroke,
    frame_problems,
    hand_font,
    inks_for,
    layout_problems,
    paced_timeline,
    write_gif,
)

DOC_TEXT = r.DOC.read_text()
FOUND = r.figures()


def _labels(scene) -> list[str]:
    return [step.text for step in scene.steps if isinstance(step, Label)]


def _step(scene, gid: str) -> Stroke:
    return next(s for s in scene.steps if isinstance(s, Stroke) and s.gid == gid)


def _label(scene, text: str) -> Label:
    return next(s for s in scene.steps if isinstance(s, Label) and s.text == text)


# --- The figures come from the doc ---------------------------------------------


def test_the_figures_are_the_ones_the_doc_states() -> None:
    assert FOUND == r.Figures(
        limit=200,
        reserve=50,
        instances=3,
        reserve_each=16,
        orders_each=50,
        cap=10,
        alarm_percent=75,
    )


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ("A quarter, 50 a minute", "A quarter, 60 a minute", "not a quarter"),
        ("16 a minute, rounded down", "17 a minute, rounded down", "split 3 ways"),
        ("| The rest | 50 a minute |", "| The rest | 49 a minute |", "the rest split"),
        ("holds at most 10 tokens", "holds at most some tokens", "the cap"),
    ],
)
def test_a_doc_whose_figures_disagree_stops_the_drawing(
    tmp_path: Path, old: str, new: str, complaint: str
) -> None:
    assert DOC_TEXT.count(old) >= 1
    doc = tmp_path / "architecture.md"
    doc.write_text(DOC_TEXT.replace(old, new, 1))
    with pytest.raises(ValueError, match=complaint):
        r.figures(doc)


def test_a_changed_figure_reaches_every_scene_that_quotes_it() -> None:
    made_up = r.Figures(
        limit=400,
        reserve=100,
        instances=3,
        reserve_each=33,
        orders_each=100,
        cap=12,
        alarm_percent=80,
    )
    assert "holds at most 12 tokens" in _labels(r.buckets(made_up))
    shares = r.sharing(made_up)
    assert shares.title == "How the account's 400 a minute are shared"
    assert "reserve: 100" in _labels(shares)
    assert _labels(shares).count("33") == 3
    assert "80% of its share:\nthe instance alarms" in _labels(r.alarm(made_up))


# --- Words ---------------------------------------------------------------------


def test_the_buckets_scene_says_these_words_in_this_order() -> None:
    scene = r.buckets(FOUND)
    assert scene.title == "One instance, two token buckets"
    assert _labels(scene) == [
        "the reserve",
        "refills all the time",
        "holds at most 10 tokens",
        "cancels, and the reads\nand lookups a cancel\nor a sort needs",
        "goes to the broker",
        "takes a token",
        "orders and lookups",
        "new orders and\nother lookups",
        "an order takes the\naccount's lease only\nnow, then the broker",
        "empty: the\nrequest waits\nthe time the\nbucket names",
    ]


def test_the_sharing_scene_says_these_words_in_this_order() -> None:
    scene = r.sharing(FOUND)
    assert scene.title == "How the account's 200 a minute are shared"
    assert _labels(scene) == [
        "reserve: 50",
        "orders and lookups: the rest",
        "16",
        "instance 1: 50",
        "16",
        "instance 2: 50",
        "16",
        "instance 3: 50",
        "Each instance takes 16 of the reserve, rounded down, and 50 of the rest.",
        "Together the shares stay under the limit, so no counter is shared.",
    ]


def test_the_alarm_scene_says_these_words_in_this_order() -> None:
    scene = r.alarm(FOUND)
    assert scene.title == "When an instance alarms, and when it waits"
    assert _labels(scene) == [
        "none",
        "its share",
        "its requests in a minute",
        "75% of its share:\nthe instance alarms",
        "an order that would pass its share\nwaits for the budget, before the\n"
        "order path takes the account's lease",
    ]


# --- Colours -------------------------------------------------------------------


def test_each_mark_carries_the_ink_its_meaning_calls_for() -> None:
    buckets, shares, alarm = r.buckets(FOUND), r.sharing(FOUND), r.alarm(FOUND)
    assert _step(buckets, "reserve-tokens").color == ACCENT
    assert _step(buckets, "orders-tokens").color == ACCENT
    assert _step(buckets, "empty").color == LOST
    assert _label(buckets, "empty: the\nrequest waits\nthe time the\nbucket names").color == LOST
    assert _step(buckets, "reserve-cap").color == MUTED
    assert _step(buckets, "reserve-cap").dashed
    assert _step(shares, "reserve").color == ACCENT
    assert _step(shares, "rest").color == INK
    for i in range(3):
        assert _step(shares, f"i{i}-reserve").color == ACCENT
        assert _step(shares, f"i{i}-orders").color == INK
    assert _step(alarm, "level").color == ACCENT
    assert _step(alarm, "alarm").color == LOST
    assert _step(alarm, "alarm").dashed


def test_every_scene_draws_on_the_colour_blind_safe_inks_alone() -> None:
    for scene in r.scenes(FOUND):
        inks, mixed = inks_for(scene, moved=True)
        assert inks == INKS
        assert mixed == []


# --- Geometry ------------------------------------------------------------------


def _x_span(stroke: Stroke) -> tuple[float, float]:
    return float(min(stroke.x)), float(max(stroke.x))


def test_the_sharing_scene_is_drawn_to_scale() -> None:
    scene = r.sharing(FOUND)
    per = r.BAR_WIDTH / FOUND.limit
    reserve0, reserve1 = _x_span(_step(scene, "reserve"))
    rest0, rest1 = _x_span(_step(scene, "rest"))
    assert reserve1 - reserve0 == pytest.approx(FOUND.reserve * per)
    assert rest1 - rest0 == pytest.approx((FOUND.limit - FOUND.reserve) * per)
    for i in range(FOUND.instances):
        a0, a1 = _x_span(_step(scene, f"i{i}-reserve"))
        b0, b1 = _x_span(_step(scene, f"i{i}-orders"))
        assert a1 - a0 == pytest.approx(FOUND.reserve_each * per)
        assert b1 - b0 == pytest.approx(FOUND.orders_each * per)
        assert a0 == pytest.approx(reserve0 + i * FOUND.reserve_each * per)
        assert b0 == pytest.approx(rest0 + i * FOUND.orders_each * per)


def test_rounding_down_leaves_the_last_reserve_share_short_of_the_reserve() -> None:
    scene = r.sharing(FOUND)
    _, reserve_end = _x_span(_step(scene, "reserve"))
    _, last_end = _x_span(_step(scene, f"i{FOUND.instances - 1}-reserve"))
    assert last_end < reserve_end


def test_the_alarm_sits_at_the_doc_s_share_of_the_gauge() -> None:
    scene = r.alarm(FOUND)
    start, end = r.GAUGE
    x = float(_step(scene, "alarm").x[0])
    assert (x - start) / (end - start) == pytest.approx(FOUND.alarm_percent / 100)
    _, level_end = _x_span(_step(scene, "level"))
    assert level_end > x


# --- Layout and frames -----------------------------------------------------------


@pytest.mark.parametrize("name", ["sharing", "buckets", "alarm"])
def test_no_label_crowds_a_stroke_another_label_or_the_edge(name: str) -> None:
    assert layout_problems(getattr(r, name)(FOUND)) == []


@pytest.mark.parametrize("name", ["sharing", "buckets", "alarm"])
def test_the_gif_keeps_every_frame_of_its_timeline(tmp_path: Path, name: str) -> None:
    scene = getattr(r, name)(FOUND)
    frames = paced_timeline(scene)
    gif = write_gif(scene, frames, tmp_path / "out.gif", INKS)
    assert frame_problems(scene, len(frames), gif) == []


# --- The doc -------------------------------------------------------------------

#: Each GIF and the opening words of the paragraph it follows.
PLACES = {
    "rate-limit-shares.gif": "The first sketch draws that table to scale",
    "rate-limit-buckets.gif": "The second sketch follows a request",
    "rate-limit-alarm.gif": "The third sketch draws both",
}


def _embed(gif: str) -> re.Match:
    found = re.search(r"!\[([^\]]*)\]\(\.\./figures/" + re.escape(gif) + r"\)", DOC_TEXT)
    assert found is not None, f"{gif} is not embedded"
    return found


def test_the_doc_embeds_exactly_these_gifs_in_the_section() -> None:
    embedded = set(re.findall(r"\]\(\.\./figures/(rate-limit-[^)]+)\)", DOC_TEXT))
    assert embedded == set(PLACES)
    assert {r.gif_name(scene) for scene in r.scenes(FOUND)} == set(PLACES)


@pytest.mark.parametrize("gif", list(PLACES))
def test_each_gif_follows_the_paragraph_it_draws(gif: str) -> None:
    before = DOC_TEXT[: _embed(gif).start()].rstrip("\n").rsplit("\n\n", 1)[-1]
    assert before.startswith(PLACES[gif])


@pytest.mark.parametrize("name", ["sharing", "buckets", "alarm"])
def test_the_alt_text_quotes_the_title_and_every_label(name: str) -> None:
    scene = getattr(r, name)(FOUND)
    alt = _embed(r.gif_name(scene))[1]
    for words in [scene.title, *_labels(scene)]:
        quoted = "“" + words.replace("\n", " ") + "”"
        assert quoted in alt, quoted


# --- The committed GIFs ----------------------------------------------------------


@pytest.mark.parametrize("gif", list(PLACES))
def test_each_gif_is_the_board_s_size_loops_and_stays_small(gif: str) -> None:
    from PIL import Image

    path = r.FIGURES_DIR / gif
    assert path.stat().st_size < 1_000_000
    with Image.open(path) as image:
        assert image.size == (WIDTH, HEIGHT)
        assert image.info.get("loop") == 0


ON_THE_DRAWING_MACHINE = (
    platform.system() == "Darwin"
    and platform.machine() == "arm64"
    and hand_font() == "Comic Sans MS"
)


@pytest.mark.skipif(
    not ON_THE_DRAWING_MACHINE,
    reason="GIF bytes depend on the font and platform; they are drawn on Darwin arm64",
)
@pytest.mark.parametrize("name", ["sharing", "buckets", "alarm"])
def test_each_committed_gif_is_a_fresh_draw(tmp_path: Path, name: str) -> None:
    scene = getattr(r, name)(FOUND)
    fresh = write_gif(scene, paced_timeline(scene), tmp_path / "fresh.gif", INKS)
    assert fresh.read_bytes() == (r.FIGURES_DIR / r.gif_name(scene)).read_bytes()
