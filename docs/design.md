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
lines of code. The next section says what that event is: a decision result,
one blob in storage that lists a component's outputs for a decision. Only the
data worker, the supervisor, and execution's send stage for its cutoff read
the clock.

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
   nothing else. Every output upstream of it is already stored, so recovering
   means starting one component again, which reads the blobs that exist and
   writes the ones that are missing, rather than replaying the day. A decision
   that halted stays halted, and the next decision runs. If the cause is still
   there, that decision finds it again and halts too.
2. **Every input and output can be read afterwards.** Each value that passes
   from one module to another is a blob whose header names the blobs it was
   made from. Auditing a decision means reading those blobs rather than
   rerunning code and hoping it reproduces. The journal already records every
   step of an order. The blobs extend that record to every step before the
   order.

The owner ruled nine more things about the shape the same day.

1. **An event pipeline.** A component runs its processing loop when there is
   something new for it, and what it produces is what sets the components
   downstream of it running. Between components, a new output is the only
   trigger.
2. **A queue per component.** A component listens for events on its queue and
   processes each one. A message is sent to a component's queue whenever
   there is something new for it to process.
3. **A stateless VM, with files as blobs in S3.** The VM keeps no state that
   matters. Every file is a blob, an object in storage, and the storage is
   S3, which marketlake already uses.
4. **Blobs keyed by decision, with the journal the only log.** Pure steps write
   create-only blobs at decision keys, with headers naming what each was made
   from. Data's first write wins. A halt is a blob at the same kind of key.
   The journal is the only numbered log. Earlier rulings the same day had kept
   an event log per component, first in SQLite and then as numbered blobs, and
   a ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10) had
   kept the journal's entries in SQLite rows. This ruling replaced all three.
   This doc's reason for the shape is that only the journal records actions
   that cannot be repeated, so only the journal needs numbering.
5. **An in-memory store for backtests.** A backtest swaps S3 for a store in
   memory, through the same port.
6. **Fetch and sync before changing state, and resolve conflicts first.** As
   in git, any step that changes state first fetches the latest state from
   where it is kept, brings its own view up to date, and resolves any
   conflict before it changes anything.
7. **Any time of day.** In the owner's words, "the architecture should
   support trading at any time of day." The ruling answered a draft that
   started every run in the evening, after marketlake's 18:30 sweep, with
   orders filling at the next open.
8. **As often as needed.** "And as frequently as needed." The owner made it
   concrete on 2026-10-30: the architecture assumes the platform runs through
   every session and trades continuously, at decisions as often as once a
   minute, on the design below, with S3 holding everything.
9. **Other strategies.** "It should be extensible to other strategies in the
   future."

Every worker is its own process, so this doc takes the in-memory store as a
memory-backed folder, such as `tmpfs` on Linux or a RAM disk on macOS, which
every worker can reach, rather than one process's memory, which only one can.
The rules below are this doc's design for those rulings, proposed to
[#22](https://github.com/l3a0/ithildin-runtime/issues/22) and
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), and not part of the
rulings themselves.

Ten entries define the design's terms.

1. A **blob** is one object in storage, written once and never replaced. An
   **output file** is a blob that holds one thing a component produced for
   one decision, such as a snapshot or a forecast, at a key fixed by the run,
   the component's stage, the decision and the output's name.
2. A **stage** is one step a component takes in a decision. Most components
   take one. Execution takes two, recover and send, and monitoring takes two,
   reconcile and report, because each acts at two points in a decision.
3. A **decision result** is the blob a stage writes last for a decision, at a
   key fixed by the run, the stage and the decision. It either lists the
   stage's output files for the decision, by key and SHA-256, or it is a
   **halt**, saying why the decision cannot go on. A stage has one result key
   per decision, so it ends each decision exactly once, with outputs or with a
   halt. An **event** is the arrival of a decision result, which a message on a
   queue announces.
4. A **worker** is one component run as its own process. It listens on its
   queue, reads its input blobs, writes its output blobs, and reaches the
   outside world only through the adapter at its edge, such as the broker or
   the data source. A worker imports `core`, the shared types, and no sibling
   module, and shares no memory with any other worker.
5. The **supervisor** is the process that writes the run's **plan**, a blob
   listing the run's mode and its decisions in order, messages the workers,
   watches their results and heartbeats, and halts the run. It starts and
   stops no process.
6. A **run** keeps all of its blobs under the run's own prefix in storage. The
   VM keeps only a working directory for the run, holding its queues and its
   workers' error output, and none of that is kept. In shadow,
   paper and live a run is one trading day of one deployment, and in a
   backtest it is the whole span.
7. A component's **listing** is its stages' blobs, listed by decision. Each
   blob's header names the run, the decision, and every input blob it was made
   from, by key and SHA-256, along with the release commit and a hash of the
   configuration, so the listing is the component's record and each entry in
   it leads back to its inputs.
8. A **decision** is one moment at which a deployment decides, such as 15:50
   on one trading day for a daily strategy, or every five minutes through the
   day for a faster one. Its key is the decision time in the exchange's time
   zone, written so keys sort in time order. Every stage writes one result
   per decision.
9. A **strategy** is the part of the platform that differs from one way of
   trading to the next: the instruments it trades, its **schedule** of
   decision times, the data it needs, its alpha model, and its configuration.
   Data, risk, cost, portfolio construction, execution and monitoring are
   shared by every strategy.
10. A **deployment** puts one or more strategies on one broker account in one
    mode. It is what the deployment's lease covers, and every key a run
    writes sits under the deployment's name.

Three stores sit outside every run, because they outlive any one run.

1. marketlake's lake, which holds captured market data.
2. The journal, one per broker account, which the book is replayed from. It
   is the one numbered log, it has more than one writer, and each entry is
   stored before the step it records happens, which is how hard limit 4 holds
   through the loss of the VM.
3. A simulated account's resting orders and positions, one blob per account
   per decision, which the simulated broker's adapter reads and writes through
   the storage port, so a shadow order still resting when one run ends is
   there in the next.

Ruling 6 runs through every rule below, and this doc applies it the same way
in each place. A step fetches what it is about to change, or what its change
depends on, immediately before it acts. When the state it fetched has moved on
in a way the step can work from, as when another writer has appended to the
journal or an earlier attempt already stored a blob, the step brings itself up
to date and carries on, the way git fast-forwards. When the fetched state
disagrees with the step's own view in a way that would mean choosing between
two versions of the truth, as when the broker's positions differ from the
journal's book, the step halts and a person resolves it, the way a git merge
conflict waits for a person. Nothing ever forces a change over state it has
not fetched: no blob is replaced, no entry is rewritten, and no order is sent
on a book not brought up to date at the start of the send stage. Five places
apply it.

1. **Every write** is conditional, so storage refuses it when the key exists,
   and a resuming worker checks its keys first, which sends it down rule 3's
   resume path rather than a second write.
2. **Every journal append** goes at the next number after the last one the
   writer knows, and a refusal because another writer moved the journal on is
   the git push refused for being behind: the writer reads what is new,
   decides again, and appends after it, as rule 8 says.
3. **Every order** waits on a fetch of the broker's positions, open orders and
   fills made at the start of execution's send stage, as rule 9 says, and,
   just before the broker call, on the kill switch, the run's halt, the
   account's lease and the journal's end, as rule 9's fourth item says.
4. **Every lease** is fetched before it is taken, and one another VM holds is
   resolved by a person, as rule 9 says.
5. **Every read of marketlake** fetches the manifest entry before the data it
   names, and a checksum that differs halts, as `## Where it runs` says.

Ten rules make the blobs something a worker can trust.

1. **Every write is a blob, created once.** A worker writes each blob with a
   single upload carrying a condition that makes S3 refuse it if the key
   exists, and the blob's SHA-256, which S3 checks before it accepts the blob.
   So a reader never sees half a blob, and no blob is ever replaced. S3
   returns the stored blob's tag and records when it stored it, and the
   storage port hands both back. The bucket's policy, scoped to the runtime's
   own principal, refuses any write on the runs', the journal's, the leases'
   and the switch's prefixes that lacks the condition, and any delete outside
   the leases, and the bucket keeps every version, so the rule holds even
   against a bug in the runtime's own code. A deterministic component's header
   carries no clock time, so two runs of the same step on the same inputs
   write the same bytes. A capture, meaning data's snapshot and execution's
   account file, and every journal entry carry the time the source was read or
   the entry was built, to the microsecond, beside the time S3 records, which
   holds whole seconds only. A write S3 refuses with a conflict, or does not
   answer, is retried until a deadline, and then halts. An S3 outage longer
   than the deadline therefore loses that decision's captures, which the halt
   reports.
2. **Two kinds of stage, declared in one place.** The one table in `runtime`
   that declares each stage's inputs also declares its kind, and the storage
   contract in `core` alone applies what follows, so no module branches on
   its kind. Most stages are deterministic: the same input blobs, release and
   configuration give the same bytes. That holds for alpha, risk, cost,
   portfolio construction, and monitoring's reconcile and report stages, the
   report naming the last journal entry it read as an input so a rerun reads
   the same journal. When S3 refuses one of their writes because the key
   exists, the contract compares the stored blob's SHA-256 with the stage's
   own. Equal means an earlier attempt already did this work. Different means
   the stage is not deterministic, which is a bug, so it halts. Two stages
   capture something that may not come again: data's snapshot, and
   execution's recover stage, which reads the broker. Execution's send stage
   reads the broker again, but as a fresh read on every attempt, which ruling
   6 asks for, and it records what it read in the journal rather than as a
   capture. For those the first
   capture wins. A resumed capturing stage reads its stored capture and
   derives every remaining output from it, never from a fresh read, and a
   fresh read that differs from the stored one is stored too, under its own
   attempt number, and named in the decision result rather than discarded.
3. **A worker resumes by reading what exists.** A worker reads the run's
   plan, then for each decision checks its stages' result keys. A result there
   means the decision is done. If none is there, the worker checks the result
   keys of its inputs for the decision. If every input has a result, it reads
   the output files those results list, computes, writes each output file it
   does not find already stored, under rule 2, and writes its own result last.
   If an input has no result, it waits. A worker keeps what it has read in its
   own memory and nowhere else, so a replacement VM has nothing to restore,
   and a reader needs no cursor, because the keys it needs are fixed by the
   decision. Since a stage has one result key per decision, a decision can never
   end that stage with both outputs and a halt, or twice. A worker whose result
   write S3 refuses because the supervisor has already written a halt there,
   as rule 5 says, takes the halt as final. Every instruction the supervisor
   gives is a blob before it is a message: the plan, a halt of the whole
   run, and the stop at the end of the run. A worker acts on a message only
   after reading the blob it names, and checks those blobs whenever it
   wakes, so a message that is dropped or never read loses nothing.
4. **A worker listens on its queue, then reads its inputs.** Each queue is a
   named pipe in the VM's working directory, a file that one process reads
   and others write to, one per worker. One table in `runtime` is the only
   place a worker's inputs are declared. Each worker's entry point hands it
   its inputs from that table, and hands the storage contract the list of
   workers that read its outputs. After a worker writes a decision result, the
   contract sends a message to the queue of every worker on that list. A
   message names the blob its reader should read: a decision result, the
   plan, the run's halt or stop, or the kill switch. Every send opens the
   pipe without waiting. A worker that is not running has no listener, and a
   worker far behind may have a full pipe, which a long backtest can fill.
   Either way the send fails at once and is skipped, because a full pipe
   already holds a message that will wake its reader, and a stopped worker
   catches up when it starts. A starting worker opens its pipe for reading
   first, and only then checks its keys to catch up, so no result written in
   between goes unannounced. Each message is one line under the pipe's
   512-byte limit, so messages from several writers never mix. A writer that
   crashes between its result and its message has already stored the result,
   so when it starts again it sends a message for its newest result before
   anything else, and a deadline catches a writer that never starts again. A
   woken worker checks every decision it has not finished rather than only the
   one a message names, so a repeated or skipped message costs nothing.
5. **Each blob names what made it.** Its header names its run, its decision,
   its kind, output or halt, its format version, every input blob by key and
   SHA-256, the release commit and a hash of the configuration. A reader
   checks each input's SHA-256 against the header that named it and halts on
   a mismatch. Nothing irreversible rests on an upstream blob alone: execution
   journals the whole target portfolio it acts on, in a journal entry, before
   its first send. Each release writes only formats the release before it can
   read, so pinning back to an earlier release still reads what storage
   holds. A worker killed by a signal or a crash writes nothing about itself
   as it dies. The host that runs it, systemd on the VM today, records the
   exit status or signal and the end of the worker's error output in a file
   of the worker's own when the worker stops, and restarts it. The restarted
   worker uploads that file as a restart record at a key fixed by the worker
   and its restart count, with anything shaped like a credential masked, and
   monitoring uploads it instead if the worker does not come back. The
   worker then resumes from storage, as rule 3 says. Each worker and the
   supervisor also write a heartbeat, a small blob at a key fixed by the
   writer and the time, from their main loop at least every fifteen seconds,
   so the supervisor can tell a live worker from a hung or lost one without
   being its parent, and monitoring can tell the same of the supervisor. The
   bucket's lifecycle expires heartbeats after a week, the one prefix where
   anything is removed.
6. **An absent, stale or empty input halts. It never reads as "trade
   nothing".** A blob whose header names the wrong decision halts as stale. An
   output file with no entries halts unless it carries an explicit "trade
   nothing" mark, and that mark means hold every position and send no order,
   never a target of zero. A worker that cannot process a decision writes a
   halt as its result for the decision, with the reason, and then exits cleanly or moves to its
   next decision, because a halt is not a crash. Hard limit 3 came from an
   execution server that read "no signals" as "hold nothing" (L4690). Here,
   "has this decision's forecast been written" is a check of one result key.
7. **A halt flows down the pipeline, and the supervisor only sends
   messages.** The host keeps every worker running and restarts one that
   exits: on the VM, a systemd unit per worker's container, as rule 10 of
   architecture.md's `## Where it runs` says. The supervisor starts, stops
   and watches no process. It takes the deployment's lease, as rule 9 says,
   writes the run's plan, which a restarted supervisor finds already there,
   and sends a message to every worker's queue. A worker that starts before
   any plan exists, or finds a plan whose run the deployment's lease does
   not name, waits, so no worker acts before a supervisor holds the lease.
   Every worker, in every mode, finishes its decision loop once it has a
   result for every decision in the plan. The supervisor then stores the
   run's stop, messages each worker, which exits cleanly, and deletes the
   deployment's lease. A timer on the host stops every unit after the close
   as well, in case a stop is lost.
   - **A halt is passed on.** A worker whose input's result for a decision is a
     halt writes its own halt as its result, naming the one it found, and goes
     on to its next decision.
   - **A crash is a restart, and a failure stops the run.** One crash is not
     a failure: the host restarts the worker within seconds and it resumes
     from storage, which rules 3 and 9 make safe to repeat. A worker has
     failed when it misses its deadline, when its heartbeat stops for longer
     than a minute, or when it restarts more often in one run than the
     host's limit allows. Its deadline runs from the moment its last needed
     input appears, so waiting on a slow upstream worker never counts against
     it. A worker that hangs answers no message, so a watchdog on the host
     kills its container when its heartbeat is thirty seconds old, well
     inside the one minute that counts as a failure, and the host restarts
     it. On a failure the supervisor writes the halt as that worker's
     result, naming its restart records and every output file it finds
     under the decision's prefix, so no stored capture is left unlisted. It
     stores a halt of the whole run, writes a halt as every stage's result
     for every decision still left in the plan, and sends every worker a
     message to halt. Each halts itself, monitoring journals the failure,
     pings the failure address and reports, and the supervisor exits
     cleanly, which its unit does not restart.
   - **Monitoring tells the owner once.** Monitoring, the one worker that reads
     every component's results and holds the alert channel, journals every
     halt with its reason and raises an alarm on the halt that names no
     earlier one. Within one run, a run of halts with the same reason alarms
     once, and again only after a decision succeeds, so a fault that persists
     through a day of frequent decisions alarms once that day, and once again
     on each later day it persists. Before it alarms, it writes an output file
     naming the halt it is handling, so a restarted monitoring finds that file
     and neither repeats nor drops an alarm. Every halted decision pings the
     heartbeat service's failure address, so the owner hears even if the alarm
     sent from the machine is lost.
   - **A halt is final for its decision.** This finality is this doc's choice
     rather than part of the ruling. The owner ruled on 2026-10-29 that the
     owner or the operator clears a halt, with a command that journals who
     cleared it, why, and any correction a person made, such as a journaled
     override that explains a mismatch. Clearing never reruns the halted
     decision. The owner narrowed it on 2026-10-30: on a live account only
     the owner clears a halt, a switch or a lease, or writes an override,
     and the operator acts on paper and shadow accounts, plus pulling a live
     account's switch.

   Everything the supervisor knows, it reads from storage, so a supervisor
   that crashes and is started again reads the plan, the results and the
   heartbeats, and carries on, while the workers keep running without it.
   Monitoring alarms when the supervisor's heartbeat stops, and a handler on
   the host alarms when any unit reaches its restart limit, so a supervisor
   that is gone does not leave the run unwatched in silence.
   Workers with no input between them
   run at the same time. Alpha and cost need only the snapshot, and risk needs
   the snapshot and the reconciled book.
8. **The journal is the one numbered log.** Its entries sit at keys fixed by
   the account and a sequence number, written with leading zeros so they sort
   in order. Each entry holds its exact serialized body, a hash of that body,
   and the hash of the entry before it. To append, a writer takes the last
   sequence number it knows, builds the next entry chained to the last one,
   and creates it at the next number's key. If S3 refuses because that key
   exists, the writer reads that entry first. If its hash equals the entry the
   writer built, the writer's own earlier attempt landed and its reply was
   lost, so the append is done. If it differs, another writer got there first.
   The writer then reads the journal past the number it knew, checks the kill
   switch and any halt among the new entries, decides its step again against
   what it has just read, so an order is never sent on a stale book or after a
   pull, and starts again from the new end. Two writers can therefore never
   take one number, and no entry is ever written twice. Each entry is
   stored before the step it records happens. A reader checks each entry's
   hash and its link to the one before, and halts on a hash that does not
   match, a link that does not chain, or a gap in the numbers.
9. **Leases make one VM, and one sender, per account.** A lease is a blob a
   process creates with the condition that it not exist, recording its
   holder: the VM, its boot, the process, the release, the run and when it was
   created. It is deleted only with a condition naming the tag S3 returned
   when it was created, so no stale holder deletes a successor's lease. Two
   leases matter.
   1. **The deployment's lease.** The supervisor takes it before it writes the
      plan, in every mode, so a replacement VM started while the old one
      still runs halts and alarms, and its workers, which find no plan under
      a lease of theirs, wait.
   2. **The account's lease.** The execution worker takes it for its whole
      life, and a second execution worker that would send and cannot take it
      halts.

   A holder record names the VM, its boot, the worker's container and its
   restart count rather than a process id, which means nothing outside one
   container. A lease whose holder is the same VM on an earlier boot, or a
   container the host shows is gone, is cleared by the next process that
   needs it, and the clearing is journaled. A stopped heartbeat alone never
   clears a lease, because a frozen process can wake. A lease held by another VM waits for the owner or
   the operator, who first confirm that VM is gone. The order manager, the
   risk gate, the journal and the order path all run inside the execution
   worker, because the gate checks each order against every order and
   position in the account's journal just before the order is sent, and a
   check passed through a blob could be stale by then. This is the one place
   a risk engineer's piece runs in another component's process. Running a
   worker again is safe everywhere except here, because a sent order cannot be
   unsent. So execution sends an order only when the journal holds no
   `sending` entry for its client order id, and asks the broker about any
   order in doubt by that id, never by sending again. Five more rules keep a
   rerun from doubling anything.
   1. Execution journals each fill under the broker's own fill id, and skips
      a fill whose id is already journaled.
   2. At the start of its send stage, before it plans, execution fetches the
      broker's positions, open orders and fills again, a fresh read on every
      attempt rather than a capture that the first one wins. It settles each
      order in doubt by its client order id, journals any fill that arrived
      since its recover stage, and compares the result with the book it
      replays from the journal. An open order at the broker that matches a
      `sending` entry, and a fill within that order's quantity, are the
      platform's own and bring the book up to date. So is an order the journal
      holds as open that the broker no longer lists, once a lookup by its
      client order id finds it `Expired`, `Rejected` or cancelled at the close
      as its time in force allows, or cancelled after a journaled cancel of
      the platform's own, such as the kill switch's or the one that keeps a
      pair whole. Execution journals each such outcome, in its recover stage
      and in this sync. Any other difference
      halts the decision for a person, because the broker moved in a way the
      platform did not ask for. Only then does it plan. It journals the
      planned orders, each with its client order id, in the same entry as the
      whole target portfolio, along with the journal entry it synced through
      and a hash and the time of the broker state it fetched, before its first
      send. A rerun in the same decision never plans again, so an order's
      id never shifts between attempts, and sends nothing more, as item 5
      says. The id is
      built from the account, the decision time and the order's place in that
      list, so it is unique across the decisions of a day and the same on
      every rerun.
   3. Its account file names the journal entry it replayed through, and the
      send stage's sync names the entry it synced through. Before it plans,
      execution halts if an entry that changes the book, and that neither its
      sync nor its own order loop wrote, lies past the entry its sync names.
      A person's override that lands after the sync halts the decision the
      same way before its next send, and applies from the next decision's
      sync.
   4. **Right before every broker call**, after the order's `sending` entry
      is stored, execution checks four things: that the kill switch is
      absent, that no halt of the run is stored, that its own send-stage
      result key is still empty, and that the account's lease still carries
      the tag execution got when it took it. Any one failing journals the
      order `Abandoned`, sends nothing, and halts the decision. So a run the
      supervisor has halted, a switch pulled mid-loop, or a lease another
      execution has taken over stops the next send, even when the message
      saying so is lost or unread.
   5. **A restarted execution sends nothing more for a decision it was in
      the middle of.** Its first acts on starting are, in order:
      1. Take the account's lease, which it may only once the host shows
         its earlier container is gone. Each worker runs under one fixed
         container name, removed when it stops, so a gone container is a
         fact the host reports, not a guess from a stopped heartbeat.
      2. Check the kill switch, and if it is set cancel every resting order,
         journaling each cancel first.
      3. For a decision whose send stage has journaled planned orders and no
         result, settle each order in doubt by its client order id, journal
         every planned order without a `sending` entry as `Abandoned`,
         cancel any accepted leg of a pair whose partner is not accepted,
         before the cancel cutoff, and write a halt as that send stage's
         result. It does this whether or not the supervisor has already
         written a halt there.
      4. Write its restart record and carry on with the next decision, which
         plans again from the book.

   The gate counts every open order at its worst case, separately for each
   limit: for each limit, an order on the side that makes that limit worse
   counts as filled, and one on the other side counts as unfilled, until the
   broker confirms its fill, its cancel or its expiry. For `max_net` that
   means every leg of a pair must fit alone, which is also the bound on a leg
   left open when its partner fails. A
   cancel is journaled before it is sent, and the order stays open in the
   book until the broker confirms it. Without this, an order left resting
   from one decision lets the next decision plan a trade that passes
   `max_position` if both fill.

   No other worker's output can loosen the gate, because its hard caps come
   from a blob the bucket's policy lets only the owner's credential write,
   and the owner ruled on 2026-10-29 that only the owner changes them, by
   hand.
   [architecture.md](architecture.md)'s `## Decisions` proposes how execution
   recovers, derives its ids and combines its limits, for
   [#7](https://github.com/l3a0/ithildin-runtime/issues/7),
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11) to settle.
10. **The kill switch is a blob, and pulling it is a command.** The switch is
    a blob at one key per broker account. Every worker checks for it when it
    starts and whenever a message wakes it, and execution checks it before
    every send. Any answer other than "no such blob", a timeout included,
    reads as pulled, with a reason that says the switch could not be read, so
    the runtime's credential can list the switch's prefix, without which S3
    answers a missing key with "access denied". Pulling it, per the owner's
    ruling on [#11](https://github.com/l3a0/ithildin-runtime/issues/11),
    creates the blob, journals the halt, raises the alarm, and cancels every
    resting order, journaling each cancel first. This doc adds how. The
    switch can be pulled from anywhere, so the supervisor checks it every few
    seconds and sends a message to every worker's queue when it finds it set.
    Execution, the one worker that holds the account's lease, then stops
    sending and cancels every resting order through the order path, and
    alarms if a cancel fails. Execution does not wait for that message. It
    reads the switch itself every few seconds whenever the account has a
    resting order, and on every pass of the loop that watches a pair's legs,
    so a pull reaches resting orders even with the supervisor down. A
    starting execution takes the account's lease and then checks the switch
    before anything else, as rule 9's fifth item says, so a switch pulled
    while execution is down still cancels, and it alarms if it cannot take
    the lease by a deadline. Execution checks the switch again right before
    every broker call, as rule 9's fourth item says. When it has cancelled
    every resting order it journals that the switch's cancels are done, and
    monitoring alarms a second time if that entry has not appeared within a
    deadline of the pull, naming the broker's own console as the next step.
    No worker sends an order with the switch set. Monitoring writes the decision's report and says the
    switch is set. The runtime's credential can create the switch but never
    delete it. The owner ruled on 2026-10-29 that only the owner's or the
    operator's command clears the switch, and that the clearing is journaled.
    Later that day the owner narrowed it: on a live account only the owner's
    command clears it. On a paper or shadow account the operator's may too. This doc
    adds that the credential that clears it never sits on the VM. When the VM
    or S3 cannot be reached, the runbook on
    [#11](https://github.com/l3a0/ithildin-runtime/issues/11) has a person
    create the switch from anywhere, cancel at the broker's own console, and
    journal both as a human override once the VM is back.

### A worker works through a span of decisions

The cost of starting a process decides how a backtest keeps
[one runtime](#one-runtime). On the owner's M1 Max under Python 3.12, a launch
that imports only the standard library took about 0.05 seconds, one that
imports pyarrow 0.2 to 0.3 seconds, and one that imports pandas about 1
second. These are exploratory measurements, the mean of 20 to 50 launches on
one machine, outside the test suite, and a rerun on a busy machine measured
pyarrow higher. Read them as a scale rather than a pin. The backtest that test
1 runs covers 1,480 decisions, one per trading session of the span quantitative-trading pins for the
strategy [architecture.md](architecture.md#the-strategy-the-skeleton-runs)
proposes on [#6](https://github.com/l3a0/ithildin-runtime/issues/6). Through
seven modules that is 10,360 worker runs. One launch per worker per decision
would take about 9 minutes at the standard library's cost, 35 to 52 minutes at
pyarrow's and 3 hours at pandas'.

The event pipeline makes the span cheap. A worker starts once and runs its
processing loop until it has a result for every decision in the plan. In
shadow, paper and live the plan holds one trading day's decisions. In a
backtest it holds the whole history. Each worker works through every decision
in turn, and the exit rule is the same in both. A worker carries nothing from one
decision to the next except through blobs, so a long span runs the same steps
as a span of one. The launch is paid once per worker, and the workers run as a
pipeline. The alpha worker can run ahead through every decision's snapshot
while portfolio construction waits on each decision's book.

A backtest swaps S3 for the memory-backed folder, through the same storage
port, so only the adapter differs by mode. The folder adapter keeps S3's
rules by writing each blob to a temporary file in the same folder, checking
its SHA-256, then linking it to the key, which refuses an existing name and
never shows half a blob. One contract test suite runs against both adapters.
It covers a write refused when its key exists, the SHA-256 check, keys listed
in order, the same answers for a refusal, two processes racing one create, a
read during a write, a writer killed partway through, a lost reply on a write
that landed, the tag a create returns, the stored time a lookup returns, and
a conditional delete with a stale tag. A rule the folder breaks fails a
test. A backtest's blobs vanish when it ends, which the premise
allows, because a backtest can be regenerated from committed data and the
pinned release.

The clock adapter is read in three places only. The data worker reads it to
decide when a decision's snapshot is written, which is as fast as possible
under the history clock and at the decision time under the wall clock. The
supervisor reads wall time for deadlines in every mode, because a deadline
guards against a hung process rather than shaping what a strategy sees.
Execution's send stage reads it before every send, to check the cutoff for
the strategy's orders, as `### Strategies, schedules and decisions` says.
Under the history clock that reading is the decision time, so a backtest
never meets the cutoff, and a drill sets a test clock past it instead. The
workers between those places read no clock and move on only when messages
arrive.

A halt ends its decision, and the span goes on to the next decision, which
halts again if its cause is still there. A failure stops the whole run.

The price of this section is five things.

1. Every value that crosses from one module to another needs a file format,
   and a test that writes it and reads it back unchanged. A deterministic
   component's format also has to be canonical, with fixed field order and
   number formatting, so the same inputs give the same bytes.
2. A worker wakes on a message rather than a call, so each step waits a
   little longer than a function call would, and every writer has to know
   which workers read its outputs, which the one table in `runtime` that
   declares every worker's inputs hands it.
3. Each write and each read in shadow, paper and live waits on S3, by an
   estimate 10 to 30 ms rather than a local disk's fraction of one. A live
   decision writes roughly 20 to 30 blobs, counting each stage's outputs and
   result, the plan and the journal entries, so the wait is about a second. The
   backtest's memory-backed folder is why test 1's tens of thousands of
   writes and reads do not cost minutes.
4. The leases, the queues, the heartbeats, the restart records, a unit per worker and the supervisor's plan add machinery
   that a single process would not need, and each of their alarms has to fire
   in a drill before the paper gate.
5. Two adapters for storage must behave alike, which the contract suite
   holds, and boto3, Amazon's Python library for S3, becomes a dependency,
   from a release whose botocore sends both the refuse-if-it-exists condition
   and the conditional delete.

### Strategies, schedules and decisions

The owner's rulings 7 to 9 move the time of day, the frequency and the
strategy out of the platform and into the strategy. The first strategy trades
once a day on daily closes, and a design shaped around it would bake in an
evening run, a key per trading session and one strategy per account. Blobs are
never rewritten, so a key or a stored format changed later leaves every reader
handling two layouts. That is the one part this subsection builds in slice 1:
keys, stored formats and the plan. Machinery that only a strategy deciding
minutes apart, or a second strategy, would exercise waits for one, and the
list of what waits is below.

This doc proposes that a strategy bring five things, each declared in the one
table in `runtime` beside the workers' inputs, so that adding a strategy adds
rows rather than changing a shared module.

1. The instruments it trades.
2. Its schedule, a list of decision times on each trading day.
3. The data it reads: the kinds, the bar length and the lookback.
4. Its alpha model, run as a stage of its own named for the strategy, so two
   strategies' forecasts never share a key.
5. Its configuration, whose hash every blob it touches records.

A run in shadow, paper and live is one trading day of one deployment. One
function in `core` turns the deployment's schedules and the exchange's trading
calendar into a list of decisions, each with its time and the strategies
deciding at it. The supervisor calls it for one day, and a backtest calls it
for the whole span, so both build their plans the same way, and an early close
drops the decision times after it. The host starts every unit of the run
before the first decision time. The workers stay up all day and work through the
decisions as they work through a backtest's span, and the data worker writes
each decision's snapshot once the wall clock reaches its time. A worker's
launch is therefore paid once a day. A daily strategy's plan holds one
decision.

Five rules keep a day of decisions safe.

1. **Each decision starts from the broker's state at its own time.**
   Execution's recover stage for a decision waits for two inputs: its own send
   result for the decision before, and data's result for this one. So it reads
   the broker after the last decision's orders went out and once the
   decision's time has come, rather than once at the start of the day, and
   reconcile, risk and portfolio construction size every decision on a
   current book. Data and recover therefore no longer overlap.
2. **Decisions send in order.** Execution sends for a decision only once it
   has a result for the one before, so two decisions never share the order
   path. Every stage's deadline fits inside the gap to the next decision time.
3. **Orders still open at the next decision are netted.** The order manager
   trades the target minus the positions and the open orders, and the risk
   gate counts each open order at its worst case, as rule 9 says, so an order
   left resting is neither sent again nor left out of a limit.
4. **The dead-man check follows the schedule.** Monitoring pings after each
   decision's report, and the heartbeat service's check runs on the
   deployment's schedule, a check by cron expression in the exchange's time
   zone, with a grace period of the longest chain of worker deadlines. On a
   market holiday the supervisor writes a plan with no decisions, and
   monitoring pings anyway. A period set to the longest gap between decisions
   would span a long weekend, so a run that died on a Tuesday would go
   unheard until Saturday.
5. **Market-on-close orders go out before the broker's cutoff, or not at
   all.** The owner ruled on question 10 of [architecture.md](architecture.md)
   that the skeleton trades the close with market-on-close orders, which
   Alpaca rejects after 15:50. The cutoff belongs to the broker account,
   because it is the broker's rule: the hard caps blob of the paper account
   holds 15:48 America/New_York on the decision's own session date, and a
   simulated account's holds the close itself. A strategy can set an earlier
   cutoff and never a later one. The cutoff applies to market-on-close orders
   only, so a market order sent at another time, such as the shadow run on
   [#16](https://github.com/l3a0/ithildin-runtime/issues/16) sends at 15:50,
   meets no cutoff. It applies to new orders only, and never stops a cancel.
   1. **Before every send, twice.** Execution reads the clock before it
      starts the order loop, before each order, and again after the order's
      `sending` entry is stored and immediately before the broker call, with
      rule 9's other checks. An order past the cutoff is journaled `Abandoned`
      and the decision halts. Inside the send stage, the retry deadline of a
      write S3 does not answer ends no later than the cutoff, so a slow write
      cannot carry a send past it.
   2. **A clock that cannot be trusted halts.** At the start of the send
      stage, execution compares the VM's clock with the broker's own clock,
      Alpaca's `GET /v2/clock`, and
      halts when they differ by more than a tolerance, because a slow clock
      reads as "before the cutoff".
   3. **A pair is kept whole while cancels are still taken.** The cancel
      cutoff is the earlier of the broker's and the exchange's cutoff for
      cancelling a closing-auction order, which this doc takes as 15:50, as
      architecture.md's kill switch section says. After the order loop, execution
      looks up each leg by its client order id until every leg is accepted or
      the cancel cutoff passes. When one leg is rejected, unsent or past the
      cutoff while another is accepted, execution cancels the accepted legs,
      journaling each cancel first. When execution crashes with its send stage
      unfinished, the host restarts it within seconds, and the restarted
      execution sends nothing more for that decision, cancels any accepted
      leg whose partner was not accepted, before the cancel cutoff, and halts
      the decision, as rule 9's fifth item says.
   4. **What stays open is bounded, not undone.** A cancel that arrives after
      the cancel cutoff, or a closing auction that fills a leg only in part,
      can leave a pair with one leg filled. The risk gate checks each leg
      alone against `max_position` and `max_net`, as rule 9 says, so that leg
      is no larger than the caps allow, and monitoring alarms after the close
      on any pair whose filled legs differ from the planned hedge. A cancel is
      not a trade, so this stays inside hard limit 3. The owner ruled on
      2026-10-29 that `max_net` is set from the venture's loss limit rather
      than from the size of a leg, because a cap equal to the strategy's own
      leg would never refuse anything, and on a day the loss stop is crossed
      the gate refuses the order that would repair a stranded leg, leaving
      that cap the only bound.
      1. **A stress move sets the cap.** One leg's stress move is derived from
         a recorded data vintage by a calculation anyone can rerun, over at
         least a weekend's hold. `max_net` is sized so one leg's loss under
         that move fits the share of the loss limit the owner sets. The hard
         caps blob stores the stress move beside the cap, with the hash of
         the vintage it came from.
      2. **A tally after each close.** Monitoring's report records every
         stranded leg, its overnight profit or loss, and how many sessions it
         stayed open, so the decision to go live rests on measured numbers.
      3. **A simulator that strands legs.** The one simulator can fill one
         leg of a pair in part, or not at all, on a seeded fault, so the
         backtest replay runs the path paper trading will. A test shows the
         next decision rebuilds the hedge when nothing halts, and is refused
         on a day the loss stop is crossed.
      4. **No unwind at the next open in slice 1**, as question 8 rules,
         unless the tally shows a stranded leg still open after two
         sessions.
   5. **A flagged price halts.** The 15:40 decision reads marketlake's
      segments before sealing, where a price spike is flagged in the snapshot
      rather than halting. The risk gate values three limits at the
      snapshot's prices, so a decision whose trades or holdings carry a
      flagged price halts, naming the instrument and both prices.

An account carries one deployment in each mode, and the supervisor refuses a
plan whose account another deployment names. Execution, the risk gate, the
journal and the account's lease therefore stay one per account, and hard limit
2's single order path holds however many strategies trade. Strategies that
share an account share its deployment, and meet in portfolio construction,
which combines their forecasts into one target, the place *Inside the Black
Box* puts the combining of alpha models.

Three things wait for the first strategy that needs them. Each is written on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), so the next reader
finds it there.

1. **How several strategies combine.** Portfolio construction's rule for
   combining forecasts, including what a strategy not deciding at a given
   time contributes, waits for a second strategy. A forecast from an earlier
   decision names the wrong decision, so rule 6 halts on it until that rule
   is written.
2. **A halt that holds the account until a person clears it.** Once a day, a
   persistent cause such as a book that disagrees with the broker halts the
   next decision again, because every decision checks again. A strategy
   deciding minutes apart would meet a gate refusal again before a person
   could look. Holding the account needs an open halt recorded in the
   journal, written by the stage that finds the cause before its halt result,
   and cleared only by the clear command's own entry.
3. **Cancel and replace.** Whether the order manager cancels an order left
   resting and sends a new one, rather than netting it, is
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10)'s to decide.

Two limits come from marketlake rather than from this design.

1. **About one decision a minute at the fastest.** marketlake captures quotes
   once a minute, so a decision more often than that sees no new data. The
   owner ruled on 2026-10-30 that this is the pace the architecture is built
   for. A
   decision also waits on S3 for about a second, as the span's price says. A
   strategy that needs seconds would need data streamed to it rather than
   read from marketlake's segments, which reopens ruling 3, and that is the
   owner's call when such a strategy exists.
2. **Fresh data from 09:30 to the option close.** marketlake captures from
   09:30 to 16:15 on a full day and 13:15 on an early close. A decision
   outside those hours reads the last sealed day, which suits a strategy that
   trades on daily closes. One that needs fresh quotes then halts as stale,
   until marketlake captures those hours too.

The price of this subsection is four things.

1. Every key holds a deployment and a decision time, and every bar its end
   time and length, which a run with one daily strategy does not need.
2. The workers run all trading day rather than for a few minutes each
   evening, so the VM is up for the whole session.
3. Recover waits for data at every decision, so the first phase no longer
   runs both at once.
4. Execution reads the clock for the cutoff, a reading that a backtest never
   exercises and only a drill with a test clock does.

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
| **worker** | One component, meaning one module of the charter's module table, run as its own process, which the host keeps running. It listens on its queue, reads its input blobs, writes its output blobs, and reaches the outside world only through the adapter at its edge. [Processes and files](#processes-and-files) gives its rules |
| **blob** | One object in storage, written once and never replaced |
| **output file** | A blob holding one thing a stage produced for one decision, at a key fixed by the run, the stage, the decision and the output's name |
| **stage** | One step a component takes in a decision. Execution's are recover and send, and monitoring's are reconcile and report. Every other component has one |
| **decision result** | The blob a stage writes last for a decision, at a key fixed by the run, the stage and the decision, listing its output files by key and SHA-256 or saying the decision halted. One key, so a stage ends each decision exactly once |
| **event** | The arrival of a decision result, which a message on a queue announces |
| **listing** | A component's stages' blobs, listed by decision. Each blob's header names its run, decision, inputs by key and SHA-256, release and configuration, so the listing is the component's record |
| **plan** | The blob the supervisor writes once per run, naming the run's mode and its decisions in order |
| **queue** | The one place a worker listens for notice of something new. The message only says something is new, and the blobs stay the record |
| **supervisor** | The process that takes the deployment's lease, writes a run's plan and messages every worker, reads the workers' heartbeats, checks the kill switch on a timer, writes a halt as a worker's result when the worker fails, and stores and messages a halt of the run on such a failure. It starts, stops and watches no process, and keeps no state outside storage |
| **run** | Everything one run writes, kept under the run's own prefix in storage. The VM keeps only a working directory for the run's queues and error output, none of which is kept |
| **halt** | A decision result saying why a decision cannot go on, naming the reason or the halt it met. Every worker downstream passes it on and skips that decision. The owner or the operator clears it, and clearing never reruns the decision. |
| **span** | The decisions in a run's plan, which every worker works through: one trading day's in shadow, paper and live, and the whole history in a backtest |
| **decision** | One moment at which a deployment decides, keyed by its time in the exchange's time zone. Every stage writes one result per decision |
| **strategy** | The instruments, schedule, data needs, alpha model and configuration that differ from one way of trading to the next. Every other module is shared by all strategies |
| **schedule** | A strategy's list of decision times on each trading day. One function in `core` turns the schedules and the trading calendar into a plan's decisions |
| **deployment** | One or more strategies on one broker account in one mode, under whose name every key a run writes sits. An account carries one deployment in each mode, and the deployment's lease covers it |
| **walking skeleton** | A build in which every module exists in its simplest form and the whole loop runs end to end |
| **exploratory** | A result found by looking at the data. It kills an idea or justifies a closer look, and is never a verdict |
| **registered** | A result whose hypothesis was committed in writing before the number was seen. Only a registered result can promote a strategy |

## Configuration

The runtime's VM keeps no state, so it keeps no configuration that matters on
its own disk. A setting comes from the release the owner pins or from a blob
in the runtime's bucket. The owner ruled on 2026-10-29 that the broker key
lives in the same store marketlake uses: AWS Systems Manager Parameter Store,
as a `SecureString` parameter encrypted under KMS, in marketlake's AWS
account, with marketlake's VM role narrowed so it cannot read it. The owner
ruled later that day that the runtime's bucket and VM live in that account
too, with the key under AWS's managed `aws/ssm` key, which the owner ruled
fine, the bucket behind a policy that names who may reach it, and
marketlake's plan role
narrowed to what `tofu plan` reads, as
[architecture.md](architecture.md#where-it-runs)'s rules 3 and 5 and its
question 11 say. This doc proposes the rest: the key sits under the
runtime's own path, the roles that keep a broad parameter read refuse that
path as a backstop, and every other credential is readable
only by the user that needs it, through a credential the VM's provisioning
gives that user alone. Nothing here sits in a file tracked in this
repository.
[architecture.md](architecture.md#where-it-runs) lists each credential the
design needs and what it may do. The owner's own machine may still keep
developer settings under `~/.config/ithildin-runtime/`. The owner updated
`CLAUDE.md`'s `## Secrets and machine paths` on 2026-10-29 to match.

No setting exists yet. The first broker or data-vendor credential gets a row
here in the change that first reads it, naming where it lives and what reads
it. The table takes the columns Setting, Secret, Lives in and Read by.

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
| A queue as the carrier of events, or a database server, between workers | A queue that carries the events themselves deletes each one once it is read, so nothing is left to audit, and a server is one more process that can fail. A queue that only wakes a worker, with the blobs as the record, is what the owner ruled for. The price is that a worker reads its inputs from files rather than being handed them |
| A supervisor that starts, watches and stops its workers as their parent | The owner asked on 2026-10-30 for a supervisor that sends messages and workers that handle them. Once each component runs in a container of its own, a parent supervisor needs the Docker socket or a systemd grant to start, stop and watch them, and either is root on the host. Crash records it wrote, lock files naming process ids, a cancelling worker it started, and a launcher seam all went with it. The host keeps workers running, and execution handles the kill switch and a stranded pair leg itself |
| One container holding the supervisor and every worker as processes | A review's way out of the root problem above. It was set aside once the supervisor stopped needing to be a parent, because a container per component gives each one its own credential and keeps the path open to a machine or a function per component |
| One process launch per worker per decision in a backtest | Test 1 would make 10,360 launches, 35 to 52 minutes at pyarrow's measured import cost. A worker takes a span of decisions instead, as [A worker works through a span of decisions](#a-worker-works-through-a-span-of-decisions) says |
| One run each evening after marketlake's sweep, as the platform's only schedule | The owner ruled on 2026-10-29 that the platform trades at any time of day and as often as a strategy needs. The time of a decision belongs to the strategy's schedule, as [Strategies, schedules and decisions](#strategies-schedules-and-decisions) says |
| A process launch per decision in shadow, paper and live | A strategy deciding every few minutes would pay every worker's launch each time. One run per trading day keeps the workers up and works through the day's decisions, the way a backtest works through its span |
| A strategy's logic spread through the shared modules, or a runtime per strategy | The owner ruled on 2026-10-29 that the platform stays open to other strategies. Spreading a strategy through the shared modules makes each new one a change to them, and a runtime per strategy would put two order paths on one account. This doc proposes a strategy as rows in the one table in `runtime`, with strategies on one account sharing its one deployment and meeting in portfolio construction |
| A controller that calls each component in a fixed order | The owner ruled on 2026-10-29 that the platform is an event pipeline, so a new event in a component's input is the only trigger. A controller would also fix an order the decision results already carry. The one table in `runtime` that declares each worker's inputs is the only statement of the wiring, which the workers, the storage contract and the supervisor's deadlines all read |
| A second attempt that supersedes an earlier output for the same decision | A halt is final and clearing it never reruns the decision, so no second attempt exists to supersede the first. A stage has one result key per decision, so a second result cannot be written, and the fields and rules for superseding were cut |
| An event log per component, as a SQLite file or as numbered blobs | The owner ruled on 2026-10-29 for blobs keyed by decision, with the journal the only log. A numbered log per component needed sequence numbers, cursors, hash chains, rules for superseding, and a rebuild after a lost VM, and three review rounds each found holes in that machinery. A deterministic step needs none of it, because running it again writes the same bytes. The price is that every deterministic step's format must be canonical |
| SQLite on the VM, as the record or as an index of S3 | Once S3 became the record, a SQLite file on the VM was only a copy to keep in step with it, and the owner dropped it on 2026-10-29 |
| Litestream, streaming SQLite to S3 | It copies each write about a second after it lands, so a lost VM lost rows that no blob could rebuild, among them halts. It also needs to delete in the bucket, and two copies streaming to one path can leave the copy impossible to restore. A review found all three, and the owner made S3 the record instead |
| A JSON Lines file per log | It reads with `cat`, but a crash partway through an append tears a line, and a review found that a writer appending after a torn line hid every event after it, a journal entry among them. Every write is now a blob that S3 creates whole or not at all |
| A messaging server such as NATS JetStream, Kafka or Redpanda | Each is a server that can fail and halt every worker, and workers would pass events through it rather than through files, which the owner's ruling asks for. Kafka and Redpanda are also built for far more than about seven events a decision on one VM |
| A worker waking on file notifications through `watchdog`, or by polling its inputs | The owner ruled on 2026-10-29 for a queue per component instead, which needs no dependency and no polling. The price is that each writer sends a message to every reader of its outputs, which one table in `runtime` wires |
| One process per action, such as the order manager and the order path each in its own | The owner ruled on 2026-10-29 that this was too granular, and narrowed the ruling to one process per component. A component is the unit the charter gives an interface and an owning role |
| More than one execution process per broker account at a time | The risk gate checks limits across every order and position in the account's journal, so two gates at once could each approve orders that together breach one. A restarted supervisor beside a still-running order path sent every order twice in a review's simulation, which is why rule 9 takes a lease |
| Durable state on the VM's disk | The owner ruled on 2026-10-29 that the VM keeps no state that matters, with every file a blob in storage. A replaced or lost VM then costs nothing but a lease to take. The price is a round trip to S3 per blob, and a memory-backed folder for backtests that must behave like S3 |
| S3 for backtests | Test 1's tens of thousands of writes and reads would each wait 10 to 30 ms. The owner ruled on 2026-10-29 for an in-memory store, which this doc takes as a memory-backed folder |
| One result per component per decision | Execution and monitoring each act at two points in a decision, and a single result written last put execution's account file behind its own send, a cycle a review's simulation showed. Each now has two stages with a result each |
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
