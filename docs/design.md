# Design — ithildin-runtime

This doc carries the reasoning. The tracker carries unbuilt scope, and this
doc links to an issue rather than restating it.
[team-charter.md](team-charter.md) carries the roles and the lifecycle, and
the module table this doc reasons about. [architecture.md](architecture.md)
draws how slice 1's modules fit together.

## Contents

- [Premise](#premise)
- [Design for 10x, plan the rewrite before 100x, build at 1x](#design-for-10x-plan-the-rewrite-before-100x-build-at-1x)
- [Every design weighs the whole lifecycle](#every-design-weighs-the-whole-lifecycle)
- [One runtime](#one-runtime)
- [Processes and files](#processes-and-files)
- [Data structures](#data-structures)
- [Design principles](#design-principles)
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

## Design for 10x, plan the rewrite before 100x, build at 1x

The owner ruled on 2026-11-02 that the architecture is designed for 100
times the load it first carries, and on 2026-11-03 adopted Jeff Dean's rule
in its place, as [Design principles](#design-principles) states it: design
for 10 times the first load, plan the rewrite before 100 times, and build
at the first load. The first version, the MVP, starts at that first load.
Slice 1's load is one portfolio deciding once a day. Ten times that is ten
portfolios, or a portfolio deciding ten times a day, or a mix. The design's
own ceiling for one deployment is higher, at a decision a minute, as
`### Strategies, schedules and decisions` says.

Designing for 10x means a choice made now does not need rewriting to reach
10x. A design passes when reaching 10x takes configuration and more
infrastructure, such as more instances, more VMs or more portfolios, and no
rewrite of a component, a file format or a rule. The pieces that carry that
are already in this doc.

1. Components pass everything through blobs and queues, and keep no state
   on a machine, so any of them can move to another machine.
2. Every component can run as several instances, with its queue as the
   router, so adding instances adds capacity without new code.
3. The account's lease and the plan's lease make only the changes that must
   happen one at a time wait for each other, and nothing else.
4. A portfolio is a scale unit of its own, so more portfolios add load
   side by side rather than on one path, within Alpaca's limits below.

Building at 1x means slice 1 runs the smallest deployment that design
allows. Each environment runs on one VM, in an Auto Scaling group of one so
a lost VM is still replaced without a person, and the cluster of three VMs
across three availability zones in rule 11 of architecture.md's
`## Where it runs` is the scale-out path for 10x. The cluster buys survival
of a lost VM or zone, and the mapping below buys capacity. The three
instances of each component the owner ruled for on 2026-11-01 run on that
one VM, where they cover a process that crashes or hangs.

A VM group also has a written limit of deployments, which this doc
proposes for [#11](https://github.com/l3a0/ithildin-runtime/issues/11). It
is the most deployments whose containers fit in 75% of one VM's memory at
the size per process measured on paper. At the estimate of 100 MB a
process, one deployment's 24 containers take about 2.4 GB, so slice 1's
8 GB VM holds two deployments under that limit, a figure the measurement
replaces. The next split past the limit is configuration rather than
code: a mapping from account to VM group, with one Auto Scaling group per
set of accounts. The release's table carries the mapping beside each
environment's deployments, and the provisioning step tells a VM its group
as it tells it its environment. Each group gets its own queue for each
component and its own generated routes, so a VM receives only its group's
messages. The mapping is a direct lookup, which suits a handful of
accounts, rather than the hash rule 3 of `### Design ahead, and write down
where the build stops` uses for partitions. A table test generates routes
and units for two deployments, and a drill runs a second deployment in
shadow before anything relies on the mapping.

At the same estimate, ten portfolios' 240 containers take five such VMs per
environment in groups of one, or six 8 GB VMs in two groups of three. A
group of three of rule 11's 2 GB machines holds one deployment, so on those
machines raising a group to three adds survival and no deployments.

Past 10x the design expects a rewrite rather than promising to avoid one,
so it writes down three things, the first two proposed for
[#11](https://github.com/l3a0/ithildin-runtime/issues/11) until the owner rules on them.

1. **Where the rewrite's planning starts.** The load parameter is
   portfolios in an environment, meaning the deployments the release's
   table lists for it, and the level is 8, which is 75% of the 10 this
   design promises, rounded up. Each environment reads its own entry,
   because no runtime role reaches another environment's resources, so one
   level can page up to three times. The lead time is the two portfolios
   between the level and the ten. Each needs an account of its own and a
   release, and the owner holds at ten until the rewrite lands. Decisions a day carry no level, because configuration alone
   takes them past 100 times the first load: one deployment's ceiling of a
   decision a minute is 390 in a full session, and the 1x alarm covers it.

   Reaching ten portfolios on one login depends on Alpaca's sub-accounts.
   Alpaca's staff
   [said](https://forum.alpaca.markets/t/feature-request-more-paper-trading-accounts/18125)
   on 2026-08-14 that one login holds one live account per account type,
   and Alpaca's own page gives three paper accounts. So without sub-accounts
   one login stops at three live accounts, and only by mixing in IRAs. The
   same thread has staff saying on 2025-11-07 that paper passes three
   through a second login, and it does not settle a second live login.
   Staff said on 2026-10-15 that sub-accounts had not officially launched,
   with a target of the end of October. A user reported them working live
   on 2026-10-28 and hoped for more than two, so ten needs a limit Alpaca
   has not stated. Alpaca also applies its wash trade rule across accounts,
   so one portfolio cannot buy what another is selling at the same time. A
   second portfolio may therefore need a check across accounts, which is a
   new rule rather than configuration. So the path to 10x by portfolios is
   not configuration alone, and the path by decisions is unaffected.
2. **What the rewrite is expected to replace.** The price of one deployment
   for each portfolio is that containers grow with every portfolio. So the
   expected rewrite shares the workers that hold no account, which are data,
   alpha, risk, cost and portfolio construction, across portfolios, and
   moves them from VM groups mapped by hand to Fargate or a container
   orchestrator. The evidence for this is thin, because no load has been
   measured, and the load that trips the level may point somewhere else.
3. **What must survive the rewrite unchanged.** Captured data, the journal,
   their formats and the partition layout, and the tests of the risk gate's
   refusals, the halts and the kill switch, as
   [Design principles](#design-principles) says. For this rewrite the
   partition layout means captured data's layout and the `runs/<deployment>/`
   and `accounts/<account>/` key shapes. Execution, its broker key, the
   account's lease and the journal also stay one per account, because hard
   limit 2's one order path rests on them.

Two things are built at 1x all the same.

1. The measure of load and its alarms. The measure is the decisions a day,
   the portfolios in each environment, and how long each stage takes against
   its deadline. One alarm says when load nears the 1x limit, and another
   says when the portfolio count reaches the level above. Otherwise the
   first sign of either is a missed deadline.
2. Least privilege, so each credential carries only what its 1x use needs
   from the first day.

The price is that the 10x paths run zero times at 1x. The test of whether
a VM on another machine has gone, the instance refresh across three VMs and
the move to another zone are designed now and built when load calls for
them, under the ranking rule in [CLAUDE.md](../CLAUDE.md), and each is
drilled before anything relies on it. The second price is a rewrite if
load passes the level above, paid so that the design does not build for
load it may never see.

## Every design weighs the whole lifecycle

The owner ruled on 2026-11-02 that the architecture always considers the
entire software development lifecycle, from planning through building,
testing, deploying and operating to retiring, and follows best practice for
ten qualities. A design that serves one stage and breaks another, such as a
fast path no test can reach or a deployment nobody can roll back, is not
finished. Each quality has a home in these two docs.

1. **Scalability.** Every part is designed for 10 times its first load,
   with the rewrite planned before 100 times, and built at that first load,
   as the section above says.
2. **Availability.** Three instances of each component, an Auto Scaling
   group that replaces a lost VM, and leases that fail over on their own,
   as rule 9 and architecture.md's rule 11 of `## Where it runs` say.
3. **Security.** No agent places a live order or moves money, a broker key
   never carries withdrawal permission, each environment has an AWS account
   of its own, and only the execution user reads the broker key, as
   architecture.md's rule 3 of `## Where it runs` says. The owner ruled on
   2026-11-02 that moving money out requires a human's sign-off, so no
   money-out call may complete on the broker key alone. Alpaca's trading
   key can add a whitelisted withdrawal address, which turns approved on
   its own after 24 hours with only an email notice, though withdrawals
   themselves are made on Alpaca's web dashboard. The owner ruled after a
   board sitting on 2026-11-02 that Alpaca stays the live broker, with
   crypto never enabled on the platform's account and no whitelisted
   withdrawal address, proven by a read-only check before the live gate
   and on a schedule after, as
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) records. The
   owner ruled the same day that execution runs the check at each session's
   start, before its first decision, and once more after its last. A
   whitelist entry a stolen key adds between sessions can turn approved
   before the next check, over a weekend for one, so Alpaca's email to the
   owner is the warning there, and an approved address still needs the
   owner's web login to withdraw anything. The board named two more gaps,
   and the owner ruled on 2026-11-02 to accept them unless mitigations
   exist. This doc proposes two, on the same check, and accepts what they
   leave.
   1. **The key can change the account's settings**, its margin multiplier,
      shorting, options level and trade suspension. The check also reads
      the account's configuration and compares it with the settings the
      hard caps blob pins, which only the owner writes. Any difference
      alarms and halts live trading. The risk gate's own limits never
      depended on the broker's settings, so a changed margin cannot make
      the platform itself trade larger.
   2. **A stolen key can move value out by trading.** The check also lists
      the account's open orders and recent fills, and any order without the
      platform's stamp is journaled as unmatched, pulls the kill switch so
      execution cancels what rests, and holds the account, as an unmatched
      order already does at a decision.

   What stays accepted is the value a thief moves between two checks,
   nights and weekends included, which the capital the owner puts in the
   account bounds.
4. **Operability.** A person acts through the one `ithildin` tool, every
   halt says why, and every live halt holds until the owner clears it, as
   architecture.md's `## What a person runs` says.
5. **Monitoring.** A monitoring component reports every decision and
   raises one alarm per cause, rarely two and never none, and a dead-man
   check outside AWS catches the
   silence nothing inside can report, as architecture.md's
   `## When something is wrong` says.
6. **Auditing.** Every order is journaled before it is sent, the journal is
   a hash-chained log under Object Lock, every blob names the blobs it was
   made from, and an organization trail records every account's API calls.
7. **Testing.** What is tested is what runs, as `## One runtime` says, and
   CI runs module tests, contract suites against every adapter and drills
   for every alarm, as architecture.md's `### How CI runs them` says.
8. **Performance.** A decision waits about a second on S3 and on S3's event
   delivery, which caps one deployment at about a decision a minute, as
   `### Strategies, schedules and decisions` says, and each component's
   pool of handlers keeps one slow item from holding up the rest.
9. **Usability.** The owner's time is the scarce thing, so `ithildin status`
   and `ithildin explain` answer what happened in one command, each cause
   alarms once, and each report names the next step.
10. **Deployment.** One container image pinned by digest, a provenance
    check before it runs, promotion from shadow to paper with live moved by
    the owner alone, rollback by pinning an earlier digest, and VM images
    rolled out by instance refresh outside sessions, as architecture.md's
    `### Four environments` and rule 11 of `## Where it runs` say.

[CLAUDE.md](../CLAUDE.md)'s `## Design for 10x, across the whole
lifecycle` says what a design pull request states about the ten. A
`## Qualities` section in the pull request body, which CLAUDE.md's
`## Pull requests` names, is this repo's convention for stating it. The
price of the rule is a longer pull request body, paid so that a change that
quietly weakens one quality, such as an alarm with no drill, is caught in
review rather than in a session.

## One runtime

What is tested must be what runs live. A backtester written apart from the
live engine is a second implementation of every strategy it runs, and the two
drift without either looking wrong. Narang names backtest software and data
feed handlers among the places model risk lives (L4616 in the
[highlights](https://github.com/l3a0/trading-strategies/blob/main/research/book-notes/inside-the-black-box.md)).

Kleppmann reaches the same conclusion about data pipelines. Keeping one piece
of logic in both a batch system and a streaming system costs significant
effort, and debugging, tuning and maintaining two systems stays costly even
when a library lets one piece of code target both (*Designing
Data-Intensive Applications*, 1st ed., O'Reilly Media, 2017, Kindle location
15718, cited below as DDIA L15718).

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
one blob in storage that lists a component's outputs for a decision. Only the data worker, the simulator, and execution's send stage for its cutoff read the clock in a way that shapes what a strategy sees.

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

1. A **blob** is one object in storage, written once and never replaced,
   except a lease, which its holder rewrites only by a condition on the tag
   it last got. An
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
   halt. The result's own SHA-256 is the decision's **root hash**: the result
   lists each output by hash, and each output's header names its inputs by
   hash, so the root covers the decision's whole history. Two copies, or a
   rerun and the original, compare by that one hash, and monitoring's report
   names each stage's root. An **event** is the store's announcement that a blob was created or deleted, which reaches the queue of every component that reads it.
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
    mode. It is what a run's plan covers, and every key a run
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
not fetched: no blob is replaced but a lease by its holder's tag, no entry is rewritten, and no order is sent
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
   just before the broker call, on the kill switch, the strategy's off switch, the run's halt, its own send-stage result key, the account's lease's tag and the cutoff, as rule 9's fourth item says.
4. **Every lease** is fetched before it is taken, and one another process
   holds, on this VM or another, is taken over only once it has gone
   unrenewed past its expiry and the margin for clock skew, as rule 9 says.
5. **Every read of marketlake** fetches the manifest entry before the data it
   names, and a checksum that differs halts, as rule 8 of architecture.md's `## Where it runs` says.

Ten rules make the blobs something a worker can trust.

1. **Every write is a blob, created once.** A worker writes each blob with a
   single upload carrying a condition that makes S3 refuse it if the key
   exists, and the blob's SHA-256, which S3 checks before it accepts the blob.
   So a reader never sees half a blob, and no blob is ever replaced, except
   a lease, which its holder rewrites only by a condition on the tag it
   last got. S3
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
   Lock enabled, and every object under the journal's, the runs' and the
   overrides' prefixes is written with a retention date of its own. This doc proposes
   for [#11](https://github.com/l3a0/ithildin-runtime/issues/11) the mode
   by environment: compliance mode on live, which nobody can shorten or
   delete through, the account's root user included, and governance mode
   on shadow and paper, which the owner alone may bypass. It also proposes
   for [#11](https://github.com/l3a0/ithildin-runtime/issues/11) an Object
   Lock legal hold on live's journal and recorded captures, which no
   retention date ends, and an alarm on any object whose retention ends
   within a set window. Heartbeats and leases carry no lock, so their lifecycle and
   deletes still work. The bucket carries OpenTofu's `prevent_destroy`, and
   a test pins its lifecycle rules to two kinds of key: the heartbeats'
   prefix, and the noncurrent versions under `leases/`, which a
   noncurrent-version expiry removes, as this doc proposes for
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11), so a lease's
   rewrites do not pile up. So no rule matches a key that holds recorded
   data, a snapshot a decision read included. Before
   live, the live journal is also replicated to a bucket in a second AWS
   account with Object Lock of its own, as the owner ruled the same day,
   because Object Lock cannot outlive the account that holds it. The owner
   ruled on 2026-11-02 that the replica also carries every override and
   every recorded capture, because the journal names an override only by
   its SHA-256, so a journal copy without its overrides cannot replay the
   book. A capturing stage writes each capture with the tag
   `recorded=true`, and the replication rule matches the journal's prefix,
   the overrides' prefix and every object under the runs' prefix that
   carries that tag, so a derived output is never copied. A test pins that
   tag on every capture the store writes. The
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
   bucket's lifecycle expires heartbeats after a week, and removes the
   noncurrent versions a lease's rewrites leave under `leases/`, as rule 1
   says. Nothing else is removed.
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
   and watches no process. Its instances take the plan's lease to write the run's plan, which names its release, as rule 9 says,
   so one instance writes it, and another, or a restarted one, finds the plan already there and uses it, and whose event reaches every worker's queue. A worker that starts before
   any plan exists waits, so no worker acts before a supervisor has written the plan. On each VM the host starts the workers only once a supervisor instance on that VM has read the run's plan and found its VM on the plan's release, through a systemd path unit that the supervisor's local release file starts, so a VM outside the run never receives from a shared queue.
   Every worker, in every mode, finishes its decision loop once it has a
   result for every decision in the plan. Once every decision has a result and, in shadow, paper and live, the last cancel cutoff the plan's orders can reach has passed, a supervisor instance stores the run's stop, so execution is still there to serve a pull, whose event reaches each worker and every other supervisor instance, which exit cleanly, and the stop ends the run. A timer on the host stops every unit after the close
   as well, in case a stop is lost, and that timer is what ends a failed run, whose supervisors have already exited.
   - **A halt is passed on.** A worker whose input's result for a decision is a
     halt writes its own halt as its result, naming the one it found, and goes
     on to its next decision.
   - **A crash is a restart, and a failure stops the run.** One crash is not
     a failure: the host restarts the worker within seconds and it resumes
     from storage, which rules 3 and 9 make safe to repeat. One instance is
     unhealthy when its heartbeat, judged by the time in its key, stops for longer than the limit set on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) above the delivery delay measured on paper, when it restarts more often in one run than the
     host's limit allows, or when it exits before it has a result for every
     decision, which execution does not count while a session is open, because its units restart on any exit. An unhealthy instance raises an alarm naming the instance and
     is restarted or replaced, while the component's other two instances
     carry on. A worker, meaning the component, has failed only when its
     stage misses its deadline or when none of its instances is healthy. Its deadline runs from the moment its last needed
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
     naming the halt it is handling, and after the push it writes a second
     create-only record saying the push was sent. An instance that finds,
     when it starts or wakes, a claim older than a short limit with no sent
     record pushes again, which this doc proposes for
     [#11](https://github.com/l3a0/ithildin-runtime/issues/11). So a
     monitoring killed between its claim and its push can repeat an alarm,
     rarely, and never drops one, a repeat being the cheaper failure. The dead-man check gets success
     pings only. healthchecks.io notifies only when a check changes state, so
     a failure ping to a check already down sends nothing, and a check left
     down by one halted day would never alarm on a dead VM the next. A halt's
     alarm therefore goes straight to the alert channel's push topic with its
     reason, which this doc proposes for
     [#11](https://github.com/l3a0/ithildin-runtime/issues/11), and drills
     cover a halted day followed by a dead VM, two reasons in one run, and a
     fault that persists into a second day, each expecting its push. Alarms
     raised before monitoring runs, such as a lease taken over from another process, a pin
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
     account until the owner clears it, as the owner ruled on 2026-11-01.
     Where that rule lives is this doc's proposal for
     [#7](https://github.com/l3a0/ithildin-runtime/issues/7). The set of
     causes that hold an account is a value of each account's hard caps
     blob, which only the owner writes, and on live it names every cause,
     as the owner ruled. One holding-halt check reads that value rather than
     the environment, so no stage branches on which environment it runs in.
     Each later decision halts while the journal holds a halt of a holding
     cause that no `ithildin clear` entry names, so no cause that clears
     itself resumes live trading without the owner. Shadow and paper name
     only an order the platform did not send, and keep the rule above for
     every other cause. A run of test 2's replay with every cause set to
     hold puts live's behaviour through CI. `ithildin
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
   stored before the step it records happens. A reader checks that each entry's `prev_hash` equals the SHA-256 of the stored bytes of the entry before it, and halts on a link that does not chain, bytes that are not canonical, or a gap in the numbers. Three things keep that check cheap and complete as the journal grows, as this doc proposes for [#10](https://github.com/l3a0/ithildin-runtime/issues/10).
   1. **Checkpoints.** After a full check, the reader writes a checkpoint, a create-only blob at `journal/<account>/checkpoints/<seq>.json` holding the sequence number and that entry's hash. A later startup checks from the newest checkpoint whose entry still hashes to the value it names.
   2. **A head anchor.** The account check at each session's start and end compares the journal's newest entry with the replica's entry at the same sequence number, through a read-only grant on the replica's bucket. Equal hashes mean identical histories, and a difference alarms and halts, so a rewrite of the whole chain, which recomputed hashes would hide, still shows.
   3. **Book snapshots.** A snapshot of the book, keyed by the sequence number it was replayed through, lets a rebuild replay only the entries after it, from the newest snapshot at or before the newest checkpoint. Snapshots are derived data with an owner and an end date, and a weekly full replay from the first entry compares its book with the newest snapshot and halts on a difference. A hash recognises only a resend of the same bytes, so a writer that restarted checks the field naming its step, such as the client order id, before it appends, as that subsection lists.
9. **Leases make one supervisor change a run, and one sender change an account, at a time.** A lease is a blob a
   process creates with the condition that it not exist, recording its
   holder: the VM, its boot, the container and its restart count, the release, the run, when it was created and when it expires. Its holder renews it every ten seconds by rewriting it with S3's `If-Match` condition on the tag it last got, which S3 refuses with a 412 if anyone else has written it since, and each renewal moves the expiry a lease period ahead. It is deleted only with a condition naming the tag S3 last returned to its holder, so no stale holder deletes a successor's lease. The
   owner ruled on 2026-11-01 that no lease covers the deployment or the
   session, because every VM of the cluster runs at once and every other
   write is create-only, and on 2026-11-02 that a lease covers the plan.
   Each of the two leases is held only while it guards a change. The run's plan,
   which the first supervisor instance writes once, already names the run and
   its release, so it carries what those named. On each VM the host reads the
   run's plan before the pin and runs the plan's
   release when the plan exists, so a VM that starts mid-session runs the
   session's release. Only when no plan exists does the host resolve and check
   the pin. If another VM's plan landed first naming a different release, as
   when the pin moved between two VMs' boots, the VM stops its units and starts
   them again on the plan's release. A session is open while its plan exists,
   its stop does not, and its close has not passed by a margin, so a run that
   died before writing its stop never blocks the next drill. The price is the
   one thing a lease's expiry told, that every supervisor had gone quiet,
   which monitoring's supervisor-heartbeat alarm reports instead.
   1. **The plan's lease.** The owner ruled on 2026-11-02 that the
      supervisor keeps several instances, and a lease so that only one
      changes the plan at a time, and later that day that the lease carries
      the run's status, which closes the gap a frozen holder would leave. So
      the lease is one blob per run, kept for the run's life, holding the
      run's status, running, halted or stopped, beside its holder and
      expiry. The first supervisor instance creates it, with the status
      running, before it writes the plan. An instance takes it by a
      conditional write on the blob's tag that names itself as holder,
      before it reads the run's state for a write that changes the run: the
      plan, a halt as a worker's result, a halt of the run with a halt for
      every decision left in the plan, and the run's stop. A halt of the run
      and the stop are first a change of status, made by one more
      conditional write on the tag the instance holds, and only then written
      as the blobs whose events reach the workers. It releases the lease by
      a conditional write that clears the holder rather than by deleting it,
      so the status stays. Each blob a change writes is create-only on its
      own key, which stops two writers taking one key but not two instances
      acting on one read of the run, such as one storing the stop while
      another halts the run. The lease stops that, and because the lock and
      the status share one blob, a takeover rewrites it, so any write a
      frozen holder sends afterwards names an old tag and S3 refuses it with
      a 412, a write already in flight included. A holder that dies after
      its change of status and before the blobs that announce it leaves the
      status saying what happened, so the next holder writes those blobs,
      which land once. A halt as a worker's result needs nothing more,
      because it shares its key with the worker's result, so whichever lands
      first counts. Another instance that needs the lease does not wait for
      it, as the account's lease below says, and a holder's lease is taken
      over once it expires unrenewed, as the account's is.
   2. **The account's lease.** Every execution instance receives from
      execution's queue, and the owner ruled on 2026-11-01 that an instance
      holds the account's lease only while it changes the account. It takes
      the lease before it re-reads the journal for a submit or a cancel, and
      deletes it once the journal holds the broker's answer or the order's
      doubt. A plan of several orders therefore takes the lease once per
      order, so a pull's cancels wait for at most one order in flight.

      The owner ruled on 2026-11-02 that work waiting on a held lease is
      woken by the lease's own event, so nothing reads storage on a clock
      to wait for it. Three rules carry it.
      1. **No handler waits.** A handler that finds the lease held puts its
         item back on the queue with a short visibility delay, two seconds
         by this doc's proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10), and is free at once.
      2. **The release wakes the work.** Deleting the account's lease is a
         write whose S3 event reaches execution's queue, as every route
         does. Whichever instance receives it checks the work that waits on
         the lease, in the plan's order: the kill switch's cancels, a
         stranded leg's cancel, and a send stage's next planned order or
         cancel. It takes the lease for the first and does it.
      3. **Any lease holder sends a planned order.** The claim keeps two
         instances from planning one decision, and once the planned list is
         journaled, whichever instance holds the lease may send its next
         order or cancel while the claim's runner is alive, under the same
         checks before the broker call. Each order is guarded three times:
         its `sending` entry is journaled first, its client order id is
         fixed by the plan, and the lease's tag is checked right before the
         call. Once the runner is gone, the fifth item below applies.

      A lost release event delays the work only until the item it was put
      back with comes back, which is SQS's own redelivery rather than a
      read on a clock. An item returned more times than a written limit, read
      from SQS's own receive count, alarms with the lease's holder. The price
      is that one decision's orders may go out from different instances.

   A holder record names the VM, its boot, the worker's container, its
   restart count and the handler, rather than a process id, which means nothing outside one
   container. A lease whose holder is the same VM on an earlier boot, or a
   container the host shows is gone, is cleared by the next process that
   needs it, and the clearing is journaled. The owner ruled on 2026-11-01
   for automatic failover across machines, so any process, on any VM, takes
   over a lease that has gone unrenewed past its expiry and a margin for
   clock skew, by the same conditional write naming the expired lease's
   tag, which a revived holder's renewal can then never overwrite. A
   handler frozen on the lease holder's own VM is taken over this way too,
   without waiting for the host to show its container gone. The takeover is
   journaled and alarms. A frozen holder that wakes finds its renewal
   refused, and stops that action, and execution's
   check of the lease's tag right before every broker call, rule 9's fourth
   item, refuses its next send. A VM whose supervisor instances find their
   own last heartbeat older than the limit after which the other VMs count
   it as gone, as after a freeze, stops its units, which start again as a
   fresh member, as architecture.md's rule 11 of `## Where it runs` says.
   The price is one window. A holder that
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
      is stored, execution checks six things.
      1. The kill switch is absent.
      2. The order's strategy is not switched off. As with the kill
         switch in rule 10, any answer other than "no such blob", a timeout
         included, reads as switched off, with the reason "strategy off
         switch unreadable", and that halts the decision as an unreadable
         control does. So execution's role can read and list
         `accounts/<account>/strategies/`.
      3. No halt of the run or of the account is stored, a list of
         `accounts/<account>/halts/` that is not empty reading as a stored
         halt, and the plan's lease holds the run's status as running, so a halt or stop the supervisor has
         recorded but not yet announced also stops the send.
      4. Its own send-stage result key is still empty.
      5. The account's lease still carries the tag that the holder's own
         last take or renewal of it returned.
      6. The market-on-close cutoff has not passed, for such an order.

      Any one failing journals the
      order `Abandoned`, sends nothing, and halts the decision, except the
      second when it finds the off switch, which abandons only that
      strategy's orders and leaves the account's other strategies sending, since a strategy's off switch,
      `accounts/<account>/strategies/<strategy>/off.json`, stops one
      strategy without a deploy and never reads as a target of zero, as rule
      1 of `### Every change can be undone or switched off` asks. So a run the
      supervisor has halted, a switch pulled mid-loop, or a lease another
      execution has taken over stops the next send, even when the message
      saying so is lost or unread. A cancel has checks of its own, because
      the cancels that protect the account run exactly when a switch is set
      or a halt is stored. The kill switch's cancels, and the cancel of a
      stranded leg by an instance taking over a send stage, check only the
      lease's tag. The
      cancel that keeps a pair whole checks the lease's tag and the cancel
      cutoff. A plan's own cancel, the cancel ahead of a replacement
      included, checks four things: no halt of the run or of the account
      is stored or held on the plan's lease, its own send result is still empty, the lease's tag, and the cancel cutoff.
      A stored halt stops it, the one cancel a halt stops, under the owner's
      2026-11-04 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). This doc's reason is that a halt stops the
      plan, while the order stays counted at its worst case and the kill
      switch can still cancel it. A check that fails journals the cancel as
      stopped, which returns the order to the state it was in, still
      counted, and ends rule 6's clock, because only a failed check proves
      nothing was sent, as this doc proposes for [#10](https://github.com/l3a0/ithildin-runtime/issues/10). A plan's cancel
      skips the strategy's off switch, so a switched-off strategy's
      cancels still go out, and the replacement itself is a submit, which
      checks all six. A cancel skips the risk gate's limits on purpose: every open
      order is already counted at its worst case, so a cancel can only lower
      each counted limit.
   5. **No instance sends more for a send stage whose runner died.** Before
      its broker fetch, a send stage writes a create-only claim at
      `runs/<deployment>/<run>/<decision>/execution/send/claim.json` naming
      the handler running it: its VM, boot, container, restart count and
      handler. The entry that journals its planned orders names the same
      handler. A send stage that finds a claim or a planned entry it did not
      write never syncs or plans for that decision, so two instances that
      both receive its event never both plan. Another instance, or the same
      one restarted, reads the claim rather than the planned entry to find
      the runner, and takes that send stage over only once its runner is
      gone. On the runner's own VM that means the host
      shows its container gone or restarted. From another VM it means the
      runner's VM counts as gone: no supervisor instance on it has written a
      heartbeat within the limit, or the Auto Scaling group reports it
      terminated, as architecture.md's rule 11 of `## Where it runs` says. Each worker instance runs under one fixed container name, its
      component and its number, removed when it stops, so a gone container is a fact the host reports, not a guess
      from a stopped heartbeat. A send stage whose runner is alive is left to
      it, which also covers SQS handing a second instance a copy of the
      runner's message. An instance that starts, and one that receives a
      send stage's event, acts in this order:
      1. Check the kill switch, and if it is set take the account's lease and cancel every resting order in the account, stamped or not, journaling each cancel first.
      2. For a send stage that has a claim and no result, and whose runner
         is gone, take the account's lease, settle each order in doubt by its client order id, journal every planned order and replacement without a `sending` entry as `Abandoned`, settle every planned cancel left `CancelSent` by a lookup,
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

   An order in doubt also blocks the next order beside it, as rule 3 of
   `### Every fact has one writer` asks. The gate refuses any order on an
   account and instrument that hold an `InDoubt` order, so a decision never
   sends a second order beside one the platform cannot see. An order
   alarms the moment it is first journaled `InDoubt`. One still unsettled
   a written time after it was sent halts the run and alerts the owner,
   rather than waiting out a time in force that a good-till-cancelled
   order may not reach for weeks. By this doc's proposal for
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10), that time is
   the smaller of 30 minutes and the time left before the cancel cutoff,
   and a supervisor deadline enforces it, reading storage before it acts,
   as any expired deadline does.

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
    creates the blob, journals the halt, raises the alarm, and cancels every resting order in the account, stamped or not, journaling each cancel first, as the owner ruled again on 2026-10-31. This doc adds how. The switch can be pulled from anywhere, and creating its blob is an event that reaches execution's, the supervisor's and monitoring's queues, whoever pulled it. Whichever execution instance receives that event takes the account's lease, which is free after at most one order in flight, or wakes on the lease's release as rule 9 says, and cancels every resting order in the account, stamped or not, through the order path, and alarms if a cancel fails. Every other instance stops sending at its next check of the switch. `ithildin switch pull` then reads the journal once at a deadline before the cancel cutoff. The blob's event reaches execution directly, so a pull reaches resting orders even with the supervisor down. No worker reads the switch on a clock, as the owner ruled on 2026-10-31. A
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
   the one cause every account's hard caps blob names among the causes
   that hold it, in every environment, under CLAUDE.md's third exception,
   a guard whose price is paid by building it late. Rule 7 says how that
   value is read.

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
   defence to test, because the platform never resends an order and settles
   doubt by lookup.

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
a reader, a prefix and an event kind. Execution's routes include the deletes
under `leases/accounts/`, so a lease's release reaches its queue. The routes generate one EventBridge
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
the organization to about 1.9 million. Consolidated billing shares SQS's
free tier across the organization's accounts, so its first million
requests a month are free once for the whole organization, and each further million costs 0.40 US dollars, as its
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
   decision it concerns, except the kill switch's event, which becomes an
   item of its own, as item 8 says. On waking and on starting it also adds every
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
   handlers go on. A handler that ends for any reason, an error included,
   first journals its order's doubt if it was sending, then deletes its
   lease, so the next holder never waits out a lease nobody renews. If the halt cannot be written, the process exits and the
   host restarts it, as rule 5 says.
6. **A hung handler stops the heartbeat.** Each handler reports progress at
   every blob write, journal append and broker call it finishes. No handler
   waits for a held lease, as rule 9 says, so a lease never holds a handler
   long enough to stop its reports. The loop
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
   supervisor. In execution, one handler of each instance is kept for the
   kill switch's work alone, so a pull's cancels run while another handler
   is in the middle of a plan. The switch blob's event becomes an item of
   its own, the account's kill switch rather than a stage and a decision,
   and only that handler takes it. Any other message the loop receives for
   that handler goes back to the queue at once with visibility 0, so
   another instance can take it. Two handlers of one
   instance meet at the account's lease the way two instances do, because
   the lease's holder record names the handler.

The price is a thread pool in every worker, a progress report at every
step, and an order rule for the stages that read the journal. A test runs
test 1's backtest with every pool at size one and again at size four, and
expects the same journal and the same blobs byte for byte, so concurrency
that changes an output fails CI. A second test runs test 2's replay with
one instance of each component and again with three, and compares the
journal and the blobs byte for byte in the same way, so a race between
instances fails CI too.

No worker on a VM receives until a supervisor instance on that
VM has read the run's plan and found its VM on the plan's release, so a VM outside the run never
takes the run's events off a shared queue. The VMs inside it share every
queue as competing consumers. Execution receives
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
   switch's cancels, and alarms when one expires first. It cancels the
   supervisor-heartbeat deadline when it hears the run's halt or stop, so a
   failed run, whose supervisors exit cleanly, raises one alarm rather than
   a second for the heartbeats that stopped after it.
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
2. Long polling's requests, about three a minute for each waiting worker, which pass SQS's free million a month at this scale, at about 35 cents a month once live runs.
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
that landed, the tag a create returns, the stored time a lookup returns, a
conditional rewrite with a stale tag, the new tag a conditional rewrite
returns, and a conditional delete with a stale tag. A rule the folder breaks fails a
test. A backtest's blobs vanish when it ends, which the premise
allows, because a backtest can be regenerated from committed data and the
pinned release.

The clock adapter is read in three places only: the data worker, the
simulator and execution's send stage. The data worker reads it to
decide when a decision's snapshot is written, which is as fast as possible
under the history clock and at the decision time under the wall clock. The
simulator's `clock()` reads the `Clock` handed to it, so a
backtest never halts on skew between two clocks.
Execution's send stage reads it before every send, to check the cutoff for
the strategy's orders, as `### Strategies, schedules and decisions` says.
Under the history clock that reading is the decision time, so a backtest
never meets the cutoff, and a drill sets a test clock past it instead. The
supervisor is apart from the three. It reads wall time for deadlines in
every mode, because a deadline guards against a hung process rather than
shaping what a strategy sees, and it reads it through a timer port of its
own, always wall time and faked in tests, so a deadline never reads the
history clock and never bypasses a port. Apart from those places, a worker reads wall time only to key its heartbeat and, in execution and monitoring, to set the deadlines of `### How a component learns of a change`, none of which shapes what a strategy sees. Otherwise the workers move on only when messages arrive.

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
never rewritten, a lease aside, so a key or a stored format changed later leaves every reader
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
   path. The order holds across instances because it rests on stored keys,
   not on which instance ran a stage. Send for decision N needs recover for
   N, which needs the stored send result for N−1. An instance that wakes
   checks the keys of every decision its component has not finished, so
   whichever instance receives the event for the last missing input runs
   the stage, and one that finds an input missing drops the message. A lost
   event leaves the stage to the supervisor's deadline, which halts the
   decision rather than sending out of order. Every stage's deadline fits
   inside the gap to the next decision time.
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
      run or of the account is stored or held on the plan's lease, its own
      send result is still empty, the account's lease
      still carries its tag, and the cancel cutoff has not passed. A cancel
      whose answer is lost stays `CancelSent`, and rule 6 of
      [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs)
      says when a lookup settles it, when it is sent again, and when it
      halts new orders. No stage resends a plan's cancel yet, under the
      owner's 2026-11-04 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), because that path has run zero times and a daily
      market-on-close plan leaves nothing resting at its next decision. The
      cancel stays `CancelSent`, counted at its worst case, and the next
      recover stage settles what it can by lookup, so rule 6's written time
      decides the rest. A strategy that rests orders across decisions names
      the stage that resends first, and the order manager plans no cancel
      or replacement for an order already `CancelSent`.
   2. **A replacement waits for its cancel's outcome.** The order manager
      journals the old order's outcome, any fill included, and only then
      journals the replacement's intent and asks the risk gate, so the gate
      sees what the old order filled and counts it at its worst case until
      then. A cancel refused because the order already filled, or a
      replacement whose quantity comes to zero, journals the replacement
      `Abandoned`, and so does a cancel a check stopped. A replacement whose cancel is not confirmed after one lookup by client
      order id is journaled `Abandoned` too, and the next decision plans from
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
      the cancel cutoff passes. When one leg is rejected, unsent or past the cutoff while another is accepted, execution cancels the accepted legs, journaling each cancel first. The same holds for a pair whose legs are an order left resting and a replacement: when the replacement is refused, abandoned or past the cutoff, execution cancels the resting leg. So does a pair whose one leg's plan cancel is confirmed while the other's is stopped: execution cancels that leg with the cancel that keeps a pair whole, which a halt does not stop, before the stage writes its halt, as this doc proposes for [#10](https://github.com/l3a0/ithildin-runtime/issues/10). A pair-keeping cancel whose answer is lost is looked up and resent by the pair step under rule 6, inside the send stage's deadline and holding no lease between tries, under the owner's 2026-11-04 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), because the pair step comes after the planned orders and holds none of them back. One still unsettled at that deadline stays `CancelSent`, and rule 6's written time decides the rest. A leg whose plan cancel is left unsettled stays under rule 6, and a leg whose partner's lost plan cancel settles later gets no pair check yet, deferred with that cancel's resend under the same ruling. When execution crashes with its send stage
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
review comments on 2026-11-01, in four points, and amended the fourth later
that day.

1. One account holds one portfolio, which several strategies may share.
2. One scale unit is a complete set of components, the supervisor and every
   worker, paired to one portfolio.
3. The platform scales out by adding portfolios, each a scale unit of its
   own.
4. Every component, the supervisor included, may run as several
   instances, and the component's one queue is the router that hands each
   message to one of them. A queue of each instance's own behind a separate
   router is turned down, as the register says.

The rest of this part is this doc's proposal for
[#22](https://github.com/l3a0/ithildin-runtime/issues/22) and
[#11](https://github.com/l3a0/ithildin-runtime/issues/11). SQS lets several
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
runs several instances too, which point 4 above now carries, and on
2026-11-02 that they take a plan's lease to change the run, as rule 9
says. So a deployment runs 21 worker containers and
three supervisor containers, and shadow and paper together run 48. The owner ruled the same day that each environment can run as a
cluster of three VMs, one in each of three availability zones, and on
2026-11-02 that this cluster is the scale-out path, for a design load the
directive of 2026-11-03 sets at 10 times slice 1's, while slice 1 starts on
one VM per environment, as
[Design for 10x, plan the rewrite before 100x, build at 1x](#design-for-10x-plan-the-rewrite-before-100x-build-at-1x) says. So in
slice 1 one VM runs all 24 of its deployment's containers, and the cluster
would spread them 8 to a VM. Shadow runs on machines of its own rather
than sharing paper's, as the owner ruled the same day. The supervisor's instances compete for its queue
like any component's, and each of its writes, the plan, a worker's halt,
a halt of the run and the run's stop, is create-only and made under the
plan's lease, so one instance changes the run at a time and a frozen one
cannot change it after a takeover. In slice 1, losing a process leaves
the other two instances of its component running, and losing the VM or its
zone stops the environment until its Auto Scaling group of one starts a
replacement. The cluster would let the other two VMs carry on, as
architecture.md's rule 11 of `## Where it runs` says.

Several instances of one component need two rules a single instance did
not.

1. **A deadline that expires reads storage before it acts.** The event that
   would clear a deadline can reach a different instance from the one that
   set it, so each instance's timer expires on its own. Before it alarms or
   halts, an instance reads the key the deadline waits on once, as the
   supervisor's deadlines already do, and acts only if what it waits for is
   still missing.
2. **One cause raises one alarm, or rarely two.** Monitoring writes the
   file naming the halt it is handling before it alarms, as rule 7 says,
   and that write is create-only. Only the instance whose write lands sends
   the alarm, and an instance whose write S3 refuses sends nothing, so three
   instances that all see one cause raise one alarm. The sent record that
   follows the push lets another instance push again for a claim whose
   writer died before pushing, as rule 7 says, so a cause can alarm twice
   and never not at all. A missed deadline's alarm goes through
   the same kind of file, named for the deadline and its decision.

The price is three times the containers, about 1.2 million long polling
requests a month on shadow and paper together and about 1.9 million once
live runs, past SQS's free million, and in slice 1 one VM per
environment with memory for 24 Python processes. At roughly 100 MB each, an
estimate to measure on [#11](https://github.com/l3a0/ithildin-runtime/issues/11), that is about 2.4 GB, and
about 0.8 GB on each VM of the cluster once load calls for it.

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
   builds the one holding-halt check for every cause, because live holds on
   every cause, and the hard caps blob's set of holding causes decides
   which apply, as rule 7 says. So holding a shadow or paper account on
   more causes waits only for the owner to widen that set. Once a day, a
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

## Data structures

The owner asked on 2026-11-02 that the design name the formal data structure behind each part and say why it fits, and ruled the same day for four upgrades this section marks. Each structure is named for what it is, so a reader can look up its known properties rather than reverse them from the prose.

| Part | Formal structure | Why it fits |
| --- | --- | --- |
| The journal | A hash chain: an append-only log in which each entry carries the SHA-256 of the entry before it, numbered in sequence | One writer per account appends in order, and a chain shows any edited, missing or reordered entry on a walk from the start |
| Journal checkpoints and the head anchor, an upgrade | Verified checkpoints of the hash chain, and its head witnessed in a second, independent store | A checkpoint bounds the walk at startup to the entries since it, and a head held in the second account shows a rewrite of the whole chain, which hashes alone cannot |
| The blobs of one decision | A Merkle DAG: each blob's header names its inputs by key and SHA-256, and each decision result lists its outputs by hash | Every output's history can be checked to the bars it was made from, as Git checks a commit's |
| A decision's root hash, an upgrade | The root of that Merkle DAG: the SHA-256 of the decision result | One hash covers the decision's whole history, so two copies, or a rerun and the original, compare by one value |
| The book | An event-sourced state: a left fold of the journal's entries | The journal is the record, and the book is derived, so it can always be rebuilt and never disagrees with its source for long |
| Book snapshots, an upgrade | A materialized view of that fold, keyed by the sequence number it was folded through | A rebuild replays only the entries after the newest snapshot, which keeps replay short as the journal grows |
| The account's lease and the plan's lease | A compare-and-swap register with an expiry, held through S3's conditional write, whose lease number fences each journal entry | It gives one holder at a time without a lock server, and the number marks every entry with the grant it was written under |
| Claims, alarm records, halts and outputs | Idempotency keys: a create-only write at a key fixed by the work, so the first write wins | Duplicate events and several instances collapse to one outcome without coordination |
| Each component's queue | An at-least-once work queue with competing consumers | Events are only hints and the state lives in the keys, so the queue needs no ordering and any free instance can take an item |
| An order's lifecycle | A finite state machine whose transitions are journal entries | Each move is recorded before it happens, so replay reaches the same state |
| The stages within a decision | A directed acyclic graph of stages, declared in one table in `runtime` | One declaration drives both the event routes and the order stages wait in |
| The broker's request budget, an upgrade | A token bucket per execution instance, one for its share of orders and lookups and one for its share of the cancel reserve | It refills continuously rather than at a minute's edge, so a burst cannot spend a minute's share in a second, and a refused request knows how long to wait |

The four upgrades are this doc's proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and [#11](https://github.com/l3a0/ithildin-runtime/issues/11), as the rules they sit in say. Five structures were weighed and turned down, as the register records: a Merkle tree for the journal, CRDTs, a consensus log, an ordered queue per account, and a Bloom filter for duplicate fills.

## Design principles

A principle earns a place here only when a design can pass or fail it, which is the test *The Art of Scalability* sets for its own (Abbott and Fisher, 1st ed., Addison-Wesley, 2009, Kindle location 4588, cited below as AoS L4588). A principle like "scales infinitely" fails that test, because no design can be checked against it. Each rule below names the check a review runs, and the reason for it.

The owner's standing directive, adopted on 2026-11-03, is Jeff Dean's rule as *Building Microservices* quotes it: design for 10 times the first load, plan to rewrite before 100 times, and build for the first load (BM L4428, cited below). A design passes when reaching 10 times takes configuration and more machines, and it writes down, for growth beyond that, the load at which planning a rewrite starts, what the rewrite would replace, and what must survive it unchanged, which here is captured data, the journal, their formats and the partition layout, and the tests of the risk gate's refusals, the halts and the kill switch. Two alarms are built at the first load. One trips at 75% of each written scale limit in the running system, beside the test of rule 2 of [Design ahead](#design-ahead-and-write-down-where-the-build-stops). The other trips when measured load on a named load parameter, read on its busiest key, reaches the level written for planning the rewrite, and pages the owner through the alert path once per parameter, level and environment, naming all three, and the owner files the issue. The page is recorded where a change of release cannot lose it, it reports under the first monitors' heartbeat, and a test feeds load at the level and asserts one page, then starts a second session above the level and asserts none. The level sits below the most that configuration alone reaches, with a stated lead time. The architect writes it beside the scale limits, and the operator runs the alarm. The book states the same idea as designing every major system so it can be split along at least two axes, while building only the split that today's load needs (AoS L4684, L8205). Its three axes are clones of the whole system (x), a split by function or by kind of data (y), and a split by a key such as account or symbol (z) (AoS L7432).

*Designing Data-Intensive Applications*, cited in [One runtime](#one-runtime), adds the rules for data that the first book leaves out: what to do about faults, clocks, retries, concurrent writers and records that must outlive every bug. A citation like DDIA L9286 is the Kindle location of one of the owner's highlights or of a screen of the book's full text. One like DDIA L\~14160 points into the full text at a location good to about one screen, because the reader shows this book's Kindle pages rather than its locations, so the location of a screen with no highlight on it is interpolated. Where its rules restate one already here, the rule below cites both books rather than appearing twice.

*Code Complete* adds the rules for the code itself, which both architecture books leave out (McConnell, 2nd ed., Microsoft Press, 2004). A citation like CC p. 197 is a printed page of that edition. Its case for writing each rule as a check rather than a habit is measured: no single way of finding defects reaches more than about 75% at its typical rate, unit and integration testing reach about 30 to 35%, and two methods combined find nearly twice what one finds (CC p. 470, p. 471). So each of its rules below names the tool, test or review that checks it.

Sam Newman's *Building Microservices* (O'Reilly Media, 2015) adds the rules for where modules are cut, how a release reaches each environment, and how the running loop is traced and watched. A citation like BM L4252 is the Kindle location of one of the owner's highlights or of a screen of the book's full text, and BM L\~932 marks a location good to about one screen, as for DDIA. The book argues for splitting a system into services, and its own author's team found splitting too soon costly. The owner ruled on 2026-10-29, on [#7](https://github.com/l3a0/ithildin-runtime/issues/7#issuecomment-6505752788), that each component runs as its own process and passes its output to the others through files. So the book's rules apply here to the boundaries between those components, to splitting a component further, to releases, to tracing and watching the running loop, and to the handling of each dependency.

Viktor Farcic's *The DevOps 2.0 Toolkit* (2016) adds the rules for checking a release where it runs, building machines from code, healing a running system without hiding a fault, and gathering its logs. A citation like DT p. 247 is a printed page of the book's PDF edition. Its tools, such as Vagrant, Ansible, Docker Swarm, Consul, nginx and Jenkins, are the book's examples rather than choices made here.

So these rules are checks on a design, and a review applies all of them. A module builds the mechanism a rule names when the path it guards first runs, which the walking skeleton does for the order path, and no sooner. Three kinds of mechanism are built from the first run whatever the count, because CLAUDE.md's ranking rule exempts them.

1. Whatever keeps data that cannot be recaptured from being lost: disk headroom for capture, capture's place outside every halt and its refusal to drop, the guard that keeps cleanup rules off recorded data, captured data stored as received with its timestamps and its own receive sequence, a schema identifier and a checksum on every recorded record, each journal entry acknowledged by durable storage before its send, a copy off the machine from the first session, a bound on how long a capture segment stays open, the guard and the plan check that keep infrastructure code from deleting, overwriting or recreating a store of recorded data, the journal's place outside the log pipeline, and a journal store's first-start record.
2. Alarms and halts, which read zero while they are healthy, including the 75% and rewrite-planning alarms, the clock-offset and sync-age halts, the lag alarms on capture and on the off-machine copy, the scheduled read-back of recorded data, the drift alarm and the alarm on a scheduled check gone silent, the restart cap's halt, the page at any other component's cap and on a restart during a session, the pages for execution past its cap and, once this platform runs capture, for capture, the halt on an unreadable deployment state and the alert on an unreadable release record or switch, the halt on a store gone missing or found empty or behind its copies, on a comparison with a copy that cannot finish, or on a halt-state store created again, execution's halted restart on an unreadable or absent switch or count, the page on restarts past a written count over a session or a day, the alert on a switch found off at a session's start, and the buffer-fill and shipping-lag alarms.
3. The guards on the order path, whose price is paid by building them late: the sizing rule, the account's lease with its tag check before every broker call, the lease number in every journal entry and client order id and its continuation in a lease store created again, the decision's id in every record and client order id, applying each fill once, the block on orders in doubt, and the halt state read just before each send. The checks in CI that hold the rules for the code itself, such as the import rule test, strict linting and type checking, and the run under `python -O`, belong here too, because strictness switched on late leaves a backlog of code written to a looser standard.

### Design ahead, and write down where the build stops

1. **The next scale step exists as a written design, and the only code built ahead of it is configuration.** The book's example is a data-access layer designed so it could reach several databases while one runs. Its migration scripts are written when the need arises, and the design is what lets a second database take weeks rather than months (AoS L1078). Here the only code built ahead is a configuration value, such as the map of partitions to machines, whose other settings a test runs. A script or path offered later as the next step has run once, in a drill on a copy, before anything relies on it.
2. **Every implementation states its written scale limit, and an alarm or a test trips at 75% of it.** Code built for "any number of read nodes" that handles five has a limit of five (AoS L1174). The limit is stated beside the code that enforces it, and 75% is the headroom ceiling in rule 4. On the order path, reaching the limit halts.
3. **A partition key and a fixed partition count are set from the first commit.** The book finds a split by function "not so easy" to design room for in advance (AoS L7781), so module boundaries and data ownership are settled early. Captured data is keyed by its source, the vendor's identifier as received, and the date it was recorded. The key only routes a record and never identifies it, and a write into a key that already holds data adds a new vintage rather than replacing one. A snapshot that a decision read is a capture of that read, keyed by its run rather than by source and date, and written once. The trading loop is keyed by account. Only the identifier part of a key, the source and vendor identifier or the account, maps to a partition through a hash, and the date orders records inside the partition, since hashing scatters keys that a range read wants together (DDIA L6235). That hash is the same in every process. DDIA warns that a language's built-in hash can differ from one process to the next, naming Java's and Ruby's (DDIA L6211), and Python's `hash()` on a string does the same. AoS treats the count of a split by key as a number to raise later (AoS L7781). DDIA finds that changing the count moves data, so a design built on fixed partitions sets the count when the data is first laid out and grows by moving whole partitions between machines (DDIA L6411, L6443). Routing by the key modulo the machine count moves most keys whenever a machine is added (DDIA L6418). So the count is fixed at the start, set well above the machine count and chosen for 100 times the first load, the ceiling a rewrite must come before, because the partition layout is part of what must survive a rewrite and changing the count moves captured data, and no higher, since each partition carries its own overhead (DDIA L6443).
4. **Plan headroom per component, at 50% of measured capacity and never above 75%.** The measured maximum can be wrong, growth estimates can be wrong, and behaviour near the limit turns erratic (AoS L4449, L4475, L4480). Headroom is computed for each component, never for the system as a whole (AoS L4496). Here that means capture disk, journal volume and the broker's API rate limits. A share of the rate limit is reserved for cancels, the kill switch's among them, and new orders and queries never draw on it.
5. **Every written scale limit names the load parameter it is measured in, and the design names the operations it assumes are common.** DDIA's case is that a scalable architecture rests on assumptions about which operations are common and which are rare, and that a wrong assumption makes the scaling work wasted at best and harmful at worst (DDIA L627). It also finds that a fast-growing service usually rethinks its architecture at each tenfold rise in load (DDIA L598). That finding matches the owner's directive, which expects a rewrite before 100 times. A design for 10 times the first load holds only along the parameters it names, so it names them, such as orders per second per account, symbols captured, and messages per second at the open, and a review checks them against measured use. Load along a parameter the design never named is the case that forces a rewrite. Stateful parts stay on one machine until cost or an availability target forces a split, which DDIA reports as the common practice for databases until recently (DDIA L614). The same chapter finds that an early product does better to iterate on its features than to scale for load it may never see (DDIA L621), which is the build-at-1x half of the directive. A written limit holds for the heaviest key, not the average. DDIA's case is a service whose users average about 75 followers while one user's post reaches over 30 million, so the spread of followers per user was the load parameter that mattered (DDIA L464, L473). Here that means the busiest symbol at the open, and the largest partition of any partitioned job, which finishes only when its slowest partition does (DDIA L12589).

### Keep the order path isolated

The **order path** runs from the target portfolio through the risk gate and the journal to the broker adapter. A **synchronous dependency** on it is a call that leaves the process or waits on input or output. A call between modules inside the process, through their shared interfaces, is not one.

1. **Nothing that does not earn money can stall the order path.** The book's first rule of fault isolation is to protect whatever is closest to the money (AoS L7130). Monitoring, reports, dashboards and research reach the order path only asynchronously, and are allowed to fail. The book's own case is a monitoring database whose slowness backed up every user query until the site went down (AoS L8616). The loss stop and the kill switch live in the risk gate and are read on every order, never in monitoring.
2. **Count the synchronous dependencies on the order path and keep them to the minimum.** Availability multiplies along a chain. One hundred parts at 99.999% each give about 99.9% together, which turns about 5 minutes of downtime a year into over 500 (AoS L8698). The minimum here is the input data, the risk gate, the account's lease, the journal and the broker. The controls and the halt checks before each send are reads on the same store that holds the journal, so they count as the journal's dependency rather than a sixth. The journal write is synchronous on purpose, because hard limit 4 puts it before the send.
3. **A fault boundary is drawn around the whole loop, never around a layer, and every crossing is listed.** In the book, a unit isolated from faults shares nothing with its neighbours, takes no calls across its boundary, and carries every transaction from start to finish inside itself (AoS L7088, L7101, L7113). A boundary drawn around all the databases, or all the risk checks, only groups like parts, and one failure there still reaches every transaction (AoS L7117). The book lets one boundary hold one tenant or many (AoS L7147). So the first build is one boundary holding every strategy sleeve, and a split by account comes when load or risk calls for it. Two strategies in one broker account share buying power, so they never sit in different boundaries. Three crossings are allowed, and any other breaks the boundary.
   1. Captured data flows in, read only and one way, and a stale feed halts the sleeves that read it.
   2. Monitoring, and the copy of recorded data to its store off the machine, flow outward only, apart from the read at a start that crossing 3 names.
   3. Operator controls, and the time reference the clock-offset halt reads, flow inward to every boundary. A control is the kill switch, a strategy's off switch, the hard caps, a person's override, a halt's clear, a fired loss stop's clear, the release pin or a component's restart and scaling switch, and a control not named here breaks the boundary like any other crossing. A start's read of a copy of the journal flows inward once, and rule 3 of [A release is checked where it runs](#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault) says what a start that cannot finish it serves. Each control meets four conditions, except that the restart and scaling switch keeps its record and key where [#32](https://github.com/l3a0/ithildin-runtime/issues/32) names, under rule 4 of [A release is checked where it runs](#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault).
      1. It is a blob at a fixed key that only named credentials write.
      2. The loop reads it at a stated point: the switches, a fired loss stop and the hard caps before each order, the release pin at a session's start, and an override or a clear at the next decision's sync.
      3. Every write is journaled under the identity of the writer.
      4. A boundary that cannot read a control, or the time reference, halts.
4. **The risk gate holds every new order to the same limits, whatever sent it, and only the execution worker holds a credential that can send.** Hard limit 2 already sends every order through the gate. Newman's confused deputy adds the reason no caller may carry its own authority past it: a service tricked into using its authority on behalf of a caller who should not have it (BM L3634). So no caller carries a flag, a role or a credential that lets its orders skip a limit. A strategy's orders and an operator command's are new orders held to every limit, and a replay runs against the simulated broker. The loss stop does not unwind in slice 1, under the owner's 2026-10-29 ruling on [#7](https://github.com/l3a0/ithildin-runtime/issues/7), and an unwind, once [#7](https://github.com/l3a0/ithildin-runtime/issues/7) adds one, is a new order held to every limit. Replay, backtest and shadow hold no broker credential, and the paper and live accounts each have their own, and the owner's 2026-10-29 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10) puts the broker key in Parameter Store, where `docs/architecture.md` proposes that only the execution worker's user reads it. Every cancel is exempt from the limits and the input checks, and from nothing else, because open orders are counted at their worst case, so a cancel cannot raise a counted limit, as rule 9 of [Processes and files](#processes-and-files) says. A plan's own cancel also stops at a stored halt, as that rule says. Slice 1 sends no protective order, and no unwind unless its tally of stranded legs calls for one. When [#7](https://github.com/l3a0/ithildin-runtime/issues/7) adds an unwind, a cancel of a protective order that the kill switch does not make is held to the limits, because it raises exposure, and the gate gains the classification that says which cancels those are, under the owner's 2026-11-04 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). Every cancel waits for its journal entry's acknowledged write, as an order does, and is sent by the lease's holder and drawn from the rate-limit share kept for cancels. A replacement is a new order held to every limit, sent in the sequence [#10](https://github.com/l3a0/ithildin-runtime/issues/10) rules. Rule 4 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs) says which cancels raise exposure.

### Modules are cut by capability, and a component splits further only with evidence

1. **Each module is cut along a capability of the business, its published output is its interface, and no other module reads its internal state.** Newman draws boundaries along bounded contexts, the parts of a domain that keep their internal models to themselves (BM L740), and finds that services sharing one database lose both cohesion and loose coupling (BM L\~932). Here the modules are the charter's module table, and under the owner's 2026-10-29 ruling each passes its output to the others as files. A module's published files, in their versioned formats, are its interface, and anything else it stores is its own. The journal is appended only through its own writer and read only through its reader, wherever [#22](https://github.com/l3a0/ithildin-runtime/issues/22) places them. Backups, the scheduled read-back and replay take recorded files whole, which is a copy rather than a read of a module's records, and any parse of a copied file goes through the owning module's reader, from wherever [#22](https://github.com/l3a0/ithildin-runtime/issues/22)'s package diagram places it. The import rule test that [#22](https://github.com/l3a0/ithildin-runtime/issues/22) specifies holds the code side. A cut along a technical layer, such as one that only wraps storage, needs a named reason, such as a performance goal, because the book makes the technical seam the second driver, never the first (BM L866).
2. **A component is cut into new components only with a named reason and the history of changes across its boundary.** Newman's own team split a product into services early, merged them back after a few months, and split it again a year later along boundaries that held, and he finds splitting too soon costly, above all in a domain the team is new to (BM L777, BM L796). So a proposal to cut a component into new ones with a new interface between them names the load, isolation or credential that calls for it and shows how often the component's interface changed. Clones of a component and splits by key, such as competing execution instances or a partition by account, follow [Design ahead](#design-ahead-and-write-down-where-the-build-stops) and need no such proposal, and neither does a split the owner has already ruled on.

### Faults are expected, and the platform checks itself while it runs

1. **Every fault the design says it handles is injected on purpose, in replay.** DDIA separates a fault, one part leaving its specification, from a failure, the whole system no longer serving (DDIA L293). It finds many critical bugs in poor error handling, and recommends causing faults on purpose to keep that handling exercised, and testing even unlikely ones (DDIA L300, L8461). This doc reads "poor" as handling that rarely runs. Here the simulated broker and the replay harness can inject twenty-two faults, the harness working on its own copy of every store and record it injects on, and a test runs each through the full loop and fails when the handling is removed.
   1. An acknowledgment of an order or of a cancel that never arrives, and one that arrives late. For a cancel the test checks each branch of rule 6: a lookup that settles the cancel, a resend journaled first, resends at one below the written number, at it and at one above, a run from another account, either way, opening no resend, a kill switch's repeat going out with no run and held back when the lookup shows a cancel pending and once the written number is used up, for every other outcome, a halt's or a pair's cancel with no run and a plan's cancel at any time among them, no resend, and, wherever the cancel stays unsettled, a resend included, a halt and a page past the written time counted from the first entry, its reason read back from the halt-state monitor, every cancel but a plan's own still passing during the halt, a pull after the last result and before the cancel cutoff still served, and a plan's own cancel stopped by it. For a plan's cancel the test also loses the answer and then fills the old order, and expects no resend, no replacement sent before the old order settles, and the old order counted at its worst case throughout. A halt stored between a plan's cancel entry and its broker call ends in no broker call, the stopped entry and no written-time halt, a pair whose one leg's cancel is confirmed while the other's is stopped has the second leg cancelled before the cutoff, and a pair-keeping cancel that loses its answer is looked up and resent inside the stage's deadline, then left to rule 6's written time.
   2. A fill delivered twice, a fill that arrives after a cancel was acknowledged, a fill the broker later corrects or busts, and a second message under one execution id that differs from the first.
   3. A process killed between the journal write and the broker call, and killed in the middle of a write to a local file.
   4. A lease holder paused after its tag check and before its request leaves, past the end of its lease.
   5. A clock that jumps forward or back, and a time service that stops answering.
   6. A feed that stops, a feed that repeats messages, and a feed that skips sequence numbers.
   7. A local disk write that blocks for seconds under capture's spill.
   8. A journal that cannot be read, which the procedure for orders in doubt in [Every fact has one writer](#every-fact-has-one-writer) settles.
   9. A derived view that lags by minutes to hours, and an off-machine copy that stops (DDIA L5126).
   10. A second execution instance taking the account's lease while the first still runs.
   11. A reporting dependency that blocks, such as the log pipeline, while orders still go out on time, where every dropped line is counted, the fill alarm fires at 75% of the bound and before the first drop, and a scheduled drift check that stops running alerts.
   12. A dependency that recovers while its breaker is open, whose recovery probe sends no order.
   13. A job off the order path that crashes its worker on every attempt, set aside at its retry maximum with an alert, tested one attempt below the maximum, at it and one above.
   14. A consumer of broker messages that crashes on one message, which halts on the first crash with the message named and journaled.
   15. A broker or store call that stalls until its breaker opens, while a cancel, the pair-keeping and takeover cancels among them, and the kill switch still go out from the slice of its limit kept for them.
   16. A store that is unreachable, where no cancel is sent and the page names the owner's step in the broker's own interface.
   17. A dependency that fails again and again through a session, whose breaker's halt releases itself up to its written count, tested one release below the count, at it and one above.
   18. Once a step that reads a metric first runs, a null, stale or failed reading behind it. A release record or restart switch that cannot be read, and a switch flipped off, each followed by a crash, where a component the order path depends on halts trading with its last exit and count written, and pages the owner, and any other component pages. Execution, with its switch or count unreadable or absent, restarts, comes up halted, serves cancels and waits for the owner. With its switch flipped off it restarts halted, serves cancels, waits for the owner and pages, naming the broker's interface only when that restart fails. With its record unreadable it restarts, serves nothing and pages naming the broker's interface. Capture keeps restarting once this platform runs it. A restart or scaling switch found off at a session's start alerts.
   19. A component that halts and is left running, which no health check restarts and whose restart count does not move, a component restarted while halted, which comes up halted, and a consumer of a journal entry or captured data that crashes on one record, which halts on the first crash with the record named and takes no second restart into it.
   20. Execution restarted one restart below its cap and at it, which comes up trading, and one above, which comes up halted and still serves cancels.
   21. A store of the journal, captured data, their backups, the halt state, the lease or execution's queue that goes missing or is created again, a journal or captured-data store found empty or behind its copies, and a lease store created again whose lease number continues, each of which halts new orders and never reads as empty, while capture, once this platform runs it, keeps recording. A journal store found empty or behind takes no entry until it is restored, a journal verified to a non-empty head whose comparison with a copy cannot finish halts new orders and serves cancels, a copy reporting a position no entry backs counts as that comparison, an empty store with its first-start record absent, unreadable or spent halts, a store that comes back leaves its halt until the owner releases it, and a missing halt-state store's reason reaches the owner on the page.
   22. Once a scaling step first runs, an instance removed by scaling or rescheduling with a broker call open, whose order stays in doubt and is never resent, a scale-down refused at execution's floor and allowed one above it, the floor's alarm silent at it and firing one below it, and a scale step tested one below the written scale limit, at it and one above.
2. **A guarantee the platform relies on is checked while it runs, and a mismatch halts.** DDIA's example is a queue that compares messages in with messages out and alerts on any gap (DDIA L372). Faults that share a cause across parts produce more failures than independent hardware faults, and the bugs behind them wait for the day an assumption stops holding (DDIA L351, L366). So the checks run on every fill and at a set interval, not only at the end of the day: the journal's orders against the broker's, fills against positions, and the vendor's sequence numbers against what capture recorded. A check that sees two moments reports a gap that is only timing, and a platform that halts on its own timing learns to ignore the alarm (DDIA L7218, L7227). The journal and the broker share no snapshot, so reconciliation records the journal position it read, reads the broker after it, and treats a difference explained only by entries journaled after that position as in flight, to recheck on the next run. Any other difference halts at once, with no tolerance on quantity and no count of repeats. An order, fill or holding change at the broker with no journal entry, such as the owner's own trade, a split or an assignment, is journaled as received, naming its source, and halts trading until a person clears it, as [Orders the platform did not send](#orders-the-platform-did-not-send) says. Queries to the broker draw on the general share of its rate limit, never on the share reserved for cancels.
3. **Every recorded record carries a checksum, and every read verifies it.** The checksums built into network protocols catch corruption on the wire, but not a bug at either end or corruption on disk, so DDIA calls for checksums from end to end (DDIA L16445, L9376). A crash can also leave a half-written record at the end of an append-only file, and a checksum is how a reader tells it from a whole one (DDIA L2471). DDIA's example detects such a record and ignores it. The journal's store writes each entry whole, as an object either exists or does not, so a torn journal entry cannot occur there. A local file can tear. A torn tail on a capture file is sealed and flagged, its bytes are moved aside and kept, and capture starts a new segment, which never halts capture. A record that fails its checksum anywhere else is corruption, and the reader halts and alerts. Recorded data nobody reads is still read back on a schedule, its checksums verified and its copies compared, because data that is only checked when read can rot unseen (DDIA L5489, L\~16792). The read-back is a rolling cycle that reads every segment once within a stated number of days, at low disk priority or against the off-machine copy, and a segment planted with a wrong checksum shows on each cycle that it can fire. A dead-man check running off the machine watches the heartbeat, the read-back's last cycle, the age of the newest off-machine copy, and the age of the scheduled drift check's last result.
4. **A case the design calls impossible ends in a halt and an alert, never in a guess.** Real code still has to handle what an algorithm assumes cannot happen, even when the handling is to stop and leave the cleanup to a person (DDIA L9488). DDIA divides properties into safety, meaning nothing bad happens, which must hold in every case, and liveness, meaning something good eventually happens, which may wait on conditions (DDIA L9454, L9466, L9469). Here every hard limit is a safety property and trading is a liveness property. A design may give up trading to keep a hard limit, and never the reverse. A safety property holds within a stated model of what can go wrong, and DDIA calls a model that bounds every pause unrealistic (DDIA L9405, L9466), so no hard limit here depends on timing. A cancel reduces risk, so it is not trading under this rule, though cancelling a resting protective order, an unwind or one leg of a pair does raise exposure, so a halt stops new work and keeps every working order, except the cancel that keeps a pair whole, and the kill switch is what cancels everything, under the owner's 2026-11-04 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). The kill switch acts as a lease holder, so it is never a second sender. It runs in whichever instance receives its event, as the owner ruled on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), takes the account's lease, waiting for a hung holder's lease to expire if it must, journals its cancels, sends them from the rate-limit share reserved for cancels, and reads the broker's open orders again until a read returns none, and an order still open whose cancel has no answer is handled under rule 6 below. Each halt states whether it releases itself after reconciliation and a hold time, or waits for the owner, and writes its reason where the monitor of the halt state reads it. On a live account only the owner releases a fired loss stop or the kill switch, and on paper and shadow the operator may too, under the owner's rulings on [#11](https://github.com/l3a0/ithildin-runtime/issues/11). The owner counts as unreachable when a page stays unacknowledged past a stated time.
5. **A value from outside the process is checked against a sane range and a size limit when it is parsed, and a value that fails halts its consumer.** DDIA recommends such checks even between services that trust each other (DDIA L9360). The sane range comes from each instrument's metadata, since some instruments trade at negative prices, and a quantity above the instrument's size limit is the kind of value that stops the reader before it reaches a decision.
6. **Every outside call has a timeout, the order path's dependencies each have a limit on concurrent calls and a breaker, and an open breaker stops new orders, stops a plan's own cancel only through the halt it stores, and never stops any other cancel.** Newman puts a timeout on every outside call, and names bulkheads and circuit breakers as tools to use where they fit (BM L4252, BM L4292, BM L4995). A breaker stops calling a dependency after a written number of failures and probes it until it answers. This design gives a limit and a breaker to the order path's dependencies: the feed, the broker, and the store that holds the lease and the journal, which share one breaker. Every other dependency, such as the time servers, the off-machine copy and reporting, has a timeout and stays asynchronous, the copy apart from the read at a start that crossing 3 of [Keep the order path isolated](#keep-the-order-path-isolated) names, under rule 1 of [Keep the order path isolated](#keep-the-order-path-isolated). His cautionary tale is a pricing queue with no retry limit, where one request that crashed its worker went back on the queue and crashed the next (BM L1286). An open breaker on the feed, the broker or the store halts new orders under hard limit 3 rather than serving a stale value, names the breaker where the halt-state monitor reads it, and pages the owner. A cancel and the kill switch pass through an open breaker with every call they need: the lease's take, renewal, tag check and delete, the journal's re-read and write, the halt-state read, and the broker lookups below. Each order-path dependency keeps a written slice of its limit on concurrent calls for those calls, so a stalled new-order call cannot hold a cancel back. A cancel whose answer is lost is looked up first. When the lookup shows the order still working and no cancel pending, a new cancel goes out, journaled first and sent under the lease, at most a written number of times for each cancel, where the contract suite's latest scheduled run against that account shows the broker applies a repeated cancel at most once, under the owner's 2026-11-03 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). No stage resends a plan's cancel yet, under the owner's 2026-11-04 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), as `A plan changes or cancels its own unfilled orders` says. A lookup that shows the order cancelled, filled or expired settles the cancel and is journaled as received. Every other outcome leaves the cancel in doubt. That covers a lookup that cannot answer or shows a cancel pending, a written number already used up, a contract case that account's run cannot provoke, and a scheduled run that is missing, stale or failed. A resend does not end the doubt until a lookup or an answer shows the order cancelled, filled or expired, so a broker's acceptance of a cancel leaves it in doubt. The clock runs from the first cancel's journal entry, so a resend never resets it, and any cancel still unsettled past a written time halts new orders and pages the owner, under the owner's ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). That halt names each unsettled cancel by client order id, with its age, where the halt-state monitor reads it. It cancels nothing further, keeps every working order, persists between sessions and waits for the owner, and its page names the owner's step in the broker's own interface. A halt still stops no cancel except a plan's own, as rule 9 of [Processes and files](#processes-and-files) says. A run counts only for the account it ran against, so paper and live stay independent, and until a run exists for a live account every lost cancel there that no lookup or answer settles ends in that halt. The kill switch alone needs no run, under the owner's ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10): its repeat keeps the lookup and the written number, at paper and at live. An answer such as "already filled" is journaled as received. An order in doubt is first looked up by client order id and cancelled once the lookup maps it. The kill switch's reads and the lookups draw on the general share of the rate limit, as every query does under rule 2, and the share reserved for cancels carries their cancels. When the store itself is down, no cancel can be journaled, so none is sent, as the owner ruled on [#10](https://github.com/l3a0/ithildin-runtime/issues/10): the page says so, and the owner's step is to cancel in the broker's own interface, which reconciliation later journals as outside entries. The call that tests whether a dependency recovered is a read, never an order. A breaker that closes ends only its own state, and the halt it raised releases under rule 4 above, after reconciliation from a recorded journal position, its hold time and no order or cancel left in doubt, at most a written number of times a session, each release journaled with its count, after which it waits for the owner and names the count. Capture's connection to the feed sits outside the trading loop's breaker, and capture reconnects with bounded backoff and never stops retrying. A retried job off the order path, such as a report or a derived view, has a maximum, past which it is set aside and alerts. A job that consumes broker messages, the journal or captured data halts with the message named, and an order send is never retried, since a send with no answer stays in doubt under the one-writer rules.

### Time comes from one clock, and order from sequence numbers

1. **Elapsed time is measured on a monotonic clock, and the journal orders its entries by a sequence number it assigns.** The time-of-day clock can jump backward when the time service resets it, and a paused virtual machine sees time jump forward (DDIA L8811, L8823, L8878). A counter orders events without measuring time, which DDIA calls the safer way to order them (DDIA L8966, L10533), when one assigner increments it. Counters kept by several nodes, or handed out in blocks, do not agree with the order events happened in (DDIA L10556). So each account's journal numbers its entries from one counter, the numbers have no gaps, a gap halts the reader, and numbers from two accounts are never compared, since order holds only within one partition (DDIA L10731, L6201). The shared clock offers both readings, every timeout reads the monotonic one, and a timestamp in the journal records when something happened, never in what order. A monotonic clock does not defend against a pause, which can fall between a check and the act it guards (DDIA L9084).
2. **The machine's offset from reference time and the age of its last sync are readings of the clock, and either past its bound halts trading.** A computer's quartz clock drifts, a blocked time service can go unnoticed, and a time server can be wrong by hours (DDIA L8846, L8853, L8859). DDIA's rule for software that relies on synchronized clocks is to monitor the offset and take a node whose clock has drifted too far out of service (DDIA L8913). Here the clock sets session boundaries, data freshness and every captured timestamp, so its offset is one of the inputs hard limit 3 halts on. A time client that cannot reach its servers keeps reporting its last offset while the real error grows, so trading also halts when the last successful sync is older than a set age, and the offset is measured against several servers, since one can be wrong by hours (DDIA L8851, L9360). The offset and the sync age are readings of the clock adapter, so the backtest clock supplies them from the replay and one halt rule reads them in every environment. Each measurement is recorded as captured data, so receive times taken while the clock drifted can be found later. The bound sits well below the smallest staleness threshold, and a staleness check on event time adds the bound to the age it measures, since a clock running behind makes stale data read as fresh. A drill on the paper host blocks the time servers and shows both halts fire. The 100-microsecond rule DDIA cites as a draft for high-frequency firms (DDIA L8887) is far tighter than a platform this slow needs.
3. **Captured data keeps the source's event time, the source's send time where the feed has one, and the local receive time side by side, and rate monitors window by event time.** Mixing event time with processing time makes a backlog processed after a restart look like a burst of activity (DDIA L14747). A clock outside the platform's control can be wrong, so DDIA logs three timestamps, the event and send times on the sender's clock and the receive time on the receiver's, and estimates the offset between the clocks from the send and receive times (DDIA L14784, L14788). None of them overwrites another. Capture also gives every record its own receive sequence, gap-free and in receive order, since vendor sequence numbers are per feed and the clock can step back. The [charter's premise](team-charter.md#the-premise-what-must-not-fail) records when data arrived and when it became knowable. Receive time is when it arrived. **Knowable time** is the earliest moment the platform held the value: the receive time for live capture, and the vendor's publication date or vintage for history. Event time is neither, since a quarter's earnings carry the quarter's end as their event time and become knowable weeks later (L3884). The point-in-time interface reads by knowable time, and event time serves rate monitors.
4. **A decision reads the time and random numbers only from the shared clock and a seed the journal records, and it depends on nothing else that can change between runs.** A replica that re-runs a statement calling the current time or a random number computes a different result (DDIA L4912), and a replay does the same. DDIA's full list adds iterating over a hash table and reading from an outside source (DDIA L13148). So no module calls the system clock or an unseeded random source directly, iterates a set of strings, or anything built in such a set's order, where the order affects the result, or fetches from outside mid-decision. A Python dictionary keeps its insertion order, so it is not on that list. Each decision's journal entry also names the version of the reference data it used: risk limits, instrument metadata, configuration and the cost model. DDIA's version of that rule gives each version of a changing reference record its own identifier and joins against the version current at the time (DDIA L14943).
5. **Replay delivers events in the order live capture received them, and never re-sorts them by event time.** A node that must decide now cannot know which earlier-stamped event is still on its way, so ordering by timestamp finds the answer only after everything has arrived (DDIA L10603). A backtest that sorts a day by event time decides with hindsight no live run had. Replay orders by capture's receive sequence. Whatever wait for late events the live loop applies, replay applies the same one. A test replays a recording whose event times are out of order and asserts delivery in receive order, and a second test replays one day twice in separate processes under different `PYTHONHASHSEED` values and asserts the same decisions.

### Every fact has one writer

1. **Each fact has one authoritative store, and a copy of a value that changes is derived.** The book requires a single point of truth for any value copied across a system, updated in one place (AoS L8153). DDIA calls the two kinds a system of record and derived data, and where they disagree the system of record is right by definition (DDIA L11985). Here the journal records every intent, order, acknowledgement, fill and cancel when it happens, and the broker is the truth for holdings and cash at the moment the platform asks. The two are independent records that reconciliation compares and never overwrites, and a mismatch halts trading. Positions and cash held anywhere else are derived and rebuilt once the two agree. Captured data, every snapshot a decision read, execution's account file, which holds the broker state its recover stage captured, every backup of any of them or of the journal, and every broker statement the platform saved are recorded facts, not derived copies.
2. **The quantity that sizes an order counts every order already sent, at its worst case for each limit.** The book asks three questions of any replicated read, and one of them decides this case: will the value feed a later write (AoS L7901)? Here the gate and the sizing both measure against the broker's holdings plus every order the journal shows as sent, working or in doubt. For a signed limit, such as net position, the gate checks two cases, every working buy filled with no sell, and every working sell filled with no buy. For gross size, open orders and cash, every working order counts as filled. Choosing filled or unfilled one order at a time can pass two orders that breach a limit together. A strategy's own position comes from the journal's fills, and reconciliation checks the strategies' sum against the account. Risk limits, instrument metadata and configuration may be cached with a maximum age. A cache past its age whose refresh fails halts what it governs, a tightened limit applies to the next order, and the refresh, like every session boundary, reads the shared clock rather than the wall clock, so a backtest refreshes the same way live trading does.
3. **One holder of the account's lease sends at a time, and an order with no answer blocks the next.** State that must survive a restart is rebuilt from the journal and the broker, and a rebuild that cannot read either leaves the platform halted. The book's order of preference is to avoid state, then to move it out of the process (AoS L8393, L8827). The lease's mechanism is the one the owner ruled for on [#10](https://github.com/l3a0/ithildin-runtime/issues/10): an object in S3, renewed by a conditional write, held only around the actions that change the account, and taken over by another instance once it expires unrenewed. Only the handler holding the lease writes an order's `sending` or cancel entry, or calls the broker to change the account. Every journal append, by any writer, is ordered by its create-only write at the next number, so monitoring, the supervisor and the `ithildin` tool append without the lease. A handler takes the lease only once it is free, expired past the skew margin, or cleared, and a handler that takes it re-reads the journal before it acts. Each send waits until durable storage has acknowledged its journal entry, since durability means waiting for the write before acting on it, and DDIA warns that even `fsync` is not guaranteed to work (DDIA L\~6894, L\~6930). For the journal's store that acknowledgement is a successful conditional write. For a local file it is `fsync`, `F_FULLFSYNC` on macOS, and a flush of the directory when the file is created. A write that fails halts trading, and its order is not sent. Each journal entry carries a client order id fixed by the decision before the send, and the broker receives the same id. An order the broker never acknowledged is in doubt rather than failed. It blocks every further order on its account and instrument, and settles only when the broker reports it, when a broker-side expiry the adapter set on it has passed, or when the owner rules on it. A lookup that finds no record never settles it, because a request can still land after its sender has gone (DDIA L4204, L4207). If it stays in doubt past a set time, trading halts and the owner is alerted. A rejection that says the broker already holds the id means the broker has the order, so the order is looked up, never marked failed. [#10](https://github.com/l3a0/ithildin-runtime/issues/10) does not rely on the broker rejecting a repeated id, so no order is resent. The simulated broker can withhold an acknowledgement, so replay runs this path. These rules go beyond the book, which discards a timed-out call (AoS L8698). When the journal cannot be read, a written procedure for orders in doubt applies, and a test asserts each step.
   1. Halt, and record that the journal is unreadable as the reason.
   2. Cancel working orders through the kill switch, once the journal takes writes again, since each cancel is journaled first. For a store found missing, empty or behind, this step comes after step 3.
   3. Restore from the journal's second copy, then from the latest backup up to its journal position.
   4. Settle the remaining orders from the broker by client order id.
   5. Keep every strategy switched off until the owner assigns the resolved positions, and resume only after a journal entry records them.
4. **The send to the broker is not fenced, so the design names the window that is left and guards it.** A process can pause at any point, in a garbage-collection pause, a suspended virtual machine, swapping or a blocking write, and wake up acting as if it still held its lease (DDIA L9084, L9094, L9102, L9110). DDIA's answer is a fencing token: each grant carries a number higher than every earlier grant, and the protected resource itself refuses a write carrying a lower number than one it has seen, since a client checking its own lease is not enough (DDIA L9274, L9286). For a resource that does not check tokens it suggests working the token into what the resource stores (DDIA L9292). The broker keeps the lease number inside the client order id but never refuses on it, and the book offers nothing for a resource that keeps a token without checking it. So the check of the lease's tag before every broker call, which [#10](https://github.com/l3a0/ithildin-runtime/issues/10) requires, narrows the window and does not close it. A holder that pauses after its check and before its request leaves can still send after the lease has passed on. Four guards bound that window.
   1. The journal's store refuses a write that names an older lease, in the same atomic step as the write, and a new holder's first act is a journal entry carrying its own lease number.
   2. A new lease number exceeds every lease number in the journal and in the client order ids the broker reports for the account, from the first commit, so a restore from backup cannot reuse one. DDIA's case of a reused number is a promoted replica whose lagging counter reissued keys another store already held, and showed private data to the wrong users (DDIA L4868).
   3. A new holder never resends an older lease's order. The order stays in doubt under rule 3, counted and blocking its instrument.
   4. Reconciliation flags any broker order whose client order id names an older lease and that was missing from the broker's order list the new holder read when it took the lease, and halts. That comparison needs no clock.

   Each guard has its own test, with the other three intact. In the pause scenario the test asserts that the broker holds exactly one order for the decision, and a separate test asserts that a late order under an older lease halts with that order named as the cause.
5. **Each fill applies once, keyed by the broker's execution id, and every decision that reads shared buying power runs under the account's lease, one at a time.** Only an id carried from end to end can drop a duplicate, because each layer below sees only its own retries (DDIA L16438). Every broker message is journaled as received. The uniqueness constraint sits on applying a fill, keyed by its execution id and the broker's correction identifier, in the same step as the write, because a check for a row that does not yet exist has nothing to lock, and two writers can each pass it (DDIA L7635). A repeated id whose content matches the first is applied once. One whose content differs is journaled beside the first and halts trading. The broker adapter states how its broker marks a correction or bust, by a new execution id that names the old one or by a version on the same id, and a correction is a new entry that names the execution it replaces, as a ledger adds a compensating entry rather than editing the old one (DDIA L\~14416). The derived position applies the latest version of each execution once. A test inserts a duplicate straight into the store, bypassing the application's check, and asserts the store refuses it. Every update to derived state sets a value or is keyed by an id that drops a repeat, since an increment applied twice counts twice (DDIA L\~14993). Two strategies that each read the account's buying power and then order against it can each pass the check and together exceed it, the race DDIA calls write skew (DDIA L7538). Its remedy without a transaction is to send every write that might conflict to one partition and process them in order (DDIA L16535), and it finds that one thread in a loop "can sometimes perform better" than concurrent execution by skipping the cost of locks (DDIA L7725). Here the account's lease serializes those decisions, whichever instance holds it. DDIA's serial execution works only when each step waits on no disk or network (DDIA L\~7743, L7821). This design waits on both inside each step, for the lease, the journal's write and the broker's buying power, so its throughput per account is the written scale limit of one lease cycle per order, measured and alarmed under the first section's rules. The holder re-reads the halt state just before each send.

### Recorded data is append-only, and every view of it is rebuilt

1. **Recorded data is never updated or deleted in place, and a correction is a new record.** Append-only files make crash recovery and concurrency simpler, because a crash cannot leave a value half overwritten (DDIA L2484). The engines DDIA describes are append-only on the way in, yet they compact segments by key, keeping only the latest value for each key and dropping the rest (DDIA L2437). So recorded data never sits in a store that compacts by key, unless the writer assigns every record a key of its own, such as its receive sequence, never one taken from the source's fields. The one deletion is the owner's written procedure in the data-cost rule below. DDIA's event sourcing records each action's intent in neutral terms rather than the side effects it had, so a later feature can derive new effects from old events (DDIA L14319). Here the journal records what a strategy asked for as well as the orders that followed, and a cancel or a correction is a new entry that points at the old one. A test rebuilds positions from the journal and compares them with the running state.
2. **A new read view is built from the same record and runs beside the old one, rather than migrating the record.** Separating the form data is written in from the forms it is read in lets a new view run alongside the existing ones, and the old one is shut down once nothing reads it (DDIA L14461, L14470). This is how a stored format changes under the two-release rule below: derived tables move, and captured data and the journal stay as written.
3. **Recorded formats are readable from any language and carry a version, and nothing recorded or read from outside is loaded with Python's `pickle`.** `pickle` is Python's built-in way to save any object as bytes and load it back. A language's own format like that ties the data to the language, tends to neglect versioning, and lets whoever controls the bytes run code when they are loaded (DDIA L3623, L3626, L3632). So every recorded record names the schema it was written with, a registry keeps every schema ever used, and `pickle` never touches captured data, the journal, or anything that arrives from outside the process. Readers keep reading every schema ever written, under the two-release rule below. Data outlives code, so a record written today is read by code that does not exist yet, and a rollback has older code read newer records (DDIA L\~3874, L\~4104). A release passes a compatibility check against every schema in the registry: a field is added only with a default, an identifier is never reused, and a field is removed only if it had a default (DDIA L\~3973). A step that reads a record and writes one back keeps the fields it does not understand, or it drops them silently (DDIA L\~4071, L4319). Identifiers never pass through a float, since a JSON parser that reads numbers as floats corrupts integers above 2^53 (DDIA L3632). Prices and quantities in recorded formats and on the order path are decimals or integer minor units, which is this doc's own rule for exactness, and research may use floats.
4. **Captured data is stored as received, and parsing it into the platform's model is a derived step that can run again.** DDIA finds that making raw data available quickly is often worth more than settling the ideal model first, and leaves its interpretation to the reader (DDIA L12909, L12920). This doc adds its own reason: a parser bug then costs a rerun rather than a lost day.
5. **A backup copies closed segments, names the journal position it is consistent with, and leaves the machine before the first capture is relied on.** A copy taken while writes continue holds parts from different moments, and restoring it makes the mismatch permanent (DDIA L7227). DDIA's segments are never modified once written (DDIA L2449), and this doc infers that a closed segment gives the backup a consistent point to copy. A new copy of a store is a snapshot tied to an exact log position, then every change after that position (DDIA L4777), so each backup records the journal position it covers. A machine in the cloud can vanish without warning (DDIA L293), so a copy of captured data and the journal exists off the machine from the first session on. A capture segment stays open no longer than a stated number of minutes, which bounds what a lost machine can take, and an alarm watches how far the newest off-machine copy trails the head. A restore drill runs on a schedule the operator sets and after every new recorded schema, restores to a scratch place, never over the live store, and checks record counts, checksums and the lease numbers the restored journal holds.
6. **A batch output is written whole to a new place and switched to in one step, and the old output stays until the new one is checked.** DDIA's example loads new files beside the old ones, switches over atomically and can switch back, since the old files are still there (DDIA L12811, L12822). Here that covers backtest results, feature tables and any derived store the loop reads. A multi-part output becomes visible through one small completion record written last, naming the parts, counts and checksums it covers, the way a single node commits when its commit record reaches the disk (DDIA L10857). A reader treats an output with no completion record as missing, which halts under hard limit 3, never as empty. Inputs are read-only, so a run can repeat without harm (DDIA L12311). A job also writes nothing outside its output and reads reference data only from a snapshot placed beside its inputs, because a query to a live store mid-run makes the job slow and its result depend on when it ran (DDIA L12545). Regenerable then means the same records on a second run, which a test checks by building each derived artifact twice in separate processes and comparing a checksum over its sorted records, since a file format may stamp its own creation time (DDIA L13148).
7. **Derived state changes only by consuming the journal in order, and stores the journal position it reflects.** Two writers updating two stores can land in opposite orders with no error, leaving the stores different for good, so no component writes to both the journal and a view of it (DDIA L\~14160). Once an entry is in the journal it is a fact, and no component downstream may reject it. A check runs before the append, or its verdict is a later entry of its own (DDIA L14372). Each derived store saves the journal position it reflects in the same write as the value, so a restore from a snapshot plus the journal's tail equals a full replay (DDIA L\~15043). A test crashes at every write boundary of the update and asserts that the saved position equals the last journal entry reflected in the stored value.

### Coordinate only where a mistake cannot be undone

1. **The order path coordinates, and everything else is asynchronous and may lag.** DDIA separates timeliness, meaning the reader sees the latest state, from integrity, meaning no state is ever wrong. A lapse in timeliness heals on its own and a lapse in integrity stays (DDIA L16612, L16626). Many businesses accept a briefly broken constraint and apologize for it, and paying for coordination only where it is needed buys speed and availability everywhere else (DDIA L16676, L16700, L16711). An order cannot be taken back, so the risk gate and the sizing coordinate through the one sender, while monitoring, reports and research may lag. A lagging view is late and still right only while delivery is reliable, ordered and free of duplicates. A view that misses one message is wrong for good (DDIA L\~16028), which the gap checks above catch. DDIA also leaves the size of a tolerated breach to the business, and notes that coordination trades fewer apologies for inconsistency against more for outages (DDIA L16676, L16693). Here a halt is the outage, and its price is missed trading. This is the reasoning behind the isolation rules above, seen from the data's side.
2. **No transaction spans the journal and the broker, and no consensus algorithm is written here.** Two-phase commit needs every participant to answer, so it spreads one part's failure to all of them, and its coordinator becomes a database whose loss leaves locks held until someone intervenes (DDIA L11110, L11150, L11174). A broker could not take part in one anyway. The journal before the send, a client order id carried end to end and reconciliation reach the same result without one, which is the log-based approach DDIA describes for a transfer between two accounts (DDIA L16554, L16636). The journal, the id and reconciliation reach that result only where the broker rejects a repeated client order id, which the one-writer rules above require the broker adapter to show. DDIA lists the problems that all reduce to agreement among nodes, among them a lock or lease, a uniqueness constraint, a counter with no gaps and an atomic commit (DDIA L\~11482). Here those are the sending lease, the journal's sequence, client order id uniqueness and the block on unknown orders. On [#10](https://github.com/l3a0/ithildin-runtime/issues/10) they all live in one store, S3, whose conditional writes are an existing compare-and-set, so none of them is consensus written here and none splits the journal's write across two systems (DDIA L11247, L11519). DDIA names three ways a single decider can fail over: wait for it, fail over by hand, or fail over automatically (DDIA L11519). The account's lease fails over automatically, as the owner ruled on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). The owner is a single decider too, and when the owner cannot be reached the platform stays halted or on paper.

### The code holds its own rules, and every check runs in production

1. **A rule that guards money is held by the code's structure, a type or a test, never by a comment or a convention alone.** McConnell separates programming in a language, limited to what it offers, from programming into one, deciding what the program needs and making the language supply it (CC p. 68, p. 69). Python enforces no boundary between modules, so the import rule test that [#22](https://github.com/l3a0/ithildin-runtime/issues/22) specifies enforces the boundaries, and that issue holds the full rule. In short, every package imports `core` and none of its siblings, only the composition package, `runtime`, imports the broker adapters, and the order path reaches the broker through the `Broker` interface in `core`. An import check guards the wiring and not the call, since a module handed the adapter can call it without importing anything. McConnell's way to make one step's dependence on another visible is to pass the earlier step's output to the later one (CC p. 349). Python lets any module build any type, so the argument shows a reader the order of the steps and a check in the send is what holds it. The send of an order takes the journal's acknowledgement alone, reads the order from the entry it names, and refuses an entry that is missing, not yet durable, or already marked as sending, which covers a forged, swapped or reused acknowledgement. The send also checks the lease's tag and reads the halt state itself, just before the call. A cancel takes an acknowledgement the same way, so it too is journaled before it is sent, including the kill switch's cancels while the journal's store cannot take a write. The owner ruled so on 2026-11-03 on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), accepting that an S3 outage holds cancels back as a low risk given S3's availability. Until the store takes the write again, the kill switch keeps retrying and the owner is paged.
2. **Every check that guards an order or a record runs the same way in tests and in production.** McConnell's default compiles assertions out of the production build (CC p. 190), though he keeps the code that checks for important errors and the code that lets a program fail safely, which the Mars Pathfinder's engineers left in and used to fix it after landing (CC p. 209, p. 210). Python drops every `assert`, and every `if __debug__:` block, when it runs with `-O`. Here that would make what runs live differ from what was tested, so a guard on the order path or on recorded data raises its exception explicitly. A lint rule refuses `assert` outside the tests, a check refuses `__debug__` outside them, and CI runs the suite a second time under `python -O`. A check that can halt trading, such as reconciliation, runs on the same schedule in every environment, read from the shared clock. Only a check off the order path, such as the scheduled read-back, takes an interval per deployment, and that interval has a maximum written beside the code, so no setting turns a check off.
3. **An absent, unknown or invalid value fails, and no code turns it into a neutral one.** McConnell lists ways to handle a bad value, among them returning a neutral value, substituting the closest legal one, and logging a warning and carrying on (CC p. 194, p. 195). He calls the choice an architectural decision, and defines correctness as returning no result rather than a wrong one, which safety-critical software favors (CC p. 197). Hard limit 3 makes that choice wherever trading reads an input: market data as the loop parses it, the broker's holdings and open orders, the journal at restart, the gate's limits, the clock's readings and the halt state. Capture sits outside it. Capture stores a bad value as received and flags it, and the check runs where the value is parsed, under the faults rules above. The choice takes four forms in the code.
   1. One variable holds one meaning, and no value such as −1 or zero stands for "missing", which McConnell calls hybrid coupling (CC p. 257).
   2. A branch over a closed set, such as the states of an order, names every case and ends in a failure, rather than treating the last case as the default (CC p. 360, p. 363). This is the faults rule's halt on an impossible case (DDIA L9488), written into each branch.
   3. A function's result starts as the failing one, as McConnell's worked example starts its status at "fail" (CC p. 224).
   4. A number is compared with zero explicitly (CC p. 441), and no condition tests a value for truth where None, zero and empty mean different things. The kill switch's loop ends only on a successful broker response that lists no open orders, never on a read that failed.

   A bad value stops new orders, stops a plan's own cancel only through the halt it stores, and never stops any other cancel. The kill switch and the cancel that keeps a pair whole keep cancelling the other orders they name, cancel an order whose state is unknown rather than skip it, retry one the broker refused with a definite answer, such as a rate-limit refusal, handle one whose answer was lost under rule 6 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs), and page the owner. A test feeds each input an absent, a stale, an empty, a malformed, an out-of-range, an oversized and a wrong-typed value. It asserts that the halt names the input and the defect, that the alert fired, and that no order reached the broker's stand-in. A second test breaks one broker response in the middle of a kill switch, and asserts that every other order on its list is cancelled.
4. **Data from outside becomes the platform's own types at the adapter, and the raw record is the only thing past it in the vendor's form.** McConnell's barricade checks and converts data at the boundary, so the code inside can treat a bad value as a bug rather than as input (CC p. 203, p. 204). The adapter records what it received unchanged before it converts anything, as the capture and journal rules above require, and only the parser and reconciliation read that raw record. A message that fails conversion stays recorded and halts its consumer. The range and size checks of the faults rules above run at the conversion. A price, a quantity, a currency, a timestamp and an id are distinct types, which is McConnell's case for creating types of one's own (CC p. 311, p. 312), and an id keeps the vendor's value exactly inside its type. A routine that takes two values of one type takes them by keyword (CC p. 180). Each adapter checks the result of every call it makes (CC p. 197). It raises the platform's own exceptions at the level of the interface it presents (CC p. 199, p. 200), keeping rejected, provably not sent, and outcome unknown apart, and counting any library exception it cannot classify as unknown, which leaves the order in doubt under the one-writer rules above. Its exceptions and log lines carry no credential, token or account number, since attackers read error messages (CC p. 196).
5. **A stand-in adapter passes the same contract tests as the real one.** McConnell's statement of the Liskov principle is that every routine of a base class means the same thing in each class derived from it (CC p. 144, p. 145). The simulated broker is the stand-in replay trusts, so one suite of contract tests runs against it, against the paper adapter's own code fed recorded broker responses in CI, and against the broker's paper account on a schedule. The suite covers the cases the one-writer rules depend on: a repeated client order id, a cancel that races a fill, a lookup of an unknown id, a broker-side expiry, a partial fill, a reject, a timeout on an order and on a cancel, a cancel of an order already cancelled, filled or expired, a cancel resent while the first is still pending, a rate-limit refusal on an order and on a cancel, and a correction or bust. A case paper cannot provoke is named as a gap in the model, and a recording older than a stated age fails the suite. A recording strips credentials, authorization headers, keys in query strings and account numbers before it is written, and a test fails on any committed recording that matches a credential pattern or the account number's format. A stand-in returns an empty answer only where the real system would answer "none", such as a quiet instrument or no open orders, and raises when a read fails or no data exists, so "nothing happened" and "missing" stay two answers.
6. **Positions, P&L, fills and fees are rebuilt in tests by a second, plain implementation, and the comparison is exact.** McConnell's example is an insurer whose rate system was checked by a separate tool that computed each rate on its own, because accuracy was essential (CC p. 722). The second implementation lives under `tests/`, which no module under `src/` imports, so it is a test oracle rather than a second code path. It reads the raw broker messages, drops a repeat by execution id in its own code, and shares no helper with the code it checks. The rebuild test in [Recorded data is append-only](#recorded-data-is-append-only-and-every-view-of-it-is-rebuilt) uses it, and the test asserts exact decimal equality over a count of fills that is not zero. Reconciliation against the broker stays the check that runs on paper and live.
7. **No change made for speed merges without a measurement before and after on recorded data, and a change that gains nothing is reverted.** McConnell's tuning procedure keeps the working version, measures each change alone, and reverts one that does not help, since more than half of attempted tunings gain little or slow the code (CC p. 606). The effect of one optimization depends on the language, its version and its libraries, so experience predicts it poorly (CC p. 603, p. 644). The measurement goes in the pull request, in the percentiles the monitoring rules require.

### Every change can be undone or switched off

1. **A strategy can be switched off without a deploy.** The book's rule is to design anything released so it can be turned off, which is faster than a rollback and leaves the rest of the release running (AoS L4800, L6358, L6996). Here the switch is a per-strategy halt that leaves the other strategies trading. Like every halt under hard limit 3, it stops new orders and never reads as a target of zero. Switching a strategy off is anyone's to do, while switching it back on in live trading is the owner's, on the owner's ruling of 2026-11-02.
2. **A change to a stored format ships in two releases: the code first, the data later.** Old fields and tables stay for as long as a rollback may need them (AoS L6327, L6335). The book puts the price of rollback at under 1% of a release for most changes, and finds the large releases that skip it are the ones that need it most (AoS L6340, L6348). Captured data and the journal are never migrated. A new format applies to new writes, every reader keeps reading every format ever written, and a test replays a journal holding each of them. Before a release, a test replays a journal and capture files the new artifact wrote through the kept artifact's readers, and the release does not go ahead when it fails.
3. **A release is compared with the one before it, and a sudden gain is a finding too.** A jump in throughput can mean a code path or a filter was dropped (AoS L5876). Replays of the same recorded day must match on their counters, such as orders, rejects and fills. Each timing carries a written tolerance in both directions, set from this platform's own run-to-run spread, and the book's worked example uses 5% (AoS L11260). Any change past those bounds without an intended cause goes to the builder (AoS L11226, L11254).
4. **One artifact is built once and is what every environment runs, and configuration may differ by environment only in a committed list of keys.** Newman builds an artifact once so that what is deployed is the thing that was tested (BM L\~2160), and keeps configuration that changes between environments to an absolute minimum (BM L2393). Here the list holds adapter selection and adapter parameters, such as a backtest's date range, a broker's rate limit and the map of partitions to machines, each environment's queue, lease object, and journal and store locations, and references to where each environment's credential is read from, never the credential itself, and the grants the owner's rulings make differ. Each environment's queue, lease and stores sit in that environment's own AWS account, under the owner's 2026-11-02 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). The risk gate's hard caps sit outside the artifact and the tracked configuration, in execution's own configuration, which only the owner changes, by hand, and the constraints file can only tighten them, under the owner's 2026-10-29 ruling on [#7](https://github.com/l3a0/ithildin-runtime/issues/7#issuecomment-6505752788). A registered strategy's parameters and every code path are identical in backtest, shadow, paper and live, while a research sweep's parameters are its run's inputs, carried with the run's record. A test loads every environment's tracked configuration, fails on a difference outside the list, asserts that no two environments share a queue, a lease or a store, asserts that no tracked file holds a cap, and asserts that a constraints value looser than its cap leaves the cap binding, where how the gate combines the two is [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15)'s proposal for [#7](https://github.com/l3a0/ithildin-runtime/issues/7). A key outside the committed list halts new orders, names the key where the halt-state monitor reads it and waits for the owner, and a cap that is missing or unreadable halts new orders, names the cap and waits for the owner, while the release still serves cancels and the kill switch under rule 5. The operator owns the artifact's record. The artifact is the image digest a release pins, whose mechanics [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds and [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15) proposes, and a worker whose digest differs from the session's halts with "release changed", names itself and waits for the owner, as [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds. A mismatched execution worker serves nothing and pages the owner naming the broker's own interface, as a failed startup check of the release's own does, and any other mismatched worker halts new orders only. In slice 1 capture is marketlake's, under the owner's 2026-10-29 ruling on [#5](https://github.com/l3a0/ithildin-runtime/issues/5#issuecomment-6502285590), and marketlake's own release keeps it recording, so its hash is read from marketlake's record, never from the trading release's. A capture this platform later runs itself gets its own rule for releasing without a gap in recording. The previous artifact is kept by its digest so a rollback can run.
5. **A release changes only between sessions, and cancels and the kill switch are served while a session is open.** Newman's blue/green deployment runs two versions side by side and switches traffic between them (BM L3044). The account's lease under [#10](https://github.com/l3a0/ithildin-runtime/issues/10) would serialize two releases' sends, but two versions acting by turns would apply different gate rules, limits or journal formats to one account's orders, and replay could not say which version decided what. So a release changes only at a session's start, under the owner's 2026-11-01 ruling that [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15) carries, and a run never mixes releases. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds the mechanics, with [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15) as the proposal. Three guarantees hold whatever those mechanics settle. No order from a new release leaves while the old release runs. While a session is open, a release serves cancels and the kill switch. The alarm when none does is monitoring's deadline on each kill switch event and the pull command's own check for execution's entry saying the cancels are done, watched from outside every release, as [PR #15](https://github.com/l3a0/ithildin-runtime/pull/15) proposes for [#11](https://github.com/l3a0/ithildin-runtime/issues/11), and it reports under the first monitors' heartbeat. Between sessions a pulled kill switch, a fired loss stop or a halt on an unsettled cancel persists, the next session reads it before any order, and a switch pulled with no session open alarms the same way. Every order the platform sends expires at the broker by its session's close, as slice 1's market-on-close orders do, and an order type that rests longer waits for a rule on [#10](https://github.com/l3a0/ithildin-runtime/issues/10). A release serves cancels, except a plan's own while a halt is stored, and the kill switch once its own digest and journal checks pass, because every cancel is journaled first, and a failure of any other startup check, such as a configuration key or the clock, halts new orders only, while a store finding follows rule 3 of [A release is checked where it runs](#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault). A release whose own check fails serves nothing and pages the owner, naming the broker's own interface as the owner's step. An order or a cancel in doubt carries over to the next session under rule 6 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs). An older artifact that cannot read the newer release's journal entries meets the procedure for an unreadable journal, which rule 2 above exists to avoid. The off-machine dead-man check fires when no release sends a heartbeat during a session. A test starts a session with the kill switch pulled, with a loss stop fired and with a halt on an unsettled cancel and asserts no order leaves, fails each startup check other than the release's own and the store checks and asserts the kill switch is still served, fails the release's own digest and then its journal check and asserts a pulled kill switch pages the owner naming the broker's interface, and puts a kill switch event on the queue, leaves it unread past its bound and asserts the alarm fires.

### A release is checked where it runs, and nothing heals by hiding a fault

1. **The checks that clear a release to trade are the code its tests ran, run where the release runs with only the target changed, and the release trades only after they pass.** Farcic runs the same integration tests against the new release twice, with only the address changed: once at its own address before it is made publicly available, and once through the entry point users reach after the switch (DT p. 173, p. 247, p. 248). Here that suite is the charter's Startup step. Its functions run in CI against the simulated broker and replay, and in each environment against that environment's adapters, so the code that clears a release is code the tests ran. No second suite exists beside it, because a second list of checks drifts from the first. A release enables strategies only after Startup passes, a test fails each Startup check in turn and asserts that no order reached the broker's stand-in, a test runs Startup with every check passing and asserts it received no order and no cancel, and a test lets a readiness wait expire and asserts the check fails. A failure follows rule 5 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off), which says what a release still serves after each kind of failed check, and a store finding follows rule 3 below. The book's second run, after the switch, is here reconciliation on every fill under rule 2 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs) and the synthetic event in shadow under the monitoring rules. No Startup check sends an order or a cancel in any environment, so Startup adds no sender beside the order path and its code is the same everywhere. The synthetic event checks shadow's round trip against the simulated broker, and the first real order is what exercises the real send path, under the operator's check of the first sessions in the charter's Production step. A check that starts something waits on a readiness check with a timeout, never on a fixed sleep, and a wait that expires fails the check. The book calls its own fixed pause something to use rarely (DT p. 200), and this doc makes it never.
2. **The owner's merge deploys to shadow and a healthy shadow to paper, and the pipeline deploys nothing to live.** Farcic's continuous deployment runs from a commit to production with no human intervention (DT p. 20). His continuous delivery stops one step short, where every build that passes could be deployed and a person decides when (DT p. 19). Hard limit 6 makes the merge and every deploy to live the owner's, so this platform takes continuous deployment as far as paper and continuous delivery at live. The pipeline ends at an artifact whose checks passed, recorded under rule 4 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off), and the owner deploys it to live. No workflow run whose code comes from a branch other than `main` holds any credential, broker or cloud, to a shadow, paper or live account or a data vendor, and no workflow run at all holds a broker credential, since the design on [#10](https://github.com/l3a0/ithildin-runtime/issues/10) gives the broker key to the execution worker's user alone. So the scheduled contract suite against the paper account is not a workflow run. It shares the one paper account under the owner's 2026-11-04 ruling on [PR #29](https://github.com/l3a0/ithildin-runtime/pull/29), so it never runs beside a session or any other test on that account, and its orders meet the hard limits and the account's lease like any other order and never reach a strategy's positions. [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds how it runs. A deploy accepts only a digest that a run on `main` recorded when it built the commit then at `main`'s head, in a store no other run and no login an agent session holds can write, where that run was started by a push, never by a manual dispatch, `pull_request_target` or `workflow_run`. A credential that deploys the platform is reachable only through a GitHub environment per cloud account, which admits `main` alone with no administrator bypass, and live's environment alone names as its required reviewer an identity no agent session holds, under the owner's 2026-11-04 ruling on [#32](https://github.com/l3a0/ithildin-runtime/issues/32), since a merge is the owner's act and deploys to shadow and paper with no second person. No login an agent session holds, on GitHub or in the cloud, can change such an environment, bypass `main`'s ruleset or assume a role that deploys or applies the code. Each account's deploy role trusts only its own environment, so a paper deploy cannot reach live. A deploy to shadow runs only on a push to `main`, a promotion to paper only on its schedule on `main`, and a manual dispatch on `main` only behind live's reviewer, so the owner's merge stays the only way onto shadow and paper. An apply to shadow or paper runs on a push to `main`, and one to live only behind live's reviewer. None runs on `pull_request_target` or `workflow_run`, which run with `main`'s privileges whatever started them, and each runs nothing but `main`'s head. The build board's Pages environment deploys on its own and is outside this rule, since it holds no credential to the platform. These credential rules are this doc's, not the book's, and [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds how they are built.
3. **Every machine is built from committed code, a check of that code against what it built finds no difference, and the code cannot destroy what cannot be recovered.** Farcic runs every step from the delivery server, and finds no need to log in to a server and work on it by hand (DT p. 88). A second run of his playbook finds almost nothing left to do (DT p. 190). Here the hosts, their users and grants, the queues and the buckets are declared in committed code, which every environment applies alike, differing only in the keys of the committed list under rule 4 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off), which names where each credential is read from and holds none, whose state stays out of the repository and holds no secret, and whose plan output and deploy logs stay out of any public log or artifact. An apply to shadow or paper follows the owner's merge, and an apply to live is the owner's by hand, under the owner's 2026-11-04 ruling on [#32](https://github.com/l3a0/ithildin-runtime/issues/32). An apply that touches execution, its roles and grants, its queue, the host's restart settings and switch, or the stores of the journal, the halt state or the lease happens between sessions, as rule 5 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off) has a release do, since a change to any of them can stop a cancel, so a merge's apply waits for the session to end, unless the owner overrides it for an incident, as [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds. An infrastructure change rolls back by a new commit on `main`, applied like any other change, that restores the code before it and detaches, never destroys, any protected store the change created. It rolls forward by bringing that store back under the code by its recorded id, as [#32](https://github.com/l3a0/ithildin-runtime/issues/32) builds. The price is that a rollback waits on a merge, which hard limit 6 makes the owner's. The paper gate drills both. After each change and on a schedule, the code runs in a mode that only reports, under a role that cannot write and reads no stored data, and a difference is drift, which alerts. Each scheduled check reports under the first monitors' heartbeat, and a result missing past a written bound alerts the owner. A change made by hand during an incident goes in the operator's change log, and is then written into the code or undone. Neither the code nor any role that applies it can delete, overwrite, append to, expire or recreate the journal, captured data, their backups, the halt state, a journal store's first-start record, the lease, apart from the noncurrent versions its rewrites leave, or execution's queue, write the hard caps, release a halt, a loss stop or a kill switch, write an override, or read, overwrite or delete a broker or data-vendor credential, grant read of one to any principal but the one the Configuration table names, or change who can act as that principal. The operator acts through a principal of its own in each paper and shadow account, which can read the log and state stores, call the restart switch, pull the kill switch and run the report-only check. It sits under the same denies, except what the owner's rulings on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) give it: on a paper or shadow account it may also release a halt, a loss stop or a kill switch and write an override, so a release this group gives the owner is the operator's too there, and so is clearing a lease, and a pull on any account journals itself. A release's rollback by its pin on a paper or shadow account is the operator's too, under the owner's 2026-10-30 ruling on [#11](https://github.com/l3a0/ithildin-runtime/issues/11), as an exception to rule 2's GitHub environment beside the owner's own hand on a pin, and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds how. An infrastructure rollback stays the owner's. In a live account the operator acts only as the owner's 2026-10-30 ruling on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) allows, so the live restart switch is the owner's, and it reads the log and state stores there under the owner's 2026-11-04 ruling on [PR #29](https://github.com/l3a0/ithildin-runtime/pull/29). That guard sits where the role it binds cannot edit it, a check on every plan refuses any such change whether or not the store's block is still in the code, and [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds how both are built and tested. A protected store found missing or created again never reads as empty. It halts new orders until the owner releases the halt and pages the owner, naming the store and saying whether any instance can still serve cancels, with the broker's interface when none can, and it names the store where the halt-state monitor reads it unless the halt state is the store that is gone. A journal store is behind when a copy holds an entry that reads and verifies and the store does not. A journal store found missing, empty or behind follows the procedure for an unreadable journal in [Every fact has one writer](#every-fact-has-one-writer) and takes no entry until it is restored, only by the owner's hand, so its cancels wait for the restore and the page names the broker's interface. A journal verified to a non-empty head whose comparison with a copy cannot finish is not such a failure, and a copy reporting a position no entry backs counts as such a comparison. It halts new orders until the owner releases the halt, alerts naming the copy and still serves cancels and the kill switch, and the price is that an outage of a copy's store at a start stops new orders until then, and an entry it writes stays through any restore under rule 1 of [Recorded data is append-only](#recorded-data-is-append-only-and-every-view-of-it-is-rebuilt). A journal store starts empty only once, on a record that says this is the first start, and evidence that is absent or unreadable halts under hard limit 3. [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds how these are compared, spent and restored. A halt-state store created again starts halted until the owner releases it. A lease store created again never restarts the lease number, under the lease guards in [Every fact has one writer](#every-fact-has-one-writer). A missing queue is created again only by the owner's hand. Capture keeps spilling to its local disk and never halts, and a captured-data store found empty or behind its copies counts as missing. In slice 1 marketlake protects its own stores, and this platform reads only its record and freshness. Every interface that can control a host or a container takes only authenticated calls from the identities the design names for it. The book's own setup listens for remote requests from any address and says a real one must be much more restrictive (DT p. 92). Deployment and provisioning code follows the rules for the code itself, so it swallows no error. The book's function that reads which release is live returns an empty value on any failure (DT p. 246). An outage there reads as no release live, and the next deploy can land on the release that is serving. Here a deployment state that cannot be read halts the deploy and names what it could not read, and a CI test feeds it an unreadable state, as another feeds the drift check a plan with a difference.
4. **An automatic restart, reschedule or scale step is capped, recorded and reported, a halt is never a failure to heal, and a missing reading leads to no step at all.** Farcic's self-healing at the level of the system restarts a failed process or scales a service by its response time. He warns that recovery often needs a person to find the cause, so a notification goes with it (DT p. 302). He finds a bare redeploy incomplete, because someone needs to be notified and what came before the failure examined (DT p. 263). His redeploy brings back the release that ran before the failure and builds nothing (DT p. 331), while Docker's `always` policy restarts a container without limit (DT p. 345). Here a restart starts the digest the open session pinned, under rules 4 and 5 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off), and the step that restarts reads the release's record and never writes it. An execution restart that cannot confirm its digest serves nothing and pages the owner, naming the broker's interface, under rules 4 and 5 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off). A record that cannot be read takes no restart and alerts, except for the components named below as keeping on. One implementation counts each component's restarts in a written window, and [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds which, apart from the two named below. The count runs across a component's instances. One instance past its own limit, execution's and capture's apart, is taken out and alarms while the others carry on, and the cap is reached when the count across instances reaches it or no instance is healthy. Each component's class under this rule is written down, a component with no written class counts as one the order path depends on, and the replay harness's restarter is a stand-in under rule 5 of [The code holds its own rules](#the-code-holds-its-own-rules-and-every-check-runs-in-production), so a crash's outcome has one definition. A window can let a slow crash loop pass, so any restart during a session of a component the order path depends on pages the owner as well, and a component of any kind whose restarts pass a written count over a session or a day pages the owner. One continuing cause raises one page, repeated at a written interval, and never holds back a different alarm. The cap of N lets the Nth restart run, and the crash after it is the one that reaches the cap. Each restart, reschedule and scale step is recorded with the component, its exit or reason and its count, and reports under the first monitors' heartbeat, so that list stays at nine. A count that survives a move to another machine, kept outside the journal's store, is the written next step for when components first move between machines on their own, and [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds how it is built. A crash while consuming a broker message, a journal entry or captured data halts under rule 6 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs) before the cap applies, and the restart comes up halted. A component counts as one the order path depends on when trading reads its output or relies on a check it runs, such as reconciliation or the clock's checks. Reaching the cap on such a component halts trading, writes the component, its last exit and the count where the halt-state monitor reads them, and pages the owner, and the halt waits for the owner. Any other component pages at its cap and stops restarting. Two kinds of component keep on past their cap, because stopping them loses something that cannot be recovered or leaves nothing serving a cancel. Capture keeps restarting and never halts, and reports time not recording under the first monitors' data freshness. In slice 1 capture is marketlake's, so its restarts are marketlake's and this platform reads only that freshness. Execution keeps restarting halted, on any exit while a session is open, a clean one included, so it can still serve cancels and the kill switch if its crash lies in the trading path. Each restarts with a delay that grows to a written maximum, and pages the owner, and execution's maximum, plus the time from a start to serving a cancel, sits below monitoring's kill switch deadline, and that sum is a written limit under rule 2 of [Design ahead](#design-ahead-and-write-down-where-the-build-stops). For these two the implementation that counts the others does not stop restarts, each comes up halted where it must, and [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds how. Any other instance that can take the account's lease keeps serving cancels and the kill switch. When none can, the page says so, and the owner's step is to cancel in the broker's interface, as it is when the journal's store is down. A halted component keeps running and reports itself halted, so no health check reads a halt as a failure, and a restart reads the halt state through the charter's Startup step. The book's own average over the last hour comes back null when no entry falls inside it (DT p. 383), and here a null, stale or failed reading takes no step and alerts. Once a scaling step first runs, execution's scaling has a written floor of instances able to answer a kill switch event within monitoring's deadline under rule 5 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off), and falling below the floor alarms under the first monitors' heartbeat. No scaling step takes a component above the written scale limit that rule 2 of [Design ahead](#design-ahead-and-write-down-where-the-build-stops) makes it state, sized for execution against the broker's rate limit. The operator can switch restarts, reschedules and scaling off for each component without a deploy, leaving it running, so an incident's evidence is not restarted away, and the switch never stops capture's restarts or execution's restarts into a halted state. A flip is recorded off the machine, where [#32](https://github.com/l3a0/ithildin-runtime/issues/32) names, the switch's position travels in every heartbeat, and a switch found off at a session's start alerts, naming the component. An unreadable switch takes no restart, reschedule or scale step and alerts, except that capture and execution keep restarting, and execution comes up halted when it cannot read its switch or its count, finds its switch absent at any start, or finds its count absent during a session. Execution's halts on an unreadable switch or count and on a flipped switch wait for the owner. With its restarts switched off, execution still restarts halted and pages the owner, and the page names the broker's interface only when that halted restart fails too. A crash of any other component that takes no restart, for any reason, counts as reaching its cap. The code declares the switch and never its position, so a flip is no drift and no apply changes it. The switch takes only authenticated calls from the owner and, outside a live account, the operator. The book's scale-down simply removes the instances it no longer wants (DT p. 286). Here an instance that scaling or rescheduling removes stops taking work, and its open broker calls and the lease's takeover follow [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and the rules in [Every fact has one writer](#every-fact-has-one-writer).
5. **Every component's log lines leave its machine as structured records, into one place that can be searched, no log write waits on that place, and the journal is never one of them.** Farcic finds a service's logs spread over several files and several servers, and gathers them into one store that can be searched (DT p. 349, p. 350, p. 362). The owner ruled on 2026-10-29 on [#7](https://github.com/l3a0/ithildin-runtime/issues/7) that the machine keeps no state, so a log kept there is lost with it. Here each component writes structured lines, those a decision led to carrying its id under rule 5 of [Monitoring answers whether there is a problem first](#monitoring-answers-whether-there-is-a-problem-first), through a bounded buffer that drops and counts what it cannot write rather than wait, under that section's rule 4. A stalled log pipeline then never stalls the order path, which rule 1 of [Keep the order path isolated](#keep-the-order-path-isolated) lets monitoring fail without. The buffer's fill and the shipping lag alarm under the first monitors' heartbeat at 75% of the buffer's bound, under rule 2 of [Design ahead](#design-ahead-and-write-down-where-the-build-stops), below the point where a line drops. Lines still in the buffer at a crash go with the process, and the buffer's bound caps how many. Comparing lines written with lines received waits for real use, under the owner's 2026-11-04 ruling on [PR #29](https://github.com/l3a0/ithildin-runtime/pull/29), and [#32](https://github.com/l3a0/ithildin-runtime/issues/32) holds the deferral. The price is that a line lost after the buffer goes unnoticed until an incident looks for it. The log store sits in the environment's own cloud account, a component's credential for it can only write, under the component's own prefix, and only the owner and the operator read it, and no line from any component carries a credential, token or account number. The lines are diagnostic data, with the owner and end date the data-cost rule below gives them. The journal and captured data are recorded facts with their own stores, so no log pipeline, retention rule or cleanup reaches them.

The faults this group adds to the list in rule 1 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs) inject its failures at run time. The failures that happen at deploy time, which are a Startup check that fails on a new release, a deployment state that cannot be read, a drift the check reports and an infrastructure change rolled back and forward, each have a CI test and a drill on the paper host under the charter's Release table. The restart cap and a crash loop slower than its window run under the host's real supervisor, as [#32](https://github.com/l3a0/ithildin-runtime/issues/32) builds, and every alarm the group adds has a drill.

### Monitoring answers whether there is a problem first

1. **The first monitors answer "is there a problem", and there are between three and ten of them.** Monitoring built bottom-up from host metrics produces alerts that teams learn to ignore, and then a customer reports the outage first (AoS L10210, L10223, L10355). Here the first monitors are these nine.
   1. P&L against expectation.
   2. Exposure against limits.
   3. Fill and reject rates.
   4. Slippage against the cost model.
   5. Data freshness.
   6. Orders and cancels in an unknown state, and reconciliation mismatches.
   7. Capture-disk headroom, and how far the off-machine copy trails.
   8. Whether the platform is halted, and why.
   9. The heartbeat.

   "Where is the problem" and "what is the problem" come after, in that order (AoS L10335).
2. **Every halt is a fixed rule, and statistics only escalate.** The book alerts when a value leaves several standard deviations of its own history at the same time on similar days (AoS L10168, L10313). Here that applies only to counters that behave like rates, such as fills, rejects and message rate, and only after the baseline holds a stated number of sessions. Until then a fixed threshold stands in, and the statistical alert is drilled on recorded history. A limit breach, the loss stop, a reconciliation mismatch and missing or stale input each halt by a fixed rule, because hard limit 3 cannot wait for a statistic.
3. **Latency is recorded as histograms and reported as percentiles, and histograms combine by adding.** A mean hides how many requests saw a delay, the slow tail is where the busiest accounts tend to sit, and averaging percentiles from several sources is meaningless (DDIA L518, L523, L536, L583). A request that waits on several calls is as slow as the slowest of them, so the tail grows with every call added (DDIA L569). So every latency monitor and every timing in the release comparison is a percentile, such as the median and the 99th, read from merged histograms. A load test sends on its schedule whether or not the last reply has come back, since a harness that waits for each reply keeps its queues artificially short (DDIA L551). Order latency is timed at the broker adapter, where the waiting the caller sees is.
4. **Every queue and buffer states its bound, in time as well as size, and what it does when full, and capture never discards data to stay inside its bound.** A backlog can grow unseen until the disk fills, as when compaction falls behind the writes with nothing to slow them (DDIA L2766). A full queue can drop, buffer or push back on its sender (DDIA L13767). A vendor feed cannot be pushed back on, so capture buffers and spills to disk and never drops a message. Only a queue that feeds a derived consumer, such as the decision loop's latest price, may drop a value a later one replaces, and diagnostic log lines may drop under rule 5 of [A release is checked where it runs](#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault). Each counts every drop. A log kept in a fixed-size ring drops its oldest entries when it fills (DDIA L14037), which for captured data is loss. How far a consumer trails the head of a log is the number to alarm on (DDIA L14056). Here that lag is measured on the capture buffer and on the copy to the off-machine store, and reports under the first monitors' data freshness and headroom, so the list stays at nine. The capture disk alarms at 75% of its limit under rules 2 and 4 of the first section. A journal write that fails halts trading under the one-writer rules.
5. **One id runs from a decision through everything it leads to, and each client order id contains it.** Newman finds that correlation ids are very hard to add later and advises adding them from the start (BM L3354). Here the decision's id is fixed with the decision, from nothing a failover or a restore could wind back, and the risk verdict, the journal entries, the broker calls, every log line and every fill carry it. Each client order id contains that id, so a decision that sends several orders gives each a unique id that still traces back to it, and the rest of the id's format, including where the lease number sits, is [#10](https://github.com/l3a0/ithildin-runtime/issues/10)'s to fix. A cancel carries the id of the order it cancels, and the kill switch's own entries carry an id of their own. A test replays a day and asserts that every order, verdict and fill maps to exactly one decision, that every cancel maps to the order it cancels and names what sent it, a decision, a halt or the kill switch, that outside, lease and halt entries carry a kind of their own, and that a decision sending two orders gets two client order ids that share its id.
6. **A synthetic event runs through the whole loop on a written schedule, in replay or in a shadow deployment against the simulated broker, and nothing built from it reaches the paper or live account.** Newman's example is an investment bank's trading system, where a fake event entered every minute or so and an alert fired when its result did not appear, which he found a far better sign of trouble than lower-level metrics (BM L3250, BM L3300). Here a shadow deployment of the same artifact runs beside paper and live for as long as they run, and the event enters only that loop's own input files, never captured data and never a file the paper or live loop reads. A marker carries from the event to every record built from it. In replay and shadow the gate holds a marked decision to every limit and routes it to the simulated broker alone. In paper and live the gate refuses a decision built on a marked input, and the broker adapters refuse any marked order and journal it as provably not sent, and each refusal halts, alerts as misrouting and waits for the owner, with a test for each. The comparison of shadow's orders with a replay of the same day leaves marked records out. The event watches shadow's loop and reports as shadow's, so a fault confined to paper's or live's broker connection, lease or account is left to the heartbeat and the other first monitors. Replay and shadow run as a user that cannot read the broker's credential. The result reports under the first monitors' heartbeat, so the list stays at nine, and a result missing past a written bound alerts the owner. The operator owns it, and the paper gate's alarm drills include it.

### Data has a cost, except the data that cannot be recaptured

1. **Every class of derived data has an owner and an end date.** Data costs more than its storage, and the book lists six costs that grow with it (AoS L8947). Most of its value falls with age, so keeping it forever needs a stated value and a time limit (AoS L8963, L9168). Diagnostic logs, intermediate backtest output and feature tables follow that rule. Derived data is rebuilt by a command rather than repaired by hand, since DDIA counts tools that recompute data among the ways to recover from human error (DDIA L391). Recorded data is exempt as a class: captured data, every snapshot a decision read, execution's account file, which holds the broker state its recover stage captured, the journal, every backup of any of them, and every broker statement the platform saved. No lifecycle or cleanup rule may match a path that holds it. A snapshot leaves the class only once a test has rebuilt that snapshot, byte for byte, from the sealed day it was read from, and only while the marketlake version it names still exists. A snapshot read from marketlake's segments before the day was sealed never leaves, because a segment and the partition built from it never share bytes. An account file never leaves, because nothing can read the broker's past state again. The operator's change log, restart records and incident evidence are kept as long as the operator sets on purpose, rather than by this rule. A legal duty to delete, such as a vendor's license terms, is the one exception, and it is a written procedure the owner carries out by hand, naming every copy it must reach, never a rule that runs on its own. The procedure appends a record of what it removed, which the read-back and the restore drill treat as expected. DDIA notes that immutable stores and backups make deletion hard, and that collected data is a hazard to keep only while it is used (DDIA L\~14509, L17128).

### Buy what does not set the venture apart

1. **A component is built here only when it passes the book's four questions.** Does it set the venture apart from competitors? Is this team the best owner of it? How soon would a competitor catch up? Can it be built and supported for less than buying (AoS L5500)? The book finds it "not uncommon" to underestimate support cost by an order of magnitude for infrastructure that runs around the clock (AoS L5320). Here support lands on the owner's hours, so a build is priced in operator hours a year, not in tokens. The four answers and that price go on the issue, and the call is the owner's. The charter's build-or-buy note names the alpha models and the research platform as what the team builds. A bought component inside the loop runs in process, through the same interface, in every environment. One that runs only at the broker sits behind the broker adapter, and its simulated stand-in is named as a gap in the model.
2. **A vendor's own features sit behind the adapter at its edge, with an issue to retire them.** Deep integration with one provider raises the cost of leaving it (AoS L6727). When a crisis forces one in, the book's rule is to wrap it and commit to removing it (AoS L6794).
3. **The off-the-shelf options are written down, so a buy is a choice rather than a search.** The table lists what exists for each module the charter's build-or-buy note says can be bought, as found on 2026-11-03, and what slice 1 uses instead. Slice 1 buys none of it, since every path runs at 1x on what the platform already holds. The first purchase worth weighing is data, on the day a strategy needs history marketlake does not hold, and the second a factor risk model, once the platform trades enough names that an exposure nobody chose can hide among them.

| Module | Commercial | Open source or free | What slice 1 uses |
| --- | --- | --- | --- |
| Data | Bloomberg, LSEG Refinitiv, CRSP, Norgate, Databento, Massive, which was Polygon.io until its rebrand on 2025-10-30, and Alpaca's market data | OpenFIGI for the identifiers a security master maps | marketlake's own capture, [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Risk model | MSCI Barra, Axioma, now SimCorp's under Deutsche Börse, Northfield and Bloomberg PORT, each a factor model across thousands of stocks | Riskfolio-Lib, skfolio, PyPortfolioOpt's risk models, and scikit-learn's Ledoit-Wolf shrinkage of a covariance matrix | The risk gate's simple limits, [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Portfolio construction | Axioma's and Barra's optimizers, and the solvers Gurobi and MOSEK | CVXPY, cvxportfolio from Stanford's convex optimization group, PyPortfolioOpt, Riskfolio-Lib and skfolio | Equal risk weight, [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| Execution algorithm | A broker's algorithms, such as Interactive Brokers' Adaptive, Arrival Price, TWAP and VWAP, and execution platforms such as FlexTrade and Bloomberg EMSX | An exchange's closing auction, which a market-on-close order joins | Market-on-close orders at Alpaca, [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| Transaction cost model | Trade cost analysis from Virtu, which bought ITG in 2019, and from Abel Noser | The standard form: half the bid-ask spread, plus commission, plus an impact that grows with the square root of the order's share of the day's volume | The flat and linear cost models, [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |

The table was checked against [Massive's rebrand notice](https://massive.com/blog/tag/announcement), [SimCorp's Axioma risk model release](https://www.simcorp.com/about-us/news/2025/simcorp-launches-improved-axioma-worldwide-equity-factor-risk-model), [Riskfolio-Lib's documentation](https://riskfolio-lib.readthedocs.io/), [skfolio's site](https://skfolio.org/), [cvxportfolio on PyPI](https://pypi.org/project/cvxportfolio/), [Interactive Brokers' algorithm list](https://www.interactivebrokers.com.hk/en/trading/orders/adaptive-algo.php) and [Virtu's 2019 annual report](https://www.sec.gov/Archives/edgar/data/0001592386/000159238620000003/virtufinancial12-31x19.htm). The other names come from general knowledge of the field, so check each against its vendor's current page before relying on it. The table is a snapshot of a market that moves, and a buy decision re-checks it rather than trusting it.

The price of these rules is that every path carries its mechanisms from its first run, and a design names its next split before any code needs it. DDIA's rules add a checksum, a schema identifier, event, send and receive times, a receive sequence and a knowable time to every captured record, a sequence number and a lease number to every journal entry, a wait for stable storage before every send, a seed and a reference-data version to every decision, a copy off the machine and a scheduled read-back of everything recorded, and histograms in place of averages. *Code Complete*'s rules add an import check and a strict lint configuration in CI, a second run of the suite under `python -O`, a type for each kind of value, a contract suite for each stand-in adapter, a second implementation of the money arithmetic in the tests, and a measurement for each tuning change. *Building Microservices*' rules add a timeout per outside call, a limit and a breaker per dependency on the order path, the decision's id on every record, a synthetic event and the shadow deployment it runs in, the pinned image digest, a test on settings that differ between environments, a replay of each release's files through the kept artifact's readers, and an alarm at the rewrite-planning level. The rules of the release group, some from *The DevOps 2.0 Toolkit* and the credential rules and halts this doc's own, add the Startup checks run as tested code in every environment, an environment per account with an approval no agent session holds and a store of built digests only `main` writes, infrastructure code with a scheduled check that must find no difference and roles that deny what the guard forbids, a restart cap per component, with a switch that stops restarts and scaling, and a log pipeline off the machine that never blocks, and a start needs a copy to answer before new orders flow. What they buy is that load reaching 10 times the first takes configuration and more machines rather than a rewrite, and that a rewrite before 100 times replaces code while what the directive's paragraph names as surviving carries over unchanged, which is what the owner's directive asks.

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

Steve McConnell's *Rapid Development* (Microsoft Press, 1996) supplies the
rules for estimating, cutting and tracking the work, cited by printed page,
so RD p. 199 is page 199. The book writes from studies of human teams in the
1980s and 1990s, so its rules are adopted for their method rather than their
numbers, and how well they fit agent loops is untested. The estimate and
actual pairs rule 2 starts are that test. Its chapters on motivation,
overtime and team structure have no counterpart for agent sessions and are
not adopted. Six rules follow.

1. **A slice builds its most important behavior first, and its usable end
   includes every mechanism the design principles build from a path's first
   run.** McConnell's staged delivery ends every stage with a product that
   meets its size, performance and quality targets as well as its function,
   and delivers capabilities from most to least important (RD p. 553). A
   working product is a more accurate status report than any paper report
   (RD p. 551). Here the order inside a slice is the build board's, set by
   its band labels, its "blocked by" links and the parent issue's
   `Board order:` line, and a slice is usable when the walking skeleton's
   smoke test reaches every module it added with those mechanisms in place.
2. **An estimate is a range drawn from loops of the same kind, posted under
   its own heading, and the next estimate of that kind carries the slip.**
   McConnell's multipliers, adapted from Boehm and others' COCOMO 2.0 of
   1995, put an estimate of effort made at the first concept anywhere from a
   quarter to four times the final figure (RD p. 169). Estimating from memory
   of similar projects is the commonest practice and goes with overruns
   (RD p. 178, citing Lederer and Prasad 1992). So a build issue carries a
   comment opening with `## Estimate` that gives a range in dollars and names
   the loops it was drawn from, which are earlier loops of the same kind, such
   as module builds or review rounds, with the same coverage as the
   `## Cost` figures they are compared with. Where no loop of that kind has
   posted a cost, the first estimate is a labelled guess inside the widest of
   McConnell's ranges. A survey of more than 300 projects found that a
   project behind schedule hardly ever makes up the lost time
   (RD p. 199, citing van Genuchten 1991), and McConnell recalibrates by
   raising what remains by the share of the slip so far (RD p. 381). So when
   a loop's `## Cost` lands above its range, the next estimate of that kind
   raises its range by the same ratio. A later estimate is a new
   `## Estimate` comment rather than an edit.
3. **A loop that runs past its estimate drops behavior from a list written in
   advance, and never a check.** McConnell's timebox drops features rather
   than quality, and he advises against boxing planning, requirements and
   design, whose mistakes cost the most downstream (RD p. 581). Here a build
   issue lists, in order, the behaviors a slip may drop. That list never
   holds anything the ranking rule's three exceptions name, a hard limit, the
   risk gate or any of its limits, a halt, the loss stop, the kill switch, a
   rollback, an alarm or the drill that shows it works, a review, a test, or a
   part the design fixes from the first commit, such as the partition key and
   count or the event and clock interface. A dropped behavior moves to an
   issue of its own, and its acceptance tests stay in the suite as strict
   expected failures that name that issue. A cut that touches the order
   path, the gate or its limits goes to the risk lens before it takes
   effect, and the risk engineer may veto it. A slice whose must-have behavior does not fit goes back to
   planning whole.
4. **Done is the charter's Definition of done, and never a percentage.**
   McConnell's test of a finished piece is whether it could be locked in a
   vault and shipped as it stands, so a piece "99 percent done" is not done
   (RD p. 381). A report of done links the merged pull request that closed
   the issue, its `## Review` comment with the mutation result, and the
   checks that ran the changed code. A check that never ran the changed code
   shows nothing.
5. **Each slice keeps a list of the risks to its delivery, and a risk to
   something that cannot be undone ranks above every one that can.**
   McConnell's top-10 risks list names the risks to a project's schedule and
   gives each its rank this week and last, its weeks on the list and the
   progress on resolving it (RD p. 85, p. 100 to p. 102). He ranks risks by
   probability times size, then moves a larger loss above its place in that
   order (RD p. 95, p. 96). Here a risk that could lose one of the premise's
   three things ranks above every other, ordered among themselves by the risk
   engineer's severity and detection scores, which still decide splits and
   detectors for each change. The list ranks what to watch, never what to
   build next, which the ranking rule in [CLAUDE.md](../CLAUDE.md) decides.
   It sits on the slice's parent issue under a `## Risks` heading. The
   status round reviews it whenever it names a next card from the slice,
   under the `/l3a0:sync-prune-next` skill's step for it, and posts a dated
   comment opening with `## Risks review` even when nothing changed, so the
   reviews can be counted. The round reports a slice with no list rather
   than writing one.
6. **A spike answers one stated question, is exploratory, and never becomes
   production code, and shared code waits for a design that names it.**
   McConnell treats each throwaway prototype as an experiment with a
   hypothesis, and stops it once the hypothesis is settled (RD p. 572).
   Prototyping is exploratory by nature, so a spike's result is
   [exploratory](#vocabulary) whatever it stated in advance. A spike lives
   in a top-level `spikes/` folder that no module under `src/` imports. It
   reads data that cannot be recaptured only through copies, never reads the
   holdout window, sends no order, paper included, and holds no broker
   credential. A question about a strategy's returns goes through the
   engine's backtest, never a spike. A spike that kills an idea is pinned
   under CLAUDE.md's research pins rather than deleted. A reusable component
   costs two to three times as much as a one-off and breaks even only after
   two or three uses, McConnell citing Biggerstaff's rule of three, so his
   rule of two builds a component as a one-off first and considers making it
   reusable at its second use (RD p. 535, p. 536, citing Tracz 1995). The
   shared types and interfaces that
   [#22](https://github.com/l3a0/ithildin-runtime/issues/22) lands before
   any module are a design named ahead of use, not reuse. Code the sibling
   repositories keep using moves to
   [ithildin-core](https://github.com/l3a0/ithildin-core) and is depended on
   at a pinned version, and code salvaged from a sibling is only code that
   sibling retires (RD p. 529).

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
| **blob** | One object in storage, written once and never replaced, except a lease, which its holder rewrites only by a condition on the tag it last got |
| **output file** | A blob holding one thing a stage produced for one decision, at a key fixed by the run, the stage, the decision and the output's name |
| **stage** | One step a component takes in a decision. Execution's are recover and send, and monitoring's are reconcile and report. Every other component has one |
| **decision result** | The blob a stage writes last for a decision, at a key fixed by the run, the stage and the decision, listing its output files by key and SHA-256 or saying the decision halted. One key, so a stage ends each decision exactly once |
| **receive loop** | The one event loop in each worker, and in the supervisor, that reads its queue and hands each item, a stage and a decision, to a free handler. [How a component learns of a change](#how-a-component-learns-of-a-change) gives its rules |
| **handler** | One thread of a worker's pool, which works on one item at a time, independently of the worker's other handlers |
| **event** | The store's announcement that a blob was created or deleted, which reaches the queue of every component that reads it. An event is a hint, and the blob is the record |
| **listing** | A component's stages' blobs, listed by decision. Each blob's header names its run, decision, inputs by key and SHA-256, release and configuration, so the listing is the component's record |
| **plan** | The blob the supervisor writes once per run, naming the run's mode and its decisions in order |
| **queue** | The one place a worker listens for events: SQS in shadow, paper and live, and a named pipe in a backtest and in CI. A worker waits on it by long polling the queue and never reads storage on a clock |
| **supervisor** | The process that writes a run's plan under the plan's lease, whose event reaches every worker, hears the workers' heartbeats and the kill switch as events, starts each stage's deadline when it hears that stage's last input, writes a halt as a worker's result when the worker fails, and stores a halt of the run on such a failure, whose event reaches every worker. It starts, stops and watches no process, and keeps no state outside storage. A deployment runs several supervisor instances, which take the plan's lease to change the run and compete for the supervisor's queue |
| **run** | Everything one run writes, kept under the run's own prefix in storage. The VM keeps only a working directory for the run's error output and heartbeat files, none of which is kept |
| **halt** | A decision result saying why a decision cannot go on, naming the reason or the halt it met. Every worker downstream passes it on and skips that decision. The owner or the operator clears it, except on a live account, where only the owner does, and clearing never reruns the decision. |
| **span** | The decisions in a run's plan, which every worker works through: one trading day's in shadow, paper and live, and the whole history in a backtest |
| **decision** | One moment at which a deployment decides, keyed by its time in UTC, as the owner ruled on 2026-10-30. Every stage writes one result per decision |
| **strategy** | The instruments, schedule, data needs, alpha model and configuration that differ from one way of trading to the next. Every other module is shared by all strategies |
| **schedule** | A strategy's list of decision times on each trading day. One function in `core` turns the schedules and the trading calendar into a plan's decisions |
| **environment** | A set of resources one of test, shadow, paper and live runs on: a machine, a bucket, roles, a broker credential, a heartbeat check and a release pin. Shadow, paper and live each run the mode of the same name. Test is GitHub's CI, which runs backtest mode among its checks |
| **deployment** | One or more strategies on one broker account in one mode, under whose name every key a run writes sits. An account belongs to one environment and carries one deployment there, whose keys and lease sit in that environment's bucket |
| **order path** | The path from the target portfolio through the risk gate and the journal to the broker adapter. Its synchronous dependencies are the calls on it that leave the process or wait on input or output |
| **written scale limit** | The most load an implementation handles as built, stated beside the code that enforces it, as distinct from the most its design allows |
| **lease number** | The number each grant of an account's lease carries, higher than every earlier grant and every number the journal or the broker's client order ids hold, recorded in every journal entry and client order id. The journal's store refuses a write that names an older lease |
| **event time** | When the source says an event happened, as the source's own timestamp states it |
| **receive time** | When capture received a record, read from the shared clock |
| **send time** | When the source says it sent a message, on the source's clock, where the feed carries one |
| **receive sequence** | The gap-free number capture gives each record in the order it received it, used to order replay |
| **knowable time** | The earliest moment the platform held a value: the receive time for live capture, and the vendor's publication date or vintage for history. The point-in-time interface reads by it |
| **walking skeleton** | A build in which every module exists in its simplest form and the whole loop runs end to end |
| **exploratory** | A result found by looking at the data. It kills an idea or justifies a closer look, and is never a verdict |
| **registered** | A result whose hypothesis was committed in writing before the number was seen. Only a registered result can promote a strategy |

## Configuration

The runtime's VM keeps no state, so it keeps no configuration that matters on
its own disk. A setting comes from the release the pin names, from a blob in the environment's bucket, or, for which environment a VM is in, from the provisioning step at boot. The owner ruled on 2026-10-30 that test, shadow, paper and live run in separate environments. This doc proposes that each has its own bucket, VM, roles and broker credential, that the release carries one table of the environments, and that the provisioning step at boot tells a VM which environment it is in, as [architecture.md](architecture.md#four-environments)'s `### Four environments` says. The owner ruled on 2026-10-29 that the broker key
lives in the same store marketlake uses, AWS Systems Manager Parameter
Store, as a `SecureString` parameter encrypted under KMS, with AWS's managed
`aws/ssm` key ruled fine, and on 2026-11-02 that each environment runs in an
AWS account of its own, so each key sits in its own environment's account,
as [architecture.md](architecture.md#where-it-runs)'s rule 3 and its
`### Four environments` rule 11 say. This doc proposes the rest: each environment's key sits under a path of its own below `/ithildin/`, which every role but execution's refuses, by the refusals and the service control policy that architecture.md's rule 3 of `## Where it runs` lists, and every other credential is readable
only by the user that needs it, through a credential the VM's provisioning
gives that user alone. Nothing here sits in a file tracked in this
repository.
[architecture.md](architecture.md#where-it-runs) lists each credential the
design needs and what it may do. The owner's own machine may still keep
developer settings under `~/.config/ithildin-runtime/`. The owner updated
`CLAUDE.md`'s `## Secrets and machine paths` on 2026-10-29 to match.

No setting exists yet. Each credential this design uses gets a row here in the
change that first reads it, naming where it lives and what reads it, and a
short-lived role records what it trusts in place of where a secret lives. A credential lives in the environment of the run, in the machine's
keychain, or for the broker key in AWS Systems Manager Parameter Store under the owner's 2026-10-29 ruling on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), and a long-lived secret never in a file. The table takes the columns Setting, Secret,
Lives in, Read by and May do, and the broker's row states that it holds no
withdrawal or transfer permission and how that was checked.

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
| A writer sending a message to each reader, and timed checks of the next key | The owner ruled on 2026-10-31 to avoid polling and to model the change event on S3's notifications. The kill switch follows the same rule since the owner dropped its direct message on 2026-11-01: a pull writes the switch blob, whose own event wakes execution, and execution also reads the switch when it starts and right before every order. A writer's message reached only the readers it knew on its own host, and a timed check cost a read every few seconds for each reader and still noticed late. The store's event covers every writer, a person off the VM included, and an absence is caught by a deadline instead |
| A pull's direct message to the queues of execution, the supervisor and monitoring | The owner ruled for it on 2026-10-31 so a pull would not wait on S3's event, and dropped it on 2026-11-01. The supervisor cannot cancel anything, the switch blob's event already reaches the same three queues, and execution checks the switch before every submit. Dropping it removed three queue grants for people's roles, a refusal path in the tool, a fake message in the folder adapter and a drill row |
| Delayed SQS messages or EventBridge Scheduler for deadlines, and a watchdog that reads heartbeats through S3 | A review of the first change-event design on 2026-10-31 found both wanting. A delayed message survives a restart in SQS but not in a named pipe, Scheduler needs a role passed to it that rule 5 of architecture.md's `### Four environments` refuses, and a heartbeat event can arrive a minute late. Deadlines are timers in memory, set again from storage on restart, and the watchdog watches local files |
| A queue that pushes to the worker: SNS or EventBridge to an HTTPS endpoint, Amazon MQ, or Kinesis enhanced fan-out | The owner asked on 2026-10-31 for an alternative to long polling that pushes. None shortens the time from a write to a worker waking, which S3's handoff to EventBridge dominates, and each adds an inbound port, a broker that bills by the hour, or a stream's fees. Measured delay on paper from SQS's renewal would reopen Amazon MQ or Kinesis |
| Hand trades in a platform account, kept apart in an outside book | The owner first ruled on 2026-10-31 that the platform ignores unstamped orders, so the owner could trade the same account by hand, and the doc built an outside book that reconciliation and the gate counted. Later that day the owner gave the platform an account of its own, and on 2026-11-01 ruled that each portfolio has its own account with no hand trading in it. So the outside book went, and an order the platform did not send is journaled as received and halts, as [Orders the platform did not send](#orders-the-platform-did-not-send) says |
| The broker's own replace call, which swaps a resting order for a new one in one request, and sending a replacement before its cancel is confirmed | The owner ruled on 2026-10-31 that a plan can change or cancel an unfilled order. A cancel and then a new order keep every broker write on the one order path with a journal entry each, and waiting for the cancel's outcome means the old and the new can never both fill unseen. The price is a gap between the two orders, and a replacement that misses the send cutoff |
| The gate classifying every cancel, and a resend passing that classification again | Slice 1 has no protective order, and open orders are counted at their worst case, so no limit could refuse a cancel. The owner ruled on 2026-11-04 on [#10](https://github.com/l3a0/ithildin-runtime/issues/10) to drop it from rule 6 until [#7](https://github.com/l3a0/ithildin-runtime/issues/7) adds an unwind, which brings it back with the first cancel a limit could refuse |
| One process per action, such as the order manager and the order path each in its own | The owner ruled on 2026-10-29 that this was too granular, and narrowed the ruling to one process per component. A component is the unit the charter gives an interface and an owning role |
| More than one execution process sending on one broker account at a time | The risk gate checks limits across every order and position in the account's journal, so two gates at once could each approve orders that together breach one. A restarted supervisor beside a still-running order path sent every order twice in a review's simulation, which is why rule 9 takes a lease. Several execution processes may run, as the owner ruled on 2026-11-01, and the account's lease lets one of them send at a time |
| Each worker handling one message at a time | The owner ruled on 2026-11-01 for a receive loop that hands each item to a pool of handlers. A pull's cancels then need not wait behind a plan in the same execution instance, and a backtest's independent stages run decisions side by side |
| One instance of each component in slice 1 | The owner ruled on 2026-11-01 for three instances of each component, for high availability, and on 2026-11-02 kept all three on slice 1's single VM, where they cover a process that crashes or hangs while the host restarts it. The price is three times the containers on that one VM, 24 of them and about 2.4 GB, and a long polling bill past SQS's free million |
| One supervisor beside several instances of each worker | The owner ruled on 2026-11-01 that the supervisor runs several instances, and on 2026-11-02 that they take the plan's lease to change the run. The price is a record that the plan's event arrived, and a lease blob kept for each run whose conditional writes carry the run's status |
| A deployment's lease, renewed for the whole session | Once the cluster ran every VM at once, the lease guarded only facts that never change during a session, the run and its release, and the check that keeps a drill and a session apart. The owner ruled on 2026-11-01 that no lease covers the deployment or the session. The price is that no expiry signals that every supervisor has gone quiet, which monitoring's supervisor-heartbeat alarm reports |
| A session record written once per run, naming the run and its release | The owner ruled on 2026-11-01, after it was briefly written in, that no session record is needed. The run's plan is already written once per run and already names its release, and the plan and the run's stop say whether a session is open |
| A queue for each component instance, with a router in front of each group that forwards each message to one instance's queue | The owner ruled on 2026-11-01 to keep one queue per component, which SQS already makes the group's router: it hands each message to one instance, only a live instance with a free handler receives, and a message a dying instance held returns for another. A router would need three instances of its own, each instance's health and free handlers published for it to read, and a way to move messages out of a dead instance's queue, and would roughly double the long polling requests. Work that must land on one particular instance, such as every message of one account on one instance, would reopen it |
| A queue for each supervisor instance, so the instance that wrote the plan receives the plan's own event | The owner ruled on 2026-11-01 to keep the create-only record that the plan's event arrived. The supervisors share one queue, so SQS hands the event to one instance, often not the writer, and the writer's deadline reads the record when it expires, the way every deadline reads storage before it acts. A queue per instance would need EventBridge to copy the event to each, queues created and deleted as the Auto Scaling group starts and ends VMs, and an exception to one queue per component, for one check a session |
| The runtime in marketlake's AWS account, its environments kept apart by lists of refusals and bucket policies | The owner ruled for it on 2026-10-29 and confirmed it for live on 2026-10-31, then reversed both on 2026-11-02 for an AWS account per environment. One IAM mistake in a shared account could cross environments, and marketlake's roles needed refusals a CI check had to keep complete as marketlake grew. The price of the reversal is four runtime accounts and a management account, and a read grant from marketlake for each environment |
| Test's bucket and queues in shadow's AWS account, scoped by resource name | The owner ruled on 2026-11-02 that test keeps an account of its own. Test is not all in memory: on each push to `main` the store and queue suites run against a real bucket and real queues, through a role GitHub assumes, and one drill lets a test principal delete a journal version on purpose. Sharing shadow's account would save one account, at the price of test and shadow kept apart by policy alone and an organization policy scoped to shadow's bucket rather than the whole account |
| AWS Backup with a logically air-gapped vault, in place of S3 Replication, for the live copy in a second account | The owner ruled on 2026-11-02 to keep S3 Replication. Its copies are ordinary objects the replay reads with no restore step, and its rule copies only the journal, the overrides and the captures tagged as recorded. AWS Backup restores a whole bucket or prefix to a point in time through a restore job, and copies derived outputs too. Wanting a point-in-time restore of the whole bucket would reopen it |
| Another instance finishing a decision's planned orders once its planner has died | The owner ruled on 2026-11-02 to keep halting the decision. The plan rests on the broker's state at its planner's last sync, and the planner may have died partway through an order, so finishing would send on a plan nothing checked again. The taking-over instance settles each order in doubt, abandons the unsent ones, cancels a half-accepted pair's accepted leg before the cancel cutoff, and halts that decision, and the next decision plans afresh from the book. The price is a decision's remaining orders never going out when its planner dies |
| A Merkle tree for the journal, as in Certificate Transparency | It proves one entry, or one copy's history, in about log2 of the journal's length, which matters when outsiders check a log they do not trust. The owner is the only auditor and a hash chain with checkpoints and a head anchor already compares two copies by one hash. Many outside verifiers, or a journal too long to walk, would reopen it |
| CRDTs, structures that merge copies written in parallel | Every fact here has one writer, so nothing is ever merged |
| An ordered queue per account, such as SQS FIFO | A kill switch's event could wait behind a plan's, and nothing needs the ordering, since the state lives in the keys |
| A Bloom filter for duplicate fills | The set of fill ids is small and exact and already sits in the journal, and a Bloom filter answers only probably |
| One VM per environment, with a replacement started by hand | The owner ruled on 2026-11-01 for a cluster of three VMs across three availability zones, and on 2026-11-02 that slice 1 starts on one VM per environment, with the cluster as the scale-out path, for a design load the directive of 2026-11-03 sets at 10 times slice 1's. Even the one VM sits in an Auto Scaling group of one, which replaces it without a person |
| Designing for 100 times the first load | The owner ruled for it on 2026-11-02 and adopted Jeff Dean's rule in its place on 2026-11-03, because a design that promises no rewrite at 100 times tends to build for load it may never see. The design now promises 10 times and writes down where the rewrite's planning starts |
| A container orchestrator, such as ECS on EC2, EKS or Nomad, placing containers across the cluster | Three identical VMs that each run one instance of everything need no placement, and an orchestrator's control plane has run zero times here, which is also why Fargate is turned down for slice 1. VMs that need different mixes of containers would reopen it, which groups of identical VMs mapped by hand do not, and so would the rewrite past 10x that `## Design for 10x, plan the rewrite before 100x, build at 1x` expects |
| Rolling a new release out VM by VM during a session, by instance refresh, CodeDeploy or an ECS rolling deployment | Each tool assumes two versions can serve side by side while it rolls. Here two releases in one run write different bytes to one key, which the create-only rule turns into a halt, so a release changes only at a session's start, through the pin and the promotion from shadow to paper to live. The owner ruled on 2026-11-01 that instance refresh rolls out changes to the VM image instead, outside sessions |
| AWS CodeDeploy for the cluster | Its agent installs a release on each VM, which the pin and the image pull already do, and it adds an agent and a role to every VM. Instance refresh covers the VM image without either |
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
| Shadow, paper and live sharing one VM and one bucket, kept apart by key prefix and by the account a lease names | One wrong role or one wrong prefix in a command could reach every mode, such as a drill pulling the live switch or a shadow run reading a broker key. The owner ruled on 2026-10-30 for separate environments, one each for test, shadow, paper and live, on 2026-11-01 that shadow and paper share one environment in slice 1, and on 2026-11-02 that shadow keeps machines of its own after all, as [architecture.md](architecture.md#four-environments) says |
| Shadow and paper sharing one environment in slice 1, each with its own containers, units, Unix users, pin and key prefix | The owner ruled for it on 2026-11-01 and reversed it on 2026-11-02, so shadow keeps its own VM, bucket and roles. The price is a second VM, bucket and set of roles in slice 1, a third once live runs, and a cluster of three VMs for each once load calls for the scale-out |
| The book's N+1 design, rule of three and multiple live sites | Redundant compute and a second live site guard availability the platform does not yet need, since it runs one paper account on one machine at 1x. The owner moved a three-VM cluster out of the first slice on 2026-11-02. They return when an availability target the platform has measured calls for them. What cannot wait is the copy of what cannot be recaptured, which the premise and the design principles already cover |
| Multi-leader replication and last-write-wins merging | DDIA calls multi-leader replication dangerous territory to avoid where possible, and finds that last-write-wins, the default in many replicated databases, silently drops concurrent writes (DDIA L5203, L7499). Every fact here has one writer, so neither is needed |
| A distributed transaction across the journal and the broker, and a consensus algorithm written here | Two-phase commit spreads one participant's failure to all of them, and a broker cannot take part in one. DDIA advises using an existing coordination service rather than writing consensus (DDIA L11519). The journal before the send, an end-to-end client order id and reconciliation give the same result. [Coordinate only where a mistake cannot be undone](#coordinate-only-where-a-mistake-cannot-be-undone) carries the reasoning |
| Compiling assertions out of the production build | *Code Complete*'s default for assertions (CC p. 190), though it keeps the code that checks for important errors (CC p. 209). Here what is tested must be what runs live, and Python's `-O` drops every `assert`, so a guard raises explicitly and runs in every environment. [The code holds its own rules](#the-code-holds-its-own-rules-and-every-check-runs-in-production) carries the reasoning |
| A neutral value, the closest legal value, or logging and carrying on, as the answer to bad input | *Code Complete* offers each one (CC p. 194, p. 195). Each turns missing or invalid input into a value the platform acts on, which hard limit 3 forbids wherever trading reads an input |
| Breaking parts of production on purpose, as Netflix's Chaos Monkey does | *Building Microservices* describes programs that switch off production machines at set hours to keep teams ready for failure (BM L4252). Here production is the owner's capital, which hard limit 1 keeps out of agents' reach, so faults are injected in replay under the faults rules instead |
| Choreography inside execution's send sequence | Newman prefers choreography, where each part reacts to events on its own, over orchestration by one coordinator (BM L\~964, BM L\~1028). Between components the owner's 2026-10-29 ruling already makes a new output the trigger. Inside execution, the sequence through the risk gate, the journal and the send is one ordered path, because hard limits 2 and 4 need every order to pass both in that order |
| Storing only the data a business operation needs, applied to market data | Newman cites the German principle of storing only the personal data an operation or a law requires (BM L\~3802). The principle stands for personal data. Captured market data holds none and is recorded whole as received, because the premise counts it among what cannot be recovered. |
| Never resending a lost cancel as the rule, leaving any further cancel to the next decision or a decision's timeout | The owner deferred a plan's cancel alone on 2026-11-04 on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), until a strategy rests orders across decisions. The owner chose "Resend if tested" over it on [#10](https://github.com/l3a0/ithildin-runtime/issues/10), confirmed 2026-11-04 UTC with this option in view. Rule 6 of [Faults are expected](#faults-are-expected-and-the-platform-checks-itself-while-it-runs) carries the rule |
| The book's "scale out, not up" and commodity hardware | For a sole operator a bigger machine is often the cheapest correct next step, and the hardware argument dates from 2009. The design principles keep the part that holds: each design names its next split, so moving to more machines takes configuration rather than a rewrite |
| Deploying every passing commit to production with no human step | *The DevOps 2.0 Toolkit*'s continuous deployment runs from a commit to production with no human intervention (DT p. 20). Hard limit 6 makes every deploy to live the owner's, so continuous deployment here stops at paper, and live takes the book's continuous delivery (DT p. 19). [A release is checked where it runs](#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault) carries the rule |
| Feature toggles that hold unfinished work switched off in production | The book calls them indispensable for continuous deployment, because they let unfinished work merge switched off (DT p. 22). Continuous deployment here stops at paper, so that reason does not reach live. A strategy's off switch is its halt, and a toggle that could switch on live trading, raise a limit or route an order past the risk gate would be a way around hard limits 1 and 2 |
| A service registry and a reverse proxy in front of the components | The book finds each running service through a registry and routes requests to it through a proxy (DT p. 100, p. 144). Here components exchange files, under the owner's 2026-10-29 ruling on [#7](https://github.com/l3a0/ithildin-runtime/issues/7), and read queues, so none calls another by address |
| Restarting a component without limit, as Docker's `always` policy does | The book lists the policy, which restarts a container whatever its exit status (DT p. 345). A restart without limit hides the cause of a crash loop, so each component here has a written cap, and only capture, and execution restarting halted so cancels stay served, keep restarting past their cap, with a growing delay and a page to the owner. Rule 4 of [A release is checked where it runs](#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault) carries the rule |
| A fixed sleep to wait for something to start | The book pauses its playbook for a fixed time and says such a pause should be rarely used (DT p. 200). A fixed sleep passes or fails by timing, so every check here that starts something waits on readiness with a timeout |
| Comparing log lines written with lines received, at the first load | The drop count, the fill alarm and the shipping-lag alarm already show loss at the buffer, and the comparison adds only loss after it, on diagnostic lines that may drop anyway. The path has run zero times, so the owner deferred it on 2026-11-04 on [PR #29](https://github.com/l3a0/ithildin-runtime/pull/29) until real use shows lines lost past the buffer |
| Switching back to the earlier release on its own after a failed check | The book switches back to the release it read when a check after the switch fails (DT p. 249). Hard limit 6 makes a deploy to live the owner's, so a failed check here halts or serves as rule 5 of [Every change can be undone or switched off](#every-change-can-be-undone-or-switched-off) says, and a person rolls back: the owner on any account, the operator on shadow and paper, under the owner's 2026-10-30 ruling on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
