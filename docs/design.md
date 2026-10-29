# Design — ithildin-runtime

This doc carries the reasoning. The tracker carries unbuilt scope, and this
doc links to an issue rather than restating it.
[team-charter.md](team-charter.md) carries the roles and the lifecycle, and
the module table this doc reasons about. [architecture.md](architecture.md)
draws how slice 1's modules fit together.

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

The price is that every module has to be written against an event from its
first commit, which is slower than a vectorised backtest over a price array.
What it buys is that a strategy's backtest and its paper run exercise the same
lines of code. The next section says what that event is: a session's input
files, on disk. Only the data worker and the supervisor read the clock.

## Processes and files

The owner ruled on 2026-10-29 that each component runs as its own process, in
parallel wherever the data allows, and that processes pass everything to each
other through the filesystem. A component is one module of the charter's
module table, such as data, the alpha model or execution. The ruling first
said each action, and the owner narrowed it to each component the same day,
because a process per action was too granular. Two things follow from it.

1. **A failure stays where it happened.** In one process, an exception or a
   crash in the alpha model ends the run that also holds the order path. As
   separate processes, a failed component leaves its own output missing and
   nothing else. Every output upstream of it is already on disk, so recovering
   means running one component again on those files rather than replaying the
   day. When a failure has halted a session, the rerun waits for the owner or
   the operator to clear the halt, as rule 5 says.
2. **Every input and output can be read afterwards.** Each value that passes
   from one module to another is a file, so auditing a decision means reading
   the files that produced it rather than rerunning code and hoping it
   reproduces. The journal already records every step of an order. Files
   extend that record to every step before the order.

A **worker** is one component run as its own process. It takes its inputs from
files and writes its outputs to files, and reaches the outside world only
through the adapter at its edge, such as the broker or the data source. The
alpha worker, for example, reads a session's snapshot file and writes that
session's forecast file. A session here is one trading session. A worker
imports `core`, the shared types, and no sibling module, and shares no memory
with any other worker. The **supervisor** is the process that starts a run's
workers, watches them and stops the run. A **run directory** holds everything
one run's workers write.

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
   new file at once and the flush may not reach the disk. Every file carries a
   format version, and each release reads the formats the release before it
   wrote, so pinning back to an earlier release still reads what is on disk.
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
   session the output is for, when it was written, and how the worker exited,
   with the reason when it halted. The journal grows, so a manifest names it by
   the sequence number and hash of the last entry read rather than by a hash
   of the whole file. A worker killed by a signal or a crash writes nothing
   about itself, so the supervisor records the exit status or signal and the
   end of the worker's error output in its place. Each worker writes its
   process id and the path of its error output into its lock file, so a
   supervisor started after a crash can do the same for a worker it did not
   start.
4. **An absent, stale or empty input halts. It never reads as "trade
   nothing".** A worker waits for each session's inputs. Only the supervisor
   declares an input missing, when the worker upstream exits with a failure or
   a halt, or when a deadline passes. A file that names the wrong session halts
   as stale. A file with no rows halts unless it carries an explicit "trade
   nothing" mark, and that mark means hold every position and send no order,
   never a target of zero. Hard limit 3 came from an execution server that
   read "no signals" as "hold nothing" (L4690). With files, "has today's
   forecast been written" is a check of one manifest and the session it names.
5. **One path in every mode, and the supervisor keeps no state of its own.**
   The scheduler starts the supervisor, and the supervisor starts every one of
   the run's workers at once. It holds an exclusive lock on the run directory,
   so two supervisors never share one. A **halt record** is the file that
   stops one session: the supervisor writes it when a worker halts, fails or
   misses its deadline. Every worker skips a session that has a halt record and
   goes on to its next session, and a worker whose span is one session simply
   exits. The halt record is final for its session, and only the owner or the
   operator clears it, with a command that journals who cleared it and why.
   This finality is this doc's choice rather than part of the ruling.
   Monitoring, the one worker that holds the alert channel, journals each halt
   record with its reason and raises the alarm. On a halted day it pings the
   heartbeat service's failure address, so the owner hears even if the alarm
   sent from the machine is lost. A failure, meaning a crash or a missed
   deadline, also stops every worker in the run but monitoring.
   Everything the supervisor knows, it reads from the run directory, so a
   supervisor that crashes and is started again starts each worker whose lock
   is free. That restarted worker begins at its first session with no
   successful output. A worker that still holds its lock, because a child
   process outlives a parent that crashes, is left to finish. Workers with no
   file between them run at the same time. Alpha and cost need only the
   snapshot, and risk needs the snapshot and the reconciled book.
6. **One process at a time talks to each broker account.** The execution
   worker is the only one that holds the broker credential. It holds an
   exclusive lock on a file tied to the account, outside every run directory,
   for its whole life, and a second execution worker that would send and
   cannot take the lock halts. The order manager, the risk gate, the journal
   and the order path all run inside it, because the gate checks each order
   against every order and position in the account's journal just before the
   order is sent, and a check passed through a file could be stale by then.
   This is the one place a risk engineer's piece runs in another component's
   process. Running a worker again is safe everywhere except here, because a
   sent order cannot be unsent. So execution sends an order only when the
   journal holds no entry for its client order id, and asks the broker about
   any order in doubt by that id. No other worker's file can loosen the gate,
   because its hard caps come from execution's own configuration, which only
   the owner changes, by hand. [architecture.md](architecture.md)'s
   `## Decisions` proposes how execution recovers, derives its ids and
   combines its limits, for [#7](https://github.com/l3a0/ithildin-runtime/issues/7),
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11) to settle.
7. **The kill switch is a file, and pulling it is a command.** The file lives
   at one path per broker account, outside every run directory. Every worker
   checks for it when it starts and while it waits, and execution checks it
   before every send. Any error other than "no such file" reads as pulled, with
   a reason that says the file could not be read. Pulling it, per the owner's
   ruling on [#11](https://github.com/l3a0/ithildin-runtime/issues/11), creates
   the file, journals the halt, raises the alarm, and starts an execution
   worker in cancel mode, which cancels every resting order and journals each
   cancel first. Two workers run with the switch set, because neither sends an
   order. The cancelling execution worker waits for the account's lock up to a
   deadline and alarms if it passes. Monitoring writes the day's report and
   says the switch is set. Only the owner's or the operator's command clears
   the switch, and the clearing is journaled.

### A worker works through a span of sessions

The cost of starting a process decides how a backtest keeps
[one runtime](#one-runtime). On the owner's M1 Max under Python 3.12, a launch
that imports only the standard library took about 0.05 seconds, one that
imports pyarrow 0.2 to 0.3 seconds, and one that imports pandas about 1
second. These are exploratory measurements, the mean of 20 to 50 launches on
one machine, outside the test suite, and a rerun on a busy machine measured
pyarrow higher. Read them as a scale rather than a pin. The backtest that test
1 runs covers 1,480 sessions, the span quantitative-trading pins for the
strategy [architecture.md](architecture.md#the-strategy-the-skeleton-runs)
proposes on [#6](https://github.com/l3a0/ithildin-runtime/issues/6). Through
seven modules that is 10,360 worker runs. One launch per worker per session
would take about 9 minutes at the standard library's cost, 35 to 52 minutes at
pyarrow's and 3 hours at pandas'.

So a worker is given a span of sessions rather than one. In shadow, paper and
live the span is one session. In a backtest it is the whole history: each
worker starts once, and for each session it waits for its inputs, writes its
output and moves to the next. A worker carries nothing from one session to the
next except through files, so a long span runs the same steps as a span of
one. The launch is paid once per worker, and the workers run as a pipeline.
The alpha worker can run ahead through every session's snapshot while
portfolio construction waits on each day's book.

The clock adapter is read at two edges only. The data worker reads it to
decide when a session's snapshot is written, which is as fast as possible
under the history clock and at the session's time under the wall clock. The
supervisor reads wall time for deadlines in every mode, because a deadline
guards against a hung process rather than shaping what a strategy sees. The
workers between those edges read no clock and move on only when files appear.

A halt ends its session's chain, and the span goes on to the next session, the
way the next day's live run starts after a halted one. A failure stops the
whole run.

The price of this section is four things.

1. Every value that crosses from one module to another needs a file format,
   and a test that writes it and reads it back unchanged.
2. A worker notices a new file by watching the directory or by polling it, so
   each step waits a little longer than a function call would.
3. A backtest writes several files per session, a few tens of thousands for
   test 1. It may delete its outputs after a clean exit, keeping its manifests,
   its journal and its report, and keeps everything after a halt or a failure.
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
| **worker** | One component, meaning one module of the charter's module table, run as its own process. It takes its inputs from files, writes its outputs to files, and reaches the outside world only through the adapter at its edge. [Processes and files](#processes-and-files) gives its rules |
| **supervisor** | The process that starts every worker of a run at once, watches them, writes a halt record when one halts, fails or misses its deadline, and stops the run on a failure. It keeps no state outside the run directory |
| **run directory** | The directory that holds every file one run's workers write, including each manifest and halt record. The journal and marketlake's lake live outside it |
| **manifest** | The record written beside each output, naming every input by path and SHA-256, the release commit, a hash of the configuration, the session, when the output was written, and how the worker exited. An output counts only once its manifest records success |
| **halt record** | The file the supervisor writes to stop one session, naming the worker and the reason. Every worker skips that session, and only the owner or the operator clears it |
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
| One process that calls every module in memory | A failure in any module ends the process that also holds the order path, and the values passed between modules exist only in memory, so auditing a decision means rerunning code. The owner ruled on 2026-10-29 for [one process per component, passing files](#processes-and-files) |
| A message queue that deletes what it delivers, or a database server, between workers | A queue of that kind leaves nothing behind to audit, and a server is one more process that can fail. A file needs neither, and stays as the record. The price is that a worker polls or watches a directory instead of being sent its input |
| One process launch per worker per session in a backtest | Test 1 would make 10,360 launches, 35 to 52 minutes at pyarrow's measured import cost. A worker takes a span of sessions instead, as [A worker works through a span of sessions](#a-worker-works-through-a-span-of-sessions) says |
| One process per action, such as the order manager and the order path each in its own | The owner ruled on 2026-10-29 that this was too granular, and narrowed the ruling to one process per component. A component is the unit the charter gives an interface and an owning role |
| More than one execution process per broker account at a time | The risk gate checks limits across every order and position in the account's journal, so two gates at once could each approve orders that together breach one. A restarted supervisor beside a still-running order path sent every order twice in a review's simulation, which is why rule 6 takes a lock |
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
