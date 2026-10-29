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
lines of code. The next section says what that event is: one record appended
to a file on disk. Only the data worker and the supervisor read the clock.

## Processes and files

The owner ruled on 2026-10-29 that each component runs as its own process, in
parallel wherever the data allows, and that processes pass everything to each
other through the filesystem. A component is one module of the charter's
module table, such as data, the alpha model or execution. The ruling first
said each action, and the owner narrowed it to each component the same day,
because a process per action was too granular. Two things follow from it.

1. **A failure stays where it happened.** In one process, an exception or a
   crash in the alpha model ends the run that also holds the order path. As
   separate processes, a failed component stops producing its own output and
   nothing else. Every output upstream of it is already on disk, so recovering
   means starting one component again, which reads the files that exist and
   retries what its event log is missing, rather than replaying the day. A
   session that halted stays halted, and the next session runs once the cause
   is gone.
2. **Every input and output can be read afterwards.** Each value that passes
   from one module to another is a file, and each component's event log
   records which files it read and which it wrote. Auditing a decision means
   reading those files and that record rather than rerunning code and hoping
   it reproduces. The journal already records every step of an order. The
   event logs extend that record to every step before the order.

The owner ruled four more things about the shape the same day.

1. **An event pipeline.** A component runs its processing loop when there is
   something new for it, and what it produces is what sets the components
   downstream of it running. Between components, a new event is the only
   trigger.
2. **A queue per component.** A component listens for events on its queue and
   processes each one. A message is sent to a component's queue whenever
   there is something new for it to process.
3. **Files for data, and an event log per component for the record.** A
   component reads its inputs from files and writes its outputs to files.
   Auditability lives in an event log per component, which holds a pointer to
   each file, and the log is the source of truth.
4. **SQLite for each event log, and resuming by retrying it.** SQLite is a
   database kept in a single file, with no server, and Python's standard
   library reads and writes it. A component resumes from a failure by reading
   the files that exist and retrying its SQLite write.

The rules below are this doc's design for those rulings, proposed to
[#22](https://github.com/l3a0/ithildin-runtime/issues/22) and
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), and not part of the
rulings themselves.

Seven terms carry the design.

1. An **output file** holds one thing a component produced for one session,
   such as a snapshot or a forecast. It is written once and never changed.
2. An **event** is one row of a component's event log. It records one output
   file, by path and SHA-256, or a halt, and names the events it came from.
3. An **event log** is the SQLite database holding one component's events in
   the order it wrote them, and it only ever grows. It is three files: the
   database, and the `-wal` and `-shm` files SQLite keeps beside it. Each event
   log in a run directory has exactly one writer, its component.
4. A **worker** is one component run as its own process. It listens on its
   queue, reads its input files through the events that point to them, writes
   its output files and appends to its own event log, and reaches the outside
   world only through the adapter at its edge, such as the broker or the data
   source. A worker imports `core`, the shared types, and no sibling module,
   and shares no memory with any other worker.
5. A **queue** is the one place a worker listens. A message on it names an
   event log and the sequence number of an event there for the worker to
   process. The message only says something is new. The event log stays the
   record whether a message arrives or not.
6. The **supervisor** is the process that starts a run's workers, writes the
   run's **control log**, which holds one event per session of the run and
   then an end-of-span event, watches the workers and stops the run.
7. A **run directory** holds every output file and event log one run's
   workers write, and their queues. A session here is one trading session.

Two stores sit outside every run directory, because they outlive any one run.

1. marketlake's lake, which holds captured market data.
2. The journal, at one path per broker account, which the book is replayed
   from. This doc proposes to
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) that it is a
   SQLite event log too, the one with more than one writer, keeping each
   entry in its row rather than in a file, because an entry is small and must
   be committed before the step it records happens. That commit is how hard
   limit 4 holds on disk.

Nine rules make the files and the logs something a worker can trust.

1. **An output file appears whole and is never changed.** A worker writes each
   output to a temporary name in its directory and flushes it to stable
   storage, with `fcntl(F_FULLFSYNC)` on macOS, because a plain `fsync` there
   does not ask the drive to empty its cache. It then publishes the file at a
   name fixed by the worker and the session, with `link`, which refuses to
   replace an existing name, rather than `rename`, which silently replaces
   one. So a reader never sees half a file, and no file is ever overwritten. A
   run's files and logs live on one local filesystem, never on a network
   share, because there a reader may not see a new file at once and SQLite's
   write-ahead mode needs memory shared on one machine.
2. **The event log is the source of truth.** A file counts as an output only
   once the worker's event log holds a row that points to it, by path and
   SHA-256. A reader never lists a directory to find inputs. It follows the
   rows. Each append is one row in one transaction, which SQLite commits whole
   or not at all. Each event log runs in SQLite's write-ahead mode, which lets
   readers read while one writer commits. Every writer connection, each of the
   journal's included, sets `synchronous=FULL` and `fullfsync=ON` each time it
   opens, because both belong to the connection rather than the file, and
   SQLite ignores `fullfsync` off macOS. Each append opens its transaction
   with `BEGIN IMMEDIATE`, which takes the write lock first, then reads the
   last row's sequence number and hash, then inserts the next row and
   commits, so two writers never pick the same sequence number. Each table
   carries three triggers. Two refuse any update or delete. The third refuses
   any insert whose sequence number is not one past the last, which also
   stops `INSERT OR REPLACE` from overwriting a row and `INSERT OR IGNORE` from
   dropping one in silence. Each row stores its event as its exact serialized
   body in one text column, with a hash of that body and the hash of the row
   before it. A link that does not chain, a hash that does not match, or a log
   that fails SQLite's quick integrity check, which a writer runs when it
   opens, halts the reader or the writer with the log and the sequence number.
3. **A worker resumes by reading what exists and retrying the log.** For each
   session, a worker first looks in its own event log. If a row for the
   session is there, the work is done. If not, it looks for the output file
   at the session's fixed name. A file there was published whole by an
   earlier attempt that crashed before its commit, so the worker reads it,
   hashes it and retries the SQLite append that points to it. With no file
   either, it computes the output, publishes the file and appends the row. A
   SQLite append that fails, for example because another writer holds the
   lock, is retried until a deadline. A file with no row pointing to it is
   left alone and never read as an output. A worker that runs again after a
   superseded attempt writes a new file under its own attempt number and a row
   that names the event it supersedes. The newest superseding event wins, and
   two events for one session with no link between them halt the reader.
4. **A worker listens on its queue, then reads its inputs.** Each queue is a
   named pipe in the run directory, a file that one process reads and others
   write to, one per worker. After a writer commits an event, it sends a
   message to the queue of every worker that reads its log. One table in
   `runtime` says which worker reads which log. A worker that is not running
   has no listener on its pipe, so the send fails at once and is skipped, and
   the worker catches up from its input logs when it starts. Each message is
   one line under the pipe's 512-byte limit, so messages from several writers
   never mix. Whichever message wakes a worker, it opens each input log
   read-only, reads every row past the last sequence number it handled,
   follows each row to its file and checks the file's SHA-256 against the
   row. It holds no transaction open between reads, since an open read keeps
   it on an old snapshot. A writer that crashes between its commit and its
   message has already committed, so when it starts again it sends a message
   for its newest event before anything else, and a deadline catches a writer
   that never starts again. A worker reads every row it finds rather than one
   per message, so a lost or repeated message costs nothing.
5. **Each event names what made it.** Its row names the last event it
   consumed from each input log, the control log included, by log, sequence
   number and hash, along with its output file's path and SHA-256, the release
   commit, a hash of the configuration, the session it is for, when it was
   written, and its format version. A worker keeps no cursor of its own. How
   far it has read each input is the last event its own log names from it, so
   a restarted worker resumes from its log alone, after first checking that
   each event it names, and each file that event points to, is still there
   with the same hash, and halting if one is not. Nothing irreversible rests
   on an upstream file alone: execution journals the whole target portfolio
   it acts on, in a committed entry, before its first send. Each release
   writes only formats the release before it can read, so pinning back to an
   earlier release still reads what is on disk. A worker killed by a signal or
   a crash writes nothing about itself, so the supervisor appends its exit
   status or signal and the end of its error output to the control log. Each
   worker writes its process id and the path of its error output into a lock
   file, so a supervisor started after a crash can do the same for a worker it
   did not start.
6. **An absent, stale or empty input halts. It never reads as "trade
   nothing".** A worker waits for each session's input events. An event that
   names the wrong session halts as stale, and so does a file that is missing
   or whose SHA-256 does not match its row. An output with no rows halts
   unless it carries an explicit "trade nothing" mark, and that mark means
   hold every position and send no order, never a target of zero. A worker
   that cannot process a session appends a **halt event** for it instead of an
   output, with the reason, and then exits cleanly or moves to its next
   session, because a halt is not a crash. Hard limit 3 came from an execution
   server that read "no signals" as "hold nothing" (L4690). In a pipeline, "has
   today's forecast been written" is a check for one row and the session it
   names.
7. **A halt flows down the pipeline, and the supervisor keeps no state of its
   own.** The scheduler starts the supervisor. The supervisor holds an
   exclusive lock on the run directory, so two supervisors never share one.
   Into an empty control log it appends one event per session of the run and
   then an end-of-span event, and only then starts every worker at once. A
   restarted supervisor finds the control log already written and appends
   none of it again. Every worker reads the control log, and every worker, in
   every mode, exits once it has appended an output or a halt event for each
   session before the end-of-span event. A worker that exits before then has
   failed, and the reason names it.
   - **A halt is passed on.** A worker that meets a halt event for a session
     in any input appends its own halt event, naming the one it met, and goes
     on to its next session.
   - **A failure stops the run.** A worker that crashes, or misses its
     deadline, has failed. Its deadline runs from the moment its last needed
     input event for the session appears, so waiting on a slow upstream worker
     never counts against it. The supervisor appends a halt event for that
     session to the control log and stops every worker in the run but
     monitoring.
   - **Monitoring tells the owner once.** Monitoring, the one worker that reads
     every event log and holds the alert channel, journals every halt event
     with its reason and raises one alarm per session, on the halt event that
     names no earlier one. It journals before it alarms, and appends an event
     naming each halt it has handled, so a restarted monitoring neither
     repeats nor drops an alarm. On a halted day it pings the heartbeat
     service's failure address, so the owner hears even if the alarm sent from
     the machine is lost.
   - **A halt is final for its session.** This finality is this doc's choice
     rather than part of the ruling. The owner ruled on 2026-10-29 that the
     owner or the operator clears a halt, with a command that journals who
     cleared it, why, and any correction a person made, such as a journaled
     override that explains a mismatch. Clearing never reruns the halted
     session. The next session runs as usual.

   Everything the supervisor knows, it reads from the run directory, so a
   supervisor that crashes and is started again starts each worker whose lock
   is free. A worker that still holds its lock, because a child process
   outlives a parent that crashes, is left to finish. Workers with no log
   between them run at the same time. Alpha and cost need only the snapshot,
   and risk needs the snapshot and the reconciled book.
8. **One process at a time talks to each broker account.** The execution
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
   any order in doubt by that id. No other worker's output can loosen the
   gate, because its hard caps come from execution's own configuration, which
   the owner ruled on 2026-10-29 only the owner changes, by hand.
   [architecture.md](architecture.md)'s `## Decisions` proposes how execution
   recovers, derives its ids and combines its limits, for
   [#7](https://github.com/l3a0/ithildin-runtime/issues/7),
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11) to settle.
9. **The kill switch is a file, and pulling it is a command.** The owner or
   the operator pulls it from outside any run, so it is a file at one path per
   broker account rather than an event in a run's log. Every worker checks for
   it when it starts and whenever a message wakes it, and execution checks it
   before every send. Pulling the switch sends a message to every worker's
   queue, so a worker that is waiting wakes and sees it. Any error other than
   "no such file" reads as pulled, with a reason that says the file could not
   be read. Pulling it, per the owner's ruling on
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11), creates the file,
   journals the halt, raises the alarm, and starts an execution worker in
   cancel mode, which cancels every resting order and journals each cancel
   first. Two workers run with the switch set, because neither sends an
   order. The cancelling execution worker waits for the account's lock up to a
   deadline and alarms if it passes. Monitoring writes the day's report and
   says the switch is set. The owner ruled on 2026-10-29 that only the owner's
   or the operator's command clears the switch, and the clearing is journaled.

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

The event pipeline makes the span cheap. A worker starts once and runs its
processing loop until it has handled every session before the end-of-span
event. In shadow, paper and live the control log holds one session. In a
backtest it holds the whole history, so each worker works through every
session in turn, and the exit rule is the same in both. A worker carries
nothing from one session to the next except through files and logs, so a long
span runs the same steps as a span of one. The launch is paid once per
worker, and the workers run as a pipeline. The alpha worker can run ahead
through every session's snapshot while portfolio construction waits on each
day's book.

The clock adapter is read at two edges only. The data worker reads it to
decide when a session's snapshot is written, which is as fast as possible
under the history clock and at the session's time under the wall clock. The
supervisor reads wall time for deadlines in every mode, because a deadline
guards against a hung process rather than shaping what a strategy sees. The
workers between those edges read no clock and move on only when messages
arrive.

A halt ends its session's chain, and the span goes on to the next session, the
way the next day's live run starts after a halted one. A failure stops the
whole run.

The price of this section is four things.

1. Every value that crosses from one module to another needs a file format,
   and a test that writes it and reads it back unchanged.
2. A worker wakes on a message rather than a call, so each step waits a
   little longer than a function call would, and every writer has to know
   which workers read its log, which one table in `runtime` holds. An event
   log is audited with the `sqlite3` command line tool rather than `cat`,
   from a backup copy when it sits on read-only media.
3. A backtest writes several files and events per session, a few tens of
   thousands for test 1, and keeps them all, because the event logs are the
   record of what ran and cannot be regenerated. An event log is always copied
   with SQLite's backup command, never as raw files, and its `-wal` and `-shm`
   files are never deleted or moved apart from it. After a crash the `-wal`
   file can hold every committed event, and a review's copy of the database
   file alone held no table at all. The output files it points to go with
   it. Each commit and each published file also waits for the disk. One
   exploratory measurement on the owner's Mac put a committed append at about
   8 ms with `fullfsync` against 0.2 ms without, so a backtest of test 1's
   size spends a few minutes syncing.
4. The locks, the event logs, the queues and the supervisor's control log add
   machinery that a single process would not need, and each of their alarms
   has to fire in a drill before the paper gate.

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
| **worker** | One component, meaning one module of the charter's module table, run as its own process. It listens on its queue, reads its input files through the events that point to them, writes its output files and appends to its event log, and reaches the outside world only through the adapter at its edge. [Processes and files](#processes-and-files) gives its rules |
| **output file** | One thing a component produced for one session, written once at a name fixed by the worker and the session, and never changed |
| **event** | One row of a component's event log, recording one output file by path and SHA-256, or a halt, and naming the events it came from |
| **event log** | The SQLite database holding one component's events in the order it wrote them, which only ever grows and is the source of truth for what the component produced. Each event log in a run directory has one writer. The journal is an event log with several writers, which SQLite's write lock takes one at a time |
| **control log** | The event log the supervisor writes, holding one event per session of the run, then an end-of-span event, and a halt event for any worker that failed |
| **queue** | The named pipe in the run directory on which one worker listens. A message on it names an event log and a sequence number with something new to process, and the event log stays the record |
| **supervisor** | The process that starts every worker of a run at once, writes the run's control log, appends a halt event when a worker crashes or misses its deadline, and stops the run on such a failure. It keeps no state outside the run directory |
| **run directory** | The directory that holds every output file, event log and queue one run's workers use. The journal and marketlake's lake live outside it |
| **halt event** | The event a worker appends instead of an output when it cannot process a session, naming the reason or the halt event it met. Every worker downstream passes it on and skips that session. The owner or the operator clears it, and clearing never reruns the session |
| **span** | The sessions in a run's control log, which every worker works through: one in shadow, paper and live, and the whole history in a backtest |
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
| A message queue that deletes what it delivers, or a database server, between workers | A queue of that kind leaves nothing behind to audit, and a server is one more process that can fail. A file needs neither, and stays as the record. The price is that a worker watches or polls its input files instead of being sent its input |
| One process launch per worker per session in a backtest | Test 1 would make 10,360 launches, 35 to 52 minutes at pyarrow's measured import cost. A worker takes a span of sessions instead, as [A worker works through a span of sessions](#a-worker-works-through-a-span-of-sessions) says |
| A controller that calls each component in a fixed order | The owner ruled on 2026-10-29 that the platform is an event pipeline, so a new event in a component's input is the only trigger. An order fixed in a controller would state the dependencies a second time, beside the event logs that already carry them |
| Outputs kept as rows in the event log itself | One write would have covered both the data and the record. The owner ruled on 2026-10-29 for separate output files with SQLite pointers, and the event log as the source of truth. The price is two writes per output, a file then a row, which rule 3 makes safe to retry because a file with no row pointing to it is never read as an output |
| A JSON Lines file per event log | It reads with `cat`, but a crash partway through an append tears a line. That needed rules to set the fragment aside and to make a reader wait at it, and a review found that a writer appending after a torn line hid every event after it, a committed journal entry among them. The owner ruled on 2026-10-29 for SQLite, whose transactions make an append whole or absent. The price is that an event log is read with `sqlite3` |
| A messaging server such as NATS JetStream, Kafka or Redpanda | Each is a server that can fail and halt every worker, and workers would pass events through it rather than through files, which the owner's ruling asks for. Kafka and Redpanda are also built for far more than about seven events a session on one VM |
| A worker waking on file notifications through `watchdog`, or by polling its input logs | The owner ruled on 2026-10-29 for a queue per component instead, which needs no dependency and no polling. The price is that each writer sends a message to every reader of its event log, which one table in `runtime` wires |
| One process per action, such as the order manager and the order path each in its own | The owner ruled on 2026-10-29 that this was too granular, and narrowed the ruling to one process per component. A component is the unit the charter gives an interface and an owning role |
| More than one execution process per broker account at a time | The risk gate checks limits across every order and position in the account's journal, so two gates at once could each approve orders that together breach one. A restarted supervisor beside a still-running order path sent every order twice in a review's simulation, which is why rule 7 takes a lock |
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
