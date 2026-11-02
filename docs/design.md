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

Three more adapters differ by mode but change only where records and
signals go, never what a strategy sees.

1. **The blob store**, which holds every output and the journal: S3 in
   shadow, paper and live, and a memory-backed folder in a backtest.
2. **The queue**, which wakes a worker when something new exists for it:
   SQS in shadow, paper and live, and a named pipe in a backtest and in CI.
3. **The alert channel**, which sends in shadow, paper and live and only
   records in a backtest, so a backtest of 1,480 decisions never pages the
   owner.

The price is that every module has to be written against an event from its
first commit, which is slower than a vectorised backtest over a price array.
What it buys is that a strategy's backtest and its paper run exercise the same
lines of code. The next section says what that event is: a decision result,
one blob in storage that lists a component's outputs for a decision. Only the data worker, the supervisor, and execution's send stage for its cutoff read the clock in a way that shapes what a strategy sees.

## Processes and files

The owner ruled on 2026-10-29 that each component runs as its own process, in
parallel wherever the data allows, and that processes pass everything to each
other through the filesystem. A component is one module of the charter's
module table, such as data, the alpha model or execution. The ruling first
said each action, and the owner narrowed it to each component the same day,
because a process per action was too granular. Two things follow from it.

1. **A crash stays where it happened.** In one process, an exception or a crash in the alpha model ends the run that also holds the order path. As separate processes, a crashed component stops producing its own output and nothing else until the host restarts it. Every output upstream of it is already stored, so recovering means that one component starting again, reading the blobs that exist and writing the ones that are missing, rather than replaying the day. A decision
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
   halt. An **event** is the store's announcement that a blob was created or deleted, which reaches the queue of every component that reads it.
4. A **worker** is one component run as its own process. It listens on its
   queue, reads its input blobs, writes its output blobs, and reaches the
   outside world only through the adapter at its edge, such as the broker or
   the data source. A worker imports `core`, the shared types, and no sibling
   module, and shares no memory with any other worker.
5. The **supervisor** is the process that writes the run's **plan**, a blob
   listing the run's mode and its decisions in order, whose event reaches the workers, watches their results and heartbeats, and halts the run. It starts and
   stops no process.
6. A **run** keeps all of its blobs under the run's own prefix in storage. The
   VM keeps only a working directory for the run, holding its workers' error output and heartbeat files, and none of that is kept. In shadow,
   paper and live a run is one trading day of one deployment, and in a
   backtest it is the whole span.
7. A component's **listing** is its stages' blobs, listed by decision. Each
   blob's header names the run, the decision, and every input blob it was made
   from, by key and SHA-256, along with the release commit and a hash of the
   configuration, so the listing is the component's record and each entry in
   it leads back to its inputs.
8. A **decision** is one moment at which a deployment decides, such as 15:40
   on one trading day for a daily strategy, or every five minutes through the
   day for a faster one. Its key is the decision time in UTC, written so keys sort in time order. Every stage writes one result
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
   per call, written before the call returns, which the simulated broker's adapter reads and writes through
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
   just before the broker call, on the kill switch, the run's halt, its own send-stage result key, the account's lease's tag and the cutoff, as rule 9's fourth item says.
4. **Every lease** is fetched before it is taken, and one another VM holds
   is taken over only once it has expired unrenewed, as rule 9 says.
5. **Every read of marketlake** fetches the manifest entry before the data it
   names, and a checksum that differs halts, as rule 8 of architecture.md's `## Where it runs` says.

Ten rules make the blobs something a worker can trust.

1. **Every write is a blob, created once.** A worker writes each blob with a
   single upload carrying a condition that makes S3 refuse it if the key
   exists, and the blob's SHA-256, which S3 checks before it accepts the blob.
   So a reader never sees half a blob, and no blob is ever replaced. S3
   returns the stored blob's tag and records when it stored it, and the
   storage port hands both back. The bucket's policy refuses every
   principal, the owner's and the operator's included, any write on the
   runs', the journal's and the simulated accounts' prefixes that lacks the
   condition, and any delete there. It refuses the runtime's principals the
   same on the leases' and the switch's prefixes, which keep the delete
   rules of rules 9 and 10, except that a lease may also be rewritten with
   `If-Match` on its current tag, which is how it is renewed and taken over. The bucket keeps every version, so the rule
   holds even against a bug in the runtime's own code or a person's
   mistake. A plain delete leaves a delete marker, and a create-only write
   can then land on the same key, so an S3 delete event under the journal's
   prefix alarms, and replay lists versions and halts on a journal key with
   a delete marker or a second version. The owner ruled on 2026-11-01 for
   S3 Object Lock now. Each environment's bucket is created with Object
   Lock enabled, and every object under the journal's and the runs'
   prefixes is written with a retention date of its own, in compliance mode
   on live, which nobody can shorten or delete through, the account's root
   user included, and in governance mode on pre-live, which the owner alone
   may bypass. Heartbeats and leases carry no lock, so their lifecycle and
   deletes still work. The bucket carries OpenTofu's `prevent_destroy`, and
   a test pins its lifecycle rules to the heartbeats' prefix alone. Before
   live, the live journal is also replicated to a bucket in a second AWS
   account with Object Lock of its own, as the owner ruled the same day,
   because Object Lock cannot outlive the account that holds it. The
   retention period is proposed for [#11](https://github.com/l3a0/ithildin-runtime/issues/11) and the replica for the
   live gate. A deterministic component's header
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
   write S3 refuses because the supervisor has already written a halt there, as rule 7 says, takes the halt as final. Every instruction the supervisor gives is a blob, and writing it is what announces it, through the store's own event: the plan, a halt of the whole run, and the stop at the end of the run. A worker acts on an event only after reading the blob it names, and checks those blobs whenever it wakes, so an event that is late or repeated loses nothing.
4. **A worker listens on its queue, then reads its inputs.** Each component
   has one queue, and what arrives on it is the store's own event for a blob
   the component reads, as `### How a component learns of a change` says. In
   shadow, paper and live each queue is SQS, fed by EventBridge rules on the
   environment's bucket. In a backtest and in CI each is a named pipe, fed
   by the memory-backed folder adapter with the same events. One table in
   `runtime` is the only place a worker's inputs are declared. Each worker's
   entry point hands it its inputs from that table, and the rules and pipes that feed each queue are generated from routes one function in `runtime` builds from the same table, so no writer
   sends anything beyond its write. An event names the blob its reader
   should read: a decision result, the plan, the run's halt or stop, or the
   kill switch. A starting worker drains its queue and then checks its keys
   once, so nothing written while it was down goes unread. A woken worker
   checks every decision it has not finished rather than only the one an
   event names, so a repeated, late or out-of-order event costs nothing.
5. **Each blob names what made it.** Its header names its run, its decision,
   its kind, output or halt, its format version, every input blob by key and
   SHA-256, the release commit and a hash of the configuration. A reader
   checks each input's SHA-256 against the header that named it and halts on
   a mismatch. Nothing irreversible rests on an upstream blob alone: execution
   journals the whole target portfolio it acts on, in a journal entry, before
   its first send. Each release writes only formats the release before it can
   read, so pinning back to the release before still reads what storage
   holds. A rollback can skip a release shadow never ran, so `ithildin pin set` and the VM's pin check both refuse a digest that cannot read the newest format already stored for some kind in that environment, which every header names. A worker killed by a signal or a crash writes nothing about itself
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
   halt as its result for the decision, with the reason, and then moves to its next decision, because a halt is not a crash. Hard limit 3 came from an
   execution server that read "no signals" as "hold nothing" (L4690). Here,
   "has this decision's forecast been written" is a check of one result key.
7. **A halt flows down the pipeline, and the supervisor only sends messages,** which this doc reads as the events of the blobs it writes. The host keeps every worker running and restarts one that
   exits: on the VM, a systemd unit per worker's container, as rule 10 of
   architecture.md's `## Where it runs` says. The supervisor starts, stops
   and watches no process. Its instances share the deployment's lease, as rule 9 says,
   and the first whose write of the run's plan lands wins, so another instance, or a restarted one, finds the plan already there and uses it, and whose event reaches every worker's queue. A worker that starts before
   any plan exists, or finds a plan whose run the deployment's lease does
   not name, waits, so no worker acts before a supervisor holds the lease. On a VM the host starts the workers only once that VM's supervisor instances hold the deployment's lease, through a systemd path unit that the supervisor's local lease file starts, so a second VM's workers never receive from a shared queue.
   Every worker, in every mode, finishes its decision loop once it has a
   result for every decision in the plan. A supervisor instance then stores the run's stop, whose event reaches each worker and every other supervisor instance, which exit cleanly, and the instance whose write of the stop landed deletes the
   deployment's lease. A timer on the host stops every unit after the close
   as well, in case a stop is lost.
   - **A halt is passed on.** A worker whose input's result for a decision is a
     halt writes its own halt as its result, naming the one it found, and goes
     on to its next decision.
   - **A crash is a restart, and a failure stops the run.** One crash is not
     a failure: the host restarts the worker within seconds and it resumes
     from storage, which rules 3 and 9 make safe to repeat. A worker has
     failed when it misses its deadline, when its heartbeat, judged by the time in its key, stops for longer than the limit set on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) above the delivery delay measured on paper, or when it restarts more often in one run than the
     host's limit allows. Its deadline runs from the moment its last needed
     input appears, so waiting on a slow upstream worker never counts against
     it. A worker that hangs answers no message, so a watchdog on the host kills its container when its local heartbeat file is thirty seconds stale, as `### How a component learns of a change` says, and the host restarts it. On a failure the supervisor writes the halt as that worker's
     result, naming its restart records and every output file it finds
     under the decision's prefix, so no stored capture is left unlisted. It
     stores a halt of the whole run, writes a halt as every stage's result
     for every decision still left in the plan, whose event reaches every worker. Each halts itself, monitoring journals the failure,
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
     and neither repeats nor drops an alarm. The dead-man check gets success
     pings only. healthchecks.io notifies only when a check changes state, so
     a failure ping to a check already down sends nothing, and a check left
     down by one halted day would never alarm on a dead VM the next. A halt's
     alarm therefore goes straight to the alert channel's push topic with its
     reason, which this doc proposes for
     [#11](https://github.com/l3a0/ithildin-runtime/issues/11), and drills
     cover a halted day followed by a dead VM, two reasons in one run, and a
     fault that persists into a second day, each expecting its push. Alarms
     raised before monitoring runs, such as a lease taken over from another VM, a pin
     the VM does not trust, a drill not clean, or the plan's own event not
     received, go out on the host's direct route, the one the `OnFailure`
     handler uses, with the reason as the body.
   - **A halt is final for its decision.** This finality is this doc's choice
     rather than part of the ruling. The owner ruled on 2026-10-29 that the
     owner or the operator clears a halt, with a command that journals who
     cleared it, why, and any correction a person made, such as a journaled
     override that explains a mismatch. Clearing never reruns the halted
     decision. The owner narrowed it on 2026-10-30: on a live account only
     the owner clears a halt, a switch or a lease, or writes an override,
     and the operator acts on paper and shadow accounts, plus pulling a live
     account's switch. A halt is open until a later decision of the same
     deployment succeeds or a person clears it. A halt for a book that
     disagrees with the broker, or an order the platform did not send, stays
     open until a person clears it. On a live account every halt holds the
     account until the owner clears it, as the owner ruled on 2026-11-01:
     each later decision halts while the journal holds a live halt that no
     `ithildin clear` entry names, so no cause that clears itself resumes
     live trading without the owner. Shadow and paper keep the rule above. `ithildin
     explain` and the alarm's second line say who may clear it: nobody
     needed, the operator or the owner, or the owner alone.

   Everything the supervisor knows, it reads from storage, so a supervisor
   that crashes and is started again reads the plan, the results and the
   heartbeats, and carries on, while the workers keep running without it.
   Each supervisor instance writes a heartbeat of its own, and monitoring alarms when the newest of them is stale, meaning no supervisor is running, and a handler on the host alarms when any unit, or the Docker daemon itself, reaches its restart limit or fails, so a supervisor
   that is gone does not leave the run unwatched in silence.
   Workers with no input between them
   run at the same time. Alpha and cost need only the snapshot, and risk needs
   the snapshot and the reconciled book.
8. **The journal is the one numbered log.** Its entries sit at keys fixed by
   the account and a sequence number, written with leading zeros so they sort
   in order. Each entry is one canonical blob holding its number, its format, the time it was built, its body and the hash of the entry before it, and its own hash is the SHA-256 of those bytes as stored, which the writer sends with the upload so S3 checks it, as architecture.md's `### File formats and keys` says. To append, a writer takes the last
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
   stored before the step it records happens. A reader checks that each entry's `prev_hash` equals the SHA-256 of the stored bytes of the entry before it, and halts on a link that does not chain, bytes that are not canonical, or a gap in the numbers. A hash recognises only a resend of the same bytes, so a writer that restarted checks the field naming its step, such as the client order id, before it appends, as that subsection lists.
9. **Leases make one VM, and one sender, per account.** A lease is a blob a
   process creates with the condition that it not exist, recording its
   holder: the VM, its boot, the container and its restart count, the release, the run, when it was created and when it expires. Its holder renews it every ten seconds by rewriting it with S3's `If-Match` condition on the tag it last got, which S3 refuses with a 412 if anyone else has written it since, and each renewal moves the expiry a lease period ahead. It is deleted only with a condition naming the tag S3 last returned to its holder, so no stale holder deletes a successor's lease. Two
   leases matter.
   1. **The deployment's lease.** The owner ruled on 2026-11-01 that the
      supervisor runs several instances that share this lease. Its holder is
      the VM and its boot, and it records which instance renewed it last.
      The first supervisor instance to start on a VM creates it before any
      instance writes the plan, in every mode, and the others find it held
      by their own VM and boot and join it. Any instance on that VM renews
      it when a renewal is due. A 412 on a renewal whose stored holder is
      still the instance's own VM and boot means a sibling renewed first, so
      the instance takes the new tag and carries on. A 412 naming another VM
      is a takeover, and stops the VM's units. So the lease outlives any one
      supervisor instance, and a replacement VM started while the old one
      still runs waits, and its workers, which find no plan under a lease of
      theirs, wait with it.
   2. **The account's lease.** Every execution instance receives from
      execution's queue, and the owner ruled on 2026-11-01 that an instance
      holds the account's lease only while it changes the account. It takes
      the lease before it re-reads the journal for a submit or a cancel, and
      deletes it once the journal holds the broker's answer or the order's
      doubt. A handler that needs the lease while another handler, in its own
      instance or another, holds it waits, and alarms if it cannot take it
      by a deadline. A plan of several orders
      therefore takes the lease once per order, so a pull's cancels, run by
      whichever instance received the switch's event, wait for at most one
      order in flight.

   A holder record names the VM, its boot, the worker's container, its
   restart count and the handler, rather than a process id, which means nothing outside one
   container. A lease whose holder is the same VM on an earlier boot, or a
   container the host shows is gone, is cleared by the next process that
   needs it, and the clearing is journaled. The owner ruled on 2026-11-01
   for automatic failover across machines, so a lease another VM holds is
   taken over once it has gone unrenewed past its expiry and a margin for
   clock skew, by a conditional write naming the expired lease's tag, which
   a revived holder's renewal can then never overwrite. The takeover is
   journaled and alarms. A frozen holder that wakes finds its renewal
   refused, and stops: its supervisor stops its VM's units, and execution's
   check of the lease's tag right before every broker call, rule 9's fourth
   item, refuses its next send. The price is one window. A holder that
   froze after that check and before its request left can still send that
   one request after a takeover. The new holder settles it the way it
   settles any order in doubt, by looking it up, and the gate already
   counts every open order at its worst case. The lease period, thirty
   seconds as this doc proposes for [#11](https://github.com/l3a0/ithildin-runtime/issues/11), is longer than a renewal's
   worst delay and shorter than the time a decision can wait. The order manager, the
   risk gate, the journal and the order path all run inside the execution
   worker, because the gate checks each order against every order and
   position in the account's journal just before the order is sent, and a
   check passed through a blob could be stale by then. This is the one place
   a risk engineer's piece runs in another component's process. Running a
   worker again is safe everywhere except here, because a sent order cannot be
   unsent. So execution sends an order only when the journal holds no
   `sending` entry for its client order id, and asks the broker about any
   order in doubt by that id, never by sending again. A request sent just
   before a crash can still reach the broker after the first lookup, so a
   lookup that finds nothing leaves the order `InDoubt`, counted by the gate
   at its worst case, until its time in force has run out. Only then is it
   journaled `Lost`. The broker client's total request timeout is shorter
   than the wait before the first lookup. Five more rules keep a
   rerun from doubling anything.
   1. Execution journals each fill under the broker's own fill id, and skips
      a fill whose id is already journaled.
   2. At the start of its send stage, before it plans, execution fetches the
      broker's positions, open orders and fills again, a fresh read on every
      attempt rather than a capture that the first one wins. It settles each
      order in doubt by its client order id, journals any fill that arrived
      since its recover stage, and compares the result with the book it
      replays from the journal. An open order at the broker that matches a
      `sending` entry, and a fill within that order's quantity, are the platform's own and bring the book up to date.  So is an order the journal
      holds as open that the broker no longer lists, once a lookup by its
      client order id finds it `Expired`, `Rejected` or cancelled at the close
      as its time in force allows, or cancelled after a journaled cancel of
      the platform's own, such as the kill switch's, the one that keeps a pair whole, or a later plan's. Execution journals each such outcome, in its recover stage and in this sync. An order or fill the journal did not send, including a fill on an order the journal holds as `Lost` or `Abandoned`, is journaled as received and halts, as `### Orders the platform did not send` says. Any other difference
      halts the decision for a person, because the broker moved in a way the
      platform did not ask for. Only then does it plan. It journals the planned orders, cancels and replacements, each with its client order id, in the same entry as the
      whole target portfolio, along with the journal entry it synced through
      and a hash and the time of the broker state it fetched, before its first
      send. A rerun in the same decision never plans again, so an order's
      id never shifts between attempts, and sends nothing more, as item 5
      says. The id is the platform's stamp, built from the strategy, the decision time and the order's place in that list, as `### Orders the platform did not send` says, so it is unique across the decisions of a day and the same on every rerun.
   3. Its account file names the journal entry it replayed through, and the
      send stage's sync names the entry it synced through. Before it plans,
      execution halts if an entry that changes the book, and that neither its
      sync nor its own order loop wrote, lies past the entry its sync names.
      A person's override that lands after the sync halts the decision the
      same way before its next send, and applies from the next decision's
      sync.
   4. **Right before every submit**, after the order's `sending` entry
      is stored, execution checks five things.
      1. The kill switch is absent.
      2. No halt of the run is stored.
      3. Its own send-stage result key is still empty.
      4. The account's lease still carries the tag execution got when it
         took it for this order.
      5. The market-on-close cutoff has not passed, for such an order.

      Any one failing journals the
      order `Abandoned`, sends nothing, and halts the decision. So a run the
      supervisor has halted, a switch pulled mid-loop, or a lease another
      execution has taken over stops the next send, even when the message
      saying so is lost or unread. A cancel has checks of its own, because
      the cancels that protect the account run exactly when a switch is set
      or a halt is stored. The kill switch's cancels, and the cancel of a
      stranded leg by an instance taking over a send stage, check only the
      lease's tag. The
      cancel that keeps a pair whole checks the lease's tag and the cancel
      cutoff. A plan's own cancel or replacement checks everything this item
      lists. A cancel skips the risk gate's limits on purpose: every open
      order is already counted at its worst case, so a cancel can only lower
      each counted limit.
   5. **No instance sends more for a send stage whose runner died.** The
      entry that journals a send stage's planned orders names the handler
      running it: its VM, boot, container, restart count and handler. Another
      instance, or the same one restarted, takes that send stage over only
      once its runner is gone, meaning the host shows the runner's container
      gone or restarted, or the runner's VM no longer holds the deployment's
      lease. Each worker instance runs under one fixed container name, its
      component and its number, removed when it stops, so a gone container is a fact the host reports, not a guess
      from a stopped heartbeat. A send stage whose runner is alive is left to
      it, which also covers SQS handing a second instance a copy of the
      runner's message. An instance that starts, and one that receives a
      send stage's event, acts in this order:
      1. Check the kill switch, and if it is set take the account's lease and cancel every resting order in the account, stamped or not, journaling each cancel first.
      2. For a send stage that has journaled planned orders and no result,
         and whose runner is gone, take the account's lease, settle each order in doubt by its client order id, journal every planned order and replacement without a `sending` entry as `Abandoned`, settle every planned cancel left `CancelSent` by a lookup,
         cancel any accepted leg of a pair whose partner is not accepted,
         before the cancel cutoff, and write a halt as that send stage's
         result. It does this whether or not the supervisor has already
         written a halt there.
      3. Write its restart record, if it restarted, and carry on with the
         next decision, which plans again from the book.

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
    creates the blob, journals the halt, raises the alarm, and cancels every resting order in the account, stamped or not, journaling each cancel first, as the owner ruled again on 2026-10-31. This doc adds how. The switch can be pulled from anywhere, and creating its blob is an event that reaches execution's, the supervisor's and monitoring's queues, whoever pulled it. Whichever execution instance receives that event takes the account's lease, which waits for at most one order in flight, and cancels every resting order in the account, stamped or not, through the order path, and alarms if a cancel fails. Every other instance stops sending at its next check of the switch. `ithildin switch pull` then reads the journal once at a deadline before the cancel cutoff. The blob's event reaches execution directly, so a pull reaches resting orders even with the supervisor down. No worker reads the switch on a clock, as the owner ruled on 2026-10-31. A
    starting execution instance checks the switch before anything else, and
    takes the account's lease to cancel if it is set, as rule 9's fifth item
    says, so a switch pulled
    while execution is down still cancels, and it alarms if it cannot take
    the lease by a deadline. Execution checks the switch again right before
    every broker call, as rule 9's fourth item says. It cancels the union of the orders the journal holds open and the orders the broker lists, so an empty or failed broker answer never reads as nothing to cancel. It journals that the switch's cancels are done only after a fresh read lists none of them and a lookup has settled each journal order the broker no longer lists, and
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
    journal both as a human override with `ithildin` once back at a computer.

### Orders the platform did not send

The owner ruled on 2026-10-31 that the platform stamps every order it sends,
and later that day that at live it trades an account and capital of its own.
In review comments on [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15)
on 2026-11-01 the owner went further, in three points.

1. One account holds one portfolio, which several strategies may share, and
   each portfolio has an account of its own.
2. Nobody trades by hand in a platform account.
3. The live broker is Alpaca too, since the platform's account is separate
   anyway, rather than Schwab as the owner ruled on 2026-10-31.

So nothing but the platform should ever place an order in its account, and
the outside book that the 2026-10-31 rulings built for hand orders in a
shared account is gone. Two of that day's rulings still hold.

1. No hand trade, in any account the owner holds, in an instrument a
   strategy trades, anything substantially identical to it, or an option on
   it. The wash sale rule of
   [IRC section 1091](https://www.law.cornell.edu/uscode/text/26/1091)
   follows the taxpayer rather than the account, and Rev. Rul. 2008-5
   extends it to the taxpayer's IRA.
2. The platform's account chooses which tax lot each sale closes, called
   specific lot identification, rather than closing the oldest lot first.
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) confirms that
   Alpaca's live account offers it.

The rest of this subsection is this doc's proposal for
[#10](https://github.com/l3a0/ithildin-runtime/issues/10).

**The stamp is the client order id.** Every order the platform sends
carries a client order id of the form `ith-<strategy>-<decision>-<n>`: a
prefix only the platform uses, the strategy's alias, the decision's UTC
time in the compact form architecture.md's `### File formats and keys`
gives, and the order's place in the decision's planned list, padded to two
digits, such as `ith-gldusobb-20261102T204000Z-03`. A strategy's alias is a
sixth row of its declaration in the table in `runtime`, up to twelve lower
case letters and digits. Alpaca's
[orders guide](https://docs.alpaca.markets/docs/working-with-orders)
recommends a distinct client order id per strategy when several run on one
account. The id is about thirty characters, under a length limit
[#10](https://github.com/l3a0/ithildin-runtime/issues/10) confirms on
Alpaca's paper account. It is unique to its decision and the same on every
rerun, as rule 9 needs. The order path and the broker adapter refuse to
send an order whose id is not a well-formed stamp with a journaled approval
and `sending` entry, so no order leaves unstamped. The journal's intent
entry names the strategy too, and `Journal.replay` gives each fill to the
strategy its order's intent names, so each strategy has a book of its own.
One order serves one strategy, so until how strategies combine is settled,
as `### Strategies, schedules and decisions` defers, portfolio construction
builds one target per strategy and never nets two strategies' wants into
one order.

**The journal maps the broker's ids to the platform's.** The owner asked on
2026-11-01 that the platform map the broker's order id to its own client
order id and track each order's details itself. So execution journals the
broker's order id with each `Ack`, beside the client order id, the strategy,
the decision and the order's place in the planned list. Every later call on
that order, a lookup or a cancel, uses the broker's id the journal holds.
Alpaca cancels only by its own order id, through `DELETE /v2/orders/{id}`,
so this is also what its adapter needs. The client order id serves one
purpose only: settling an order in doubt, which has no journaled ack yet,
through Alpaca's `GET /v2/orders:by_client_order_id`. A broker that took no
client order id would lose only that lookup, which its adapter would answer
another way, such as by listing the account's orders since the send.

**Sorting what the broker reports.** Execution sorts each order and fill by
the broker's own order id.

1. A fill whose broker order id the journal mapped from a platform `Ack`, or
   whose order a lookup by client order id finds carrying a stamp, is the
   platform's, and journals into its strategy's book within that order's
   quantity.
2. Anything else halts: an order or fill with no stamp, a stamped one the
   journal never sent, a fill past its order's quantity or on an order the
   journal holds as `Lost` or `Abandoned`, and an order the broker placed
   itself, such as a liquidation or an assignment, which the owner ruled on
   2026-10-31 must never be quiet. Execution first journals what it
   received, as an `unmatched_order` or `unmatched_fill` entry keyed by the
   broker's order or fill id, which replay leaves out of every book until a
   person's `ithildin override` brings it in. So the record of a real
   account event survives even when the platform did not cause it.
3. That halt holds the account. Every later decision halts while the
   journal holds an unmatched entry that no `ithildin clear` entry names.
   Once such an order has filled, nothing else would make the next decision
   halt, and it would trade on top of a position nobody explained. This is
   the one cause for which slice 1 builds the holding halt that
   `### Strategies, schedules and decisions` otherwise defers, under
   CLAUDE.md's third exception, a guard whose price is paid by building it
   late.

**What reconciling and the gate count.** Monitoring checks each stamped
order's filled quantity at the broker against the journal's, and the
broker's position in each instrument against the sum of the strategies'
books. The risk gate checks every cap against the account, and the daily
loss stop counts the whole account. The kill switch cancels every resting
order in the account, a platform order by the broker's id its `Ack`
journaled and any other by the broker's order id the listing gives, each
through the order path and journaled first, as the owner ruled on
2026-10-29 on question 7 and again on 2026-10-31.

The price is three things.

1. An order the platform did not send, a broker's liquidation included,
   halts trading until a person clears it, even when it turns out harmless.
2. Each `Ack` entry carries the broker's order id and the order's details,
   one more mapping replay has to keep right.
3. The stamp's length limit, whether Alpaca refuses a repeated client order
   id, which its order reference does not say, and specific lot
   identification need confirming on Alpaca's live account before the live
   gate, on [#10](https://github.com/l3a0/ithildin-runtime/issues/10).
   Nothing in the design rests on that refusal. It is a second line of
   defence to test, because the platform never resends and settles doubt by
   lookup.

### How a component learns of a change

The owner ruled on 2026-10-31 that a component learns of a change the way a
process watches a folder, by an event when a file changes, with S3's
notifications as that event and no polling. Before the ruling, this doc
layered three routes: a message the writer sent, a timed check of the next
key, and S3's notifications once a component sat off the VM. A writer's
message reached only the readers it knew on its own host, and a timed check
cost a read every few seconds for each reader and still noticed late. The
store already sees every write, whoever made it, so the store announces it.
The rest of this subsection is this doc's proposal for
[#22](https://github.com/l3a0/ithildin-runtime/issues/22) and
[#11](https://github.com/l3a0/ithildin-runtime/issues/11).

**One event, from the store.** For every blob created, S3 sends an event
naming the bucket, the key, its size, its ETag, its version and a sequencer,
the same whether a worker on the VM or a person running `ithildin` from their
own machine wrote it. A deleted key, such as a cleared switch or a lease
released, sends a removed event the same way. Each environment's bucket
sends its events to EventBridge, as AWS's
[S3 and EventBridge page](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventBridge.html)
describes. One function in `runtime` turns the table that declares every
worker's inputs, and the control blobs every component hears, into routes:
a reader, a prefix and an event kind. The routes generate one EventBridge
rule each, which copies each matching event to that reader's SQS queue, and
the same routes drive the memory-backed folder adapter. Each rule's pattern
matches `aws.s3` as the source, the event's kind, the environment's bucket
and the prefix, because EventBridge refuses a custom event whose source
starts with `aws.`, so nobody can put a forged S3 event on the bus that a
rule would pass on.

**Waiting is long polling the queue, never storage.** SQS cannot push to a
process, so a worker waits in a receive call that SQS answers as soon as a
message arrives, holding the call open for up to twenty seconds and then
opening the next. AWS calls this
[long polling](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html).
It is a repeated request to the queue, about three a minute while nothing
arrives, and it never reads storage. A waiting request returns the moment a
message arrives, so the twenty seconds only bound how long a quiet request
stays open, and the worker opens the next one at once. A message that
arrives in the gap between two requests waits in the queue for the next,
so nothing is lost and nothing is late by more than that gap.

SQS caps a wait at twenty seconds, and the cap does work for the platform.
A connection that dies without a clean close, through a network blip or a
stalled firewall, looks exactly like a quiet queue, so a bounded wait, with the client's read timeout set a little over twenty seconds rather than botocore's default of sixty, is how a worker learns it has gone deaf rather than hanging on a dead connection. Network devices between the VM and SQS, such
as a NAT gateway, also drop connections idle for minutes, which a short
renewal stays under. Each renewal is a billable request. Workers run only
during the session, about seven hours a day, so each makes about 1,260
requests a trading day and about 26,000 a month. Three instances of each
of seven components and of the supervisor make 24 containers in each of
shadow and paper, about 1.2 million requests a month, and live's 24 bring
the account to about 1.9 million. SQS's first million requests a month are
free, and each further million costs 0.40 US dollars, as its
[pricing page](https://aws.amazon.com/sqs/pricing/) says, so the waiting
costs about 35 cents a month once live runs.

**A queue that pushes buys no speed.** Five AWS services do push to a process, weighed here in four groups.

1. **SNS to an HTTPS endpoint,** and EventBridge's API destinations, both
   call a web address the worker serves, which opens the VM to inbound
   connections it accepts none of today.
2. **Amazon MQ, RabbitMQ or ActiveMQ,** sends each message down a
   connection the worker holds open, with no inbound port, but it is a broker that runs and bills all day, and a single-instance broker is one more machine to keep up.
3. **Kinesis Data Streams with enhanced fan-out** pushes records over a
   held connection renewed every five minutes, at a charge per shard and
   per consumer each hour, for about nine events a decision.
4. **Lambda** runs the code on the event, so nothing waits at all. It fits
   a short step and not execution, which stays alive through a session to
   watch a pair's legs and the switch, and it is the serverless route rule
   10 of architecture.md's `## Where it runs` keeps open.

Every push to a worker that accepts no inbound connection rests on the worker holding one open, so a broker or a stream renews or pings underneath as SQS's long polling does in plain sight. What decides between them is the time from a write to
a worker waking, and almost all of it is S3 handing the event to
EventBridge, which comes before any of these. So SQS stays. Measured delay
on paper is what would reopen it: if SQS's renewal ever added time that
mattered, Amazon MQ and Kinesis are the pushes that keep the VM closed to inbound connections.

**One receive loop hands work to a pool of handlers.** The owner ruled on
2026-11-01 that each component instance processes its queue through an
event loop. One receive loop in each worker reads from the queue and hands
each item to a pool of handlers, and each handler works on its item
independently of the others. The list below is this
doc's proposal for [#22](https://github.com/l3a0/ithildin-runtime/issues/22).

1. **The receive loop is one implementation.** It lives in `runtime`, and
   every worker and the supervisor run it in every mode, over the queue
   port, so a backtest's named pipe goes through the same loop as SQS. It is
   a Python `asyncio` event loop, and each handler is a thread from a pool
   the loop owns, because boto3 and Alpaca's SDK block on the network. The
   handlers share one boto3 client, which boto3 documents as safe to share
   across threads. Each handler builds its own broker client, because the
   SDK's HTTP session is not documented as safe to share.
2. **An item is a stage and a decision, not a message.** The loop turns
   each message, once it passes the checks below, into the stage and
   decision it concerns. On waking and on starting it also adds every
   decision it has not finished, in the plan's order. One instance never
   runs an item twice at once, so the loop deletes a message whose item is
   already in a handler.
3. **The loop receives only for free handlers.** It asks SQS for at most as
   many messages as it has free handlers, up to SQS's ten, so no message
   waits hidden inside a busy instance while another instance could take
   it. While a handler works, the loop extends its message's visibility
   timeout, and it deletes the message once the handler's result is stored.
   A message whose instance dies returns to the queue for another.
4. **Stages that read the journal keep the plan's order.** Execution's two
   stages and monitoring's reconcile read a book the decision before
   changed, so a handler starts decision N of one of them only once the same
   stage's result for decision N−1 is stored, a halt included. Every other
   stage reads only blobs keyed by its own decision, so its handlers may run
   decisions side by side.
5. **A handler's failure stays in its item.** A handler that raises an error
   it does not handle writes a halt as its stage's result for that
   decision and alarms, and the loop deletes its message, so the other
   handlers go on. If the halt cannot be written, the process exits and the
   host restarts it, as rule 5 says.
6. **A hung handler stops the heartbeat.** Each handler reports progress at
   every blob write, journal append and broker call it finishes. The loop
   touches the worker's heartbeat file only while every busy handler has
   reported within the heartbeat limit, so a hung handler lets the host's
   watchdog kill the container, and rule 9's takeover applies. The loop
   renews a lease only for a handler that is still reporting, so a hung
   handler never keeps the account's lease alive. Every broker and S3 call
   therefore carries a timeout shorter than that limit.
7. **Stopping drains the pool.** When the host stops the container, the loop
   stops receiving and lets each handler finish within the unit's stop
   timeout. A message whose handler did not finish returns to the queue,
   and an execution send stage left unfinished is taken over as rule 9's
   fifth item says.
8. **The pool's size is configuration.** The deployment's config sets it
   for each component, four by this doc's proposal, and one for the
   supervisor. In execution, a second handler lets a pull's cancels run
   while another handler is in the middle of a plan. Two handlers of one
   instance meet at the account's lease the way two instances do, because
   the lease's holder record names the handler.

The price is a thread pool in every worker, a progress report at every
step, and an order rule for the stages that read the journal. A test runs
test 1's backtest with every pool at size one and again at size four, and
expects the same journal and the same blobs byte for byte, so concurrency
that changes an output fails CI.

No worker on a VM receives until that
VM's supervisor instances hold the deployment's lease, so a second VM's workers never
take the lease holder's events off a shared queue. Execution receives
without the account's lease, which it takes only to change the account, as
rule 9 says.

**An event is a hint, and the keys come from the plan.** AWS's
[event notifications page](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html)
says events "are designed to be delivered at least once" and are
"typically" delivered "in seconds but can sometimes take a minute or
longer", and its
[message structure page](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-content-structure.html)
says they are not guaranteed to arrive in order. So an event can repeat,
arrive late, arrive out of order, or, in a backtest or CI or while its
reader is down, not arrive at all. A worker treats an event only as a reason
to wake. It drops one whose bucket is not its environment's or whose key
lies outside a prefix its routes give it, and it reads only keys it builds
from the plan and the table, never a key taken from the event. On waking,
and once when it starts, it checks every decision it has not finished, in
the plan's order, so a repeated, late, reordered or lost event costs nothing
but time. It reads anything that touches money again right before it acts.

**An absence is a deadline, not a poll.** No event announces that something
did not happen: a worker that hung, a result never written, a heartbeat that
stopped, or an event that never came. Each is caught by a deadline that an
earlier event set, kept as a timer in the memory of the process that set it,
which reads no storage while it waits. A process that restarts sets its
deadlines again from storage, once, as it starts.

1. **The supervisor's deadlines.** When the supervisor hears the event for a
   stage's last needed input, it starts that stage's deadline, as rule 7
   says, in every mode. The event for that stage's result clears the
   deadline the moment it arrives, so a stage that finishes early is never
   waited on. Only a deadline whose result event never came expires. Then
   the supervisor reads that result key once, because the event could have
   been lost, and a result still missing fails the worker, with the halt naming whether the
   worker's input event was ever received, so a lost event reads differently
   from a hung worker.
2. **Monitoring's deadlines.** Monitoring starts a deadline on each of the
   supervisor's heartbeat events and on each kill switch event, for the
   switch's cancels, and alarms when one expires first.
3. **The host's watchdog.** Each worker also touches a heartbeat file in its
   own memory-backed directory on the VM. The watchdog watches those files
   with the kernel's own file change events and kills a container whose
   file is thirty seconds stale. This is the one check that never touches
   S3, because a heartbeat event a minute late must not kill a healthy
   worker. The supervisor and monitoring count a heartbeat only after reading its blob, never from the event's key alone, and judge it by the time the blob holds, not by when its event arrives, against a limit set on
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11) above the
   delivery delay measured on paper.

A failure of the event path itself is caught three ways.

1. After writing the plan, the supervisor waits for the plan's own event on
   its queue, and halts the run before the first decision with "event
   delivery: plan event not received" if it does not come in time. Any
   supervisor instance may receive that event, so the one that does writes
   a create-only record that it arrived, and the writer's deadline reads
   that record when it expires.
2. Every rule's target has a dead-letter queue, and monitoring alarms on its
   depth and on EventBridge's failed deliveries.
3. An outage of SQS or EventBridge as a whole stops monitoring's pings, so
   the heartbeat service's dead-man check, which sits outside AWS, raises
   it.

**The kill switch reaches execution through its blob's event.** The owner
ruled on 2026-10-31 that components wait only by long polling their queues,
and that a pull also sent a message straight to execution, the supervisor
and monitoring. On 2026-11-01 the owner dropped that message. The supervisor
cannot cancel anything, because only execution talks to the broker, and the
switch blob's own S3 event already reaches the queues of execution, the
supervisor and monitoring, whoever pulled it. `ithildin switch pull`, the
one supported way to pull it, writes the switch blob, then journals the pull
and raises the alarm. It then reads the journal once at a deadline set
before the cancel cutoff, and if execution's "cancels done" entry is not
there, it tells the person to cancel at the broker's own console while the
broker still takes cancels. Execution also reads the switch right before
every submit and when it starts, as rule 9 says, so no new order goes out
after a pull whatever the event's delay. No component reads the switch on a
clock, as the owner accepted on 2026-10-31. The price is a pull that waits on
S3's event delivery, usually seconds and sometimes a minute or longer, so a
pull in the two minutes between the send cutoff and the cancel cutoff can
reach execution after the broker stops taking cancels on a slow day. The
tool's deadline and the broker's console are the bound then. No person's
role may send to any queue, so every message on a queue is an event from an
environment's own rule.

**The same events in a backtest and in CI.** The memory-backed folder
adapter plays S3's part from the same routes. After each write it delivers
the event to each reader's named pipe, and under a seeded fault it repeats,
delays by an amount drawn from the delay measured on paper, reorders or
drops events. A named pipe keeps nothing while its reader is down, so the
contract suite holds both adapters only to what both guarantee: an event
can be lost, and a worker recovers by checking its keys. A writer that
restarts announces its newest result again through the storage port, which
the folder adapter delivers and the S3 adapter skips, because S3 already
announced it.

**Rules only grow, and a release checks them.** A release's rules are added
and never removed by a later one, so rolling a pin back to an earlier
release leaves every rule it needs in place, and an extra rule only sends
events a reader drops. When a session starts, the VM checks that every rule its release's routes need exists with the pattern and target those routes give, and halts with "event rules do not match release", naming the rule that is missing or differs. A rule keeps its name only while its pattern and target stay the same, so a later release adds a new rule rather than narrowing an old one.

The price is six things.

1. An EventBridge rule for each route and an SQS queue with a dead-letter
   queue for each component, in each environment, generated from the
   routes, and drills that delete a rule, deny a queue's policy and turn off
   the bucket's EventBridge delivery, each expecting its alarm.
2. Long polling's requests, about three a minute for each waiting worker, inside SQS's free million a month at this scale.
3. S3's delivery delay added to each step of a decision. A decision passes
   through about five steps, so a slow day that delays each by two minutes
   can push the 15:40 decision past the 15:48 cutoff, where execution
   journals its orders `Abandoned` rather than sending them late.
   Monitoring records each step's delay in the decision's report, alarms
   when a decision's total passes a share of the eight minutes, and the
   paper gate names the measured delay.
4. Deadlines and heartbeat limits long enough to cover delivery's slow tail,
   or a healthy decision alarms, and the dead-man check's grace above them.
5. A pull's delivery delay against the cancel cutoff, bounded only by the
   tool's deadline and the broker's console, which the owner accepted on
   2026-11-01 when dropping the pull's direct message.
6. A second implementation of delivery in the folder adapter, held to S3's
   behaviour only as far as the contract suite reaches.

What no route gives is a reader that sees a change the instant it is
written, which is why nothing that changes money rests on an event.

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
receive loop until it has a result for every decision in the plan. In
shadow, paper and live the plan holds one trading day's decisions. In a
backtest it holds the whole history. Each worker hands every decision to its pool
of handlers, in the plan's order, and the exit rule is the same in both. A worker carries nothing from one
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
guards against a hung process rather than shaping what a strategy sees. It
reads it through a timer port of its own, always wall time and faked in
tests, so a deadline never reads the history clock and never bypasses a
port. The simulator's `clock()` reads the `Clock` handed to it, so a
backtest never halts on skew between two clocks.
Execution's send stage reads it before every send, to check the cutoff for
the strategy's orders, as `### Strategies, schedules and decisions` says.
Under the history clock that reading is the decision time, so a backtest
never meets the cutoff, and a drill sets a test clock past it instead. Apart from those places, a worker reads wall time only to key its heartbeat and, in execution and monitoring, to set the deadlines of `### How a component learns of a change`, none of which shapes what a strategy sees. Otherwise the workers move on only when messages arrive.

A halt ends its decision, and the span goes on to the next decision, which
halts again if its cause is still there. A failure stops the whole run.

The price of this section is five things.

1. Every value that crosses from one module to another needs a file format,
   and a test that writes it and reads it back unchanged. A deterministic
   component's format also has to be canonical, with fixed field order and number formatting, so the same inputs give the same bytes. architecture.md's `### File formats and keys` proposes canonical JSON for it.
2. A worker wakes on an event rather than a call, so each step waits on S3's delivery as well as on the step before, and the rules that route each write's event are generated from the one table in `runtime` that declares every worker's inputs.
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

This doc proposes that a strategy bring six things, each declared in the one
table in `runtime` beside the workers' inputs, so that adding a strategy adds
rows rather than changing a shared module.

1. The instruments it trades.
2. Its schedule, a list of decision times on each trading day.
3. The data it reads: the kinds, the bar length and the lookback.
4. Its alpha model, run as a stage of its own named for the strategy, so two
   strategies' forecasts never share a key.
5. Its configuration, whose hash every blob it touches records.
6. Its alias, up to twelve lower case letters and digits, which its orders' stamp carries.

A run in shadow, paper and live is one trading day of one deployment. One
function in `core` turns the deployment's schedules and the exchange's trading
calendar into a list of decisions, each with its time and the strategies
deciding at it. The supervisor calls it for one day, and a backtest calls it
for the whole span, so both build their plans the same way. A decision time
stated as minutes before the close moves with an early close, and one stated
as a clock time after an early close is dropped. The host starts every unit of the run
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
3. **A plan changes or cancels its own unfilled orders.** The owner ruled
   on 2026-10-31 that a plan can change or cancel an existing order if it
   has not been filled yet. At each decision the order manager compares the
   strategy's target with its positions and its own open orders. A resting
   order of the platform's that the target no longer wants, in whole or in
   part, is cancelled, and one whose quantity or price should change is
   cancelled and replaced by a new order with a stamp of its own. Only the
   unfilled remainder is ever cancelled, and fills already made stand. The
   rest of this item is this doc's proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10).
   1. **Every cancel and replacement passes the order path.** The planned
      list the send stage journals before its first send names each cancel
      and each replacement beside the new orders, so a rerun in the same
      decision reuses that list and plans nothing again, as rule 9 says. A
      replacement's quantity is journaled as a rule rather than a number:
      the planned quantity less whatever the old order filled after the
      plan, never below zero, which every rerun computes the same way from
      the journal. Each cancel is journaled before it reaches the broker,
      and right before the broker call execution checks that no halt of the
      run is stored, its own send result is still empty, the account's lease
      still carries its tag, and the cancel cutoff has not passed. A cancel
      is never sent twice for one client order id, and one whose answer is
      lost stays `CancelSent` until a lookup by client order id settles it,
      as rule 9 does for any order in doubt.
   2. **A replacement waits for its cancel's outcome.** The order manager
      journals the old order's outcome, any fill included, and only then
      journals the replacement's intent and asks the risk gate, so the gate
      sees what the old order filled and counts it at its worst case until
      then. A cancel refused because the order already filled, or a
      replacement whose quantity comes to zero, journals the replacement
      `Abandoned`. A replacement whose cancel is not confirmed by the send
      cutoff is journaled `Abandoned` too, and the next decision plans from
      the book. A plan replaces an order only while the replacement can
      still go out before the send cutoff, and otherwise leaves the old
      order resting.
   3. **It touches only the strategy's own stamped orders.** A plan never
      cancels or changes another strategy's order,
      and never after the exchange's or the broker's cancel cutoff, past
      which a market-on-close order stays as sent.
   An open order still wanted as it stands is left resting, so it is
   neither sent again nor left out of a limit.
4. **The dead-man check follows the schedule.** Monitoring pings after each
   decision's report, and the heartbeat service's check runs on the
   deployment's schedule, a check by cron expression on the exchange's clock, one of the rules stated on the exchange's clock, as architecture.md's `### File formats and keys` says, with a grace period of the longest chain of worker deadlines. On a
   market holiday the supervisor writes a plan with no decisions, and
   monitoring pings anyway, at or after the check's scheduled time, from a
   ping-only entry the plan carries at that time. healthchecks.io expects a
   cron check's next ping at the first scheduled time after the last one, so
   a morning ping on an empty plan would still leave the check expecting
   one at the decision time. A period set to the longest gap between decisions
   would span a long weekend, so a run that died on a Tuesday would go
   unheard until Saturday.
5. **Market-on-close orders go out before the broker's cutoff, or not at
   all.** The owner ruled on question 10 of [architecture.md](architecture.md)
   that the skeleton trades the close with market-on-close orders, which
   Alpaca rejects when "submitted after 3:50pm", ten minutes before a full
   day's close. The owner asked on 2026-11-01 for times stated as minutes
   before the session's close, written close−N, so an early close moves
   every time with it. The skeleton decides at close−20, which is 15:40 on a
   full day. The cutoff belongs to the broker account, because it is the
   broker's rule. This doc recommends close−12, 15:48 on a full day, for the
   owner to set in the paper account's hard caps blob, and the calendar
   function in `core` turns it into a UTC instant on the decision's own
   session date from that day's close. A simulated account's cutoff is the
   close itself. A strategy can set an earlier
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
      cancelling a closing-auction order, which this doc takes as close−10,
      15:50 on a full day, as architecture.md's kill switch section says. After the order loop, execution
      looks up each leg by its client order id until every leg is accepted or
      the cancel cutoff passes. When one leg is rejected, unsent or past the cutoff while another is accepted, execution cancels the accepted legs, journaling each cancel first. The same holds for a pair whose legs are an order left resting and a replacement: when the replacement is refused, abandoned or past the cutoff, execution cancels the resting leg. When execution crashes with its send stage
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
         a recorded data vintage by a calculation anyone can rerun, over at least a weekend's hold. The owner ruled on 2026-10-31 to read every alert before the next trading day's decision, about 24 hours on a trading day and never longer than a weekend, which is the hold this window covers. `max_net` is sized so one leg's loss under
         that move fits the share of the loss limit the owner sets. The owner
         ruled on 2026-11-01 that the loss limit is the account's portfolio
         value, so the cap follows the account rather than a fixed figure.
         `max_net` is s × V ÷ m, where s is the share, V the portfolio value
         and m one leg's stress move as a fraction. The owner set s at 1% the
         same day, so one stranded leg's loss under the stress move is 1% of
         the portfolio, and a hundred such losses in a row would exhaust it. The hard caps blob stores s and m, with the hash
         of the vintage m came from, and the gate computes the cap at each
         decision. V is the account's valuation at the prior session's
         official closes, which the loss stop already stores, so the cap
         shrinks after a loss and grows after a gain with no one acting, as
         the owner ruled on 2026-11-01. The capital limit the owner sets by
         hand under hard limit 1 is therefore s and m, and only a write of
         the hard caps changes either. V comes from the book the journal
         replays, valued at official closes, never from the broker's equity
         figure, so a broker's reporting error cannot raise the cap. A
         missing valuation halts, as any missing input does.
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

An account belongs to one environment and carries one deployment there, and the supervisor refuses a
plan whose account another deployment names. Execution, the risk gate, the
journal and the account's lease therefore stay one per account, and hard limit
2's single order path holds however many strategies trade. Strategies that share an account share its deployment, and meet in portfolio construction, the place *Inside the Black Box* puts the combining of alpha models. Until how they combine is settled, it builds one target per strategy, as `### Orders the platform did not send` says.

**A scale unit per portfolio.** The owner set the shape of scaling out in
review comments on 2026-11-01, in four points.

1. One account holds one portfolio, which several strategies may share.
2. One scale unit is a complete set of components, the supervisor and every
   worker, paired to one portfolio.
3. The platform scales out by adding portfolios, each a scale unit of its
   own.
4. Every component but the supervisor may run as several instances, with a
   router in front that hands each message to an instance that is free.

The rest of this part is this doc's proposal for
[#22](https://github.com/l3a0/ithildin-runtime/issues/22) and
[#11](https://github.com/l3a0/ithildin-runtime/issues/11). For every stage
but execution's send, the queue is already that router. SQS lets several
processes long poll one queue at once, hands each message to one of them,
and hides it from the rest until that one deletes it or its visibility
timeout runs out, as AWS's
[visibility timeout page](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
describes. An instance that dies mid-message lets the message return to the
queue for another. Two instances that both compute one decision write the
same bytes, which the create-only write and rule 2's comparison turn into
one result. So a second instance of data, alpha, risk, cost, portfolio
construction or monitoring needs nothing new.

Execution is the exception, because sends on one account happen one at a
time. The risk gate checks each order against every order in the account's
journal just before it goes out, and two instances sending at once would
each check a book missing the other's order. So execution instances compete
for execution's queue like any other component's, as the owner ruled on
2026-11-01, and the account's lease lets one of them change the account at a
time. An instance holds the lease only from re-reading the journal for one
order until the journal holds the broker's answer, as rule 9 says, so the
others never wait long. The instance running a send stage extends its
message's visibility timeout while it works, so the message returns to the
queue only once that instance dies, and rule 9's fifth item says who then
takes the send stage over. Within one portfolio, more execution instances
buy failover rather than throughput, because sends still go one at a time,
and throughput comes from more portfolios, each sending on its own
account.

The owner ruled on 2026-11-01 that slice 1 runs three instances of each
component, for high availability, and later that day that the supervisor
runs several instances too, sharing the deployment's lease, as rule 9 says.
That amends point 4 above. So a deployment runs 21 worker containers and
three supervisor containers, and pre-live's two deployments run 48
containers on its one VM. The supervisor's instances compete for its queue
like any component's, and each of its writes, the plan, a worker's halt,
a halt of the run and the run's stop, is create-only, so the first
instance's write counts and the others use it. Three instances on one VM cover a process
that crashes or hangs, which the other two absorb while the host restarts
it. They do not cover the loss of the VM, which only a replacement VM
taking over the deployment's lease covers, as architecture.md's
`## Where it runs` says.

Several instances of one component need two rules a single instance did
not.

1. **A deadline that expires reads storage before it acts.** The event that
   would clear a deadline can reach a different instance from the one that
   set it, so each instance's timer expires on its own. Before it alarms or
   halts, an instance reads the key the deadline waits on once, as the
   supervisor's deadlines already do, and acts only if what it waits for is
   still missing.
2. **One cause raises one alarm.** Monitoring writes the file naming the
   halt it is handling before it alarms, as rule 7 says, and that write is
   create-only. Only the instance whose write lands sends the alarm, and an
   instance whose write S3 refuses sends nothing, so three instances that
   all see one cause raise one alarm. A missed deadline's alarm goes through
   the same kind of file, named for the deadline and its decision.

The price is three times the containers, about 1.2 million long polling
requests a month on pre-live and about 1.9 million once live runs, past
SQS's free million, and a VM with memory for 48 Python processes. At
roughly 100 MB each, an estimate to measure on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), that is about
5 GB.

Shadow needs no broker account. Its simulated account lives in the shadow
environment's bucket, so a shadow deployment can run the same portfolio as
a paper one without touching paper's account.

Two things wait for the first strategy that needs them. Each is written on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), so the next reader
finds it there.

1. **How several strategies combine.** Portfolio construction's rule for
   combining forecasts, including what a strategy not deciding at a given
   time contributes, waits for a second strategy. A forecast from an earlier
   decision names the wrong decision, so rule 6 halts on it until that rule
   is written.
2. **A halt that holds a shadow or paper account until a person clears
   it**, for every cause but an order the platform did not send. Slice 1
   builds the holding halt for that cause, as
   `### Orders the platform did not send` says, and for every cause on a
   live account, as rule 7 says. Once a day, a
   persistent cause such as a book that disagrees with the broker halts the
   next decision again, because every decision checks again. A strategy
   deciding minutes apart would meet a gate refusal again before a person
   could look. Holding the account needs an open halt recorded in the
   journal, written by the stage that finds the cause before its halt result,
   and cleared only by the clear command's own entry.

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
| **target portfolio** | The positions portfolio construction asks for one strategy. The difference between it and that strategy's own positions and open orders is what execution trades |
| **risk gate** | The pre-trade check every order passes on the one order path. It enforces the risk engineer's limits and can refuse an order |
| **journal** | The append-only record of every intent, order, acknowledgment, fill, cancel and human override, written before the action it records, and of every order or fill the platform did not send, journaled as received before execution halts on it |
| **stamp** | The client order id the platform gives every order it sends, `ith-<strategy>-<decision>-<n>`, which marks the order as the platform's and names its strategy |
| **worker** | One component, meaning one module of the charter's module table, run as its own process, which the host keeps running. It listens on its queue through its receive loop, which hands each item to a handler in its pool, reads its input blobs, writes its output blobs, and reaches the outside world only through the adapter at its edge. [Processes and files](#processes-and-files) gives its rules |
| **blob** | One object in storage, written once and never replaced |
| **output file** | A blob holding one thing a stage produced for one decision, at a key fixed by the run, the stage, the decision and the output's name |
| **stage** | One step a component takes in a decision. Execution's are recover and send, and monitoring's are reconcile and report. Every other component has one |
| **decision result** | The blob a stage writes last for a decision, at a key fixed by the run, the stage and the decision, listing its output files by key and SHA-256 or saying the decision halted. One key, so a stage ends each decision exactly once |
| **receive loop** | The one event loop in each worker, and in the supervisor, that reads its queue and hands each item, a stage and a decision, to a free handler. [How a component learns of a change](#how-a-component-learns-of-a-change) gives its rules |
| **handler** | One thread of a worker's pool, which works on one item at a time, independently of the worker's other handlers |
| **event** | The store's announcement that a blob was created or deleted, which reaches the queue of every component that reads it. An event is a hint, and the blob is the record |
| **listing** | A component's stages' blobs, listed by decision. Each blob's header names its run, decision, inputs by key and SHA-256, release and configuration, so the listing is the component's record |
| **plan** | The blob the supervisor writes once per run, naming the run's mode and its decisions in order |
| **queue** | The one place a worker listens for events: SQS in shadow, paper and live, and a named pipe in a backtest and in CI. A worker waits on it by long polling the queue and never reads storage on a clock |
| **supervisor** | The process that takes the deployment's lease, writes a run's plan, whose event reaches every worker, hears the workers' heartbeats and the kill switch as events, starts each stage's deadline when it hears that stage's last input, writes a halt as a worker's result when the worker fails, and stores a halt of the run on such a failure, whose event reaches every worker. It starts, stops and watches no process, and keeps no state outside storage. A deployment runs several supervisor instances, which share the deployment's lease and compete for the supervisor's queue |
| **run** | Everything one run writes, kept under the run's own prefix in storage. The VM keeps only a working directory for the run's error output and heartbeat files, none of which is kept |
| **halt** | A decision result saying why a decision cannot go on, naming the reason or the halt it met. Every worker downstream passes it on and skips that decision. The owner or the operator clears it, except on a live account, where only the owner does, and clearing never reruns the decision. |
| **span** | The decisions in a run's plan, which every worker works through: one trading day's in shadow, paper and live, and the whole history in a backtest |
| **decision** | One moment at which a deployment decides, keyed by its time in UTC, as the owner ruled on 2026-10-30. Every stage writes one result per decision |
| **strategy** | The instruments, schedule, data needs, alpha model and configuration that differ from one way of trading to the next. Every other module is shared by all strategies |
| **schedule** | A strategy's list of decision times on each trading day. One function in `core` turns the schedules and the trading calendar into a plan's decisions |
| **environment** | A set of resources one of test, shadow, paper and live runs on: a machine, a bucket, roles, a broker credential, a heartbeat check and a release pin. Shadow, paper and live each run the mode of the same name. Test is GitHub's CI, which runs backtest mode among its checks |
| **deployment** | One or more strategies on one broker account in one mode, under whose name every key a run writes sits. An account belongs to one environment and carries one deployment there, whose keys and lease sit in that environment's bucket |
| **walking skeleton** | A build in which every module exists in its simplest form and the whole loop runs end to end |
| **exploratory** | A result found by looking at the data. It kills an idea or justifies a closer look, and is never a verdict |
| **registered** | A result whose hypothesis was committed in writing before the number was seen. Only a registered result can promote a strategy |

## Configuration

The runtime's VM keeps no state, so it keeps no configuration that matters on
its own disk. A setting comes from the release the pin names, from a blob in the environment's bucket, or, for which environment a VM is in, from the provisioning step at boot. The owner ruled on 2026-10-30 that test, shadow, paper and live run in separate environments. This doc proposes that each has its own bucket, VM, roles and broker credential, that the release carries one table of the environments, and that the provisioning step at boot tells a VM which environment it is in, as [architecture.md](architecture.md#four-environments)'s `### Four environments` says. The owner ruled on 2026-10-29 that the broker key
lives in the same store marketlake uses: AWS Systems Manager Parameter Store,
as a `SecureString` parameter encrypted under KMS, in marketlake's AWS
account, with marketlake's VM role narrowed so it cannot read it. The owner
ruled later that day that the runtime's bucket and VM live in that account
too, with the key under AWS's managed `aws/ssm` key, which the owner ruled
fine, the bucket behind a policy that names who may reach it, and
marketlake's plan role
narrowed to what `tofu plan` reads, as
[architecture.md](architecture.md#where-it-runs)'s rules 3 and 5 and its
question 11 say. This doc proposes the rest: each environment's key sits under a path of its own below `/ithildin/`, the roles that keep a broad parameter read refuse that
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
| A messaging server such as NATS JetStream, Kafka or Redpanda | Each is a server that can fail and halt every worker, and workers would pass events through it rather than through files, which the owner's ruling asks for. Kafka and Redpanda are also built for far more than about nine events a decision on one VM. SQS and EventBridge, which carry events since 2026-10-31, escape the first reason because an event is only a hint and the blobs stay the record, and an outage of either stops monitoring's pings, which the dead-man check outside AWS raises |
| A worker waking on file notifications through `watchdog`, or by polling its inputs | The owner ruled on 2026-10-29 for a queue per component instead, and on 2026-10-31 that S3's own events feed it, with no polling, as [How a component learns of a change](#how-a-component-learns-of-a-change) says. Watching a local folder sees nothing written in S3. The price is an EventBridge rule for each reader's prefix, generated from one table in `runtime` |
| A writer sending a message to each reader, and timed checks of the next key | The owner ruled on 2026-10-31 to avoid polling and to model the change event on S3's notifications, with one exception the owner ruled the same day: a kill switch pull sends its own message straight to execution, the supervisor and monitoring. A writer's message reached only the readers it knew on its own host, and a timed check cost a read every few seconds for each reader and still noticed late. The store's event covers every writer, a person off the VM included, and an absence is caught by a deadline instead |
| A pull's direct message to the queues of execution, the supervisor and monitoring | The owner ruled for it on 2026-10-31 so a pull would not wait on S3's event, and dropped it on 2026-11-01. The supervisor cannot cancel anything, the switch blob's event already reaches the same three queues, and execution checks the switch before every submit. Dropping it removed three queue grants for people's roles, a refusal path in the tool, a fake message in the folder adapter and a drill row |
| Delayed SQS messages or EventBridge Scheduler for deadlines, and a watchdog that reads heartbeats through S3 | A review of the first change-event design on 2026-10-31 found both wanting. A delayed message survives a restart in SQS but not in a named pipe, Scheduler needs a role passed to it that rule 5 of architecture.md's `### Four environments` refuses, and a heartbeat event can arrive a minute late. Deadlines are timers in memory, set again from storage on restart, and the watchdog watches local files |
| A queue that pushes to the worker: SNS or EventBridge to an HTTPS endpoint, Amazon MQ, or Kinesis enhanced fan-out | The owner asked on 2026-10-31 for an alternative to long polling that pushes. None shortens the time from a write to a worker waking, which S3's handoff to EventBridge dominates, and each adds an inbound port, a broker that bills by the hour, or a stream's fees. Measured delay on paper from SQS's renewal would reopen Amazon MQ or Kinesis |
| Hand trades in a platform account, kept apart in an outside book | The owner first ruled on 2026-10-31 that the platform ignores unstamped orders, so the owner could trade the same account by hand, and the doc built an outside book that reconciliation and the gate counted. Later that day the owner gave the platform an account of its own, and on 2026-11-01 ruled that each portfolio has its own account with no hand trading in it. So the outside book went, and an order the platform did not send is journaled as received and halts, as [Orders the platform did not send](#orders-the-platform-did-not-send) says |
| The broker's own replace call, which swaps a resting order for a new one in one request, and sending a replacement before its cancel is confirmed | The owner ruled on 2026-10-31 that a plan can change or cancel an unfilled order. A cancel and then a new order keep every broker write on the one order path with a journal entry each, and waiting for the cancel's outcome means the old and the new can never both fill unseen. The price is a gap between the two orders, and a replacement that misses the send cutoff |
| One process per action, such as the order manager and the order path each in its own | The owner ruled on 2026-10-29 that this was too granular, and narrowed the ruling to one process per component. A component is the unit the charter gives an interface and an owning role |
| More than one execution process sending on one broker account at a time | The risk gate checks limits across every order and position in the account's journal, so two gates at once could each approve orders that together breach one. A restarted supervisor beside a still-running order path sent every order twice in a review's simulation, which is why rule 9 takes a lease. Several execution processes may run, as the owner ruled on 2026-11-01, and the account's lease lets one of them send at a time |
| Each worker handling one message at a time | The owner ruled on 2026-11-01 for a receive loop that hands each item to a pool of handlers. A pull's cancels then need not wait behind a plan in the same execution instance, and a backtest's independent stages run decisions side by side |
| One instance of each component in slice 1 | The owner ruled on 2026-11-01 for three instances of each component, for high availability. The price is three times the containers on the VM and a long polling bill past SQS's free million |
| One supervisor beside several instances of each worker | The owner ruled on 2026-11-01 that the supervisor runs several instances that share the deployment's lease. The price is a renewal shared among instances and a record that the plan's event arrived |
| One execution process holding the account's lease for its whole life, with standbys that receive nothing until they take it over | The owner ruled on 2026-11-01 that every execution instance competes for execution's queue and holds the account's lease only while it changes the account. The price is an S3 round trip to take and delete the lease for each order, and a send stage another instance has to take over when its runner dies |
| Durable state on the VM's disk | The owner ruled on 2026-10-29 that the VM keeps no state that matters, with every file a blob in storage. A replaced or lost VM then costs nothing but a lease to take. The price is a round trip to S3 per blob, and a memory-backed folder for backtests that must behave like S3 |
| S3 for backtests | Test 1's tens of thousands of writes and reads would each wait 10 to 30 ms. The owner ruled on 2026-10-29 for an in-memory store, which this doc takes as a memory-backed folder |
| One result per component per decision | Execution and monitoring each act at two points in a decision, and a single result written last put execution's account file behind its own send, a cycle a review's simulation showed. Each now has two stages with a result each |
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
| Schwab or Interactive Brokers as the paper broker | Schwab is not known to offer an API paper account, and Interactive Brokers' needs a gateway process running beside the platform. The owner ruled on 2026-10-30 for Alpaca's paper account, which is free, has a REST API, and holds a paper key that cannot move money |
| Schwab as the live broker | The owner ruled on 2026-10-31 for a Schwab account of the platform's own, then on 2026-11-01 for Alpaca at live too, since the platform's account is separate anyway. Alpaca's paper and live accounts share one API and differ only by base URL and key, so the adapter paper exercises is the one live runs, and Schwab's open questions, a client order id and an API paper account among them, no longer apply |
| Calling the broker's REST API with an HTTP client passed in, rather than its SDK | The owner ruled on 2026-11-01 to use the broker's SDK, alpaca-py, wrapped in an adapter of the platform's own so a test hands in a fake. The wrapper keeps the SDK's types out of every module, so the import rule still fences the broker to `execution.brokers` |
| A long-running ECS Fargate service, or Lambda per component, as slice 1's host | Lambda's 15-minute limit rules out a worker that lasts the session, which execution needs. Fargate would remove the machine, but its task roles and placement have run zero times, while the VM's design is reviewed and its refusals are ruled. Since 2026-10-31 the VM uses the same SQS adapter Fargate would, so the queue no longer separates them. The owner ruled on 2026-10-30 for the VM for slice 1, and what running the VM costs is the evidence that would reopen it, as question 12 of [architecture.md](architecture.md#questions-for-the-owner) says |
| Shadow, paper and live sharing one VM and one bucket, kept apart by key prefix and by the account a lease names | One wrong role or one wrong prefix in a command could reach every mode, such as a drill pulling the live switch or a shadow run reading a broker key. The owner ruled on 2026-10-30 for separate environments, one each for test, shadow, paper and live, and on 2026-11-01 that shadow and paper share one pre-live environment in slice 1 while live keeps its own, as [architecture.md](architecture.md#four-environments) says. Live, where the mistake would cost money, still never shares |
