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
events on its queue, and a message reaches a component's queue whenever there
is something new for it. Every file is a blob in S3, written once at a key
fixed by the run, the component and the decision, and the VM keeps no state.
The journal is the one numbered log.
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

1. **Two liquid ETFs, daily closes.** The backtest reads daily closes only,
   with no futures rolls and no stock panel. The paper decision also reads one
   quote per ETF at 15:40, from marketlake's segments.
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

Each quirk lives in the strategy's own pieces rather than in a shared module,
so a second strategy changes none of them. The daily refit and the `n − 1`
live in the alpha model. The zero cost lives in the cost model's
configuration for this strategy. The return over gross dollars lives in test
1's own comparison and in a reporting setting of the strategy, never as a
shared default.

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
    loop -->|"reports, alarms"| alerts
    loop -->|"ping after each decision"| heartbeat
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

The owner ruled on 2026-10-29 that the runtime may read the day's data before
marketlake seals it. marketlake's capture daemon already writes a segment
each cycle and never rewrites it once its manifest entry lands, so marketlake
uploads each segment to its bucket as soon as that entry lands, refusing to
replace an existing key. Any decision time inside marketlake's capture
hours reads those segments, and the decision minute
[#16](https://github.com/l3a0/ithildin-runtime/issues/16) sets for shadow
running, ten minutes before the close, is one such time. The data worker reads
the segments up to the decision time, as a provisional read, because marketlake's validation
battery runs only when it seals a day at the close plus fifteen minutes. So
the data worker runs its own minimum checks before anything decides. A
missing or stale quote at the decision time halts the decision, and a price
spike is flagged in the snapshot without halting. The skeleton's strategy
decides this way at 15:40, as the owner ruled on question 10. A decision
after marketlake has sealed and uploaded the day reads sealed partitions and
needs none of this. The snapshot names each segment it read by key
and SHA-256 and marks the read provisional. Once marketlake seals the day,
monitoring compares the sealed partition with what the snapshot read, and
journals and alarms any difference. It also reports the gap between the
price the decision used and the official close, and between each
market-on-close fill and that close, as report lines rather than alarms,
since the two differ by design. This is proposed for
[#5](https://github.com/l3a0/ithildin-runtime/issues/5) and
[#16](https://github.com/l3a0/ithildin-runtime/issues/16), with marketlake's
uploader on [l3a0/marketlake#943](https://github.com/l3a0/marketlake/issues/943).

It sends orders to one place, the broker's paper account.

Two switches come from outside the loop.

1. **The live switch** moves the broker from the paper account to a real one.
   Only the owner holds it, which is hard limit 1 in
   [CLAUDE.md](../CLAUDE.md).
2. **The kill switch** halts trading. The owner ruled on 2026-10-28 that the
   risk engineer builds it and writes when to pull it, and the operator pulls
   it by that guidance. The owner can pull it too.

A dead-man check raises an alarm when a ping it expects fails to arrive. Here
monitoring pings a heartbeat service as each decision's last step, and the
service alarms when a ping is late against the deployment's schedule. The service sits outside the machine because a
check on the same machine dies with it, and a machine where nothing runs is
the failure the check exists for.

## The loop and its adapters

[design.md](design.md#one-runtime)'s `## One runtime` says backtest, paper and
live run the same modules and differ only in adapters at the edges. The usual
name for that shape is ports and adapters. A port is an interface the modules
define, and an adapter is one implementation of it that talks to the outside
world. No module imports an adapter.

The owner ruled on 2026-10-29 that each component runs as its own process,
that processes pass everything through files, and that the platform is an
event pipeline. [design.md](design.md#processes-and-files)'s
`## Processes and files` gives the reasoning and the rules. So each module
here runs as one worker, a process of its own. The supervisor writes the
run's plan and starts every worker at once, and each one listens on its own
queue. A message there names an input's decision result, and the worker then
reads the output files that result lists, writes its own output files, and
writes its own decision result last. A worker's entry point builds the
adapters for the mode it is given and hands them to the module, so the module
never learns which mode it is in.

```mermaid
flowchart TB
    sup["Supervisor<br/>writes the plan, starts every worker,<br/>keeps no state of its own"]
    plan[("plan<br/>the run's decisions")]
    sup --> plan

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

    snap[("snapshot")]
    acct[("account<br/>book and broker account")]
    book[("reconciled book")]
    fc[("forecast")]
    cons[("constraints")]
    sched[("cost schedule")]
    target[("target portfolio")]
    outcome[("send outcome")]

    plan --> data
    plan --> exec_recover
    snap --> exec_recover
    data --> snap
    exec_recover --> acct --> mon_recon --> book
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
    exec_send --> outcome --> mon_report

    subgraph adapters["Adapters, built by each worker's entry point"]
        lakesrc["marketlake reader<br/>over the local mirror"]
        clock["History clock or wall clock"]
        broker["Simulated or paper broker"]
        store["Blob store<br/>S3, or a memory-backed folder"]
        alertsink["Alerts and heartbeat"]
    end

    lakesrc -. "DataSource" .-> data
    clock -. "Clock" .-> data
    clock -. "Clock, for the cutoff" .-> exec_send
    broker -. "Broker" .-> exec_recover
    broker -. "Broker" .-> exec_send
    store -. "BlobStore" .-> workers
    alertsink -. "Alerts" .-> mon_report
    sup -. "starts" .-> workers
```

Each cylinder is one component's output files for a decision, and the decision
result that lists them, each a blob written once at a key fixed by the run,
the component and the decision. A solid arrow into a worker is an output it
reads. When a worker writes its decision result, a message goes to the queue
of each worker that reads it, which runs that worker's processing loop. Every
worker reads the plan, which the diagram draws only into the two that start
the day, and monitoring reads every component's results, which the diagram
leaves out to stay legible. Each dotted arrow from an adapter is a port,
labelled with its name, and each mode is one choice of adapter for each port.
Execution and monitoring each appear twice because each has two stages in one
process, and each stage writes its own decision result. Execution recovers first and then, once the target portfolio
exists, sends. Monitoring reconciles first and reports last.

Two groups of workers run at the same time.

1. Execution's recovery for a decision waits for data's snapshot and for its
   own send result for the decision before, so it reads the broker at the
   decision's own time. Monitoring's reconcile follows it.
2. Alpha and cost need only the snapshot, and risk also needs the reconciled
   book, so the three models overlap.

Execution sends only what the target portfolio asks for, so nothing it sends
can come from a snapshot or a book that is not stored yet. When it sends, it
checks each order against the book it replays from the journal at that
moment, not against a blob.

| Mode | Data source | Clock | Broker, and when it fills | Blob store and journal | Alerts and heartbeat |
| --- | --- | --- | --- | --- | --- |
| Backtest | marketlake reader, the bars a test names | History clock | Simulated, by order type: a market order at the decision quote, a buy at the ask and a sell at the bid, and a market-on-close order at the official close | A memory-backed folder, holding the backtest's own journal, gone when it ends | Recorded, not sent |
| Shadow | marketlake reader, captured bars, plus the imported download until capture covers the lookback | Wall clock | Simulated, by order type, the same rule as the backtest, except that a market-on-close order fills at the last price of marketlake's 16:00 cycle, marked provisional until the official close arrives | S3, the shadow journal | Sent |
| Paper | marketlake reader, captured bars, plus the imported download until capture covers the lookback | Wall clock | Paper broker, at its own simulated fill, which for a market-on-close order is the quote near the close rather than the auction price | S3, the paper journal | Sent |
| Live | marketlake reader, captured bars, plus the imported download until capture covers the lookback | Wall clock | Live broker, at its own fill | S3, the live journal | Sent |

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

The blob store, which holds the journal too, and the alert channel also differ
by mode, but they change only where records go. A backtest of 1,480 decisions
must not append to the paper journal or page the owner. The Decisions table
proposes adding those two to design.md's `## One runtime`.

A module that asks which mode it is in has broken the shape, because its
backtest would then test a branch the paper run never takes. The account a
worker's lease and kill switch belong to comes from its broker adapter, so a
backtest's simulated account never shares the paper account's lease. The
simulated broker keeps its resting orders and positions as an output file per
decision, so a shadow order still resting when one run ends is there to fill
in the next, in a new process or on a new VM. One simulator serves backtest
and shadow, and it fills by the order's type and time in force rather than
by mode. It can also fill one leg of a pair in part, or not at all, on a
seeded fault, so a replay runs the stranded-leg path paper trading will. The event that design.md says
modules are written against is one decision result. In a backtest each worker
works through every decision of the span in turn, and in the other modes
through one trading day's decisions, as design.md's
`### A worker works through a span of decisions` says.

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

    d1[("D1 The run's blobs in S3,<br/>keyed by component and decision")]
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
    d1 -->|"decision results, halts, send outcome"| p9
    d3 -->|"orders and fills"| p9
    p9 -->|"report"| d4
    p9 -->|"alarms"| ownerx
```

Every arrow into or out of D1 is a blob kept after the run, whose header
names the blobs it came from. That is what makes each decision
readable afterwards. The security master is the table that maps each ticker to
the platform's own instrument id, so a vendor's renamed ticker does not become
a new instrument.

Three things the diagram rules out.

1. **Only execution talks to the broker.** Processes 2 and 8 are the one
   execution worker. Orders go out through it, and the broker's acks, fills,
   positions and open orders come back through it. Monitoring reconciles
   against the account file execution wrote rather than asking the broker itself.
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
decision with nothing wrong, for the skeleton's daily strategy. The owner
ruled on 2026-10-29 that the platform trades at any time of day and as often
as a strategy needs, so the time comes from the strategy's schedule, as
design.md's `### Strategies, schedules and decisions` says. The owner ruled on
question 10 that the skeleton trades the close, so the diagram draws its
decision at 15:40, on marketlake's segments read before sealing, with
market-on-close orders that fill at the close. A strategy that decides
several times a day runs the same three phases once per decision, with the
workers left running between. The notes split it into three phases. The supervisor starts every worker at the start, and a
`par` block marks workers working at the same time. Each dashed arrow from
the blob store to a worker is a message on that worker's queue.

Alpaca rejects a market-on-close order "submitted after 3:50pm but before
7:00pm ET", so every order goes out by 15:48, a cutoff the owner's hard caps
hold, as design.md's `### Strategies, schedules and decisions` says. That rule
also checks the cutoff again just before each broker call, and keeps a pair
whole by cancelling an accepted leg whose partner fails. The official daily
bar arrives the next evening, and monitoring then reports how far the
decision's price and the fills sat from it.

```mermaid
sequenceDiagram
    autonumber
    participant S as Scheduler
    participant V as Supervisor
    participant F as Blob store
    participant D as Data
    participant X as Execution
    participant B as Broker
    participant J as Journal
    participant M as Monitoring
    participant A as Alpha model
    participant K as Risk model
    participant C as Cost model
    participant P as Portfolio construction

    S->>V: start the day's run
    V->>F: the deployment's lease taken, the release recorded
    V->>V: kill switch blob absent?
    V->>F: the run's plan
    V->>D: start
    V->>X: start
    V->>M: start
    V->>A: start
    V->>K: start
    V->>C: start
    V->>P: start
    Note over V,M: Phase 1, read the data, then recover
    X->>X: create the account's lease
    D->>F: snapshot, naming each bar read, and data's decision result
    F-->>X: a message: data's result
    X->>B: orders in doubt, fills since the last decision
    B-->>X: fills since the last decision
    X->>J: each fill journaled
    X->>B: positions and open orders
    B-->>X: positions and open orders
    X->>F: account file, naming the journal entry it replayed through, and execution's recover result
    F-->>M: a message: execution's recover result
    M->>F: reconciled book and monitoring's reconcile result
    Note over V,P: Phase 2, decide
    par
        F-->>A: a message: data's result
        A->>F: forecast, hedge ratio, z-score, and alpha's result
    and
        F-->>C: a message: data's result
        C->>F: cost schedule and cost's result
    and
        F-->>K: messages: data's and monitoring's results
        K->>F: constraints and risk's result
    end
    F-->>P: messages: alpha's, risk's and cost's results
    P->>F: target portfolio and portfolio construction's result
    Note over X,J: Phase 3, send, in the execution worker still running
    F-->>X: a message: portfolio construction's result
    X->>B: positions, open orders and fills since recover
    B-->>X: the broker's state now
    X->>J: any new fill journaled, under its fill id
    X->>X: book from the journal agrees with the broker?
    X->>J: the whole target portfolio and its planned orders journaled
    X->>X: clock agrees with the broker's, and before the cutoff?
    loop each order the order manager plans
        X->>X: kill switch blob absent, and before the cutoff?
        X->>J: intent journaled
        X->>J: replay the book
        X->>X: risk gate check
        X->>J: approval journaled
        X->>J: sending journaled
        X->>X: kill switch absent, and before the cutoff, again?
        X->>B: send, with its client order id
        B-->>X: ack
        X->>J: ack journaled
    end
    X->>B: is every leg accepted?
    alt a leg rejected or unsent while another is accepted
        X->>J: each cancel journaled
        X->>B: cancel the accepted legs
    end
    X->>F: send outcome and execution's send result
    F-->>M: a message: execution's result
    M->>F: the decision's report and monitoring's report result
    M->>M: ping the heartbeat service
```

The phases in order.

1. **Read the data and recover.** Data reads the bars the decision's lookback
   needs from marketlake, about 40 sessions of each ETF, and records which ones
   it read before any model sees them. Execution, which took the broker
   account's lease when it started, waits for that snapshot, so it reads the
   broker at the decision's own time. It settles any order a crash left in
   doubt, journals the fills that arrived since the last decision, and writes
   the book it replays
   from the journal beside the broker's positions and open orders. Monitoring
   then checks the two agree, position by position and order by order. A run that starts on a book the broker disagrees with would trade
   the wrong difference.
2. **Decide.** The alpha, risk and cost models run at once, each on the blobs
   it needs. The risk model sets the constraints, meaning the limits for this
   decision. Portfolio construction builds the target portfolio within them.
3. **Send.** Execution, which has held the account's lease since phase 1, reads
   the target portfolio its result lists. Following the owner's ruling to
   fetch and sync before changing state, it first fetches the broker's
   positions, open orders and fills again, journals any fill that arrived
   since phase 1, and checks the journal's book against the broker. A fill of
   its own order brings the book up to date, and any other difference halts
   for a person. Only then does it journal the target whole, with the planned
   orders and their client order ids, the journal entry it synced through and
   a hash of the broker state it fetched, in one stored entry. A target portfolio names every
   instrument the book holds or the forecast names, with a flat leg as an
   explicit zero, and execution halts on one that leaves an instrument out.
   Its order manager turns the target minus the positions and the open orders
   into orders. Each order goes through the order path alone. The order path
   checks the kill switch again, journals the intent, replays the book from the
   journal so the risk gate sees every order already approved, asks the gate,
   journals the approval, journals that it is sending, checks the switch and
   the market-on-close cutoff once more, and only then sends. After the loop
   it looks up each leg, and cancels any accepted leg whose partner was
   rejected or never sent.
   Monitoring writes the decision's report and pings the heartbeat service
   last, so a decision that dies or hangs never pings.

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

1. The worker that finds the problem writes a halt as its decision result,
   with the reason, in place of its outputs.
2. Every worker downstream finds that halt as its input's result, writes its
   own halt naming it, skips the decision and goes on to its next one. A
   worker exits once it has a result for every decision in the plan.
3. Monitoring, which reads every component's results, journals every halt,
   raises an alarm on the halt that names no earlier one, once per reason in
   a run until a decision succeeds, writes the decision's report and pings the
   heartbeat service's failure address.

The same route runs in every mode, and the run goes on to its next decision,
which halts again if the cause is still there. A failure, meaning a crash or
a missed deadline, takes it too. The supervisor writes the halt as the failed
worker's result, writes a halt for every decision left in the plan, and
stops every worker but monitoring, which reports and exits.

### A missing or stale bar

```mermaid
sequenceDiagram
    participant D as Data
    participant X as Execution
    participant F as Blob store
    participant M as Monitoring
    participant J as Journal
    participant L as Alert channel

    D->>D: GLD fresh, USO missing
    D->>F: a halt as data's result, reason "USO missing for the decision"
    F-->>X: a message: data's result is a halt
    X->>F: a halt as execution's send result, naming data's
    F-->>M: a message: data's result is a halt
    M->>J: halt journaled
    M->>L: alarm
    Note over D,M: No model reads a snapshot. No order is sent.<br/>Positions stay as they are.
```

A stale bar takes the same path. Stale means the newest bar is older than the
decision time allows, which is how a feed that stopped looks when it keeps
answering. Data reports the gap and never fills it with the last close.
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
    participant F as Blob store
    participant M as Monitoring
    participant L as Alert channel

    Note over V: The machine rebooted. Yesterday's execution worker died<br/>after "sending journaled" and before the ack.
    V->>F: the deployment's lease, taken, and the run's plan
    V->>V: kill switch blob absent?
    V->>X: start
    V->>D: start
    V->>M: start
    par
        X->>F: the account's lease, clearing the one this VM's earlier boot left, journaled
        X->>J: which orders were sending with no ack?
        J-->>X: one
        X->>J: in doubt journaled
        X->>B: is there an order with this client order id?
        B-->>X: yes, acknowledged
        X->>J: ack journaled
        X->>F: account file and execution's recover result
    and
        D->>D: are the segments up to the decision time in marketlake?
        D->>F: a halt as data's result, reason "data not ready"
    end
    F-->>X: a message: data's result is a halt
    X->>F: a halt as execution's send result, naming data's, without sending
    F-->>M: a message: data's result is a halt
    M->>J: halt journaled
    M->>L: alarm
    Note over V,L: Execution never sends before the data is ready
```

The restart runs the same first phase as any trading day.

1. Execution settles the order the dead worker left half-sent, by asking the
   broker about it rather than sending it again.
2. Data finds the segments up to the decision time not yet in marketlake and
   halts.

The order in which the processes come up no longer matters. Execution may
start before data, as it does here, but it sends only what a target
portfolio asks for, and that cannot exist until a snapshot does. The charter
gives the operator the startup order: execution may send an order only once
every upstream worker has written a decision result that lists outputs, not a
halt, for the decision.
The pipeline makes that order something the platform cannot get wrong. "Not ready" is a halt, never an empty forecast.

### The risk gate refuses an order

```mermaid
sequenceDiagram
    participant X as Execution
    participant J as Journal
    participant B as Broker
    participant F as Blob store
    participant M as Monitoring
    participant L as Alert channel

    X->>J: intent journaled, buy 900 USO
    X->>X: risk gate check
    X->>J: refusal journaled with the limit and the value
    X->>F: a halt as execution's result, reason "over the maximum position"
    F-->>M: a message: execution's result is a halt
    M->>J: halt journaled
    M->>L: alarm
    Note over B: The broker never sees the order
```

A refusal is final, and execution halts rather than sending the rest of its
orders, because portfolio construction sized the rest assuming this one would
go through. Nothing resizes the order to fit either, since an order cut down
to pass the risk gate is a trade nobody chose. Clearing the halt sends nothing
more for that decision. The next decision plans again from the book. Holding
the account after a refusal until a person clears it waits for a strategy
that decides minutes apart, as design.md's
`### Strategies, schedules and decisions` says.

The daily loss stop works through the same gate. Once the day's loss passes the
stop, the risk gate refuses every new order. [#7](https://github.com/l3a0/ithildin-runtime/issues/7)
lists the limit as "an automatic unwind and stop", and an unwind is itself a
trade. Slice 1 stops only, as question 8 settles.

### The book and the broker disagree

```mermaid
sequenceDiagram
    participant X as Execution
    participant M as Monitoring
    participant F as Blob store
    participant J as Journal
    participant L as Alert channel

    X->>F: book holds 100 USO, broker holds 90 USO
    M->>M: reconcile, mismatch of 10 USO
    M->>F: a halt as monitoring's result, with both positions
    F-->>X: a message: a halt passed on from monitoring's
    X->>F: a halt as execution's send result, without sending
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
    K->>K: create the kill switch blob
    K->>J: halt journaled, reason "kill switch"
    K->>A: alarm
    K->>K: a message on every worker's queue in each run open on the account
    K->>X: start in cancel mode
    X->>X: wait for the account's lease, up to a deadline
    X->>J: orders in doubt settled
    X->>J: each cancel journaled
    X->>B: cancel
    X-->>K: orders cancelled, cancels failed
    K->>A: second alarm if any cancel failed or the deadline passed
    Note over K,X: Every other worker checks the file when it starts<br/>and whenever a message wakes it, and execution before every send

    Note over H,A: A decision that never finishes
    H->>H: no ping by the deadline
    H->>A: dead-man alarm
```

The two cover different failures.

1. **The kill switch** is for a run that is alive and doing something nobody
   wants.
2. **The dead-man check** is for a run that is not alive at all, including a
   supervisor that died, since monitoring pings only as each decision's last
   step. The check runs on the deployment's schedule, and its grace period has
   to exceed the longest chain of worker deadlines plus the report, or a slow,
   healthy decision alarms.

The kill switch is a command, not a running process. It creates a file at one
path per broker account, which every worker reads, and it cancels resting
orders through an execution worker it starts in cancel mode, so the cancel is
journaled like any other step. Two workers run with the switch set, because
neither sends an order: that cancelling worker, and monitoring, whose report
says the switch is set. If a running execution worker holds the account's
lease, it sees the switch when the switch's message wakes it or before its
next send and exits, and the cancelling worker takes the lease once it is
free. A switch that cannot be read halts with the reason "kill switch unreadable" and the error,
so the journal never mistakes a permission problem for a deliberate pull. The split of the kill switch work is on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11). Market-on-close
orders rest at the broker from about 15:48 until the close, so a switch
pulled in that window stops nothing already sent unless it also cancels
them, which is why it does, as question 7 settles. The exchange stops taking
cancels of closing-auction orders some minutes before the close, so a switch
pulled after that cannot stop the fill, and the cancelling worker's report
says so. The cancel cutoff is the earlier of Alpaca's and the exchange's.
NYSE Arca, where GLD and USO list, refuses cancels of closing-auction
orders from 15:59, as a
[Lightspeed guide](https://lightspeed.com/support/lightspeed-trader-on-close-and-on-open-guide)
and an [SEC filing](https://www.sec.gov/files/rules/sro/nyse/2018/34-84804.pdf)
on its closing freeze describe. Alpaca's
[order page](https://docs.alpaca.markets/docs/orders-at-alpaca) names no
separate cancel cutoff, so this doc takes 15:50, Alpaca's entry cutoff, as
the cancel cutoff until Alpaca says otherwise.

### Every alarm and its drill

The charter's paper gate asks for every alarm to fire in a drill first. An
alarm reads zero while it is healthy, so only a drill shows it works. This
shape adds alarms that one process would not need, and each one gets a row
here, with the issue that builds its drill.

| Alarm | Reason it writes | Drill | Built on |
| --- | --- | --- | --- |
| Missing or stale bar | The instrument and its state | A replay with a bar removed or aged, test 2 | [#5](https://github.com/l3a0/ithildin-runtime/issues/5), [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Data not ready | "data not ready" | A run whose segments up to the decision time are missing, test 2 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Risk gate refusal | The limit and the value | One order over each limit, test 3 | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Book and broker disagree | Both positions, or both sets of open orders | A simulated broker account edited by hand | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| The broker moved between execution's recover and send stages in a way the platform did not ask for | Both positions, or both sets of open orders, at the send stage | A simulated broker account edited by hand between the stages | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A fill with no `sending` entry, or past its order's quantity | The client order id | A simulated broker that reports an unknown fill | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A target portfolio that leaves out an instrument the book holds | The instrument | A target portfolio file written without one leg | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| A bad constraints file or hard cap | The field and its value | A file with a missing field, a NaN and a wrong sign | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| A send that would start after the market-on-close cutoff | The decision time, the cutoff and the clock's time | A test clock set past the cutoff before the order loop, and one that crosses it after the first leg's ack | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| The VM's clock differs from the broker's by more than the tolerance | Both times | A test clock skewed by three minutes | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A pair with one leg accepted and another rejected or unsent | Each leg's client order id and state | A broker double that rejects the second leg on the submit, and one that rejects it after acknowledging it | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| Execution failed between a pair's legs | The decision and each leg's state | Kill execution after the first leg's ack | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| After the close, a pair whose filled legs differ from the planned hedge | Each leg's fill | A broker double that fills one leg in part | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A traded or held instrument whose price carries a spike flag | The instrument and both prices | A provisional segment with one spiked quote | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Kill switch pulled | "kill switch" | Pull it before a run, test 4 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Kill switch unreadable | "kill switch unreadable" and the error | Deny the runtime's credential read access to the switch blob | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A write S3 refuses for a reason other than an existing key, or does not answer by the deadline | The key and S3's answer | Point a run at a bucket the credential cannot write, and block the S3 endpoint mid-run | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| A lease another VM holds, the deployment's or the account's | The holder's VM, boot, process and run | Start a second VM while the first runs | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| The broker key cannot be read | The secret store's answer | Deny the execution user's read of the secret | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A cancel failed or timed out | How many orders were cancelled and which failed | A broker double that refuses a cancel | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A worker crashed | Its exit status or signal and the end of its error output | Kill a worker mid-decision | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A worker missed its deadline | The worker and the deadline | A worker that sleeps past it | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A second execution worker, or a second supervisor on one VM | The lease or lock that was held | Start two | [#10](https://github.com/l3a0/ithildin-runtime/issues/10), [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A supervisor on a different release | Both releases | Restart mid-run under another tag | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| The scheduler's restart limit reached | The supervisor's last exit | Make the supervisor crash on start | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A journal entry whose hash or link does not match, or a gap in its numbers | The account and the sequence number | Alter an entry in a copy of the journal | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| An output file that is missing, or whose SHA-256 does not match the decision result listing it | The file and the result | Change a byte in a copy of an output file | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| A deterministic step that writes different bytes for the same inputs | The key and both SHA-256s | Run a step that puts the clock time in its output | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| A worker exits before it has a result for every decision in the plan | The worker | A worker that exits after its first decision | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A blob for the wrong decision, or an output file with no entries and no "trade nothing" mark | The key and the decision | Write one by hand | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| The sealed day differs from what a provisional read saw | The segment's key and both SHA-256s | Change a sealed partition after a run read its segments | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| A supervisor restarted mid-run | The plan it found and the workers still running | Kill the supervisor between two decisions | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| An order the journal holds open that the broker no longer lists, in a state its time in force does not explain | The client order id and the broker's answer | A simulated broker that drops a good-till-cancelled order | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| The Docker daemon stopped or restarted mid-run | The daemon's state and the run's last decision | Restart the daemon during a paper decision | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Dead-man | None. The heartbeat service raises it | A day of two decisions whose second ping is skipped, test 4 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |

Each deadline that the supervisor enforces is set on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), beside the
heartbeat's grace period, which has to exceed them.

## Types and interfaces

A class diagram shows the types the workers pass to each other, each written
as an output file that a decision result lists, and the interfaces each module implements. Two diagrams follow, one
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
        +datetime end
        +str interval
        +datetime first_seen
    }
    class Snapshot {
        +datetime decision_at
        +dict bars
        +dict status
        +require_fresh()
    }
    class Basket {
        +dict legs
    }
    class Forecast {
        +str strategy
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
        +time moc_cutoff
    }
    class TargetPortfolio {
        +datetime decision_at
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
        +datetime decision_at
        +str target_hash
        +list orders
    }
    class BlobRef {
        +str key
        +str sha256
    }
    class StoredBlob {
        +BlobRef ref
        +str etag
        +datetime stored_at
    }
    class BlobHeader {
        +int format
        +str deployment
        +str run
        +str component
        +datetime decision_at
        +dict inputs
        +str release
        +str config_hash
    }
    class DecisionResult {
        +BlobHeader header
        +list outputs
        +HaltRecord halt
    }
    class HaltRecord {
        +str reason
        +BlobRef cause
        +int exit_status
        +str error_tail
    }
    class Plan {
        +str deployment
        +str run
        +str mode
        +list decisions
    }
    class JournalEntry {
        +int seq
        +datetime built_at
        +str body
        +str hash
        +str prev_hash
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
        +snapshot(decision_at) Snapshot
    }
    class Clock {
        <<Protocol>>
        +now() datetime
        +wait_until(datetime)
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
    class BlobStore {
        <<Protocol>>
        +create(key, data, sha256) str
        +get(key) bytes
        +head(key) StoredBlob
        +list(prefix, after) list
        +delete_if_match(key, etag)
    }
    class Alerts {
        <<Protocol>>
        +alarm(reason)
        +heartbeat()
    }
    class Journal {
        -BlobStore store
        -str account
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
        +cancel(client_order_id) Ack
        +cancel_resting() list
    }
    class Monitoring {
        -Alerts alerts
        +reconcile(Book, BrokerAccount) bool
        +alarm(reason)
        +report(run)
    }

    Journal o-- BlobStore
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
   (L3884). `end` and `interval` say when the bar closed and how long it is,
   so one daily bar and minute bars of the same instrument can sit side by
   side, keyed in a snapshot by instrument and interval. Every `DataSource`
   adapter applies one rule: `snapshot(decision_at)` returns only bars whose
   `end` is at or before the decision time.
3. **`Snapshot.require_fresh()` is how a module refuses a gap.** A snapshot is
   the set of bars one decision read, each pointing at the partition that holds
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
   approved or sending, including those of an earlier attempt, each at its
   worst case for each limit. Three of the seven limits need the snapshot's
   prices to value the book.
   1. Maximum portfolio size, the `max_gross` field.
   2. Net exposure.
   3. The loss stop.
   The `Constraints` come from the risk model's constraints file, so the order
   path first checks that every field of that file and every hard cap in the
   hard caps blob only the owner can write is present, finite and of the sign the field expects, and
   halts otherwise. `moc_cutoff` must be a time in the exchange's time zone,
   applied on the decision's own session date. Then it takes the tighter of
   each pair: the smaller maximum, the smaller loss for the loss stop, and
   the earlier cutoff. The gate refuses a market-on-close order past the
   cutoff, given the clock's reading. A constraints file can
   tighten a limit and never loosen it. This is a proposal for
   [#7](https://github.com/l3a0/ithildin-runtime/issues/7).
6. **The book comes from the journal.** `Journal.replay()` applies every entry
   in order to build a `Book`. The broker's positions and open orders are read
   only to reconcile. `Journal` itself lives in `core`, because execution,
   monitoring and the kill switch command all append to it.
7. **The cost model writes a schedule rather than answering calls.**
   Portfolio construction priced each candidate trade by calling the cost
   model. With each module in its own process, the cost model instead writes a
   `CostSchedule` per decision, a fixed and a per-share cost for each
   instrument, and portfolio construction evaluates it. Slice 1's flat or
   linear model fits that shape. This is a proposal for
   [#8](https://github.com/l3a0/ithildin-runtime/issues/8).
8. **Every type that crosses between workers has a file form.** That
   includes `BrokerAccount` and `SendOutcome`. Each travels as an output file
   that begins with a `BlobHeader`, naming its inputs by key and SHA-256, and
   that its component's `DecisionResult` lists. A test writes each one and reads
   it back unchanged, and a deterministic component's format is canonical, so
   the same inputs give the same bytes. `Halt` stays an exception inside a
   worker, and at the worker's edge it becomes a `DecisionResult` that holds a
   `HaltRecord`, whose `cause` names the halt it met when it is passing one on,
   and is empty when the worker found the problem itself. The supervisor
   writes the same record, with an exit status and the end of the error
   output, for a worker that crashed. `Plan` holds the run's decisions, each with its time and the strategies
   deciding at it, built by one function in `core` from the schedules and the
   trading calendar, and
   `JournalEntry` is the one numbered record, chained to the entry before it.
   `BlobStore` is the storage port, S3 in shadow, paper and live and a
   memory-backed folder in a backtest, and one contract test suite runs
   against both. `create` returns the stored blob's tag and `head` its stored
   time, so a lease can be deleted only by the tag its creator got. `Journal`
   writes through `BlobStore` too, for the account the broker adapter names,
   so the journal has no storage port of its own.

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
    Approved --> Abandoned: switch set or cutoff passed before the broker call
    Acknowledged --> Expired: day or market-on-close order not filled by the close
    PartlyFilled --> Expired: rest not filled by the close
    Sending --> Rejected: broker refuses the submit
    Acknowledged --> Rejected: broker rejects
    Acknowledged --> Cancelled: kill switch, or its pair's other leg failed
    PartlyFilled --> Cancelled: kill switch, or its pair's other leg failed
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
rather than leaving the next decision to guess. The only way out is to ask the
broker by client order id. Sending the order again is never a way out. An order
that ends `Abandoned` or `Lost` never reached the broker, so the next decision's
order manager plans from the book as if it had never existed.

`Cancelled` is reached two ways: through the kill switch, as question 7
settles, and when one leg of a pair is accepted and the other fails, as
design.md's `### Strategies, schedules and decisions` says. Either way the
cancel is journaled first, and the sync then counts it as the platform's own.

## The life of a run

One decision's chain moves through these states. In shadow, paper and live a
run is one trading day's decisions. In a backtest it is every decision of the
span. A halt ends only its own decision's chain.

```mermaid
stateDiagram-v2
    [*] --> Starting
    Starting --> Halted: kill switch set
    Starting --> Preparing: workers waiting on the decision's inputs
    Preparing --> Halted: data missing, stale or not ready, an unknown fill, or book and broker disagree
    Preparing --> Deciding: snapshot written, book reconciled
    Deciding --> Halted: a worker halts
    Deciding --> Sending: target portfolio written
    Sending --> Halted: risk gate refuses, kill switch set, or an order in doubt
    Sending --> Reporting: every order journaled and answered
    Halted --> Reporting: a halt written as a result, then journaled and alarmed by monitoring
    Reporting --> Done: report written, heartbeat pinged
    Done --> [*]
    Starting --> Failed: a supervisor on another release
    Preparing --> Failed: a worker crashes or misses its deadline
    Deciding --> Failed: a worker crashes or misses its deadline
    Sending --> Failed: a worker crashes or misses its deadline
    Failed --> Reporting: every worker but monitoring stopped
```

`Preparing` is the first phase of a decision: data reads the snapshot
while execution settles orders in doubt, journals fills and writes the book,
and monitoring reconciles. The supervisor holds none of these states in
memory. It reads them from storage: which components have a decision result,
and whether a result is a halt. A restarted supervisor finds the state the
same way, and starts every worker whose lock is free. Each one checks its own
result key for each decision, reads the blobs that exist, and writes the ones
that are missing.

A halt and a failure differ in what they stop. A halt stops one decision, and
in a backtest the span goes on to the next. A failure stops the whole run.
Both pass through `Reporting`, so a halted or failed day still writes its
report and its journal entry, and monitoring pings the heartbeat service's
failure address so the owner hears even if the alarm from the machine is lost.
If monitoring itself is what failed, no ping arrives, and the dead-man check
raises the alarm. A halt is final for its decision, which is this doc's choice.
The owner ruled on 2026-10-29 that the owner or the operator clears it, and
clearing never reruns the decision. The next decision starts fresh, and it
halts again while the kill switch is set or the cause is still there.

## What is stored

An entity-relationship diagram shows what is kept and how the records point at
each other. A pair of bars at a line's end means exactly one, and a circle with a
crow's foot means zero or more.

```mermaid
erDiagram
    VINTAGE ||--o{ BAR : "holds"
    INSTRUMENT ||--o{ BAR : "is priced by"
    RUN ||--|{ SNAPSHOT : "acts on, one per decision"
    SNAPSHOT }o--o{ BAR : "lists"
    RUN ||--o{ JOURNAL_ENTRY : "writes"
    JOURNAL_ENTRY }o--|| ORDER : "records a step of"
    ORDER ||--o{ FILL : "fills by"
    RUN ||--|{ REPORT : "reports each decision"
    RUN ||--o{ SIGNAL : "records"
    RUN ||--|{ DECISION_RESULT : "ends each component's decision with"
    DECISION_RESULT ||--o{ OUTPUT_FILE : "lists"

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
        datetime end
        string interval
        float close
        datetime first_seen
    }
    RUN {
        string id
        string deployment
        datetime first_decision
        datetime last_decision
        string mode
        string release
        string outcome
    }
    SIGNAL {
        string run_id
        datetime decision_at
        string strategy
        string name
        float value
        string release
    }
    DECISION_RESULT {
        string key
        string sha256
        string component
        datetime decision_at
        string kind
        string halt_reason
        string halt_cause
        string release
        string config_hash
        datetime stored_at
    }
    OUTPUT_FILE {
        string key
        string sha256
        string component
        datetime decision_at
        string name
        string inputs
        datetime stored_at
    }
    JOURNAL_ENTRY {
        int seq
        string hash
        string body
        datetime built_at
        datetime stored_at
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
2. **The run, D1, holds `SNAPSHOT`, `SIGNAL`, `DECISION_RESULT` and
   `OUTPUT_FILE`,** as blobs in S3 under the run's prefix, each written once at
   a key fixed by the run, the component, the decision and, for an output
   file, its name. A snapshot lists the marketlake bars one decision read, by
   partition and checksum. A signal record holds a value the run computed,
   such as the hedge ratio, the z-score or the forecast, keyed by run,
   component, decision and name. Each blob's header names the blobs it came from, and a halt says
   why a decision stopped. A deterministic component's blobs are regenerable
   from marketlake, the journal and the code, but data's snapshots and
   execution's account files capture what was true at the moment, and the
   halts the supervisor writes record exits, so none of them can be. A
   backtest's blobs sit in a memory-backed folder and vanish when it ends,
   because a backtest can be regenerated. The blobs also make a question such
   as "how did the backtest's z-score on a decision differ from the shadow
   run's" a lookup rather than a recomputation.
3. **The security master, D2, holds `INSTRUMENT`.** It is a small committed file
   that changes only through a pull request.
4. **The journal, D3, holds `JOURNAL_ENTRY`.** It is the one numbered log:
   one per broker account, outside every run, because the book is replayed
   from it across runs. Each entry is a blob at a key fixed by the account and
   its sequence number, holding the entry's body, its hash and the hash of the
   entry before it, and the time it was built, to the microsecond, so the
   entry itself is the record. The owner's ruling that the journal is the only
   numbered log, when SQLite was dropped, replaced the earlier ruling on
   [#10](https://github.com/l3a0/ithildin-runtime/issues/10) that kept entries
   in SQLite rows. `Journal` writes through `BlobStore` for the account the
   broker adapter names.
   `ORDER` and `FILL` are read from journal entries rather than kept as
   records of their own. Each entry is stored before the step it records
   happens. The kill switch command, monitoring and execution can all write,
   and S3's refusal of an existing key is what keeps two of them from taking
   one number: the writer that is refused reads the new entries, decides its
   step again, and takes the next number. Changing an entry would break the
   link from the entry after it, and the bucket refuses any replacement or
   delete and keeps every version, so an edit cannot hide.
5. **Run reports, D4, hold `RUN` and `REPORT`.** A report is computed from the
   journal and the snapshot, so losing one costs nothing. The run writes it to S3 for the
   owner to read, not as a record.
6. **The simulated broker's account, one blob per simulated account per
   decision, outside every run,** holds the resting orders and positions the
   simulator keeps. The simulated broker's adapter reads and writes it
   through a `BlobStore` the entry point hands it, so execution's own outputs
   are the same whichever broker it holds. Backtest and shadow use the same
   form, so an order still resting when a shadow run ends is there in the
   next run, or on a new VM. Each simulated account has its
   own lease and kill switch, so a backtest never shares the paper account's.
   This is a proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10).

## Where it runs

A deployment diagram shows which machine each piece runs on and what crosses
the machine's edge. The owner ruled on 2026-10-29 that the VM keeps no state
that matters, and that the storage is S3, so everything the platform must
keep sits in the runtime's bucket, and the VM can be replaced by starting a
new one from the pinned release.

```mermaid
flowchart TB
    subgraph host["The runtime's VM, separate from marketlake's, holding no state"]
        start["Start unit<br/>starts the scheduler"]
        timer["Scheduler<br/>a systemd timer, once per trading day"]
        run["Supervisor and workers<br/>at a release the owner pinned"]
        work[("Working directory<br/>queues, lock files, error output")]
        mirror[("Local lake mirror<br/>the partitions the strategies need")]
        start --> timer --> run
        run --> work
        mirror --> run
    end
    mlvm["marketlake's VM<br/>capture daemon"]
    lake[("marketlake's bucket")]
    bucket[("The runtime's S3 bucket<br/>run blobs, journal entries,<br/>leases, kill switch, hard caps")]
    secrets["Secret store<br/>broker paper key"]
    broker["Broker paper API"]
    beat["Heartbeat service"]
    alerts["Alert channel"]

    mlvm --> lake
    lake -->|"download, checksum checked"| mirror
    run <-->|"create-only writes, reads"| bucket
    secrets -. "execution worker's user only" .-> run
    run <--> broker
    run --> beat
    run --> alerts
    beat --> alerts
```

Ten rules hold the deployment to the premise and the hard limits.

1. **The owner pins the release.** The scheduler runs a tagged commit the owner
   merged and chose, so the code that traded is always known. Deploying is the
   owner's under hard limit 6. The release also pins boto3, from a version
   whose botocore, 1.35.67 or later, sends both S3's refuse-if-it-exists
   condition and its conditional delete.
2. **Nothing the platform must keep lives only on the VM.** Every blob a run
   writes and every journal entry is created in the runtime's bucket before
   anything relies on it. The VM's working directory holds only queues, lock
   files and error output, and none of it is kept. The journal is one of the
   three things the premise says cannot be recovered, so losing the VM must
   not take it. Captured bars, another of the three, live in marketlake,
   which backs up its own lake.
3. **The broker key lives in a secret store, and only execution can read it.**
   The execution worker runs as a Unix user of its own, started by a systemd
   unit with that user, never by a process that keeps root. Only root and one
   provisioning user can reach the VM's instance metadata service, which a
   firewall rule matched on the process's owner enforces across reboots,
   because any process that can reach that service can fetch the VM's
   credentials. At boot the provisioning step writes each worker user a
   credential file only that user can read, never carried in the VM's start-up
   data. Only the execution user's credential can read the secret store.
   Execution reads the key from the store into its own memory, never into its
   environment, which other processes and child processes can see, and never
   into a file. It is a paper key with no withdrawal or transfer permission,
   which is hard limit 5. A drill on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) has a worker other than execution
   try each route to the key, the metadata service, the start-up data, another
   process's environment and the unit's settings, and expects every one to
   fail. The owner ruled on 2026-10-29 that the store is the one marketlake
   uses, AWS Systems Manager Parameter Store, holding the key as a
   `SecureString` parameter encrypted under KMS, and that the runtime's
   parameter, bucket and VM all live in marketlake's AWS account, the second
   choice of question 11. That account is shared with a public repository's
   CI, so the ruling puts the data behind a list of who may reach it, the
   bucket policy in rule 5, and narrows the one role that runs on public
   pull requests.
   1. **AWS's managed `aws/ssm` key encrypts the parameter.** The owner ruled
      on 2026-10-29 that AWS's key is fine, rather than a KMS key of the
      runtime's own. Every principal in the account may decrypt through SSM
      under that key, so who can read the parameter is decided by IAM alone:
      a grant of `ssm:GetParameter` on that parameter, given only to the
      execution user's credential, and the refusals in item 3 for every role
      that holds a broader read. The price is that a role added later with a
      broad parameter read can read the key until someone refuses it, which
      the drill in item 6 catches only when it runs.
   2. **marketlake's plan role reads only what `tofu plan` needs.** Its
      `ReadOnlyAccess`, which reads every bucket and parameter and runs on
      any pull request of a public repository, gives way to a policy that
      lists the reads, with no parameter read of any kind and object reads on
      marketlake's state bucket alone. The owner chose this fix at the cause
      on 2026-10-29, because a missing grant stops the next plan in plain
      sight, while a missing refusal exposes data silently.
   3. **The roles that keep a broad read refuse the key's path as a
      backstop.** marketlake's VM role, its apply role, which runs only after
      the owner's approval, and the runtime's own VM role each refuse
      `ssm:GetParameter`, `ssm:GetParameters` and `ssm:GetParameterHistory`
      on the broker key's path, and `ssm:GetParametersByPath` on every path,
      because a recursive read of a parent path returns every parameter below
      it. The runtime's VM role refuses the same on `/marketlake/config` and
      everything below it, which holds marketlake's market data credentials
      and alert keys.
   4. **marketlake's apply and deploy roles cannot touch the runtime's VM.**
      The VM and its volume carry a tag of the runtime's own, and both roles
      refuse every EC2 action on resources with that tag, and any change to
      that tag or to marketlake's capture-host tag on them, so neither can
      rewrite the VM, copy its disk or point marketlake's deploy document at
      it. This one is a list of refusals, so a check in marketlake's CI fails
      any role that lacks it. The worker credential files also live in
      memory-backed storage, which a copy of the disk would not hold, and the
      runtime uses no Run Command in that account, because marketlake's
      deploy role can read every command's output there.
   5. **The parameter lives in us-east-1**, the region marketlake's refusals
      name, and the owner writes its value by hand through standard input,
      as marketlake writes its own. No OpenTofu configuration, state or
      variable ever holds it.
   6. **A drill** reads the key with the plan, apply and VM roles'
      credentials and the runtime's VM role's, directly, by a recursive read
      from `/` and through its history, reads marketlake's path with the
      runtime's VM role, and tries an EC2 action on the runtime's VM with the
      apply and deploy roles, and expects "access denied" every time.

   [l3a0/marketlake#944](https://github.com/l3a0/marketlake/issues/944)
   builds marketlake's side. What remains needs the owner's approval or the
   owner's own credential. An approved apply can edit a role's refusal,
   which its policy diff shows. The owner's administrator session, and the
   account's root user, can edit any of these policies, and only a service
   control policy in an AWS organization binds a root user, which this
   account does not use.
4. **The kill switch and the hard caps are blobs.** The runtime's credential
   can create the switch, which pulls it, and can never delete it. The owner
   ruled on 2026-10-29 that only the owner's credential clears a live
   account's switch, and that the operator's may also clear a paper or shadow
   account's. Neither credential sits on the VM, and the bucket's policy
   exempts only those principals, account by account, from its refusal of
   deletes on the switch's key. The hard caps blob can be written or deleted only by the
   owner's credential, which an explicit refusal in the bucket's policy for
   every other principal enforces, because raising a cap raises a capital
   limit, which hard limit 1 keeps for the owner. Copying an older version
   back counts as a write, so the same refusal covers it. Pulling the switch
   needs no deploy.
5. **The runtime's credential does only what the rules need.** It can create
   and read blobs under the runs', the journal's and the simulated accounts'
   prefixes, create and read the leases, create but not delete the switch,
   read the hard caps, and list each of those prefixes, without which S3
   answers a missing key with "access denied" rather than "no such blob". A
   drill reads an absent switch with that list right and expects "no such
   blob". Only the supervisor's and execution's credentials may delete a
   lease, and only with the tag the creator got, which leaves a delete marker
   so the lease's history survives. No credential the runtime holds can delete
   a version, or change the bucket's policy, versioning or lifecycle. The
   bucket's policy, whose refusals name the runtime's principals, refuses any
   write on those prefixes that lacks the refuse-if-it-exists condition, any
   delete outside the leases, and any lease delete without the tag condition.
   It also refuses every S3 action on the bucket to any principal that is not
   one of the runtime's roles or the owner's, matched on `aws:PrincipalArn`,
   and refuses changes to the policy itself to all but the owner. An explicit
   refusal in a bucket policy overrides any role's own grant, so a role
   marketlake adds later is refused without anyone remembering it, which
   matters because the bucket lives in marketlake's account. The release
   writes every blob with one single-part upload, never a
   multipart one, which S3 cannot make conditional, and the policy marks a
   multipart start so a stray one fails when it completes rather than
   halfway.
6. **A rollback is a pin to the previous tag.** Every blob carries a format
   version, and each release writes only formats the release before it can
   read, so the old release reads what the new one left. The owner stops the
   day's run with `systemctl stop` before moving the pin, which fails the
   run's remaining decisions with the reason "release changed". A worker
   stopped mid-send leaves an order in doubt, which the next recover stage
   settles by its client order id. The old release takes the next trading
   day's run.
7. **The scheduler restarts a supervisor that crashes, up to a limit.** That
   is systemd's `Restart=on-failure` with `StartLimitBurst`, and a
   `StartLimitIntervalSec` of a whole trading day, since systemd's default of
   ten seconds would never reach the limit for a supervisor that crashes once
   a minute. The unit sets `KillMode=process`, so the workers outlive a
   supervisor that crashes and the restarted one leaves them to finish, as
   design.md's rule 7 says, and alarms that it restarted. `RuntimeMaxSec`
   ends a run that is still going before the next day's timer fires, because
   systemd leaves a timer's unit alone while it is still active. Reaching the
   restart limit raises the alarm, and the dead-man check covers a scheduler
   that stops too.
8. **The runtime has a VM of its own, apart from marketlake's.** The owner
   ruled on 2026-10-29 that the runtime runs on a VM separate from the one
   marketlake's capture daemon runs on, and downloads the parts of the lake its
   strategies need. So a run on the runtime cannot starve capture, and a
   failure on one VM leaves the other running. The data worker downloads each
   partition a decision's lookback names from marketlake's bucket into a local
   mirror, checks it against the checksum in marketlake's manifest, and reads
   it through the same bar query. A partition not yet in the bucket is "data
   not ready", which halts. A capture segment read before sealing is checked
   the same way, against its own manifest entry, and the read credential
   covers the segments' prefix too. The mirror is a copy, so losing it costs only a
   download. The runtime's credential for marketlake's bucket reads and never
   writes, and it gets its row in design.md's `## Configuration` when
   [#5](https://github.com/l3a0/ithildin-runtime/issues/5) first reads it.
9. **A replacement VM's supervisor takes the lease, then runs.** The
   supervisor takes the deployment's lease before it writes the plan or starts
   a worker, the same in every mode. If another VM holds it, the supervisor
   halts and alarms, and starts nothing. Once it holds
   the lease, there is nothing to restore, because every worker finds its
   place by checking its own keys in S3. Who starts a replacement VM, from
   which VM image, and how a person confirms the old VM is gone before clearing
   its lease, is a runbook on [#11](https://github.com/l3a0/ithildin-runtime/issues/11).
10. **The release is one container image, and CI tests that image.** This
    is this doc's proposal, raised by the owner on 2026-10-30. One Docker
    image holds the whole runtime: one Python version, every dependency
    installed from `uv.lock` with its hashes enforced, and an entry point per
    worker. Each run is one container from that image, holding the
    supervisor and its workers as the processes and Unix users the rules
    above describe, so a backtest, CI and the VM run the same bytes and the
    same process tree.

    A container per component was considered and turned down. With systemd
    or Docker starting each worker, the supervisor stops being their
    parent, so it no longer learns of a crash the moment it happens, and
    starting execution in cancel mode for a stranded pair leg, stopping
    every worker on a failure, or keeping a worker from starting before the
    deployment's lease is held, would each need the Docker socket or a
    systemd grant, and either one is root on the host. Process ids would
    also stop naming a worker across containers. One container keeps every
    rule above as written.

    The owner said on 2026-10-30 that eventually each component's container
    could run on a machine of its own, or serverless. One container per run
    keeps that path open, because the image already has an entry point per
    worker and every worker already reads and writes through S3. Two seams
    in `runtime` would carry the move, and this doc proposes building them
    with the supervisor on [#11](https://github.com/l3a0/ithildin-runtime/issues/11),
    each with one adapter today.
    1. **A launcher**, through which the supervisor starts, watches and stops
       a worker. Today's adapter starts a child process. A later one could
       start an ECS task, a container on another machine, or a function.
    2. **A queue**, through which a worker's message reaches its readers.
       Today's adapter is a named pipe. A later one could be SQS.

    A contract test suite runs each port's adapters alike, as storage's
    does, so moving a component changes an adapter and not the supervisor's
    rules. What the review of a container per component found stays the
    price of that move: a launcher that is not the parent has to report a
    crash within the time the send stage allows, and starting a worker
    elsewhere needs a credential scoped to the runtime's own workers, never
    one that is root on a host.

    If adopted, rule 10 changes these:
    1. **The pin is an image digest.** Rule 1's tagged commit becomes the
       digest of the image built from it. The plan records the digest, and
       the supervisor halts with "release changed" if its own differs. A
       rollback, rule 6, stops the run's unit, confirms with `docker ps`
       that no container remains, and moves the pin to the previous digest,
       which the VM keeps pulled.
    2. **The container is unprivileged on the host.** Docker's user
       namespace remapping makes the container's root an unprivileged user
       on the host, so the supervisor can start each worker as its own Unix
       user inside the container without root on the VM. The container
       drops every capability but those that switching users and signalling
       workers need, has a read-only root file system, and never runs
       privileged, never shares the host's network, process ids or IPC, and
       never mounts the Docker socket or the system D-Bus.
    3. **Credentials reach the container as files, and the instance's
       never do.** Rule 3's provisioning step, on the host, writes each
       worker user's credential file to memory-backed storage, which the
       container mounts read-only. Only execution's credential reads the
       broker key, which is fetched into memory and never mounted. The
       instance is launched with IMDSv2 required and a token hop limit of 1,
       set explicitly, because AWS's own
       [defaults](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html)
       give 2 for an AMI that requires IMDSv2, and 2 lets a container
       through. A `DOCKER-USER` firewall rule also drops traffic from
       Docker's bridges to the metadata service's addresses. Rule 3's drill
       adds, from inside the container, a token request and a tokenless
       request to the metadata service, each expected to fail.
    4. **systemd runs the one container.** Rule 7's unit starts the
       container rather than the supervisor directly, with the same restart
       limit, and stopping the unit stops the container, so a stop or
       `RuntimeMaxSec` ends the whole run. Stopping the Docker daemon stops
       the container, so a daemon restart is a failure of the run, which
       gets an alarm row, and the daemon is never upgraded during a session.

    CI builds the image on every pull request and runs the tests inside it,
    in the job still named `test` that the ruleset requires: the suite, test
    1's backtest and test 3's fake broker under the supervisor, with the
    memory-backed folder inside the container. A branch of this repository
    can change a workflow, so no control on a pull request can promise that
    it never publishes. The control is provenance. One job, run only on a
    push to `main` and never on `pull_request_target`, `workflow_run` or a
    manual dispatch, rebuilds the image, runs the tests on it, publishes it
    to GitHub's container registry tagged with its commit, and attests its
    build provenance. That job alone holds `packages: write`,
    `id-token: write` and `attestations: write`, and every other job
    reads only. Before pinning a digest, the owner runs
    `gh attestation verify` on it, requiring the publish workflow on
    `refs/heads/main`, and the deploy runbook on
    [#11](https://github.com/l3a0/ithildin-runtime/issues/11) refuses a
    digest that fails. Every action is pinned to a full commit hash. The
    base image is pinned by digest, a bot proposes its updates, and a
    scheduled job rescans the digest the owner pinned. A `.dockerignore`
    keeps `.git`, `.venv`, `.env*` and `.claude/` out, and no secret enters
    the build through an argument or an environment variable. The owner
    makes the package public once after its first publish, so the VM pulls
    by digest with no GitHub credential. Deploying a digest stays the
    owner's, under hard limit 6.

    The price is a Docker daemon on the VM, running as root, to patch outside
    sessions, an image build on every pull request, and a verification step
    before each deploy. The image carries over to Fargate, though the queue
    into execution, how execution starts and the kill switch's route still
    change there, as question 12 says.

## Package layout and the import rule

```mermaid
flowchart TB
    core["ithildinruntime.core<br/>shared types, Halt, every Protocol,<br/>Journal, the storage contract"]
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
   `Journal`, which three processes append to, and the storage contract every
   worker uses: the `BlobStore` port, writing a blob S3 refuses to replace,
   the blob header, the decision result and its halt, the plan, and the
   message to each reader's queue.
2. Only `runtime` imports the modules together. Each worker's entry point
   lives there. It picks the adapters for the mode it is given, and the
   execution worker's entry point hands the risk module's `RiskGate` to the
   order path. The supervisor lives there too, and imports only `core`. It
   starts each worker as its own process, by name, and writes nothing but the
   plan, the deployment's lease, crash records and the halts of workers that
   crashed, which `core`'s storage contract covers, besides its own lock on
   the VM. Only the entry points read the plan's mode, and a module is handed
   only the decisions.
3. Only `runtime` imports `execution.brokers`, and only the execution worker's
   entry point builds a broker, which it hands to `OrderPath` alone.

A test reads every module's imports with Python's `ast` module and fails on an
arrow the diagram does not draw. A second test runs a day under the supervisor
against a fake broker that fails on three things.

1. An order whose client order id has no approval in the journal.
2. An order whose `sending` entry is not yet stored in the journal.
3. A client order id it has already received. Together they turn
test 3's "no order reaches the broker adapter without passing the gate" into
something executed rather than reviewed. The price is two more tests to keep
in step with the diagram, and a module that needs a sibling has to argue for a
new arrow.

## How the four acceptance tests exercise it

| Test, from [#4](https://github.com/l3a0/ithildin-runtime/issues/4) | Mode | What it runs | What it pins |
| --- | --- | --- | --- |
| 1. The backtest reproduces a replicated strategy | Backtest | marketlake reader over the imported vintages, history clock, simulated broker filling market-on-close orders at the close, zero cost | APR 0.17824928 and Sharpe 0.96467289 to 5e-9, the 162 changes of unit, and the unit on each of the 1,480 days as `chan.bollinger` computes it at `19aadbeb` |
| 2. A replay emits pinned orders, through a missing feed, a stale price and a restart in the wrong order | Backtest | A recorded stretch of sessions with three faults injected. The restart kills the supervisor mid-run and starts execution before data | The orders on each day, and a halt with no orders on each faulted day |
| 3. The risk gate refuses an order over each limit, and nothing bypasses it | Unit and import | `RiskGate` against one order over each of the seven limits, including a market-on-close order past the cutoff, and a pair whose second leg alone breaks `max_net`, the import rule test, and the fake broker | Seven refusals and the pair's, no import of `execution.brokers` outside `runtime`, and no order reaching the fake broker without a journaled approval and `sending` entry, or twice |
| 4. The kill switch and the dead-man check fire in a drill | Paper, drill | Set the switch before a run, and skip the second ping of a day of two decisions | A halt journaled with reason "kill switch", and a dead-man alarm delivered |

Test 1 fills at the close of the day the signal used, because that is what
Chan's script assumes. The owner ruled on question 10 that the paper run
trades that close too, with market-on-close orders, so both fill at the same
price. The two still decide at different times. Test 1's deployment decides
at the close, on the imported daily bars, through a simulated account whose
cutoff is the close, because a 15:40 decision there would see only the day
before's close and shift every signal a session. The paper deployment
decides at 15:40, because no live decision can read a close that has not
happened. Test 1 checks the engine against Chan's timing. A backtest of the
deployed 15:40 schedule needs minute history at 15:40 that the imported
vintages lack, so it waits for marketlake's captured minutes to cover a long
enough span, and shadow running measures the gap until then. A paper order also needs whole shares, or a fractional order the
broker accepts, while the backtest holds fractional dollar positions. So the
two runs differ in two ways.

1. **Timing of the signal.** The paper decision computes its signal on the
   price at 15:40, twenty minutes before the close the backtest's signal
   uses, and a price that moves in those minutes can flip the unit.
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
| `core`'s shared types and every `Protocol`, the file form of each type, `Journal`, the storage contract of `BlobStore`, create-only writes, the blob header, the decision result, the plan and queue messages, the S3 and memory-backed folder adapters with one contract test suite, the import rule test, as proposed below | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| `Bar`, `Snapshot`, the marketlake reader, the snapshot and signal records, security master | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Bollinger band alpha model, `Basket`, `Forecast` | [#6](https://github.com/l3a0/ithildin-runtime/issues/6) |
| `Constraints`, `RiskGate`, the daily loss stop, the stale-data rule | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Flat and linear cost models, `CostSchedule` | [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |
| Equal-risk-weight portfolio construction, `TargetPortfolio` naming every instrument | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| Order manager, `OrderPath`, the journal written through `BlobStore`, each entry stored before its step, fills keyed by the broker's fill id, simulated and paper brokers, the simulated account's blob, the account's lease | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| `Monitoring`, reconciliation, reports, alert channel adapter, dead-man check, kill switch, startup order, the alarms and their drills, each worker's deadline, and the supervisor and the fake-broker test as proposed below | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |

Each module issue also writes its own worker's entry point under `runtime`,
the code that picks that worker's adapters for a mode.

Equal risk weight sizes each forecast's basket so they all carry the same
risk. With one basket in slice 1, it reduces to sizing that basket, which
question 3 settles.

Two pieces belong to no single module.

1. `core`, with the shared types, every `Protocol`, and the storage contract
   every worker uses: the create-only write, the blob header, the decision
   result and its halt, the plan and the queue message.
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
| Pure steps write create-only blobs at decision keys, data's first write wins, a halt is a blob at the same kind of key, and the journal is the only numbered log | An event log per component, as a SQLite file or as numbered blobs, with or without Litestream | A deterministic step's format must be canonical, and the journal alone keeps sequence numbers | Ruled by the owner on 2026-10-29, after the SQLite and numbered-blob designs before it |
| A decision result per stage, listing its outputs or holding a halt, with execution and monitoring each split into two stages, and each stage's kind declared in the `runtime` table that the storage contract alone acts on | One result per component per decision, which put execution's account file behind its own send | Two more result keys a decision, and a canonical format for every deterministic stage | [#22](https://github.com/l3a0/ithildin-runtime/issues/22), as [design.md](design.md#processes-and-files) proposes |
| A backtest uses an in-memory store through the same port, which this doc takes as a memory-backed folder every worker process can reach | S3 for backtests, which would cost minutes of round trips | A second adapter, held to S3's rules by one contract test suite | Ruled by the owner on 2026-10-29, the folder this doc's reading of it |
| Each component keeps a queue, listens for events on it, and reads its inputs from files and writes its outputs to files. A message is sent to a component's queue whenever there is something new for it | File notifications through `watchdog`, or polling | A message has to reach every component with something new | Ruled by the owner on 2026-10-29 |
| Each queue is a named pipe in the VM's working directory, written without waiting. The storage contract sends each message after a decision result is written, to the readers one table in `runtime` declares, and again for the newest result when a worker starts, and a starting worker opens its pipe before it catches up | A socket, a queue server, or every write messaging every queue | A message is skipped when its reader is down or its pipe is full, so a starting worker catches up by checking its keys | [#22](https://github.com/l3a0/ithildin-runtime/issues/22), as [design.md](design.md#processes-and-files) proposes |
| Every write is created once with S3's refuse-if-it-exists condition and its SHA-256. A deterministic component treats a refused write with the same hash as done and a different hash as a bug that halts, and a capturing one, data or execution, treats the stored blob as the one that counts | Blobs that may be replaced, or one rule for both kinds of component | Each component has to know its kind, and the bucket's policy has to refuse writes without the condition | [#22](https://github.com/l3a0/ithildin-runtime/issues/22), as [design.md](design.md#processes-and-files) proposes |
| The VM keeps no state that matters, and every file is a blob in S3 | Durable state on the VM's disk | A round trip to S3 per blob | Ruled by the owner on 2026-10-29 |
| The journal is numbered blobs per account, each entry the body of its own blob, stored before the step it records, with S3's refusal of an existing number keeping two writers apart | A journal in SQLite on the VM | One S3 round trip before each order step | The entry as its own record ruled by the owner on 2026-10-29, the rest proposed for [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A deployment's lease and an account's lease, as blobs in S3 that record their holder and are deleted only with a condition naming their version. The kill switch and the hard caps are blobs beside them | A lock file, a switch file and a config file on the VM | A lease another VM left behind needs a person to clear it | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) for the deployment's lease and the switch, [#10](https://github.com/l3a0/ithildin-runtime/issues/10) for the account's lease |
| The runtime runs on a VM of its own and downloads the partitions its strategies need into a local mirror | Running on marketlake's VM beside the capture daemon | A second VM, and a read-only credential for marketlake's bucket | Ruled by the owner on 2026-10-29 |
| The skeleton's strategy decides at 15:40 on marketlake's segments read before sealing and sends market-on-close orders by 15:48, a cutoff for market-on-close orders in the paper account's hard caps that execution checks before every send and again before each broker call. A pair left with one leg accepted is cancelled, and each leg must fit `max_net` alone. One simulator fills by order type in backtest and shadow, a market order at the decision quote as [#16](https://github.com/l3a0/ithildin-runtime/issues/16) rules | Deciding in the evening and trading the next open, on the official daily bar or on marketlake's captured close | A signal twenty minutes before the close it trades, execution reads the clock for the cutoff, and a kill switch cannot stop a closing-auction fill after the exchange's cancel cutoff | Trading the close ruled by the owner on 2026-10-29, question 10. The times are proposed for [#10](https://github.com/l3a0/ithildin-runtime/issues/10) and [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| `max_net` is set from the venture's loss limit: one leg's stress move, from a recorded vintage over at least a weekend, sized to a share of the loss limit the owner sets, stored with its input's hash. Monitoring tallies stranded legs after each close, and the simulator strands legs on a seeded fault | A cap equal to the larger leg, which never refuses anything, or closing a stranded leg at the next open, after the overnight gap has landed | A smaller pair at live size, and a stress move to derive and pin | Ruled by the owner on 2026-10-29, on a board sitting's recommendation. [#7](https://github.com/l3a0/ithildin-runtime/issues/7) for the cap, [#11](https://github.com/l3a0/ithildin-runtime/issues/11) for the tally, [#10](https://github.com/l3a0/ithildin-runtime/issues/10) for the simulator |
| The release is one container image, pinned by digest, and each run is one unprivileged container holding the supervisor and its workers as today. CI tests the image on every pull request, and one job on `main` publishes it with a provenance attestation the owner verifies before pinning | A Python environment installed on the VM, or a container per component started by systemd or the Docker socket | A Docker daemon to maintain and patch outside sessions, an image build per pull request, and a verification step before each deploy | Raised by the owner on 2026-10-30. This doc's proposal for [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| The runtime's bucket, VM and broker key live in marketlake's AWS account. The bucket policy names who may reach the data, the broker key stays under AWS's managed `aws/ssm` key behind a grant to the execution user and refusals for broad readers, marketlake's plan role reads only what `tofu plan` needs, and marketlake's apply and deploy roles refuse the runtime's VM | A separate AWS account, which a board sitting recommended | The VM is guarded by refusals a CI check has to keep complete, and the account's root user can edit every policy | Ruled by the owner on 2026-10-29, question 11. marketlake's side is [l3a0/marketlake#944](https://github.com/l3a0/marketlake/issues/944) |
| The runtime reads marketlake's capture segments before the day is sealed, as a provisional read, with its own minimum checks and a comparison with the sealed day afterwards | Waiting for marketlake's upload at the close plus fifteen minutes, or sealing continuously in marketlake | Data at decision time has not passed marketlake's validation battery, and a difference after sealing has to be journaled and explained | Ruled by the owner on 2026-10-29. The read is proposed for [#5](https://github.com/l3a0/ithildin-runtime/issues/5) and [#16](https://github.com/l3a0/ithildin-runtime/issues/16), and the uploader on [l3a0/marketlake#943](https://github.com/l3a0/marketlake/issues/943) |
| A supervisor that writes the plan, starts every worker at once and keeps no state, with a halt that flows down the pipeline and is final for its decision | A supervisor that holds the run's state in memory, or one that launches each worker only once its inputs exist, which would handle a halt one way on paper and another in a backtest | Locks, leases and a plan that one process would not need. The owner ruled on 2026-10-29 that the owner or the operator clears a halt | [#11](https://github.com/l3a0/ithildin-runtime/issues/11), as [design.md](design.md#processes-and-files) proposes |
| `Journal` and the storage contract live in `core` | The journal writer in `execution`, which the supervisor and monitoring could not import | `core` holds behaviour beyond what its types need | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| Only the order path talks to the broker, for sends, lookups, fills, positions and cancels, inside one execution worker per broker account | Letting the supervisor or monitoring call the broker | One more method on `OrderPath` per kind of broker call | This doc |
| Monitoring reconciles positions and open orders between execution's recovery and the models, and journals and alarms every halt | Reconciling inside execution, or the supervisor raising alarms | One more output file, the broker's account, which also makes the reconciliation readable afterwards | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Execution journals a fill only when its client order id has a `sending` entry and the fill stays within that order's quantity, and halts otherwise | Journaling every fill the broker reports | A fill from an order placed by hand at the broker halts the platform | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| Fetch and sync before any step that changes state, and resolve conflicts before modifying. A state that moved on in a way the step can work from brings it up to date, and a conflict between two versions of the truth halts for a person | Acting on state fetched earlier, or resolving a conflict automatically | A round trip before each change, and a halt wherever the broker moved unasked | Fetching, syncing and resolving first ruled by the owner on 2026-10-29. Up to date against a halt for a person is this doc's application, in [design.md](design.md#processes-and-files) |
| Execution's send stage fetches the broker afresh, settles orders in doubt, journals new fills, and halts before planning on any difference that is not its own open order or fill. It then journals the whole target portfolio and its planned orders, each with its client order id, with the journal entry it synced through and a hash of the broker state, in one entry before its first send, reuses that list on a rerun in the same decision, journals each fill under the broker's fill id, and halts before planning if an entry that changes the book, and that neither its sync nor its order loop wrote, lies past the entry its sync names | Planning again on a rerun, ids from the order's place in a fresh plan, fills matched by order and quantity alone, or planning on the recover stage's read | A rerun cannot catch up on a changed target within a decision, and a broker change during the order loop waits for the next recover | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| A target portfolio names every instrument the book holds, with a flat leg as an explicit zero | Reading a missing instrument as zero | One more check, and a halt on a leg left out | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| The gate's hard caps come from a blob only the owner's credential can write, every field of both sources is checked before use, and the tighter of each pair applies | Trusting the risk model's constraints file alone | Two places to set a limit. The owner ruled on 2026-10-29 that only the owner changes the caps, by hand | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| The cost model writes a cost schedule that portfolio construction reads | Portfolio construction calling the cost model for each candidate trade, which would cross a process boundary per trade | One more type in `core`, and a change to the charter's module table | [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |
| The current book comes from the journal, and the broker is only reconciled against | Reading positions from the broker each run | A journal bug halts trading instead of being silently patched over by the broker's numbers | This doc |
| The blob store, which holds the journal, and the alert channel are adapters chosen per mode | Writing records and alarms the same way in every mode | Two more ports in `core`, and a change to design.md's `## One runtime` | This doc |
| A missing, stale or empty input raises `Halt` | Defaulting a gap to the last value or to zero | A day with a data hiccup is a day with no trading | This doc, with the staleness rule on [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| A forecast names a basket | A forecast per instrument, with the pair logic in portfolio construction | One more type in `core`, and a change to the charter's module table | [#6](https://github.com/l3a0/ithildin-runtime/issues/6) |
| The supervisor and the fake-broker test are built last, on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) | Building the supervisor with `core`, which cannot run a day before every worker exists, or a separate issue. Writing the fake-broker test with the import rule test in `core`'s sub-issue, which cannot run before the supervisor exists | No end-to-end run happens until [#11](https://github.com/l3a0/ithildin-runtime/issues/11) lands, and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) grows past its Monitoring row and takes on part of test 3 | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |
| Run the Bollinger band on GLD and USO for test 1 | Entry 21's linear rule on the same data, which is always in the market with continuous units, or the Kalman-filter hedge on EWA and EWC, which adds a model to verify first | The engine carries the strategy's quirks, such as no capital account and a daily resize of one leg | [#6](https://github.com/l3a0/ithildin-runtime/issues/6), which also sets [#5](https://github.com/l3a0/ithildin-runtime/issues/5)'s ETFs |
| The platform trades at any time of day and as often as a strategy needs. A decision is keyed by its time, a run in shadow, paper and live is one trading day of one deployment, and its plan lists the day's decisions, built by one function in `core` from the schedules and the trading calendar | One run per trading session after marketlake's evening sweep, filling at the next open | The workers run all trading day, every key carries a decision time, recover waits for data at every decision, and the fastest useful decision is about one a minute, marketlake's capture rate | Ruled by the owner on 2026-10-29. The shape is proposed for [#11](https://github.com/l3a0/ithildin-runtime/issues/11), and question 10 sets the skeleton's strategy at 15:40 |
| A strategy is a set of rows in the one table in `runtime`: its instruments, schedule, data needs, alpha model as a stage of its own, and configuration. An account carries one deployment in each mode, and strategies on one account share it and meet in portfolio construction | A runtime per strategy, or the strategy's logic spread through the shared modules | Every key carries the deployment and every forecast its strategy. How strategies combine and a halt that holds the account wait for the first strategy that needs them, written on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) | Open to other strategies ruled by the owner on 2026-10-29. The shape is this doc's proposal for [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| The risk gate counts every open order at its worst case, as filled on the side that grows exposure and unfilled on the other, until the broker confirms it | Counting open orders at their net | An order left resting can block a trade that would have fitted | [#7](https://github.com/l3a0/ithildin-runtime/issues/7), as [design.md](design.md#processes-and-files) proposes |
| marketlake is the one bar provider: every source is imported into it, and every mode reads one query | A bar store in this repository, or a reader per source | It reverses marketlake's rule that it holds no history from before capture, so every bar carries whether it was captured or imported. This repository also depends on marketlake's package at a pinned commit | [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) and [#5](https://github.com/l3a0/ithildin-runtime/issues/5), ruled by the owner on 2026-10-29 |
| Each run records the signals it computed | Recomputing them whenever two runs are compared | One more output file per decision, which holds nothing that cannot be regenerated | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
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
   and without it a switch pulled between 15:48 and the exchange's cancel
   cutoff stops nothing already sent.
   The owner accepted it on 2026-10-29, and the ruling is on
   [#11](https://github.com/l3a0/ithildin-runtime/issues/11).
8. **Does the daily loss stop also unwind positions?** The recommendation is
   no for slice 1. Crossing the stop makes the risk gate refuse every new
   order. An unwind is a trade of its own, so it would need its own path
   through the gate and its own tests, and
   [#7](https://github.com/l3a0/ithildin-runtime/issues/7) can add it later.
   The owner accepted it on 2026-10-29, and the ruling is on [#7](https://github.com/l3a0/ithildin-runtime/issues/7).
   A later ruling the same day, for a pair leg left filled alone at the close,
   kept the unwind out too, and set `max_net` from the venture's loss limit
   instead, as design.md's `### Strategies, schedules and decisions` rule 5
   says.
9. **Where are `core` and the import rule test written?** The
   recommendation is a new sub-issue of
   [#4](https://github.com/l3a0/ithildin-runtime/issues/4), built first, which
   every module issue waits on through a "blocked by" link. Every module
   imports `core`, so the dependency is real. The price is one step before any
   module starts. In return the shared shapes land once, reviewed on their own,
   rather than inside whichever module happens to go first. The owner accepted
   it on 2026-10-29, and the work is [#22](https://github.com/l3a0/ithildin-runtime/issues/22).
10. **When does the skeleton's strategy decide, and which close does it
    read?** The owner ruled on 2026-10-29 that it trades the close: it decides
    before the close on marketlake's segments read before sealing, and sends
    market-on-close orders, so the fill lands on the close the backtest fills
    at. Two evening choices were turned down: deciding on the official daily
    bar, which arrives a session later and fills two nights after the signal,
    and deciding on marketlake's captured 16:15 close, which fills at the next
    open, one night after the signal.

    This doc proposes the times. Alpaca's order documentation says
    market-on-close orders "submitted after 3:50pm but before 7:00pm ET will
    be rejected", so the decision runs at 15:40 and every market-on-close
    order goes out by 15:48, a cutoff the paper account's hard caps hold.
    Execution checks it before every send and again just before each broker
    call. Alpaca's order page does not state a cutoff for an early-close
    day, so on those days the plan drops the 15:40 decision, which falls
    after the 13:00 close, and the strategy does not trade. Alpaca's paper
    account accepts market-on-close orders, but its
    [paper trading page](https://docs.alpaca.markets/docs/paper-trading)
    says it fills "based on the real-time quotes" and gives "partial fills for
    a random size 10% of the time", and Alpaca staff on its forum say paper
    does not tell a market-on-close order from a market order. So paper fills
    measure the quote near the close rather than the closing auction's
    price, and the random partial fills exercise the path that keeps a pair
    whole. The price
    is a signal computed twenty minutes before the close it trades, a pair
    whose second leg can be rejected after the first is accepted, which
    execution then cancels while the exchange still takes cancels, and a kill
    switch that cannot stop a closing-auction fill after the exchange's
    cancel cutoff. A missing input on a day halts that decision rather than
    switching to another time.
11. **Which AWS account holds the runtime's bucket and VM?** The owner ruled
    on 2026-10-29 for the second choice, marketlake's account with the four
    controls, against the board's recommendation of the first. Rule 3 and
    rule 5 now carry it. Rule 3 had assumed marketlake's account, where the
    owner ruled earlier that day that the broker key lives. Three facts about that account matter. marketlake's plan role
    holds AWS's `ReadOnlyAccess`, which reads every bucket and every parameter
    in the account, and runs on pull requests of a public repository, which
    [l3a0/marketlake#944](https://github.com/l3a0/marketlake/issues/944)
    narrows to what `tofu plan` reads whichever choice the owner makes. Its
    apply role holds `ec2:*`, so an approved apply can reach any VM in the
    account as root. And a refusal added to one role protects nothing from a
    role added later. Two choices follow.
    1. **A separate AWS account for the bucket, the VM and the broker key.**
       It refuses every marketlake role by default, the VM included, and
       reads the lake through one read-only grant on marketlake's bucket. A
       member account in AWS Organizations costs nothing, as the
       [Organizations pricing page](https://docs.aws.amazon.com/organizations/latest/userguide/pricing.html)
       says. The price is a second root login and recovery email to guard,
       the setup hours, and moving the key out of marketlake's account, which
       reverses part of the owner's ruling. The key cannot stay in
       marketlake's account while the runtime runs elsewhere, because a
       parameter encrypted under AWS's managed key cannot be shared across
       accounts, as AWS's
       [shared parameters page](https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-shared-parameters.html)
       says.
    2. **marketlake's account, with the data behind lists of who may reach
       it.** Four controls do the work.
       1. The narrowed plan role reads no parameter and no object outside
          marketlake's state bucket, so a pull request cannot print the
          runtime's data, and a grant added by mistake later still meets
          the next two controls.
       2. The runtime's bucket policy refuses every S3 action to any
          principal that is not one of the runtime's roles or the owner's,
          matched on `aws:PrincipalArn`, and refuses changes to the policy
          itself to all but the owner. An explicit refusal in a bucket policy
          overrides any role's own grant, as AWS's
          [policy evaluation logic](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html)
          says, so a role marketlake adds later is refused without anyone
          remembering it.
       3. The broker key's parameter is encrypted under a KMS key of the
          runtime's own, whose policy names only the execution user's
          principal and the owner's, and leaves out the statement that lets
          IAM policies grant access. Without that statement, "IAM policies
          that allow access to the key are ineffective", as AWS's
          [default key policy page](https://docs.aws.amazon.com/kms/latest/developerguide/key-policy-default.html)
          says. The key costs $1 a month. The owner later ruled AWS's
          managed key fine instead, so the parameter rests on IAM grants and
          refusals, as rule 3 says.
       4. The VM cannot be listed that way, so marketlake's apply and deploy
          roles carry explicit refusals of EC2 actions on instances tagged as
          the runtime's, and of any change to that tag. That part is still a
          list of refusals, and a check in marketlake's CI fails any role
          that lacks it.

       The price is that the VM stays behind a list someone has to keep
       right, and that the account's root user can still edit every one of
       these policies. Only a service control policy binds a root user, and
       it does so only in a member account of an organization, as the
       [service control policies page](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html)
       says.

    A board sitting on 2026-10-29 recommended the first, with one condition:
    no role in the runtime's account can be assumed from a pull request on
    either public repository, since ithildin-runtime is public too. It
    weighed the separate account's setup hours against the second choice's
    list of refusals that has to be kept right as marketlake grows, and
    judged the journal, which cannot be recovered, worth the hours. When the
    setup starts, now or after the candidate ranking due 2026-11-11, is the
    owner's call.
12. **What hosts the runtime: a VM, or a long-running Fargate service?** The
    owner ruled on 2026-10-30 that the platform runs through every session
    and trades continuously, at decisions as often as once a minute, so the
    host keeps the supervisor and the workers running from before the open
    until after the close. Lambda per component is turned down: about 390
    decisions a day, each fanning out to every worker, would move the
    supervisor's work into a second orchestrator such as Step Functions,
    while a backtest keeps the local supervisor, so the two would orchestrate
    differently. Lambda's
    [15-minute limit](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)
    also rules out a worker that lasts the session. Two choices remain.
    1. **The VM, as `## Where it runs` draws it.** The workers run as Unix
       users of their own, the queues are named pipes, and only the
       execution user's credential reads the broker key. The price is a
       machine to patch and restart, the firewall rule that keeps worker
       users off the instance metadata service, and the refusals that keep
       marketlake's apply and deploy roles off the VM, as rule 3 says.
    2. **A long-running ECS Fargate service**, started before the open and
       stopped after the close, with the same supervisor and workers in a
       container. There is no machine to patch, and marketlake's EC2
       permissions have no VM to reach. But ECS hands a task's IAM role to
       every container in the task, and its
       [task role page](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)
       says "containers are not a security boundary", while each Fargate
       task has its own. So execution moves into a task of its own, with the
       only role that reads the broker key. Three things change with it: the
       queue from portfolio construction to execution crosses tasks, through
       a queue service such as SQS rather than a named pipe; the supervisor
       starts execution's task rather than a process; and the kill switch
       command reaches the run through ECS rather than a shell on the VM.

    The recommendation is the VM for slice 1. Its design is complete and has
    been reviewed, and the refusals that guard it are already ruled. Moving
    to Fargate changes the queue into execution, how the supervisor starts
    it, and the kill switch's route, each of which would need its own review
    and drills, on a path that has run zero times. The case for Fargate is
    real, no machine and no reach from marketlake's EC2 roles, and the
    evidence that would settle it is what running the VM costs: hours spent
    patching and restarting it, and any marketlake apply that touches it.
