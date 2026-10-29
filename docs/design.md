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
   crash in the alpha model ends the run that also holds the order path. As
   separate processes, a failed action leaves its own output missing and
   nothing else. Every output upstream of it is already on disk, so recovering
   means running one action again on those files rather than replaying the
   day.
2. **Every input and output can be read afterwards.** Each value that passes
   from one module to another is a file, so auditing a decision means reading
   the files that produced it rather than rerunning code and hoping it
   reproduces. The journal already records every step of an order. Files
   extend that record to every step before the order.

A **worker** is one action run as its own process. It takes its inputs from
files and writes its outputs to files, and reaches the outside world only
through the adapter at its edge, such as the broker or the data source. The
alpha worker, for example, reads a session's snapshot file and writes that
session's forecast file. A session here is one trading session. A worker
imports `core`, the shared types, and no sibling module, and shares no memory
with any other worker. The **supervisor** is the process that launches each
worker, watches it and stops the run. It replaces the in-memory run controller
that [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15)'s
`docs/architecture.md` draws. A **run directory** holds everything one run's
workers write.

Two stores sit outside every run directory, because they outlive any one run.

1. marketlake's lake, which holds captured market data.
2. The journal, at one path per broker account, which the book is replayed
   from.

Seven rules make the files something a worker can trust.

1. **An output appears whole, once, and only with its manifest.** A worker
   writes to a temporary name in the destination directory and flushes it to
   stable storage. On macOS that takes `fcntl(F_FULLFSYNC)`, because plain
   `fsync` there does not ask the drive to empty its cache. The worker then
   publishes the file with `link`, which refuses to replace an existing name,
   rather than `rename`, which silently replaces one. A **manifest** goes
   beside the output last, and an output counts as present only once its
   manifest records a successful exit. A run's files live on one local
   filesystem, never on a network share, because there a reader may not see a
   new file at once and the flush may not reach the disk.
2. **A file is never changed after it is written.** A second attempt at a
   worker writes under its own attempt number, so a failed attempt stays
   readable, and a reader takes the newest attempt whose manifest records
   success. The journal is the one file that grows. Each entry is appended
   under an exclusive lock and flushed to stable storage before the step it
   records happens, and no entry is ever changed. A reader that finds a final
   entry cut short discards it, and one that finds an earlier entry it cannot
   parse halts.
3. **Each output names what made it.** The manifest names every input file by
   path and SHA-256, the release commit, a hash of the configuration, the
   session the output is for, when it was written, and how the worker exited.
   The journal grows, so a manifest names it by the sequence number and hash
   of the last entry read rather than by a hash of the whole file. A worker
   killed by a signal or a crash writes nothing about itself, so the
   supervisor, which sees every exit, records the exit status or signal and
   the end of the worker's error output in its place.
4. **An absent, stale or empty input halts. It never reads as "trade
   nothing".** A worker waits for each session's inputs. Only the supervisor
   declares an input missing, when the worker upstream exits with a failure or
   a halt, or when a deadline passes, and the supervisor then writes a **halt
   record** naming the file. A file that names the wrong session halts as
   stale. A file with no rows halts unless it carries an explicit "trade
   nothing" mark. Hard limit 3 came from an execution server that read "no
   signals" as "hold nothing" (L4690). With files, "has today's forecast been
   written" is a check of one manifest and the session it names.
5. **The supervisor keeps no state of its own.** The scheduler starts it, and
   it holds an exclusive lock on the run directory, so two supervisors never
   share one. Everything it knows, it reads from that directory. A supervisor
   that crashes is started again, reads the directory, and launches a worker
   only when its output is missing and no earlier copy of it still holds the
   worker's lock, because a child process outlives a parent that crashes. A
   halt record is final for its session: a restarted supervisor launches
   nothing more for that session, and only the owner or the operator clears
   it. On any halt or failure, the supervisor journals the halt with its
   reason and raises the alarm. It records the release commit when a run
   starts, and a supervisor started on a different release halts rather than
   finish the run with mixed code. The dead-man check catches a supervisor
   that dies. Workers with no file between them run at the same time: once
   the snapshot and the reconciled book exist, the alpha and risk workers run
   in parallel.
6. **One process at a time talks to each broker account.** The order path is
   the only worker that holds the broker credential, and it holds an exclusive
   lock on a file tied to the account, outside every run directory, for its
   whole life. A second order path that cannot take the lock halts without
   sending. It is not split into one process per order, because the risk gate
   checks limits across every order and position in the account's journal,
   such as the number of open orders and the net exposure. Two gates running
   at once could each approve an order that together breach a limit, so rule 6
   is the one place the ruling's "in parallel" gives way.
   - **Recovery comes first.** The order path settles orders in doubt,
     journals fills and reconciles before any model runs, then writes the
     session's book file that the risk worker and portfolio construction read.
     A mismatch halts there.
   - **A rerun never sends twice.** The order path reads exactly one intents
     file per session and journals its SHA-256 before the first send. It sends
     an intent only when the intent's client order id has no journal entry,
     and asks the broker about any order in doubt by that id. Running a worker
     again is safe everywhere else, because a sent order cannot be unsent.
   - **The gate trusts no other worker for its caps.** The order path reads
     the hard caps on its limits from its own configuration, and a constraints
     file from the risk worker can only tighten them.
7. **The kill switch is a file, and pulling it is a command.** The file lives
   at one path per account, outside every run directory. Every worker checks
   for it when it starts, and the order path checks before every send. Any
   error other than "no such file" reads as pulled, so a switch that cannot be
   read stops trading. Pulling it, per the owner's ruling on
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11), creates the
   file, journals the halt, raises the alarm, and launches the order path to
   cancel every resting order under the account's lock, journaling each cancel
   first. Only the owner's or the operator's command clears it, and the
   clearing is journaled.

### A worker works through a span of sessions

The cost of starting a process decides how a backtest keeps
[one runtime](#one-runtime). On the owner's M1 Max under Python 3.12, a launch
that imports only the standard library took about 0.05 seconds, one that
imports pyarrow 0.2 to 0.3 seconds, and one that imports pandas about 1
second. Each figure is the mean of 20 to 50 launches on one machine, and a
review rerun on a busy machine measured pyarrow higher, so read the figures as
a scale rather than a pin. The backtest that test 1 runs covers 1,480
sessions, the span quantitative-trading pins for the strategy [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15) proposes
on [#6](https://github.com/l3a0/ithildin-runtime/issues/6). Through seven
modules that is 10,360 worker runs. One launch per worker per session would
take about 9 minutes at the standard library's cost, 35 to 52 minutes at
pyarrow's and 3 hours at pandas'.

So a worker is given a span of sessions rather than one. In shadow, paper and
live the span is one session. In a backtest it is the whole history: each
worker starts once, and for each session it waits for its inputs, writes its
output and moves to the next. A worker carries nothing from one session to the
next except through files, so a long span runs the same steps as a span of
one. A restarted worker begins at its first session with no successful
output. The launch is paid once per worker, and the workers run as a
pipeline. The alpha worker can run ahead through every session's snapshot
while portfolio construction waits on each day's book.

The clock adapter is read at two edges only. The data worker reads it to
decide when a session's snapshot is written, which is as fast as possible
under the history clock and at the session's time under the wall clock. The
supervisor reads wall time for deadlines in every mode, because a deadline
guards against a hung process rather than shaping what a strategy sees. The
workers between those edges read no clock and move on only when files appear.

A halt ends its session's chain, and the span goes on to the next session, the
way the next day's live run starts after a halted one. A failure, meaning a
worker that crashes or misses its deadline, stops the whole run and every
worker still waiting.

The price of this section is four things.

1. Every value that crosses from one module to another needs a file format,
   and a test that writes it and reads it back unchanged.
2. A worker notices a new file by watching the directory or by polling it, so
   each step waits a little longer than a function call would.
3. A backtest writes several files per session, a few tens of thousands for
   test 1. It may delete its outputs after a clean exit, keeping its manifests
   and its report, and keeps everything after a halt or a failure. Shadow,
   paper and live run directories are kept, and leave the machine with the
   journal.
4. The locks, the manifests and the supervisor's records add machinery that a
   single process would not need, and each of their alarms has to fire in a
   drill before the paper gate.

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
| **worker** | One action run as its own process. It takes its inputs from files, writes its outputs to files, and reaches the outside world only through the adapter at its edge. [Processes and files](#processes-and-files) gives its rules |
| **supervisor** | The process that launches each worker, watches it, writes the halt record when an input is missing, and stops the run. It keeps no state outside the run directory |
| **run directory** | The directory that holds every file one run's workers write, including each manifest and halt record. The journal and marketlake's lake live outside it |
| **manifest** | The record written beside each output, naming every input by path and SHA-256, the release commit, a hash of the configuration, the session, when the output was written, and how the worker exited. An output counts only once its manifest records success |
| **halt record** | The file the supervisor writes when a worker halts or an input is missing, naming the file and the reason. It is final for its session until the owner or the operator clears it |
| **span** | The sessions one worker is given to work through: one in shadow, paper and live, and the whole history in a backtest |
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
| A message queue that deletes what it delivers, or a database server, between workers | A queue of that kind leaves nothing behind to audit, and a server is one more process that can fail. A file needs neither, and stays as the record. The price is that a worker polls or watches a directory instead of being sent its input |
| One process launch per worker per session in a backtest | Test 1 would make 10,360 launches, 35 to 52 minutes at pyarrow's measured import cost. A worker takes a span of sessions instead, as [A worker works through a span of sessions](#a-worker-works-through-a-span-of-sessions) says |
| One order path process per order, or more than one per account at a time | The risk gate checks limits across every order and position in the account's journal, so two gates at once could each approve orders that together breach one. A restarted supervisor beside a still-running order path sent every order twice in a review's simulation, which is why rule 6 takes a lock |
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
