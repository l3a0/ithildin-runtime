# Architecture — ithildin-runtime slice 1

Seven module issues are about to be built by seven separate sessions, and the
pieces they share are the expensive thing to change later. A bar type, an
order type and the one path an order takes to the broker are read by every
module. Getting one wrong after four modules use it means four rewrites. This
doc draws those shared pieces now, while disagreeing with them costs an edit.

Slice 1 is the walking skeleton on
[#4](https://github.com/l3a0/ithildin-runtime/issues/4): the first version of
the platform in which every module exists in its simplest form and one
strategy runs end to end, every trading day, on a broker's paper account, a
practice account that trades with no real money. Each
module issue, [#5](https://github.com/l3a0/ithildin-runtime/issues/5) to
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), still decides what
its module must do. Where this doc and an issue disagree, the issue wins.

The owner ruled on 2026-10-29 that each component runs as its own process and
passes everything to the others through files, so a failure stays inside one
process and every input and output can be read afterwards. So the diagrams
draw a supervisor launching one worker per module, rather than one program
calling each module in turn. The owner added the same day that the platform
is an event pipeline with a queue per component: each component listens for
events on its queue, reads its inputs from files and writes its outputs to
files, and a message reaches a component's queue whenever there is something
new for it. Each component's SQLite event log points to its files and is the
source of truth. In this doc's design, a worker sends those messages right
after it commits an event.
[design.md](design.md#processes-and-files)'s `## Processes and files` carries
the rules the workers follow.

The owner's part in reviewing this doc is two sections at the end.

1. [Decisions](#decisions), a table where each row says what this doc chose, what it
   turned down and where the choice gets settled. Object to a row there.
2. [Questions for the owner](#questions-for-the-owner), ten questions that
   need an answer before the build starts. Each carries a recommendation, so a
   yes is enough.

The diagrams are Mermaid, which GitHub draws in place. Each one is followed by
what it shows and what to look for. Nothing here is built yet.

## Contents

- [The strategy the skeleton runs](#the-strategy-the-skeleton-runs)
- [Context: what the platform talks to](#context-what-the-platform-talks-to)
- [The loop and its adapters](#the-loop-and-its-adapters)
- [Data flow](#data-flow)
- [One trading day](#one-trading-day)
- [When something is wrong](#when-something-is-wrong)
- [Types and interfaces](#types-and-interfaces)
- [The life of an order](#the-life-of-an-order)
- [The life of a run](#the-life-of-a-run)
- [What is stored](#what-is-stored)
- [Where it runs](#where-it-runs)
- [Package layout and the import rule](#package-layout-and-the-import-rule)
- [How the four acceptance tests exercise it](#how-the-four-acceptance-tests-exercise-it)
- [Decisions](#decisions)
- [Questions for the owner](#questions-for-the-owner)

## The strategy the skeleton runs

Acceptance test 1 asks the backtest to reproduce a strategy that
[quantitative-trading](https://github.com/l3a0/quantitative-trading) has
already replicated. A mismatch then points at the platform rather than the
idea, because the strategy's numbers are already pinned there.

The proposal is Chan's Bollinger band on the price spread between GLD, a gold
ETF, and USO, an oil ETF. It is Example 3.2 of *Algorithmic Trading* and
Entry 26 of that repository's replication log. A Bollinger band is a moving
average with a band of one standard deviation on each side, and this strategy
trades when the spread leaves its band. Read at quantitative-trading's commit
`19aadbeb`, the rule is four steps.

1. Each day, fit the hedge ratio `h`, the number of GLD shares that offset one
   USO share. It is the slope of a regression of USO's close on GLD's close
   and a constant, over the last 20 closes.
2. Form the spread `USO − h·GLD`.
3. Measure how many standard deviations the spread sits from its own 20-day
   mean. That count is the spread's z-score.
4. Go long one unit of the spread when the z-score falls below −1, and exit
   when it rises above 0. Go short one unit above +1, and exit below 0.
   Otherwise hold. One unit is `+1` share of USO and `−h` shares of GLD.

Four facts make it the simplest candidate there.

1. **Two liquid ETFs, daily closes only.** No minute data, no futures rolls, no
   stock panel.
2. **One committed source.** `data/inputdata_etf/gld.csv` and
   `data/inputdata_etf/uso.csv` were lifted from Chan's `inputData_ETF.mat`, cover
   2006-04-26 to 2012-04-09, and each has a recorded SHA-256 checksum. Each
   file is a vintage, meaning one source's copy of a series stored once and
   never changed, as [design.md](design.md#vocabulary) defines it.
3. **A discrete unit.** The number of spread units held is only ever −1, 0 or
   +1, so a test can check it on each day, not only a summary ratio. The share
   counts are not discrete. One unit holds `−h` shares of GLD, and `h` is a
   regression slope refitted daily, so the GLD leg is fractional on every day
   the strategy holds a position. The replication never deals in shares. It
   turns units, the hedge ratio and prices into a dollar position per leg, so
   test 1 checks the unit and the dollars, never a share count.
4. **Tight pins.** `tests/test_bollinger.py` pins the APR at 0.17824928 and the
   Sharpe ratio at 0.96467289, both to 5e-9, over 1,480 rows. It also pins 162
   changes of unit, made of 77 entries, 76 exits and 9 reversals.

The engine has to carry four quirks of the replication rather than tidy them
away.

1. The strategy refits the hedge ratio every day, so it resizes the GLD leg
   even on days the unit does not change.
2. The return is profit over the gross dollars held, with no capital account.
3. The moving standard deviation divides by `n − 1`, as MATLAB does. Dividing
   by `n` moves the APR to 0.183306.
4. The replication charges no transaction cost, so the cost model in
   [#8](https://github.com/l3a0/ithildin-runtime/issues/8) runs at zero for
   test 1. Paper trading then charges a flat or linear cost.

Every quantitative-trading result is exploratory, meaning found by looking at
the data, and this one is too. Test 1 uses it as a known answer for checking
the engine. Running it on paper promotes the platform's version, not the
strategy, because the charter's backtest gate for a strategy also needs a
registered out-of-sample test, which this one has not passed. Registered means
the hypothesis was committed in writing before the number was seen.

## Context: what the platform talks to

A context diagram shows the platform as one box and everything outside it that
it talks to.

```mermaid
flowchart LR
    owner(["Owner"])
    operator(["Operator, an agent role"])
    subgraph platform["ithildin-runtime"]
        loop["The live loop<br/>data, alpha, risk, cost,<br/>portfolio, execution, monitoring"]
    end
    qt[("quantitative-trading<br/>committed vintages")]
    vendor[("Vendor download<br/>recent sessions, once")]
    lake[("marketlake<br/>captured and imported bars")]
    broker["Broker<br/>paper account"]
    alerts["Alert channel"]
    heartbeat["Heartbeat service<br/>outside the machine"]
    github["GitHub<br/>code, issues, board"]

    qt -->|"imported once"| lake
    vendor -->|"imported once"| lake
    lake -->|"bars, through one query"| loop
    loop -->|"orders"| broker
    broker -->|"acks, fills, positions"| loop
    loop -->|"daily report, alarms"| alerts
    loop -->|"ping at the end of each run"| heartbeat
    heartbeat -->|"alarm when a ping is late"| alerts
    alerts --> owner
    owner -->|"live switch, kill switch"| loop
    operator -->|"kill switch"| loop
    owner -->|"merges, pins a release"| github
    github -->|"pinned release"| loop
```

The platform reads bars from one place, marketlake, the sibling repository
that captures market data before anything computes on it. The sources differ
in volume, cadence and shape, so each is transformed into marketlake once,
stored durably there, and read by every mode through one query. The runtime
runs on a VM of its own and reads a local mirror of the partitions it
downloads from marketlake's bucket, as `## Where it runs` says. The owner ruled
so on 2026-10-29, and [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) builds it. marketlake then holds three kinds of
bar.

1. **Captured bars,** which shadow, paper and live trading read.
   [l3a0/marketlake PR #928](https://github.com/l3a0/marketlake/pull/928) added GLD and USO, with daily and minute bars, to
   marketlake's roster on 2026-10-29.
   Capture starts once the owner onboards them on marketlake's VM.
2. **quantitative-trading's committed vintages,** imported once, which the
   backtest reads.
3. **A one-time vendor download** of the most recent sessions, imported once.
   The strategy's first z-score needs about 40 sessions of closes: 20 to fit
   the first hedge ratio, which Chan's script then drops with the first 20
   rows, and 20 more spreads to fill the z-score's 20-day window. Without the
   drop it is 39. This download covers them until capture does.

Every bar says whether it was captured or imported, and where an imported one
came from, so a consumer can refuse imported history where only captured data
will do. Imported history never overwrites a captured partition.

For every mode to read the bars it needs through one query, one marketlake
rule has to change. Its VM keeps 22 sessions on its own disk and deletes older
partitions of every kind at one cutoff once its backup bucket holds them. The
2006 to 2012 history, and a lookback longer than 22 sessions, would then drop
out of a read with no error, which is exactly the silent gap a halt exists
for.

The owner ruled on 2026-10-29 to make that window configurable, and
[l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) builds it in three parts.

1. **A default per kind of data.** Option chains, quotes and bars each keep 22
   sessions unless set otherwise, so nothing changes until an override is set.
2. **An override per kind.** Bars are set to keep every session, which keeps
   both the runtime's lookback and the imported history readable. Bars add
   about 0.1 MB a session, against about 564 MB for option chains, as
   [l3a0/marketlake#786](https://github.com/l3a0/marketlake/issues/786)
   measured.
3. **An override per ticker and kind,** for a later strategy that needs one
   ticker's data kept longer.

marketlake refuses a window below what its own readers need, and a
configuration that would not fit the VM's disk.

It sends orders to one place, the broker's paper account.

Two switches come from outside the loop.

1. **The live switch** moves the broker from the paper account to a real one.
   Only the owner holds it, which is hard limit 1 in
   [CLAUDE.md](../CLAUDE.md).
2. **The kill switch** halts trading. The owner ruled on 2026-10-28 that the
   risk engineer builds it and writes when to pull it, and the operator pulls
   it by that guidance. The owner can pull it too.

A dead-man check raises an alarm when a ping it expects fails to arrive. Here
monitoring pings a heartbeat service as each run's last step, and the service
alarms when a ping is late. The service sits outside the machine because a
check on the same machine dies with it, and a machine where nothing runs is
the failure the check exists for.

## The loop and its adapters

[design.md](design.md#one-runtime)'s `## One runtime` says backtest, paper and
live run the same modules and differ only in adapters at the edges. The usual
name for that shape is ports and adapters. A port is an interface the modules
define, and an adapter is one implementation of it that talks to the outside
world. No module imports an adapter.

The owner ruled on 2026-10-29 that each component runs as its own process and
passes everything to the others through files, and that the platform is an
event pipeline. [design.md](design.md#processes-and-files)'s
`## Processes and files` gives the reasoning and the rules. So each module
here runs as one worker, a process of its own. The supervisor starts every
worker of a run at once, and each one listens on its own queue. A message there
says an input event log has something new, and the worker then reads the
input files its events point to, writes its own output file, and appends an
event pointing to it. A worker's entry point builds the adapters for the
mode it is given and hands them to the module, so the module never learns
which mode it is in.

```mermaid
flowchart TB
    sup["Supervisor<br/>starts every worker at once,<br/>keeps no state of its own"]
    ctl[("control log<br/>one event per session")]
    sup --> ctl

    subgraph workers["Workers, one process per module, identical in every mode"]
        direction TB
        data["Data"]
        exec_recover["Execution, first<br/>orders in doubt, fills,<br/>positions, open orders"]
        mon_recon["Monitoring, first<br/>reconcile"]
        alpha["Alpha model<br/>Bollinger band"]
        risk["Risk model"]
        cost["Cost model"]
        pc["Portfolio construction<br/>equal risk weight"]
        exec_send["Execution, then<br/>order manager, risk gate,<br/>journal, order path"]
        mon_report["Monitoring, last<br/>report, heartbeat"]
    end

    snap[("data's event log<br/>and snapshot files")]
    xs[("execution's event log<br/>account and send outcome files")]
    book[("monitoring's event log<br/>reconciled book files, halts handled")]
    fc[("alpha's event log<br/>and forecast files")]
    cons[("risk's event log<br/>and constraints files")]
    sched[("cost's event log<br/>and cost schedule files")]
    target[("portfolio construction's event log<br/>and target portfolio files")]

    ctl --> data
    ctl --> exec_recover
    data --> snap
    exec_recover --> xs --> mon_recon --> book
    snap --> alpha --> fc
    snap --> risk
    book --> risk --> cons
    snap --> cost --> sched
    fc --> pc
    cons --> pc
    sched --> pc
    book --> pc --> target
    target --> exec_send
    cons --> exec_send
    snap --> exec_send
    exec_send --> xs
    xs --> mon_report

    subgraph adapters["Adapters, built by each worker's entry point"]
        lakesrc["marketlake reader<br/>over the local mirror"]
        clock["History clock or wall clock"]
        broker["Simulated or paper broker"]
        journalstore["Journal store"]
        alertsink["Alerts and heartbeat"]
    end

    lakesrc -. "DataSource" .-> data
    clock -. "Clock" .-> data
    broker -. "Broker" .-> exec_recover
    broker -. "Broker" .-> exec_send
    journalstore -. "JournalStore" .-> exec_send
    alertsink -. "Alerts" .-> mon_report
    sup -. "starts" .-> workers
```

Each cylinder is one worker's output files and the SQLite event log that
points to each of them. A solid arrow into a worker is an event log it reads.
A new event there comes with a message on the worker's queue, which runs its
processing loop, and the event it appends after writing its own file sends a
message that runs the next worker's. Every worker reads the control log,
which the diagram draws only into the two that start the day, and monitoring
reads every event log, which the diagram leaves out to stay legible. Each
worker writes one event log. Execution's points to its account files and its
send outcome files, and monitoring's points to the reconciled book files and
records each halt it has handled. Each dotted arrow from an adapter
is a port, labelled with its name, and each mode is one choice of adapter for
each port. Execution and monitoring each appear twice because each does two
things in one process. Execution recovers first and then, once the target
portfolio exists, sends. Monitoring reconciles first and reports last.

Two groups of workers run at the same time.

1. Data and execution's recovery share no input, so they work together.
2. Alpha and cost need only the snapshot, and risk also needs the reconciled
   book, so the three models overlap.

Execution sends only what the target portfolio asks for, so nothing it sends
can come from a snapshot or a book that is not on disk yet. When it sends, it
checks each order against the book it replays from the journal at that
moment, not against an event.

| Mode | Data source | Clock | Broker, and when it fills | Journal | Alerts and heartbeat |
| --- | --- | --- | --- | --- | --- |
| Backtest | marketlake reader, the bars a test names | History clock | Simulated, at the signal's close | A journal of its own, kept with the run directory | Recorded, not sent |
| Shadow | marketlake reader, captured bars, plus the imported download until capture covers the lookback | Wall clock | Simulated, at the next open | Shadow journal | Sent |
| Paper | marketlake reader, captured bars, plus the imported download until capture covers the lookback | Wall clock | Paper broker, at the next open | Paper journal | Sent |
| Live | marketlake reader, captured bars, plus the imported download until capture covers the lookback | Wall clock | Live broker, at the next open | Live journal | Sent |

Every mode reads bars through the same marketlake reader. The modes differ in
which bars they read and which clock drives them, not in how they read. Shadow
reads live bars and decides real orders, but fills them in the simulated
broker, so no order reaches any broker. The diagram leaves out the
live broker, because only the owner switches to it.

[design.md](design.md#one-runtime) names three adapters, the ones that change
what a strategy sees.

1. The data source.
2. The clock.
3. The broker.

The journal store and the alert channel also differ by mode, but they change
only where records go. A backtest of 1,480 sessions must not append to the
paper journal or page the owner. The Decisions table proposes adding those two
to design.md's `## One runtime`.

A module that asks which mode it is in has broken the shape, because its
backtest would then test a branch the paper run never takes. The account a
worker's lock and kill switch file belong to comes from its broker adapter, so
a backtest's simulated account never shares the paper account's lock. The
simulated broker keeps its resting orders and positions in a file per
simulated account beside the journal, so a shadow order sent one evening is
still there to fill at the next open, in a new process. The event that
design.md says modules are written against is one event in an input log. In a
backtest each worker works through every session in turn, and in the other
modes through one, as design.md's `### A worker works through a span of
sessions` says.

## Data flow

A data flow diagram shows where data comes from, which process changes it, and
where it is kept, with no sense of time. The cylinders are stores, the boxes
are processes, and the rounded boxes are things outside the platform. The
numbers name the processes, not their order. The next section gives the order.

```mermaid
flowchart LR
    vendor(["marketlake"])
    brokerx(["Broker"])
    ownerx(["Owner"])

    p1["1 Data: record the snapshot"]
    p2["2 Execution: recover"]
    p3["3 Monitoring: reconcile"]
    p4["4 Alpha: forecast the spread"]
    p5["5 Risk: set constraints"]
    p6["6 Cost: write the cost schedule"]
    p7["7 Portfolio construction"]
    p8["8 Execution: plan, gate,<br/>journal and send orders"]
    p9["9 Monitoring: report and alarm"]

    d1[("D1 Run directory,<br/>an event log and files per worker")]
    d2[("D2 Security master")]
    d3[("D3 Journal")]
    d4[("D4 Run reports")]

    vendor -->|"bars"| p1
    d2 -->|"instrument ids"| p1
    p1 -->|"snapshot, naming each bar read"| d1
    brokerx -->|"order states, fills, positions"| p2
    p2 -->|"orders settled, fills"| d3
    d3 -->|"replayed book"| p2
    p2 -->|"book, broker positions and open orders"| d1
    d1 -->|"book, broker positions and open orders"| p3
    p3 -->|"reconciled book"| d1
    d1 -->|"snapshot"| p4
    p4 -->|"forecast, hedge ratio, z-score"| d1
    d1 -->|"snapshot, reconciled book"| p5
    p5 -->|"constraints"| d1
    d1 -->|"snapshot"| p6
    p6 -->|"cost schedule"| d1
    d1 -->|"forecast, constraints,<br/>cost schedule, book"| p7
    p7 -->|"target portfolio"| d1
    d1 -->|"target portfolio,<br/>constraints, snapshot"| p8
    d3 -->|"book, replayed before each check"| p8
    p8 -->|"every step, before it happens"| d3
    p8 -->|"approved orders"| brokerx
    brokerx -->|"acks"| p8
    p8 -->|"send outcome"| d1
    d1 -->|"halt events, send outcome"| p9
    d3 -->|"orders and fills"| p9
    p9 -->|"report"| d4
    p9 -->|"alarms"| ownerx
```

Every arrow into or out of D1 is a file and the event that points to it, both
kept after the run, each event naming the events it came from. That is what makes each decision
readable afterwards. The security master is the table that maps each ticker to
the platform's own instrument id, so a vendor's renamed ticker does not become
a new instrument.

Three things the diagram rules out.

1. **Only execution talks to the broker.** Processes 2 and 8 are the one
   execution worker. Orders go out through it, and the broker's acks, fills,
   positions and open orders come back through it. Monitoring reconciles
   against the event execution appended rather than asking the broker itself.
   Execution journals a fill only when its client order id has a `sending`
   entry in the journal and the fill does not take that order past its
   quantity. Any other fill halts, because a fill nobody journaled would
   otherwise enter the book and agree with the broker.
2. **The current book comes from the journal.** The book is the platform's own
   record of what it holds and what orders are open. Process 2 replays it from
   the journal, and the broker's positions and open orders are used only at
   process 3, where a
   disagreement halts trading rather than quietly replacing the platform's
   record.
3. **Bars reach the models only after process 1 has recorded which ones were
   read.** A model never reads a feed directly, so every number it acted on can
   be read back later from marketlake. Captured market data is the first of the
   three things [design.md](design.md#premise) says cannot be recovered, and
   marketlake is where it is kept.

## One trading day

A sequence diagram shows the same pieces in time order. This is one paper
session with nothing wrong. The run starts each evening after marketlake's
18:30 sweep, and the orders it sends fill at the next open. The notes split it
into three phases. The supervisor starts every worker at the start, and a
`par` block marks workers working at the same time. Each dashed arrow from
the logs to a worker is a message on that worker's queue.

That sweep holds the session's minute bars but not its daily bar, because
marketlake fetches a session's daily bar only once the next session has
sealed. So which bar the evening run reads for the session's close is open, as
question 10 asks.

```mermaid
sequenceDiagram
    autonumber
    participant S as Scheduler
    participant V as Supervisor
    participant F as Files and logs
    participant D as Data
    participant X as Execution
    participant B as Broker
    participant J as Journal
    participant M as Monitoring
    participant A as Alpha model
    participant K as Risk model
    participant C as Cost model
    participant P as Portfolio construction

    S->>V: start the session's run
    V->>F: lock the run directory, record the release
    V->>V: kill switch file absent?
    V->>F: control event for the session
    V->>D: start
    V->>X: start
    V->>M: start
    V->>A: start
    V->>K: start
    V->>C: start
    V->>P: start
    Note over V,M: Phase 1, read the data and recover, in parallel
    par
        D->>F: snapshot file, naming each bar read, and its event
    and
        X->>X: take the account lock
        X->>B: orders in doubt, fills since the last run
        B-->>X: last session's fills
        X->>J: each fill journaled
        X->>B: positions and open orders
        B-->>X: positions and open orders
        X->>F: account file, the book replayed from the journal and the broker's account, and its event
    end
    F-->>M: account event appears
    M->>F: reconciled book file and its event
    Note over V,P: Phase 2, decide
    par
        F-->>A: snapshot event
        A->>F: forecast file, hedge ratio, z-score, and its event
    and
        F-->>C: snapshot event
        C->>F: cost schedule file and its event
    and
        F-->>K: snapshot and reconciled book events
        K->>F: constraints file and its event
    end
    F-->>P: forecast, constraints and cost schedule events
    P->>F: target portfolio file and its event
    Note over X,J: Phase 3, send, in the execution worker still running
    F-->>X: target portfolio event appears
    X->>J: target portfolio's hash journaled
    loop each order the order manager plans
        X->>X: kill switch file absent?
        X->>J: intent journaled
        X->>J: replay the book
        X->>X: risk gate check
        X->>J: approval journaled
        X->>J: sending journaled
        X->>B: send, with its client order id
        B-->>X: ack
        X->>J: ack journaled
    end
    X->>F: send outcome file and its event
    F-->>M: send outcome event appears
    M->>M: daily report, to the reports store
    M->>M: ping the heartbeat service
```

The phases in order.

1. **Read the data and recover.** Data reads the bars the session's lookback
   needs from marketlake, about 40 sessions of each ETF, and records which ones
   it read before any model sees them. At the same time, execution takes the
   broker account's lock, settles any order a crash left in doubt, journals
   the fills that arrived since the last run, and writes the book it replays
   from the journal beside the broker's positions and open orders. Monitoring
   then checks the two agree, position by position and order by order. A run that starts on a book the broker disagrees with would trade
   the wrong difference.
2. **Decide.** The alpha, risk and cost models run at once, each on the events
   it needs. The risk model sets the constraints, meaning the limits for this
   run. Portfolio construction builds the target portfolio within them.
3. **Send.** Execution, which has held the account's lock since phase 1, reads
   the target portfolio file its event points to and journals it whole, in a
   committed entry. A target portfolio names every
   instrument the book holds or the forecast names, with a flat leg as an
   explicit zero, and execution halts on one that leaves an instrument out.
   Its order manager turns the target minus the positions and the open orders
   into orders. Each order goes through the order path alone. The order path
   checks the kill switch again, journals the intent, replays the book from the
   journal so the risk gate sees every order already approved, asks the gate,
   journals the approval, journals that it is sending, and only then sends.
   Monitoring writes the report and pings the heartbeat service last, so a run
   that dies or hangs never pings.

The journal entries carry the weight of hard limit 4, which says every order is
journaled before it is sent. If the execution worker dies after "sending
journaled" and before the ack, the journal holds an order in doubt. The next
execution worker asks the broker for it by its client order id, an id the
platform chooses and sends with the order, rather than sending it again.
Alpaca, the recommended paper broker,
accepts a client order id and can look an order up by it. Whether it refuses a
second order with the same id needs confirming on the paper account before
[#10](https://github.com/l3a0/ithildin-runtime/issues/10) relies on it.

## When something is wrong

Hard limit 3 says missing input means stop, not flatten. One firm's execution
server rebooted before its alpha server, read "no signals" as "hold nothing",
and sold most of its book. The [charter](team-charter.md) cites it at L4690, a
Kindle location in the owner's highlights of Narang's *Inside the Black Box*.
Each diagram below is one way a day goes wrong, and each ends with no new
orders and the positions left as they were.

Every halt takes the same route.

1. The worker that finds the problem appends a halt event for the session,
   with the reason, in place of its output.
2. Every worker downstream meets that halt event in its input, appends its own
   halt event naming it, skips the session and goes on to its next one. A
   worker exits once it has handled every session before the control log's
   end-of-span event.
3. Monitoring, which reads every event log, journals every halt event, raises
   one alarm for the session on the halt that names no earlier one, writes the
   day's report and pings the heartbeat service's failure address.

The same route runs in a backtest, where the span goes on to the next session,
and on paper, where the run ends. A failure, meaning a crash or a missed
deadline, takes it too. The supervisor appends the halt event to the control
log, which every worker reads, and stops every worker but monitoring.

### A missing or stale bar

```mermaid
sequenceDiagram
    participant D as Data
    participant X as Execution
    participant F as Files and logs
    participant M as Monitoring
    participant J as Journal
    participant L as Alert channel

    D->>D: GLD fresh, USO missing
    D->>F: halt event, reason "USO missing for the session"
    F-->>X: halt event, so it skips the session
    F-->>M: halt event
    M->>J: halt journaled
    M->>L: alarm
    Note over D,M: No model reads a snapshot. No order is sent.<br/>Positions stay as they are.
```

A stale bar takes the same path. Stale means the newest close is older than the
session the clock says it is, which is how a feed that stopped looks when it
keeps answering. Data reports the gap and never fills it with the last close.
The charter gives the risk engineer the halt on stale data, so
[#7](https://github.com/l3a0/ithildin-runtime/issues/7) decides how old is too
old. Data applies that rule.

An empty answer halts the same way. If the alpha model writes a forecast file
with no entries and no "trade nothing" mark, portfolio construction halts rather
than reading the silence as a target of zero. A flat position is a forecast
with direction 0, never a missing forecast.

### A restart before the data is ready

```mermaid
sequenceDiagram
    participant V as Supervisor
    participant X as Execution
    participant B as Broker
    participant J as Journal
    participant D as Data
    participant F as Files and logs
    participant M as Monitoring
    participant L as Alert channel

    Note over V: The machine rebooted. Yesterday's execution worker died<br/>after "sending journaled" and before the ack.
    V->>F: lock the run directory, record the release
    V->>V: kill switch file absent?
    V->>F: control event for the session
    V->>X: start
    V->>D: start
    V->>M: start
    par
        X->>X: take the account lock
        X->>J: which orders were sending with no ack?
        J-->>X: one
        X->>J: in doubt journaled
        X->>B: is there an order with this client order id?
        B-->>X: yes, acknowledged
        X->>J: ack journaled
        X->>F: account file and its event
    and
        D->>D: is this session's close in marketlake?
        D->>F: halt event, reason "data not ready"
    end
    F-->>X: halt event, so it exits without sending
    F-->>M: halt event
    M->>J: halt journaled
    M->>L: alarm
    Note over V,L: Execution never sends before the data is ready
```

The restart runs the same first phase as any trading day.

1. Execution settles the order the dead worker left half-sent, by asking the
   broker about it rather than sending it again.
2. Data finds the session's close not yet in marketlake and halts.

The order in which the processes come up no longer matters. Execution may
start before data, as it does here, but it sends only what a target
portfolio event asks for, and that event cannot exist until a snapshot event
does. The charter gives the operator the startup order: execution may send an
order only once every upstream worker has appended its output event, not a
halt event, for the session.
The pipeline makes that order something the platform cannot get wrong. "Not ready" is a halt, never an empty forecast.

### The risk gate refuses an order

```mermaid
sequenceDiagram
    participant X as Execution
    participant J as Journal
    participant B as Broker
    participant F as Files and logs
    participant M as Monitoring
    participant L as Alert channel

    X->>J: intent journaled, buy 900 USO
    X->>X: risk gate check
    X->>J: refusal journaled with the limit and the value
    X->>F: halt event, reason "over the maximum position"
    F-->>M: halt event
    M->>J: halt journaled
    M->>L: alarm
    Note over B: The broker never sees the order
```

A refusal is final, and execution halts rather than sending the rest of its
orders, because portfolio construction sized the rest assuming this one would
go through. Nothing resizes the order to fit either, since an order cut down
to pass the risk gate is a trade nobody chose. Clearing the halt sends nothing
more for that session. The next session plans again from the book.

The daily loss stop works through the same gate. Once the day's loss passes the
stop, the risk gate refuses every new order. [#7](https://github.com/l3a0/ithildin-runtime/issues/7)
lists the limit as "an automatic unwind and stop", and an unwind is itself a
trade. Slice 1 stops only, as question 8 settles.

### The book and the broker disagree

```mermaid
sequenceDiagram
    participant X as Execution
    participant M as Monitoring
    participant F as Files and logs
    participant J as Journal
    participant L as Alert channel

    X->>F: book holds 100 USO, broker holds 90 USO
    M->>M: reconcile, mismatch of 10 USO
    M->>F: halt event, with both positions
    F-->>X: halt event, passed on through risk's constraints, so it exits without sending
    M->>J: halt journaled
    M->>L: alarm
    Note over X,L: No model reads a book until a person<br/>explains the difference
```

A mismatch means either a fill the platform never journaled or a position the
broker changed on its own, and both need a person. Monitoring compares open
orders as well as positions, so an order the broker holds and the journal
marks lost or abandoned halts the day it appears, not the day it fills. Reconciliation lives on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11). The charter has the
trading systems engineer write it, so that role reviews it there.

### The kill switch and the dead-man check

```mermaid
sequenceDiagram
    participant P as Owner or operator
    participant K as Kill switch command
    participant X as Execution, cancelling
    participant B as Broker
    participant J as Journal
    participant H as Heartbeat service
    participant A as Alert channel

    P->>K: pull the kill switch
    K->>K: create the kill switch file
    K->>J: halt journaled, reason "kill switch"
    K->>A: alarm
    K->>K: a message on every worker's queue in each run open on the account
    K->>X: start in cancel mode
    X->>X: wait for the account lock, up to a deadline
    X->>J: orders in doubt settled
    X->>J: each cancel journaled
    X->>B: cancel
    X-->>K: orders cancelled, cancels failed
    K->>A: second alarm if any cancel failed or the deadline passed
    Note over K,X: Every other worker checks the file when it starts<br/>and whenever a message wakes it, and execution before every send

    Note over H,A: A day the run never finishes
    H->>H: no ping by the deadline
    H->>A: dead-man alarm
```

The two cover different failures.

1. **The kill switch** is for a run that is alive and doing something nobody
   wants.
2. **The dead-man check** is for a run that is not alive at all, including a
   supervisor that died, since monitoring pings only as the run's last step.
   Its grace period has to exceed the longest chain of worker deadlines plus
   the report, or a slow, healthy day alarms.

The kill switch is a command, not a running process. It creates a file at one
path per broker account, which every worker reads, and it cancels resting
orders through an execution worker it starts in cancel mode, so the cancel is
journaled like any other step. Two workers run with the switch set, because
neither sends an order: that cancelling worker, and monitoring, whose report
says the switch is set. If a running execution worker holds the account's
lock, it sees the file when the switch's message wakes it or before its next
send and exits, and
the cancelling worker takes the lock once it is free. A switch file that
cannot be read halts with the reason "kill switch unreadable" and the error,
so the journal never mistakes a permission problem for a deliberate pull. The split of the kill switch work is on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11). Orders sent in
the evening rest at the broker overnight, so a switch pulled before the open
stops nothing already sent unless it also cancels them, which is why it does,
as question 7 settles.

### Every alarm and its drill

The charter's paper gate asks for every alarm to fire in a drill first. An
alarm reads zero while it is healthy, so only a drill shows it works. This
shape adds alarms that one process would not need, and each one gets a row
here, with the issue that builds its drill.

| Alarm | Reason it writes | Drill | Built on |
| --- | --- | --- | --- |
| Missing or stale bar | The instrument and its state | A replay with a bar removed or aged, test 2 | [#5](https://github.com/l3a0/ithildin-runtime/issues/5), [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Data not ready | "data not ready" | A run started before marketlake's sweep, test 2 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Risk gate refusal | The limit and the value | One order over each limit, test 3 | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Book and broker disagree | Both positions, or both sets of open orders | A simulated broker account edited by hand | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A fill with no `sending` entry, or past its order's quantity | The client order id | A simulated broker that reports an unknown fill | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A target portfolio that leaves out an instrument the book holds | The instrument | A target portfolio file written without one leg | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| A bad constraints file or hard cap | The field and its value | A file with a missing field, a NaN and a wrong sign | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Kill switch pulled | "kill switch" | Pull it before a run, test 4 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Kill switch unreadable | "kill switch unreadable" and the error | Make the switch's folder unreadable | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A cancel failed or timed out | How many orders were cancelled and which failed | A broker double that refuses a cancel | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A worker crashed | Its exit status or signal and the end of its error output | Kill a worker mid-session | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A worker missed its deadline | The worker and the deadline | A worker that sleeps past it | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A second execution worker, or a second supervisor | The lock that was held | Start two | [#10](https://github.com/l3a0/ithildin-runtime/issues/10), [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A supervisor on a different release | Both releases | Restart mid-run under another tag | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| The scheduler's restart limit reached | The supervisor's last exit | Make the supervisor crash on start | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| An event log row or journal entry whose hash or link does not match, an append the triggers refuse, or an event log that fails SQLite's integrity check | The event log and the sequence number | Alter a row in a copy of an event log and of the journal, and try an `INSERT OR REPLACE` | [#22](https://github.com/l3a0/ithildin-runtime/issues/22), [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| An output file that is missing, or whose SHA-256 does not match the event pointing to it | The file and the event | Change a byte in a copy of an output file | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| An input event a restarted worker names is no longer there | The event log, the sequence number and the hash | Replace an event log a downstream worker has read with a backup copy missing its newest row | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| A second output event for one session, a target portfolio among them | The log and both sequence numbers | Append a second target event by hand | [#10](https://github.com/l3a0/ithildin-runtime/issues/10), [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| A worker exits before handling every session of its span | The worker | A worker that exits after its first session | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| An event for the wrong session, or an output file with no entries and no "trade nothing" mark | The event log, the event and the session | Append one by hand | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| Dead-man | None. The heartbeat service raises it | Skip a run's ping, test 4 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |

Each deadline that the supervisor enforces is set on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), beside the
heartbeat's grace period, which has to exceed them.

## Types and interfaces

A class diagram shows the types the workers pass to each other, each written
as an output file that an event points to, and the interfaces each module implements. Two diagrams follow, one
for the data the workers pass and one for the interfaces.

Three kinds of arrow appear.

1. A plain arrow means one type refers to the other.
2. A hollow diamond means the type holds one it was given.
3. A dotted arrow means the type returns or uses the other.

### The data the workers pass

```mermaid
classDiagram
    direction LR

    class Instrument {
        +InstrumentId id
        +str symbol
    }
    class Bar {
        +InstrumentId instrument
        +date session
        +float open
        +float high
        +float low
        +float close
        +int volume
        +str basis
        +str origin
        +str vintage_id
        +datetime first_seen
    }
    class Snapshot {
        +date session
        +dict bars
        +dict status
        +require_fresh()
    }
    class Basket {
        +dict legs
    }
    class Forecast {
        +Basket basket
        +int direction
        +int horizon_sessions
        +float confidence
    }
    class Constraints {
        +float max_order_qty
        +float max_position
        +float max_gross
        +int max_open_orders
        +float max_net
        +float loss_stop
    }
    class TargetPortfolio {
        +date session
        +dict shares
    }
    class Book {
        +dict positions
        +list open_orders
        +float realised_pnl
    }
    class OrderIntent {
        +str client_order_id
        +InstrumentId instrument
        +float qty
        +str type
        +str time_in_force
    }
    class Fill {
        +str client_order_id
        +float qty
        +float price
        +datetime at
    }
    class Ack {
        +str client_order_id
        +str broker_order_id
        +str state
    }
    class Verdict {
        +bool approved
        +str limit
        +float value
    }
    class CostSchedule {
        +dict per_share
        +dict fixed
        +estimate(instrument, qty) float
    }
    class BrokerAccount {
        +dict positions
        +list open_orders
    }
    class SendOutcome {
        +date session
        +str target_hash
        +list orders
    }
    class HaltEvent {
        +str worker
        +date session
        +str reason
        +EventRef cause
    }
    class ControlEvent {
        +str kind
        +date session
        +str worker
        +int exit_status
        +str error_tail
    }
    class EventRef {
        +str log
        +int seq
        +str hash
    }
    class EventHeader {
        +int format
        +str worker
        +date session
        +dict inputs
        +str file_path
        +str file_sha256
        +str release
        +str config_hash
        +datetime produced_at
    }
    class Halt {
        +str reason
    }

    Snapshot o-- Bar
    Bar --> Instrument
    Forecast --> Basket
    TargetPortfolio --> Instrument
```

### The interfaces

The interfaces are Python `Protocol` classes. A `Protocol` names the methods a
type must have, and any class with those methods satisfies it without
inheriting from it. That is what lets a run swap the simulated broker for the
paper one without either knowing about the other.

```mermaid
classDiagram
    direction LR

    class DataSource {
        <<Protocol>>
        +snapshot(session) Snapshot
    }
    class Clock {
        <<Protocol>>
        +session() date
        +next_session() date
    }
    class Broker {
        <<Protocol>>
        +submit(OrderIntent) Ack
        +lookup(client_order_id) Ack
        +fills(since) list
        +positions() dict
        +open_orders() list
        +cancel(client_order_id) Ack
    }
    class AlphaModel {
        <<Protocol>>
        +forecast(Snapshot) list
    }
    class RiskModel {
        <<Protocol>>
        +constraints(Book, Snapshot) Constraints
    }
    class CostModel {
        <<Protocol>>
        +schedule(Snapshot) CostSchedule
    }
    class RiskGate {
        <<Protocol>>
        +check(OrderIntent, Book, Snapshot, Constraints) Verdict
    }
    class JournalStore {
        <<Protocol>>
        +path(account) Path
    }
    class Alerts {
        <<Protocol>>
        +alarm(reason)
        +heartbeat()
    }
    class Journal {
        -JournalStore store
        +append(entry)
        +in_doubt() list
        +replay() Book
    }
    class OrderPath {
        -Journal journal
        -RiskGate gate
        -Broker broker
        +settle_in_doubt() list
        +journal_fills(since) list
        +broker_account() BrokerAccount
        +send(OrderIntent, Snapshot, Constraints) Ack
        +cancel_resting() list
    }
    class Monitoring {
        -Alerts alerts
        +reconcile(Book, BrokerAccount) bool
        +alarm(reason)
        +report(run_directory)
    }

    Journal o-- JournalStore
    Monitoring o-- Alerts
    OrderPath o-- Journal
    OrderPath o-- RiskGate
    OrderPath o-- Broker
    DataSource ..> Snapshot
    AlphaModel ..> Forecast
    RiskModel ..> Constraints
    CostModel ..> CostSchedule
    RiskGate ..> Verdict
    Broker ..> Ack
    Broker ..> Fill
    Journal ..> Book
```

Eight choices in the two diagrams for a reviewer to check.

1. **A forecast is about a basket, not an instrument.** The Bollinger strategy
   forecasts a spread, which is two legs in a fixed ratio for the day. A
   `Basket` holds `{USO: 1, GLD: −h}`, so one forecast carries the whole
   position and portfolio construction never has to know it is a pair. The
   charter and [#6](https://github.com/l3a0/ithildin-runtime/issues/6) say a
   forecast is per instrument, so this is a proposal for
   [#6](https://github.com/l3a0/ithildin-runtime/issues/6) rather than a
   decision here.
2. **A bar carries where it came from.** `origin` says whether marketlake
   captured it or imported it, `vintage_id` names the vintage it was read
   from, and `first_seen` is when the platform first held it. `basis` says
   which price the series carries, such as `raw` for as traded or
   `adjusted` for split and dividend adjusted, as quantitative-trading's
   vintage records do. `vintage_id` and `first_seen` come from the data
   engineer's duty, in the charter, to store when each value became available
   (L3884).
3. **`Snapshot.require_fresh()` is how a module refuses a gap.** A snapshot is
   the set of bars one session read, each pointing at the partition that holds
   it. A
   module calls `require_fresh()` before reading, and it raises `Halt` when any
   bar it needs is missing or stale. So the default when something is absent
   is to stop.
4. **Only `OrderPath` holds a `Broker`.** Every call to the broker goes through
   it, one method per kind of call: settling orders in doubt, journaling fills,
   reading positions and open orders, sending and cancelling. Nothing else in
   the package is given a broker, and only the execution worker's entry point
   builds one, which the import rule enforces. Test 3's fake broker checks the
   send path itself, as the section on the import rule says.
5. **The risk gate holds no state of its own.** `check` takes the order, the
   book, the snapshot and the run's `Constraints`. `send` replays the book from
   the journal just before each check, so the gate counts every order already
   approved or sending, including those of an earlier attempt. Three of the
   six limits need the snapshot's prices to value the book.
   1. Maximum portfolio size, the `max_gross` field.
   2. Net exposure.
   3. The loss stop.
   The `Constraints` come from the risk model's constraints file, so the order
   path first checks that every field of that file and every hard cap in execution's own
   configuration is present, finite and of the sign the field expects, and
   halts otherwise. Then it takes the tighter of each pair: the smaller
   maximum, and the smaller loss for the loss stop. A constraints file can
   tighten a limit and never loosen it. This is a proposal for
   [#7](https://github.com/l3a0/ithildin-runtime/issues/7).
6. **The book comes from the journal.** `Journal.replay()` applies every entry
   in order to build a `Book`. The broker's positions and open orders are read
   only to reconcile. `Journal` itself lives in `core`, because execution,
   monitoring and the kill switch command all append to it.
7. **The cost model writes a schedule rather than answering calls.**
   Portfolio construction priced each candidate trade by calling the cost
   model. With each module in its own process, the cost model instead writes a
   `CostSchedule` per session, a fixed and a per-share cost for each
   instrument, and portfolio construction evaluates it. Slice 1's flat or
   linear model fits that shape. This is a proposal for
   [#8](https://github.com/l3a0/ithildin-runtime/issues/8).
8. **Every type that crosses between workers has a file form.** That
   includes `BrokerAccount` and `SendOutcome`. Each travels as an output file
   whose header, and the event pointing to it, carry the `EventHeader` fields
   naming the input events it came from, and a test writes each one and reads
   it back unchanged. Every event log row has the same physical shape, a
   sequence number, the body, its hash and the hash of the row before, and
   `EventHeader`, `HaltEvent`, `ControlEvent` and a journal entry are what a
   body holds. `Halt` stays an exception
   inside a worker, and at the worker's edge it becomes a `HaltEvent`, whose
   `cause` names the halt event it met when it is passing one on, and is empty
   when the worker found the problem itself. `ControlEvent` covers the control
   log's three kinds: a session, the end of the span, and a worker's exit.

## The life of an order

A state diagram shows the states one thing can be in and what moves it between
them. Each transition is a journal entry written before the next step, except
the two a crash causes, `Abandoned` and the crash half of `InDoubt`. The next
run writes those when it finds the gap.

```mermaid
stateDiagram-v2
    [*] --> Intended: order manager plans it
    Intended --> Refused: risk gate refuses
    Intended --> Approved: risk gate approves
    Approved --> Abandoned: crash before sending
    Approved --> Sending: sending journaled
    Sending --> Acknowledged: broker acks
    Sending --> InDoubt: no answer, or a crash
    InDoubt --> Acknowledged: lookup by id finds it
    InDoubt --> Lost: lookup finds nothing, so it never arrived
    Acknowledged --> PartlyFilled: some shares fill
    PartlyFilled --> Filled: the rest fill
    Acknowledged --> Filled: all shares fill
    Acknowledged --> Expired: day order not filled
    PartlyFilled --> Expired: rest not filled by the close
    Acknowledged --> Rejected: broker rejects
    Acknowledged --> Cancelled: kill switch cancels it
    PartlyFilled --> Cancelled: kill switch cancels the rest
    Refused --> [*]
    Abandoned --> [*]
    Lost --> [*]
    Filled --> [*]
    Expired --> [*]
    Rejected --> [*]
    Cancelled --> [*]
```

`Refused` is the platform's own risk gate saying no. `Rejected` is the broker
saying no.

`InDoubt` is the state a crash between sending and the ack lands in. It lets
the journal say "the platform does not know whether the broker has this order",
rather than leaving the next run to guess. The only way out is to ask the
broker by client order id. Sending the order again is never a way out. An order
that ends `Abandoned` or `Lost` never reached the broker, so the next run's
order manager plans from the book as if it had never existed.

`Cancelled` is reached through the kill switch, as question 7 settles.

## The life of a run

One session's chain moves through these states. In shadow, paper and live a
run is one session. In a backtest it is every session of the span, and a halt
ends only its own session's chain.

```mermaid
stateDiagram-v2
    [*] --> Starting
    Starting --> Halted: kill switch set
    Starting --> Preparing: workers waiting on the session's events
    Preparing --> Halted: data missing, stale or not ready, an unknown fill, or book and broker disagree
    Preparing --> Deciding: snapshot written, book reconciled
    Deciding --> Halted: a worker halts
    Deciding --> Sending: target portfolio written
    Sending --> Halted: risk gate refuses, kill switch set, or an order in doubt
    Sending --> Reporting: every order journaled and answered
    Halted --> Reporting: halt event appended, then journaled and alarmed by monitoring
    Reporting --> Done: report written, heartbeat pinged
    Done --> [*]
    Starting --> Failed: a supervisor on another release
    Preparing --> Failed: a worker crashes or misses its deadline
    Deciding --> Failed: a worker crashes or misses its deadline
    Sending --> Failed: a worker crashes or misses its deadline
    Failed --> Reporting: every worker but monitoring stopped
```

`Preparing` is the first phase of the trading day: data reads the snapshot
while execution settles orders in doubt, journals fills and writes the book,
and monitoring reconciles. The supervisor holds none of these states in
memory. It reads them from the event logs: which events each worker has
appended, and whether a halt event names the session. A restarted supervisor
finds the state the same way, and starts every worker whose lock is free. Each
one resumes after the last input event its own event log names, reading the
files that exist and retrying the SQLite write that is missing.

A halt and a failure differ in what they stop. A halt stops one session, and
in a backtest the span goes on to the next. A failure stops the whole run.
Both pass through `Reporting`, so a halted or failed day still writes its
report and its journal entry, and monitoring pings the heartbeat service's
failure address so the owner hears even if the alarm from the machine is lost.
If monitoring itself is what failed, no ping arrives, and the dead-man check
raises the alarm. A halt is final for its session, which is this doc's choice.
The owner ruled on 2026-10-29 that the owner or the operator clears it, and
clearing never reruns the session. The next session's
run starts fresh, and it halts again while the kill switch is set or the
cause is still there.

## What is stored

An entity-relationship diagram shows what is kept and how the records point at
each other. A pair of bars at a line's end means exactly one, and a circle with a
crow's foot means zero or more.

```mermaid
erDiagram
    VINTAGE ||--o{ BAR : "holds"
    INSTRUMENT ||--o{ BAR : "is priced by"
    RUN ||--|{ SNAPSHOT : "acts on, one per session"
    SNAPSHOT }o--o{ BAR : "lists"
    RUN ||--o{ JOURNAL_ENTRY : "writes"
    JOURNAL_ENTRY }o--|| ORDER : "records a step of"
    ORDER ||--o{ FILL : "fills by"
    RUN ||--|{ REPORT : "reports each session"
    RUN ||--o{ SIGNAL : "records"
    RUN ||--|{ EVENT : "appends"
    RUN ||--o{ HALT_EVENT : "stops a session with"

    VINTAGE {
        string id
        string origin
        string vendor
        string symbol
        string basis
        date first
        date last
        date downloaded
        string sha256
    }
    BAR {
        string instrument
        date session
        float close
        datetime first_seen
    }
    RUN {
        string id
        date first_session
        date last_session
        string mode
        string release
        string outcome
    }
    SIGNAL {
        string run_id
        date session
        string name
        float value
        string release
    }
    EVENT {
        string log
        int seq
        string hash
        string prev_hash
        string file_path
        string file_sha256
        int format
        string worker
        date session
        string inputs
        string release
        string config_hash
        datetime produced_at
    }
    HALT_EVENT {
        string run_id
        date session
        string worker
        string reason
        string cause_ref
    }
    JOURNAL_ENTRY {
        int seq
        string hash
        datetime at
        string kind
        string client_order_id
        string payload
        string prev_hash
    }
    ORDER {
        string client_order_id
        string instrument
        float qty
        string state
    }
    FILL {
        string client_order_id
        float qty
        float price
        datetime at
    }
```

The entities live in six stores, one of them kept by marketlake, and
each keeps its own rule.

1. **marketlake holds `VINTAGE` and `BAR`.** Every bar, captured or imported,
   lives there, written once and never changed, with its source and checksum
   in marketlake's manifest. Each marketlake partition is one vintage here, and
   a bar's `vintage_id` is its partition's path. marketlake's lake is private
   and backed up off the machine, which is what question 4 and the premise ask
   of captured bars. The checksums
   question 4 asks to commit are the partition checksums a snapshot records,
   for any run a test or a report pins. Imported history
   from quantitative-trading arrives with that repository's own checksums,
   from its `data/vintages.jsonl`, so the import can prove it read the
   recorded bytes. [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) builds the import.
2. **The run directory, D1, holds `SNAPSHOT`, `SIGNAL`, `EVENT` and
   `HALT_EVENT`,** as one event log per worker with the output files its
   events point to. A snapshot lists the marketlake bars one session read, by partition
   and checksum. A signal record holds a value the run computed, such as the
   hedge ratio, the z-score or the forecast, keyed by run, session and name.
   Each event names the events it came from, and a halt event says why a
   session stopped. The output files are regenerable from marketlake, the
   journal and the code, but the event logs, with their times and the exits
   the supervisor records, say what happened and cannot be. So this doc proposes, for
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11), that shadow and
   paper run directories are kept and leave the machine with the journal. A
   backtest keeps everything after a halt or a failure. The event logs also make
   a question such as "how did the backtest's
   z-score on a session differ from the shadow run's" a lookup rather than a
   recomputation.
3. **The security master, D2, holds `INSTRUMENT`.** It is a small committed file
   that changes only through a pull request.
4. **The journal, D3, holds `JOURNAL_ENTRY`.** It is a SQLite event log with
   the same row shape as every other, and the one that keeps its content in
   its rows rather than in files, as design.md proposes to [#10](https://github.com/l3a0/ithildin-runtime/issues/10). `Journal`
   appends through the same log contract.
   `JournalStore` picks only the path, which is what differs by mode. `ORDER`
   and `FILL` are read from journal entries rather than kept as tables of their
   own. Each entry is committed before the step it records happens. It lives
   at one path per broker account, outside every run directory, because the
   book is replayed from it across runs. Each entry carries the hash of the
   one before it, which makes the journal a hash chain. Changing an entry
   breaks the link from the entry after it, even past the triggers that refuse
   an update. That catches an edit unless someone recomputes every later
   hash. Recording the latest hash off the machine each day, in the backup or
   the report, catches that case and a cut-off tail. The kill switch command,
   monitoring and execution can all write, so each append opens its
   transaction with `BEGIN IMMEDIATE`, which takes SQLite's write lock first,
   then reads the last entry's sequence number and hash, and inserts the new
   entry. Two writers therefore never chain from the same entry. Every event log
   chains its rows the same way, as design.md's rule 2 says.
5. **Run reports, D4, hold `RUN` and `REPORT`.** A report is computed from the
   journal and the snapshot, so losing one costs nothing. The run keeps it for
   the owner to read, not as a record.
6. **The simulated broker's account, one file per simulated account beside
   the journal,** holds the resting orders and positions the simulator keeps.
   Backtest and shadow use the same file, so an order a shadow run sends one
   evening is still resting at the next open, in a new process. Each simulated
   account has its own lock and kill switch path, so a backtest never shares
   the paper account's. This is a proposal for
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10).

## Where it runs

A deployment diagram shows which machine each piece runs on and what crosses
the machine's edge.

```mermaid
flowchart TB
    subgraph host["The runtime's VM, separate from marketlake's"]
        timer["Scheduler<br/>launchd or a systemd timer,<br/>once per session"]
        run["Supervisor and workers<br/>at a release the owner pinned"]
        keychain["Keychain<br/>broker paper key"]
        cfg["~/.config/ithildin-runtime/<br/>limits and hard caps, kill switch file"]
        store[("~/.local/share/ithildin-runtime/<br/>journal, run directories, reports")]
        mirror[("Local lake mirror<br/>the partitions the strategies need")]
        timer --> run
        mirror --> run
        keychain -. "execution worker only" .-> run
        cfg --> run
        run --> store
    end
    mlvm["marketlake's VM<br/>capture daemon"]
    lake[("marketlake's bucket")]
    broker["Broker paper API"]
    backup[("Off-machine backup")]
    beat["Heartbeat service"]
    alerts["Alert channel"]

    mlvm --> lake
    lake -->|"download, checksum checked"| mirror
    run <--> broker
    store --> backup
    run --> beat
    run --> alerts
    beat --> alerts
```

Eight rules hold the deployment to the premise and the hard limits.

1. **The owner pins the release.** The scheduler runs a tagged commit the owner
   merged and chose, so the code that traded is always known. Deploying is the
   owner's under hard limit 6.
2. **The journal leaves the machine every day,** copied with SQLite's backup
   command, as design.md says. It is one of the three things the premise says
   cannot be recovered, so a disk failure must not take it.
   This doc proposes, for
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11), that shadow and
   paper run directories leave with it.
   Captured bars, another of the three, live in marketlake, which backs up its
   own lake.
3. **The broker key lives in the keychain.** Only the execution worker's entry
   point reads it, into that process's own environment and never into a file,
   tracked or not. The supervisor never holds it, so no other worker can
   inherit it. It is a paper key with no withdrawal or transfer permission,
   which is hard limit 5.
4. **The kill switch is a file the owner and the operator can create.** Every
   worker checks it when it starts and whenever a message wakes it, and
   execution checks it
   before every send. Pulling it needs no deploy. The hard caps beside it
   change only by the owner's hand, because raising one raises a capital
   limit, which hard limit 1 keeps for the owner.
5. **Every file lives on the machine's own disk,** for the reason
   design.md's rule 1 gives.
6. **A rollback is a pin to the previous tag.** Every event carries a format
   version, and each release writes only formats the release before it can
   read, so the old release reads what the new one left. A session in flight halts
   on the supervisor's release check, and the next session runs on the old
   release.
7. **The scheduler restarts a supervisor that crashes, up to a limit.** That
   is systemd's `Restart=on-failure` with `StartLimitBurst`, or launchd's
   `KeepAlive` with `SuccessfulExit` set to false. Reaching the limit raises
   the alarm, and the dead-man check covers a scheduler that stops too.
8. **The runtime has a VM of its own, apart from marketlake's.** The owner
   ruled on 2026-10-29 that the runtime runs on a VM separate from the one
   marketlake's capture daemon runs on, and downloads the parts of the lake its
   strategies need. So a run on the runtime cannot starve capture, and a
   failure on one VM leaves the other running. The data worker downloads each
   partition a session's lookback names from marketlake's bucket into a local
   mirror, checks it against the checksum in marketlake's manifest, and reads
   it through the same bar query. A partition not yet in the bucket is "data
   not ready", which halts. The mirror is a copy, so losing it costs only a
   download. The runtime's credential for the bucket reads and never writes,
   and it gets its row in design.md's `## Configuration` when [#5](https://github.com/l3a0/ithildin-runtime/issues/5) first
   reads it.

`~/.local/share/ithildin-runtime/` is a new machine path. design.md's
`## Configuration` names only `~/.config/ithildin-runtime/`, so the first
module to write there adds its row.

## Package layout and the import rule

```mermaid
flowchart TB
    core["ithildinruntime.core<br/>shared types, Halt, every Protocol,<br/>Journal, the log contract"]
    data["data"]
    alpha["alpha"]
    risk["risk"]
    cost["cost"]
    portfolio["portfolio"]
    execution["execution<br/>order manager, order path"]
    brokers["execution.brokers<br/>simulated, paper"]
    monitoring["monitoring"]
    runtime["runtime<br/>supervisor, worker entry points, modes, CLI"]

    data --> core
    alpha --> core
    risk --> core
    cost --> core
    portfolio --> core
    execution --> core
    monitoring --> core
    brokers --> core
    runtime --> data
    runtime --> alpha
    runtime --> risk
    runtime --> cost
    runtime --> portfolio
    runtime --> execution
    runtime --> brokers
    runtime --> monitoring
```

An arrow means "may import". The diagram draws three rules.

1. Every module imports `core` and none of its siblings. The types that cross
   modules, such as `Forecast`, `Constraints` and `TargetPortfolio`, and every
   `Protocol`, including `RiskGate`, live in `core` for that reason. So do
   `Journal`, which three processes append to, and the log contract every
   worker uses: publishing an output file with `link`, the SQLite append of
   one event, the message to each reader's queue, the event header, and the
   halt event.
2. Only `runtime` imports the modules together. Each worker's entry point
   lives there. It picks the adapters for the mode it is given, and the
   execution worker's entry point hands the risk module's `RiskGate` to the
   order path. The supervisor lives there too, and imports only `core`. It
   starts each worker as its own process, by name, and writes nothing but the
   control log and its own lock, which `core`'s log contract covers.
3. Only `runtime` imports `execution.brokers`, and only the execution worker's
   entry point builds a broker, which it hands to `OrderPath` alone.

A test reads every module's imports with Python's `ast` module and fails on an
arrow the diagram does not draw. A second test runs a day under the supervisor
against a fake broker that fails on three things.

1. An order whose client order id has no approval in the journal.
2. An order whose `sending` entry is not yet committed to the journal.
3. A client order id it has already received. Together they turn
test 3's "no order reaches the broker adapter without passing the gate" into
something executed rather than reviewed. The price is two more tests to keep
in step with the diagram, and a module that needs a sibling has to argue for a
new arrow.

## How the four acceptance tests exercise it

| Test, from [#4](https://github.com/l3a0/ithildin-runtime/issues/4) | Mode | What it runs | What it pins |
| --- | --- | --- | --- |
| 1. The backtest reproduces a replicated strategy | Backtest | marketlake reader over the imported vintages, history clock, simulated broker filling at the signal's close, zero cost | APR 0.17824928 and Sharpe 0.96467289 to 5e-9, the 162 changes of unit, and the unit on each of the 1,480 days as `chan.bollinger` computes it at `19aadbeb` |
| 2. A replay emits pinned orders, through a missing feed, a stale price and a restart in the wrong order | Backtest | A recorded stretch of sessions with three faults injected. The restart kills the supervisor mid-run and starts execution before data | The orders on each day, and a halt with no orders on each faulted day |
| 3. The risk gate refuses an order over each limit, and nothing bypasses it | Unit and import | `RiskGate` against one order over each of the six limits, the import rule test, and the fake broker | Six refusals, no import of `execution.brokers` outside `runtime`, and no order reaching the fake broker without a journaled approval and `sending` entry, or twice |
| 4. The kill switch and the dead-man check fire in a drill | Paper, drill | Set the switch before a run, and skip a run's ping | A halt journaled with reason "kill switch", and a dead-man alarm delivered |

Test 1 fills at the close of the day the signal used, because that is what
Chan's script assumes. A paper run cannot trade at a close it has already seen,
so it fills at the next open. A paper order also needs whole shares, or a
fractional order the broker accepts, while the backtest holds fractional dollar
positions. So the two runs differ in two ways.

1. **Timing.** One night of price movement separates the backtest's fill from
   the paper fill.
2. **Rounding.** Rounding each leg to whole shares moves it by at most half a
   share. Whether portfolio construction or the order manager rounds, and
   whether the broker allows a fractional short sale for the GLD leg, is open
   on [#9](https://github.com/l3a0/ithildin-runtime/issues/9).

Shadow running measures both gaps rather than leaving them to argument.

The charter's paper gate also asks for every alarm to fire in a drill, which
covers more than test 4 names. The reconciliation mismatch above is one such
alarm, and its drill belongs on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11).

Which issue writes each piece. Where a piece is a shared type, it lives in
`core` whichever issue writes it.

| Piece | Written on |
| --- | --- |
| Importing every source into marketlake, provenance on each bar, the one bar query | [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) |
| `core`'s shared types and every `Protocol`, the file form of each type, `Journal`, the log contract of published files, SQLite appends and queue messages, the event header and the halt event, the import rule test, as proposed below | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| `Bar`, `Snapshot`, the marketlake reader, the snapshot and signal records, security master | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Bollinger band alpha model, `Basket`, `Forecast` | [#6](https://github.com/l3a0/ithildin-runtime/issues/6) |
| `Constraints`, `RiskGate`, the daily loss stop, the stale-data rule | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Flat and linear cost models, `CostSchedule` | [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |
| Equal-risk-weight portfolio construction, `TargetPortfolio` naming every instrument | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| Order manager, `OrderPath`, journal store adapters, simulated and paper brokers, the simulated broker's account file, the account lock | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| `Monitoring`, reconciliation, daily report, alert channel adapter, dead-man check, kill switch, startup order, the alarms and their drills, each worker's deadline, and the supervisor and the fake-broker test as proposed below | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |

Each module issue also writes its own worker's entry point under `runtime`,
the code that picks that worker's adapters for a mode.

Equal risk weight sizes each forecast's basket so they all carry the same
risk. With one basket in slice 1, it reduces to sizing that basket, which
question 3 settles.

Two pieces belong to no single module.

1. `core`, with the shared types, every `Protocol`, and the log contract
   every worker uses: the file publish, the SQLite append, the queue message,
   the event header and the halt event.
2. The import rule test, which reads every module's imports.

Both are written on [#22](https://github.com/l3a0/ithildin-runtime/issues/22), which every module issue waits on, as question 9
settles.

The supervisor imports no module, but it cannot run a day until every worker
exists, so it is built last. This doc proposes building it on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), which owns the startup order and waits on every
other module. The fake-broker test goes there too, since it runs a whole day
under the supervisor. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) names neither today, and its acceptance
covers tests 2 and 4 but not test 3, so the Decisions table lists both as a
proposal.

## Decisions

Each row is a choice this doc makes, what it turned down, the price, and where
the choice is settled. A row settled here is a shape every module shares. A row
marked with an issue is a proposal: once the owner accepts it, it goes on that
issue, which then decides. Either way, each turned-down alternative gets a row
in [design.md](design.md#considered-and-rejected)'s register when the row is
accepted.

| Decision | Turned down | Price | Settled on |
| --- | --- | --- | --- |
| Each component runs as its own process, in parallel, passing files | One process calling every module in memory, or a process per action | A file format and a round-trip test for every type that crosses between modules | Ruled by the owner on 2026-10-29, with the rules in [design.md](design.md#processes-and-files) |
| The platform is an event pipeline: each component runs its processing loop when there is something new for it, and its outputs trigger those downstream | A controller that calls each component in a fixed order | A worker waits for a message on its queue instead of being called | Ruled by the owner on 2026-10-29 |
| Each event log is a SQLite file, one per component | A JSON Lines file per log, or a messaging server such as NATS JetStream | An event log is read with `sqlite3` rather than `cat` | Ruled by the owner on 2026-10-29 |
| Inputs and outputs are separate files with SQLite pointers, the event log is the source of truth, and a process resumes by reading the files that exist and retrying its SQLite write | Outputs kept as rows in the event log itself | Two writes per output, a file then a row | Ruled by the owner on 2026-10-29 |
| Each component keeps a queue, listens for events on it, and reads its inputs from files and writes its outputs to files. A message is sent to a component's queue whenever there is something new for it | File notifications through `watchdog`, or polling | A message has to reach every component with something new | Ruled by the owner on 2026-10-29 |
| Each queue is a named pipe in the run directory, written without waiting. The log contract sends each message after a commit, to the readers one table in `runtime` declares, and again for the newest event when a worker starts, and a starting worker opens its pipe before it catches up | A socket, a queue server, or every commit messaging every queue | A message is skipped when its reader is down or its pipe is full, so a starting worker catches up from its input logs | [#22](https://github.com/l3a0/ithildin-runtime/issues/22), as [design.md](design.md#processes-and-files) proposes |
| Each worker publishes each output file from a unique read-only temporary file with `link` at a name fixed by the worker and the session, unlinks the temporary name and flushes the directory, points to the file by path and SHA-256, and writes one event log that only grows, under three triggers, with each event stored as its exact body, hashed and chained to the row before, and every event naming the last input events it consumed. Every writer opens with `synchronous=FULL` and `fullfsync=ON` and appends under `BEGIN IMMEDIATE`, and an event log is copied only with SQLite's backup command | Event logs that may be edited in place, or a cursor kept apart from the output | A check on restart that each named input is still there, and a few minutes of disk syncs per backtest | [#22](https://github.com/l3a0/ithildin-runtime/issues/22), as [design.md](design.md#processes-and-files) proposes |
| The runtime runs on a VM of its own and downloads the partitions its strategies need into a local mirror | Running on marketlake's VM beside the capture daemon | A second VM, and a read-only credential for marketlake's bucket | Ruled by the owner on 2026-10-29 |
| A supervisor that starts every worker at once and keeps no state, a control log that ends the span, and a halt that flows down the pipeline and is final for its session | A supervisor that holds the run's state in memory, or one that launches each worker only once its inputs exist, which would handle a halt one way on paper and another in a backtest | Locks, event headers and a control log that one process would not need. The owner ruled on 2026-10-29 that the owner or the operator clears a halt | [#11](https://github.com/l3a0/ithildin-runtime/issues/11), as [design.md](design.md#processes-and-files) proposes |
| `Journal` and the log contract live in `core` | The journal writer in `execution`, which the supervisor and monitoring could not import | `core` holds behaviour beyond what its types need | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| Only the order path talks to the broker, for sends, lookups, fills, positions and cancels, inside one execution worker per broker account | Letting the supervisor or monitoring call the broker | One more method on `OrderPath` per kind of broker call | This doc |
| Monitoring reconciles positions and open orders between execution's recovery and the models, and journals and alarms every halt | Reconciling inside execution, or the supervisor raising alarms | One more output file, the broker's account, which also makes the reconciliation readable afterwards | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Execution journals a fill only when its client order id has a `sending` entry and the fill stays within that order's quantity, and halts otherwise | Journaling every fill the broker reports | A fill from an order placed by hand at the broker halts the platform | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| Execution reads one target portfolio per session, journals it whole, and halts if a second appears. Each client order id comes from the session, the instrument, the target's hash and the order's place in the plan, and the order manager subtracts open orders as well as positions | Ids from a counter, or planning from positions alone | A target rebuilt mid-session halts rather than trading | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A target portfolio names every instrument the book holds, with a flat leg as an explicit zero | Reading a missing instrument as zero | One more check, and a halt on a leg left out | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| The gate's hard caps come from execution's own configuration, every field of both sources is checked before use, and the tighter of each pair applies | Trusting the risk model's constraints file alone | Two places to set a limit. The owner ruled on 2026-10-29 that only the owner changes the caps, by hand | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| The cost model writes a cost schedule that portfolio construction reads | Portfolio construction calling the cost model for each candidate trade, which would cross a process boundary per trade | One more type in `core`, and a change to the charter's module table | [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |
| The current book comes from the journal, and the broker is only reconciled against | Reading positions from the broker each run | A journal bug halts trading instead of being silently patched over by the broker's numbers | This doc |
| The journal store and the alert channel are adapters chosen per mode | Writing records and alarms the same way in every mode | Two more ports in `core`, and a change to design.md's `## One runtime` | This doc |
| A missing, stale or empty input raises `Halt` | Defaulting a gap to the last value or to zero | A day with a data hiccup is a day with no trading | This doc, with the staleness rule on [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| A forecast names a basket | A forecast per instrument, with the pair logic in portfolio construction | One more type in `core`, and a change to the charter's module table | [#6](https://github.com/l3a0/ithildin-runtime/issues/6) |
| The supervisor and the fake-broker test are built last, on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) | Building the supervisor with `core`, which cannot run a day before every worker exists, or a separate issue. Writing the fake-broker test with the import rule test in `core`'s sub-issue, which cannot run before the supervisor exists | No end-to-end run happens until [#11](https://github.com/l3a0/ithildin-runtime/issues/11) lands, and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) grows past its Monitoring row and takes on part of test 3 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Run the Bollinger band on GLD and USO for test 1 | Entry 21's linear rule on the same data, which is always in the market with continuous units, or the Kalman-filter hedge on EWA and EWC, which adds a model to verify first | The engine carries the strategy's quirks, such as no capital account and a daily resize of one leg | [#6](https://github.com/l3a0/ithildin-runtime/issues/6), which also sets [#5](https://github.com/l3a0/ithildin-runtime/issues/5)'s ETFs |
| One run per session after marketlake's evening sweep, filling at the next open | Running just before the close and sending market-on-close orders | The paper fills differ from the backtest's by one night. In return the run never acts on a bar still forming. Which close the run reads that evening is question 10 | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| The journal is a SQLite event log with the same row shape as every other, and the one that keeps its content in its rows | JSON Lines fsynced per entry, which a crash partway through an append could tear, or entries kept as files | It is read with `sqlite3` rather than in a terminal, and it uses the rows-only option the owner set aside for components, so it is the owner's to confirm | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| marketlake is the one bar provider: every source is imported into it, and every mode reads one query | A bar store in this repository, or a reader per source | It reverses marketlake's rule that it holds no history from before capture, so every bar carries whether it was captured or imported. This repository also depends on marketlake's package at a pinned commit | [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) and [#5](https://github.com/l3a0/ithildin-runtime/issues/5), ruled by the owner on 2026-10-29 |
| Each run records the signals it computed | Recomputing them whenever two runs are compared | One more store, which holds nothing that cannot be regenerated | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Talk to the paper broker over its REST API, with the HTTP client passed in as an argument | The broker's own SDK | A few more lines per call. In return tests replace the client with a fake that returns scripted answers, as quantitative-trading's Alpha Vantage fetcher does, and no SDK enters the dependency list | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |

## Questions for the owner

Each question has a recommendation, so a review can answer it with a yes.

1. **Which paper broker?** The recommendation is Alpaca's paper account. It is
   free, it has a REST API, and a paper key cannot move money. Schwab, which
   marketlake already reads, is not known to offer an API paper account, so
   confirm that before choosing it. Interactive Brokers has one, but it needs a
   gateway process running beside the platform.
2. **Where do live GLD and USO bars come from?** The recommendation was to add
   both to marketlake's `config/tickers.yaml`, and [l3a0/marketlake PR #928](https://github.com/l3a0/marketlake/pull/928) did on
   2026-10-29. Capture starts once the owner onboards them on marketlake's VM.
   marketlake captures forward from that day and keeps no history from before
   it. So the first z-score's 40 or so sessions come from a vintage recorded
   once from a vendor download, or the paper run waits that long before its
   first trade. The owner ruled on 2026-10-29 that every
   source goes through marketlake, so that download is imported there too, and
   [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) builds the import.
3. **How large is one unit on paper?** The replication has no capital account.
   The recommendation is to size one unit so the gross position is a fixed
   dollar amount the owner sets, recorded in the run's configuration. The owner
   accepted it on 2026-10-29, and the ruling is on
   [#9](https://github.com/l3a0/ithildin-runtime/issues/9).
4. **May captured bars be committed to this public repository?** Vendor terms
   can forbid redistributing data. The recommendation follows
   quantitative-trading's archive pattern: keep the files in a private store,
   and commit only their checksums, so a reader can still check that a run's
   inputs are the ones recorded. The owner accepted it on 2026-10-29, and the
   ruling is on [#5](https://github.com/l3a0/ithildin-runtime/issues/5). The
   owner's later ruling the same day made marketlake's private lake that
   store.
5. **Where does the platform run?** The recommendation is one machine the owner
   controls with a daily scheduler, either the owner's own machine or the VM
   marketlake runs on. The VM keeps the run beside the lake it reads, but it
   can power itself off each day, so the run's time has to fit inside its
   window. The owner's machine needs no new host. The owner accepted one owner
   controlled machine with a daily scheduler on 2026-10-29, and the ruling is
   on [#11](https://github.com/l3a0/ithildin-runtime/issues/11). Later the same
   day the owner ruled that the runtime runs on a VM of its own, separate from
   marketlake's, and downloads the parts of the lake it needs, as
   `## Where it runs` says.
6. **Which heartbeat service?** The recommendation is a hosted check that
   alarms when a ping is late, outside every machine the platform uses. A
   GitHub Actions schedule is not one, because quantitative-trading measured
   only about 11% of its scheduled board runs firing over 185 hours. The owner
   chose healthchecks.io on 2026-10-29, in the project marketlake already runs,
   with its ntfy push and email integrations as the alert channel. The ruling
   is on [#11](https://github.com/l3a0/ithildin-runtime/issues/11).
7. **Does pulling the kill switch cancel orders resting at the broker?** The
   recommendation is yes, through the kill switch command, which cancels each
   resting order through the order path and journals it. Cancelling an order
   that has not filled sells nothing, so it is still a stop and not a flatten,
   and without it a switch pulled overnight stops nothing until after the open.
   The owner accepted it on 2026-10-29, and the ruling is on
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11).
8. **Does the daily loss stop also unwind positions?** The recommendation is
   no for slice 1. Crossing the stop makes the risk gate refuse every new
   order. An unwind is a trade of its own, so it would need its own path
   through the gate and its own tests, and
   [#7](https://github.com/l3a0/ithildin-runtime/issues/7) can add it later.
   The owner accepted it on 2026-10-29, and the ruling is on [#7](https://github.com/l3a0/ithildin-runtime/issues/7).
9. **Where are `core` and the import rule test written?** The
   recommendation is a new sub-issue of
   [#4](https://github.com/l3a0/ithildin-runtime/issues/4), built first, which
   every module issue waits on through a "blocked by" link. Every module
   imports `core`, so the dependency is real. The price is one step before any
   module starts. In return the shared shapes land once, reviewed on their own,
   rather than inside whichever module happens to go first. The owner accepted
   it on 2026-10-29, and the work is [#22](https://github.com/l3a0/ithildin-runtime/issues/22).
10. **Which bar does the evening run read for the session's close?**
    marketlake fetches a session's official daily bar only at the next
    evening's sweep, once the following session has sealed. Two choices follow.
    1. **Wait for the official daily bar.** The run for session D reads D's
       daily bar on the evening of D+1, and its orders fill at the open of
       D+2, a session and two nights after the close the signal used, rather
       than one night.
    2. **Read the close marketlake captures that day.** marketlake records each
       session's option-close quote, at 16:15 on a full day and 13:15 on an
       early close, and seals it fifteen minutes later, at 16:30 or 13:30. It already judges the daily bar
       against that quote's last price when the bar lands. The run reads that
       close and trades the next open, one night after the signal, as the mode
       table assumes above. So that every mode still reads one query,
       [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) serves that close through the bar query as a provisional daily
       bar, marked provisional until the official bar replaces it.

    The recommendation is the second, with the next evening's daily bar
    checked against the close the run used, and a disagreement journaled and
    reported. The price is a close that is marketlake's capture rather than
    the vendor's official candle.
