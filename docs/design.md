# Design — ithildin-runtime

This doc carries the reasoning. The tracker carries unbuilt scope, and this
doc links to an issue rather than restating it.
[team-charter.md](team-charter.md) carries the roles and the lifecycle, and
the module table this doc reasons about.

## Contents

- [Premise](#premise)
- [One runtime](#one-runtime)
- [Processes and files](#processes-and-files)
- [How work is cut and ordered](#how-work-is-cut-and-ordered)
- [Vocabulary](#vocabulary)
- [Configuration](#configuration)
- [Considered and rejected](#considered-and-rejected)

## Premise

A trading platform loses three kinds of thing for good, and everything else it
computes can be computed again.

1. **Captured market data.** A quote not recorded when it existed is gone. A
   vendor's history is no substitute, because vendors restate history after
   the fact. The sibling
   [quantitative-trading](https://github.com/l3a0/quantitative-trading)
   repository measured that drift: an adjusted close is a function of the day
   it was downloaded.
2. **The order and fill record.** It is the audit trail, the input that
   calibrates the transaction cost model, and the only way to reconcile what
   the platform thinks it holds against what the broker says it holds. A fill
   nobody journaled cannot be reconstructed from the platform's own state.
3. **Capital.** A strategy can be re-researched. A loss cannot be undone.

A forecast, a backtest, a target portfolio and a report are all regenerable
from those three. That asymmetry is what the ranking rule in
[CLAUDE.md](../CLAUDE.md) protects. A path that can lose captured data or the
order record is never deferred, and a path that can lose capital sits behind
the hard limits, which no ranking overrides.

## One runtime

What is tested must be what runs live. A backtester written apart from the
live engine is a second implementation of every strategy it runs, and the two
drift without either looking wrong. Narang names backtest software and data
feed handlers among the places model risk lives (L4616 in the
[highlights](https://github.com/l3a0/trading-strategies/blob/main/research/book-notes/inside-the-black-box.md)).

So the platform has one set of modules, following the boxes in the charter's
module table, and three adapters at its edges.

1. **The data source**, which replays committed history in a backtest and
   reads a live feed on paper and live.
2. **The clock**, which steps through history in a backtest and follows the
   wall clock otherwise.
3. **The broker**, which is a simulator in a backtest and in shadow running, a
   broker's paper account on paper, and the real account only once the owner
   switches it.

The price is that every module has to be written against an event and clock
interface from its first commit, which is slower than a vectorised backtest
over a price array. What it buys is that a strategy's backtest and its paper
run exercise the same lines of code. The next section says what that event is:
a session's input files, on disk.

## Processes and files

The owner ruled on 2026-10-29 that each action runs as its own process, in
parallel wherever the data allows, and that processes pass everything to each
other through the filesystem. Two things follow from that.

1. **A failure stays where it happened.** In one process, an exception or a
   crash in the alpha model ends the run that also holds the order path,
   possibly between journaling an order and sending it. As separate
   processes, a failed action leaves its own output missing and nothing else.
   Every output upstream of it is already on disk, so recovering means
   running one action again on those files rather than replaying the day.
2. **Every input and output can be read afterwards.** Each value that passes
   from one module to another is a file, so auditing a decision means reading
   the files that produced it rather than rerunning code and hoping it
   reproduces. The journal already records every step of an order. Files
   extend that record to every step before the order.

A **stage** is one action run as its own process. It reads only files and its
configuration, and writes only files. The alpha stage, for example, reads the
session's snapshot file and writes the session's forecast file. It imports no
other module and shares no memory with any other stage. A **run directory**
holds everything one run's stages read and write.

Seven rules make the files something a stage can trust.

1. **A file appears whole or not at all.** A stage writes to a temporary name
   in the destination directory, flushes it to disk with `fsync`, renames it
   into place, and flushes the directory. A rename within one filesystem is
   atomic on POSIX, so a reader never sees half a file, and a stage that dies
   mid-write leaves only a temporary file. A run's files therefore live on one
   local filesystem, never a network share, where neither the rename nor the
   flush keeps that promise.
2. **A file is written once.** No stage changes an output after writing it. A
   second attempt at a stage writes beside the first, so a failed attempt stays
   readable. The journal is the one file that grows, one appended entry at a
   time, and no entry in it is ever changed.
3. **Each output names what made it.** Beside each output, the stage writes a
   **manifest**: the path and SHA-256 of every input file, the release commit,
   a hash of its configuration, when it started and ended, and how it exited.
   Any number in a report traces back to the exact bytes it came from.
4. **A missing file halts. It never reads as empty.** A stage whose input is
   absent writes a halt record naming the file, and exits with a halt status.
   A stage that means "trade nothing" writes a file that says so. Hard limit 3
   came from an execution server that read "no signals" as "hold nothing"
   (L4690). With files, "has today's forecast been written" is a check of one
   file and the session it names.
5. **The supervisor keeps no state of its own.** The supervisor is the process
   that launches each stage once its inputs exist, watches it, and halts the
   run when a stage fails or misses its deadline. Everything it knows, it reads
   from the run directory. A supervisor that crashes is started again, reads
   the directory, and launches only the stages whose output is missing. Stages
   with no file between them run at the same time. The alpha, risk and cost
   stages each read only the snapshot, so all three run in parallel.
6. **One process talks to the broker.** The order path is a single stage, and
   the only process that holds the broker credential. It is not split into one
   process per order, because the risk gate checks limits across all the run's
   orders, such as the number of open orders and the net exposure. Two gates
   running in parallel could each approve an order that together breach a
   limit. Running a stage again is safe everywhere except here, because a sent
   order cannot be unsent. So the order path recovers by reading the journal
   and asking the broker about each order in doubt by its client order id,
   never by sending again.
7. **The kill switch is a file.** Every stage checks for it when it starts, and
   the order path checks for it before every send. Pulling the switch means
   creating one file, with no process that has to be running to receive it.

### A stage works through a span of sessions

The cost of starting a process decides how a backtest keeps
[one runtime](#one-runtime). On the owner's M1 Max under Python 3.12, a launch
that imports only the standard library took about 0.05 seconds, one that
imports pyarrow about 0.2 seconds, and one that imports pandas about 1 second.
Each figure is the mean of 20 to 50 launches on one machine, so read it as a
scale rather than a pin. Test 1's backtest runs 1,480 sessions through seven
modules, which is 10,360 stage runs. One launch per stage per session would
take about 9 minutes at the standard library's cost, 35 minutes at pyarrow's
and 3 hours at pandas'.

So a stage is given a span of sessions rather than one. In shadow, paper and
live the span is one session. In a backtest it is the whole history: each
stage starts once, and for each session it waits for its inputs, writes its
output and moves to the next. The launch is paid once per stage, and the
stages run as a pipeline. The alpha stage can run ahead through every
session's snapshot while portfolio construction waits on each day's book. The
code is the same in every mode, and only the span differs.

A stage waiting on a file that a failed stage upstream will never write must
not wait forever. The supervisor watches every stage it launched, and when one
exits with a failure or a halt, it stops the run and every stage still
waiting.

The price of this section is three things.

1. Every value that crosses from one module to another needs a file format,
   and a test that writes it and reads it back unchanged.
2. A stage notices a new file by watching the directory or by polling it, so
   each step waits a little longer than a function call would.
3. A backtest writes several files per session, a few tens of thousands for
   test 1, into a run directory it deletes when it ends.

## How work is cut and ordered

A slice is the smallest set of deliverables that leaves the product usable by
someone at the end of it. Not a layer, and not a subsystem. A slice that ends
with a component nobody can run is a slice cut the wrong way.

Each slice lives on the tracker as three things.

1. A milestone with the slice's name, so a filed issue has one to take.
2. A parent issue holding what the slice leaves usable and the tests that show
   it works, each stated as what a test executes rather than what the code
   looks like.
3. One sub-issue per deliverable.

A sub-issue that cannot start until another finishes gets a "blocked by" link
to it, and a slice's parent issue that waits on another slice gets one to that
slice's parent issue. Name only the dependencies that are real. An invented
dependency is how a plan quietly becomes a layered build.

Order the slices by what the ranking rule in [CLAUDE.md](../CLAUDE.md)
decides: what is missing from the shortest path to a product someone can use
goes first. Then ship it, use it, and let what breaks set the order after
that. Slice 1 is the walking skeleton, on [#4](https://github.com/l3a0/ithildin-runtime/issues/4). The next slice is chosen
by what breaks while the skeleton runs on paper.

## Vocabulary

Terms with exact definitions, reused on purpose. A term listed here is not a
candidate for a synonym. Anything not listed here follows the writing-style
rule in [CLAUDE.md](../CLAUDE.md), which prefers deleting an in-group term to
glossing it.

| Term | Definition |
| --- | --- |
| **vintage** | One series as one source held it on one date, recorded with vendor, symbol, span, that date and which price the series carries, and kept unchanged once written. The term and its rules come from the sibling quantitative-trading repository |
| **target portfolio** | The positions portfolio construction asks for. The difference between it and the current positions is what execution trades |
| **risk gate** | The pre-trade check every order passes on the one order path. It enforces the risk engineer's limits and can refuse an order |
| **journal** | The append-only record of every intent, order, acknowledgment, fill, cancel and human override, written before the action it records |
| **stage** | One action run as its own process, which reads only files and its configuration and writes only files. [Processes and files](#processes-and-files) gives its rules |
| **run directory** | The directory that holds every file one run's stages read and write, including each stage's manifest and halt record |
| **manifest** | The record a stage writes beside each output, naming every input file by path and SHA-256, the release commit, a hash of the configuration, the start and end times, and how the stage exited |
| **walking skeleton** | A build in which every module exists in its simplest form and the whole loop runs end to end |
| **exploratory** | A result found by looking at the data. It kills an idea or justifies a closer look, and is never a verdict |
| **registered** | A result whose hypothesis was committed in writing before the number was seen. Only a registered result can promote a strategy |

## Configuration

Machine-local config lives under `~/.config/ithildin-runtime/`. Nothing
tracked in this repository carries a secret or a machine-specific path.

No setting exists yet. The first broker or data-vendor credential gets a row
here in the change that first reads it, naming where it lives and what reads
it. A credential lives in the environment of the run or in the machine's
keychain, and never in a file. The table takes the columns Setting, Secret,
Lives in and Read by.

## Considered and rejected

Machinery that was considered and cut, pinned here with the reason. The point
is that a decision stays decided. When something new is cut, add it here in
the same change that cuts it.

Reviews armor what exists and rarely ask whether it should exist, so this
register is also where the answer to "why is this needed" gets written down
once rather than re-argued.

| Cut | Why |
| --- | --- |
| A separate vectorised backtester | It would be a second implementation of every strategy, which the [one runtime](#one-runtime) rule exists to prevent |
| One process that calls every module in memory | A failure in any module ends the process that also holds the order path, and the values passed between modules exist only in memory, so auditing a decision means rerunning code. The owner ruled on 2026-10-29 for [one process per action, passing files](#processes-and-files) |
| A message queue, a socket or a database between stages | Each needs a server or a live peer that can itself fail, and a message consumed from a queue leaves nothing behind to audit. A file needs neither, and stays as the record. The price is that a stage polls or watches a directory instead of being sent its input |
| One process launch per stage per session in a backtest | Test 1 would make 10,360 launches, about 35 minutes at pyarrow's measured import cost. A stage takes a span of sessions instead, as [A stage works through a span of sessions](#a-stage-works-through-a-span-of-sessions) says |
| One order path process per order | The risk gate checks limits across all of a run's orders, so two gates in parallel could each approve orders that together breach one |
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
