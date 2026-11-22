"""Each strategy's tracked configuration, the table that declares it, and the header's hashes.

#102's acceptance names these tests. `core/strategy.py` holds the reader's
rules, and `tests/test_strategy_loop.py` runs the hashes through the receive
loop and the store.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import (
    STRATEGIES,
    BlobHeader,
    Halt,
    StageKind,
    load_strategies,
    parse_strategy,
)
from ithildinruntime.runtime.loop import Stage, Table

D = StageKind.DETERMINISTIC
CHAN = "chan_bollinger"
ROOT = Path(__file__).parents[1]


def _tracked_strategy_files() -> list[str]:
    folder = STRATEGIES.relative_to(ROOT).as_posix()
    listed = subprocess.run(
        ["git", "ls-files", "--", folder], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return sorted(Path(path).name for path in listed)


def test_every_tracked_strategy_file_loads() -> None:
    files = _tracked_strategy_files()
    assert f"{CHAN}.toml" in files
    assert all(name.endswith(".toml") for name in files), files
    names = [name.removesuffix(".toml") for name in files]
    loaded = load_strategies(STRATEGIES, names)
    assert [s.name for s in loaded] == sorted(names)
    for strategy in loaded:
        assert (
            strategy.config_hash
            == hashlib.sha256((STRATEGIES / f"{strategy.name}.toml").read_bytes()).hexdigest()
        )


def test_chans_band_declares_gld_and_uso() -> None:
    (chan,) = load_strategies(STRATEGIES, [CHAN])
    assert chan.instruments == ("GLD", "USO")


def test_only_the_named_strategy_is_loaded(tmp_path) -> None:
    (tmp_path / f"{CHAN}.toml").write_bytes((STRATEGIES / f"{CHAN}.toml").read_bytes())
    (tmp_path / "reversal_weights.toml").write_bytes(b'instruments = ["SPY", "QQQ"]\n')
    assert [s.name for s in load_strategies(tmp_path, [CHAN])] == [CHAN]
    assert [s.name for s in load_strategies(tmp_path, ["reversal_weights"])] == ["reversal_weights"]


def test_two_names_given_in_reverse_order_load_sorted(tmp_path) -> None:
    for name in ("alpha_one", "beta_two"):
        (tmp_path / f"{name}.toml").write_bytes(b'instruments = ["GLD"]\n')
    assert [s.name for s in load_strategies(tmp_path, ["beta_two", "alpha_one"])] == [
        "alpha_one",
        "beta_two",
    ]


# The hash


def test_the_hash_is_the_sha256_of_the_files_bytes() -> None:
    data = b'instruments = ["GLD", "USO"]\n[alpha]\nlookback = 20\n'
    assert parse_strategy("chan", data).config_hash == hashlib.sha256(data).hexdigest()


BASE = b'instruments = ["GLD", "USO"]\n[alpha]\nlookback = 20\n# a\n'


@pytest.mark.parametrize(
    "changed",
    [
        BASE.replace(b"20", b"21"),
        BASE.replace(b"# a", b"# b"),
        BASE + b"\n",
    ],
    ids=["value", "comment", "added-newline"],
)
def test_a_one_byte_change_gives_another_hash(changed) -> None:
    assert len(changed) - len(BASE) in (0, 1)
    assert parse_strategy("chan", changed).config_hash != parse_strategy("chan", BASE).config_hash


def test_a_comment_alone_changes_the_hash_and_not_the_settings() -> None:
    one = parse_strategy("chan", b'instruments = ["GLD"]\n# version a\n')
    two = parse_strategy("chan", b'instruments = ["GLD"]\n# version b\n')
    assert one.instruments == two.instruments and dict(one.sections) == dict(two.sections)
    assert one.config_hash != two.config_hash


# What the reader reads


def test_a_figure_reads_back_as_its_exact_decimal() -> None:
    strategy = parse_strategy("chan", b'instruments = ["GLD"]\n[cost]\nrate = 0.0035\n')
    rate = strategy.section("cost")["rate"]
    assert type(rate) is Decimal and rate == Decimal("0.0035")


def test_a_plain_date_and_a_utc_datetime_are_allowed() -> None:
    data = (
        b'instruments = ["GLD"]\n[data]\nfirst = 2006-05-24\n'
        b"at = 2026-11-02T15:00:00Z\nalso = 2026-11-02T15:00:00+00:00\n"
    )
    section = parse_strategy("chan", data).section("data")
    assert section["first"] == date(2006, 5, 24)
    assert section["at"] == datetime(2026, 11, 2, 15, tzinfo=UTC)
    assert section["also"].tzinfo is UTC


def test_a_section_is_read_only() -> None:
    section = parse_strategy("chan", b'instruments = ["GLD"]\n[alpha]\nk = [1, 2]\n').section(
        "alpha"
    )
    with pytest.raises(TypeError):
        section["k"] = 3  # type: ignore[index]
    assert section["k"] == (1, 2)


# Every refusal halts, naming the strategy and the file, section or key.


GOOD = b'instruments = ["GLD"]\n'


@pytest.mark.parametrize(
    ("data", "named"),
    [
        (b"instruments = [", "is not TOML"),
        (b"\xff\xfe", "is not UTF-8"),
        (b"[alpha]\nk = 1\n", "has no instruments"),
        (b"instruments = []\n", "holds no list of instruments"),
        (b'instruments = ["GLD", "GLD"]\n', "GLD more than once"),
        (b'instruments = "GLD"\n', "holds no list of instruments"),
        (b"instruments = [1, 2]\n", "instrument 1"),
        (b'instruments = [""]\n', "instrument ''"),
        (GOOD + b"lookback = 20\n", "holds lookback as a int, not a section"),
        (GOOD + b"[[alpha]]\nk = 1\n", "holds alpha as a list, not a section"),
        (GOOD + b"[risk]\nmax_age = inf\n", "risk.max_age = Infinity"),
        (GOOD + b"[risk]\nmax_age = -inf\n", "risk.max_age = -Infinity"),
        (GOOD + b"[risk]\nmax_age = nan\n", "risk.max_age = NaN"),
        (GOOD + b"[risk]\nmax_age = 1e400\n", "risk.max_age = 1E+400"),
        (GOOD + b"[risk]\nk = 1" + b"0" * 39 + b"\n", "which the codec refuses"),
        (GOOD + b"[data]\nat = 15:40:00\n", "data.at = 15:40:00, a local time"),
        (GOOD + b"[data]\nat = 2026-11-02T15:40:00\n", "a local datetime with no offset"),
        (GOOD + b"[data]\nat = 2026-11-02T15:40:00-05:00\n", "whose offset is not UTC's"),
        (GOOD + b"[data]\nk = [1, 15:40:00]\n", "data.k[1] = 15:40:00"),
        (GOOD + b"[data.inner]\nat = 15:40:00\n", "data.inner.at"),
    ],
    ids=[
        "malformed",
        "not-utf8",
        "instruments-missing",
        "instruments-empty",
        "instruments-repeated",
        "instruments-string",
        "instruments-numbers",
        "instrument-empty",
        "top-level-not-a-table",
        "array-of-tables",
        "infinite",
        "minus-infinite",
        "not-a-number",
        "past-the-magnitude",
        "past-the-digits",
        "local-time",
        "local-datetime",
        "offset-not-z",
        "local-time-in-a-list",
        "local-time-in-a-table",
    ],
)
def test_the_reader_halts_naming_the_strategy_and_file(data, named) -> None:
    with pytest.raises(Halt) as caught:
        parse_strategy("chan", data)
    reason = caught.value.reason
    assert reason.startswith("strategy chan's configuration chan.toml "), reason
    assert named in reason, reason


@pytest.mark.parametrize(
    "data",
    [GOOD, GOOD + b"[cost]\n", GOOD + b"[alpha]\nk = 1\n"],
    ids=["missing", "empty", "another-section"],
)
def test_a_missing_or_empty_section_halts_when_asked_for(data) -> None:
    strategy = parse_strategy("chan", data)
    with pytest.raises(Halt) as caught:
        strategy.section("cost")
    assert caught.value.reason == (
        "strategy chan's configuration chan.toml has no [cost] section, or an empty one"
    )


@pytest.mark.parametrize("name", ["", "-x", "a/b", "a..b", "a b", ".x"])
def test_a_name_the_key_builder_refuses_halts(name) -> None:
    with pytest.raises(Halt, match="cannot be part of a key"):
        parse_strategy(name, GOOD)


@pytest.mark.parametrize(
    ("names", "named"),
    [
        ([], "no strategy is named"),
        (["missing"], "strategy missing has no configuration file"),
        (["../chan"], "cannot be part of a key"),
        (["chan", "chan"], "strategy chan is named more than once"),
        (["broken"], "strategy broken's configuration strategies/broken.toml is not TOML"),
    ],
    ids=["no-names", "no-file", "bad-name", "named-twice", "malformed"],
)
def test_loading_halts_naming_the_strategy(tmp_path, names, named) -> None:
    folder = tmp_path / "strategies"
    folder.mkdir()
    (folder / "chan.toml").write_bytes(GOOD)
    (folder / "broken.toml").write_bytes(b"instruments = [")
    with pytest.raises(Halt) as caught:
        load_strategies(folder, names)
    assert named in caught.value.reason


def test_loading_refuses_one_name_given_as_a_string(tmp_path) -> None:
    with pytest.raises(TypeError):
        load_strategies(tmp_path, "chan")


# The table


CHAN_CONFIG = parse_strategy("chan", b'instruments = ["GLD", "USO"]\n')
OTHER = parse_strategy("other", b'instruments = ["SPY"]\n')


def test_the_table_reads_each_strategys_instruments_and_hashes() -> None:
    table = Table(
        stages=(
            Stage("data", D),
            Stage("alpha/chan", D, ("data",), strategy="chan"),
            Stage("alpha/other", D, ("data",), strategy="other"),
        ),
        strategies=(OTHER, CHAN_CONFIG),
    )
    assert table.strategy("chan").instruments == ("GLD", "USO")
    assert dict(table.hashes_for("alpha/chan")) == {"chan": CHAN_CONFIG.config_hash}
    assert dict(table.hashes_for("alpha/other")) == {"other": OTHER.config_hash}
    assert dict(table.hashes_for("data")) == {
        "chan": CHAN_CONFIG.config_hash,
        "other": OTHER.config_hash,
    }


def test_the_table_refuses_a_strategy_declared_twice() -> None:
    twice = parse_strategy("chan", b'instruments = ["GLD"]\n# another file\n')
    with pytest.raises(ValueError, match="strategy chan is declared twice"):
        Table(stages=(Stage("data", D),), strategies=(CHAN_CONFIG, twice))


def test_the_table_refuses_a_stage_of_a_strategy_it_does_not_declare() -> None:
    with pytest.raises(ValueError, match="strategy other, which the table does not declare"):
        Table(stages=(Stage("alpha/other", D, strategy="other"),), strategies=(CHAN_CONFIG,))


@pytest.mark.parametrize(
    ("name", "strategy"),
    [("alpha", "chan"), ("alpha/chanx", "chan"), ("alpha/chan/x", "chan"), ("cost/chan", "other")],
)
def test_a_strategys_stage_is_named_for_the_strategy(name, strategy) -> None:
    with pytest.raises(ValueError, match=f"so it is named [a-z]+/{strategy}$"):
        Stage(name, D, strategy=strategy)


def test_a_positional_stage_still_reads_with_no_strategy() -> None:
    stage = Stage("book", D, ("alpha",), True)
    assert stage.keeps_order and stage.strategy is None


# The header


def _header(hashes) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment="ci",
        run="2026-11-02",
        stage="data",
        kind="deterministic",
        decision_at=datetime(2026, 11, 2, 15, tzinfo=UTC),
        inputs={},
        release="sha256:release",
        config_hashes=hashes,
    )


@pytest.mark.parametrize(
    ("hashes", "named"),
    [
        ({}, "at least one strategy"),
        ({"a/b": "0" * 64}, "cannot be part of a key"),
        ({"..": "0" * 64}, "cannot be part of a key"),
        ({"chan": "0" * 63}, "not a lower case hex SHA-256"),
        ({"chan": "A" * 64}, "not a lower case hex SHA-256"),
        ({"chan": "c1"}, "not a lower case hex SHA-256"),
    ],
    ids=["empty", "slash", "dots", "short", "upper-case", "not-hex"],
)
def test_the_header_refuses_hashes_it_cannot_name(hashes, named) -> None:
    with pytest.raises(ValueError, match=named):
        _header(hashes)


def test_the_headers_bytes_do_not_depend_on_the_maps_order() -> None:
    from ithildinruntime.core import encode

    one = _header({"chan": "0" * 64, "other": "f" * 64})
    two = _header({"other": "f" * 64, "chan": "0" * 64})
    assert encode(one) == encode(two)
