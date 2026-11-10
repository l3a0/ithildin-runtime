# Architecture, section 10: Quality requirements

This page is section 10 of [the architecture](00-README.md), in the order of the arc42 template. It answers how the four acceptance tests exercise the design.

## Contents

- [How the four acceptance tests exercise it](#how-the-four-acceptance-tests-exercise-it)
  - [How CI runs them](#how-ci-runs-them)

## How the four acceptance tests exercise it

**Table:** What each acceptance test runs

| Test, from [#4](https://github.com/l3a0/ithildin-runtime/issues/4) | Mode | What it runs | What it pins |
| --- | --- | --- | --- |
| 1. The backtest reproduces a replicated strategy | Backtest | marketlake reader over the imported vintages, history clock, simulated broker filling market-on-close orders at the close, zero cost | APR 0.17824928 and Sharpe 0.96467289 to 5e-9, the 162 changes of unit, and the unit on each of the 1,480 days as [`chan.bollinger`](https://github.com/l3a0/quantitative-trading/blob/19aadbeb9730bf34f724af5688669b6bc208d136/src/chan/bollinger.py) computes it at `19aadbeb` |
| 2. A replay emits pinned orders, through a missing feed, a stale price, a restart in the wrong order and an ack withheld across two decisions | Backtest | A recorded stretch of sessions with four faults injected. The restart kills a worker mid-run, and the host brings execution up before data. The simulated broker withholds one order's ack across two decisions. A second run sets every cause to hold in the hard caps blob, as live sets it, and adds a halt of a cause the set does not name, once with `release_needs_blob` set and once absent | The orders on each day, the expected orders and halts for each fault, a halt with no orders on each faulted day, and the second decision's order on the withheld order's instrument refused. In the second run, every decision after the first halt halts until it is released, by the release test in [Halts and drills](../halts-and-drills.md#how-a-sort-runs), and the unnamed cause holds in both cases |
| 3. The risk gate refuses an order over each limit, and nothing bypasses it | Unit and import | `RiskGate` against an order one step below, at and above each of the seven limits, including a market-on-close order past the cutoff, and a pair whose second leg alone breaks `max_net`, an order on an instrument with an order in doubt, an order that adds to a position whose worst loss cannot be computed, a replacement whose old order fills during its cancel, the import rule test, and the fake broker | Seven refusals, each naming its limit, with [#7](https://github.com/l3a0/ithildin-runtime/issues/7) stating whether the limit itself is allowed, the pair's, the refusal beside an order in doubt, the refusal when a worst loss cannot be computed, a replacement cut by a fill that raced its cancel, no import of `execution.brokers` outside `runtime`, no order reaching the fake broker without a journaled approval and `sending` entry, or twice, and no cancel reaching it before its `cancel` entry is stored |
| 4. The kill switch and the dead-man check fire in a drill | Paper, drill | Set the switch before a run, pull it five minutes after a decision with a platform order and an order the platform did not send both resting, and skip the second ping of a day of two decisions | A broker read just before the pull that lists at least one stamped and one unstamped order, or the test fails, a halt journaled with reason "kill switch", a journaled cancel for each resting order and a "cancels done" entry, and a dead-man alarm delivered. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds how an order is kept resting |

Every test that asserts no order left pairs with an identical run that sends one, and asserts which check stopped it.

Test 1 fills at the close of the day the signal used, because that is what
Chan's script assumes. The paper run
trades that close too, with market-on-close orders, so both fill at the same
price. The two still decide at different times. Test 1's deployment decides
at the close, on the imported daily bars, through a simulated account whose
cutoff is the close, because a close−20 decision there would see only the day
before's close and shift every signal a session. The paper deployment
decides at close−20, because no live decision can read a close that has not
happened. Test 1 checks the engine against Chan's timing. A backtest of the
deployed close−20 schedule needs minute history at that minute that the imported
vintages lack, so it waits for marketlake's captured minutes to cover a long
enough span, and shadow running measures the gap until then. A paper order also needs whole shares, or a fractional order the
broker accepts, while the backtest holds fractional dollar positions. So the
two runs differ in two ways.

1. **Timing of the signal.** The paper decision computes its signal on the
   price at close−20, twenty minutes before the close the backtest's signal
   uses, and a price that moves in those minutes can flip the unit.
2. **Rounding.** Rounding each leg to whole shares moves it by at most half a
   share. Whether portfolio construction or the order manager rounds, and
   whether the broker allows a fractional short sale for the GLD leg, is open
   on [#9](https://github.com/l3a0/ithildin-runtime/issues/9).

Shadow running measures both gaps rather than leaving them to argument.

The charter's paper gate also asks for every alarm to fire in a drill, which
covers more than test 4 names. The reconciliation mismatch in [The book and the broker disagree](06-runtime.md#the-book-and-the-broker-disagree) is one such
alarm, and its drill is the row Book and broker disagree in
[halts-and-drills.md](../halts-and-drills.md#alarms-on-the-order-path), which [#11](https://github.com/l3a0/ithildin-runtime/issues/11) builds.

Which issue writes each piece. Where a piece is a shared type, it lives in
`core` whichever issue writes it.

**Table:** Which issue writes each piece

| Piece | Written on |
| --- | --- |
| Importing every source into marketlake, provenance on each bar, the one bar query | [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938), closed 2026-11-04 |
| `core`'s shared types and every `Protocol`, the file form of each type, `Journal`, the storage contract of `BlobStore`, create-only writes, the blob header, the decision result, the plan, the queue port with its SQS and named-pipe adapters and their contract suite, the S3 and memory-backed folder adapters with one contract test suite, the import rule test | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| `Bar`, `Snapshot`, the marketlake reader, the snapshot and signal records, security master | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Bollinger band alpha model, `Basket`, `Forecast` | [#6](https://github.com/l3a0/ithildin-runtime/issues/6) |
| `Constraints`, `RiskGate`, the daily loss stop, the stale-data rule | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| Flat and linear cost models, `CostSchedule` | [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |
| Equal-risk-weight portfolio construction, `TargetPortfolio` naming every instrument | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| Order manager, `OrderPath`, a plan's cancels and replacements, the journal's entries for orders and fills, fills keyed by the broker's fill id, the broker id mapped on each ack, the simulator and the Alpaca adapter around its SDK, the simulated account's blob, the account's lease | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| `Monitoring`, reconciliation, reports, alert channel adapter, dead-man check, kill switch, startup order, the alarms and their drills, each worker's deadline, and the supervisor and the fake-broker test as proposed below | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |

Each module issue also writes its own worker's entry point under `runtime`,
the code that picks that worker's adapters for a mode.

Equal risk weight sizes each forecast's basket so they all carry the same
risk. With one basket in slice 1, it reduces to sizing that basket, so the
gross position is a fixed dollar amount the owner sets, recorded in the run's configuration.

Two pieces belong to no single module.

1. `core`, with the shared types, every `Protocol`, and the storage contract
   every worker uses: the create-only write, the blob header, the decision
   result and its halt, the plan and the queue message.
2. The import rule test, which reads every module's imports.

Both are written on [#22](https://github.com/l3a0/ithildin-runtime/issues/22), which every module issue waits on.

The supervisor imports no module, but it cannot run a day until every worker
exists, so it is built last. This doc proposes building it on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11), which owns the startup order and waits on every
other module. The fake-broker test goes there too, since it runs a whole day
under the supervisor. [#11](https://github.com/l3a0/ithildin-runtime/issues/11)'s body names neither today, and its acceptance
covers tests 2 and 4 but not test 3, so [Proposals awaiting a ruling](09-decisions.md#proposals-awaiting-a-ruling) lists both as a
proposal.

### How CI runs them

CI has to test the components as they run on the VM: separate containers
learning of each other's writes from the store's events and reading their inputs from
a store that refuses an existing key. Testing the modules in one process
would not exercise the messages, the restarts or the store's rules. GitHub's hosted
runners are Linux VMs with Docker, so CI runs the real image. This is this
doc's proposal for the CI that [#22](https://github.com/l3a0/ithildin-runtime/issues/22)
and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) build, in
three levels, from fast to slow.

1. **Module tests, in one process.** Each module's logic, the risk gate's
   seven refusals, its refusal beside an order in doubt and its refusal when a worst loss cannot be computed, the import rule and the journal's replay run as ordinary
   tests, as the existing suite does today.
2. **Contract tests for the three swappable pieces.** Each runs every adapter
   against the same rules.
   - **The store suite** holds the list design.md's
     `### A worker works through a span of decisions` gives, which stays the
     one statement of it, including two processes racing one create, a read
     during a write and a writer killed partway through. A store that checks
     for a key and then writes passes a test of one refused write and fails a
     racing create, and one that creates the key and then writes into it
     shows a reader half a blob. The suite
     runs against the memory-backed folder and against the S3 adapter, the
     second through a local S3 emulator that honours the refuse-if-it-exists
     condition, which the first build confirms, and against the test environment's bucket on each push to `main`.
   - **The queue suite** holds the SQS adapter and the named-pipe adapter
     to what both guarantee. A write reaches the queue of every reader its
     routes name and no other, and the suite loads the generated rules into
     the emulator rather than routing by hand. An event can arrive twice,
     late, out of order or not at all, and a reader still handles each
     decision once, in the plan's order, by checking its keys when it wakes
     and when it starts. Dropping events while a reader is down, by purging
     its queue in the emulator and by the folder adapter's seeded fault,
     shows the recovery CI runs is the one live runs. A writer killed between
     its write and the folder adapter's delivery announces again when it
     restarts. The SQS adapter runs through a local emulator that sends S3's
     events to EventBridge and SQS, named and pinned by digest when
     [#22](https://github.com/l3a0/ithildin-runtime/issues/22) picks it, and
     against the test environment's bucket and queues on each push to
     `main`. The queue port's `done` deletes a message, `extend` lengthens
     its visibility timeout, and `release` returns it at once with
     visibility 0. The named-pipe adapter implements `extend` and `release`
     as no-ops, because a pipe hands each message to one reader once and
     keeps nothing to hide or return. So the suite runs the receive loop
     over both adapters only for what the pipe guarantees: a message whose
     item is already in a handler is not run twice, the loop never holds
     more messages than it has free handlers, and a hung handler stops the
     heartbeat. The loop's extend, delete and return path, a message whose
     handler is killed returning to the queue included, runs against the
     SQS emulator.
   - **The broker suite** holds every `Broker` adapter to the methods
     `OrderPath` calls: a submit with a client order id where the broker keeps one, a find of an order from its intent that answers the broker's id, not found or more than one, whether its list was whole and each paired row as a `BrokerOrder`, a list of every order since a time across all its pages, a lookup by the broker's order id that returns a `BrokerOrder`, with the client order id where the broker keeps one, a cancel by the broker's order id, fills
     with their own ids, positions, open orders, the read [#10](https://github.com/l3a0/ithildin-runtime/issues/10) names for cash, the broker's records through `activities`, the clock, a
     market-on-close order the broker refuses past its own cutoff, and a 429, the broker's answer for too many requests, on a cancel. The cutoff case runs outside a session, and like every case it sends only through `OrderPath`, so no case sends past the hard caps' cutoff. How it reaches the broker's refusal is [#10](https://github.com/l3a0/ithildin-runtime/issues/10)'s to settle, without loosening the cutoff, without a flag on `OrderPath` and without a separate hard caps file for the suite. It runs as [The code
     holds its own rules](../design.md#the-code-holds-its-own-rules-and-every-check-runs-in-production) says: against the
     simulator and recorded responses in CI on every pull request, and against Alpaca's paper account on a
     schedule. No case calls the broker outside `OrderPath`. An adapter that cannot honour a method fails the suite, and the
     gap is closed inside that adapter, never by a broker's branch in
     `OrderPath`.
     The early-close cutoff is confirmed apart from the suite, by a test order on Alpaca's paper account on an early-close day, sent through `OrderPath`, as the row on close−12 in [Proposals awaiting a ruling](09-decisions.md#proposals-awaiting-a-ruling) says.
3. **The runtime in containers.** CI builds the image once, and the runner's own systemd starts every component's container and the supervisor's from their unit files, with the host's watchdog, as the second required check below says. Each starts with the user and flags its unit gives it, and a test pins the restart policy in the unit files. Every container mounts the queue folder and one memory-backed folder created on the runner standing in for S3, so a blob
   written before every container stops is still there after. Tests 1 and 2
   and test 3's fake-broker run go through this level, with the simulated
   broker and the history clock, and so do the drills that crash, freeze or
   restart a worker, among them the crash between a pair's legs. Tests show that a write in one container reaches the queue of every container the table lists as its reader, and no other's. This level runs two instances of each component and of the supervisor, as slice 1's deployment does, and runs test 2's replay without its kill, once as a deployment does and once with one instance of each, expecting the same journal and the same blobs byte for byte, apart from what design.md's `### How a component learns of a change` leaves out.

On a run with no credential, tests 1 and 2 read [the vintages](https://github.com/l3a0/quantitative-trading/blob/19aadbeb9730bf34f724af5688669b6bc208d136/data/vintages.jsonl) quantitative-trading commits, at a pinned commit and against their committed checksums, as [#5](https://github.com/l3a0/ithildin-runtime/issues/5) holds. An absent input fails the check.

The required `test` check runs the first two levels. A second required check, which `.github/rulesets/default.json` gains when [#22](https://github.com/l3a0/ithildin-runtime/issues/22) builds it, runs the third on the same
image, test 2 and the restart drills included, because the restart path is
what this design rests on. That check also runs the real unit files and the
host's watchdog under the runner's own systemd, which hosted runners appear
to allow with `sudo`, as the first build confirms. A local backtest has no
restart behaviour, so no test host that reimplements systemd exists.

What CI cannot cover runs in each environment that has the resource, as [When drills run](07-deployment.md#rule-10-when-drills-run) says, as drills on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11): the instance
metadata fence against AWS's own service, S3's bucket policy and CloudTrail,
and the account's real roles. The memory-backed folder and the emulator only
imitate S3's rules, which the real test bucket checks.

The price is a CI run that builds an image and starts several containers on
every pull request, a second required check, an S3 emulator and a test
bucket to keep, and drills that only the VM can run.
