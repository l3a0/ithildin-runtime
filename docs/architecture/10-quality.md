# Architecture, section 10: Quality requirements

This page is section 10 of [the architecture](00-README.md), in the order of the arc42 template. It answers how the four acceptance tests exercise the design.

## Contents

- [How the four acceptance tests exercise it](#how-the-four-acceptance-tests-exercise-it)
  - [How CI runs them](#how-ci-runs-them)

## How the four acceptance tests exercise it

**Table:** What each acceptance test runs

| Test, from [#4](https://github.com/l3a0/ithildin-runtime/issues/4) | Mode | What it runs | What it pins |
| --- | --- | --- | --- |
| 1. The backtest reproduces a replicated strategy | Backtest | marketlake reader over the imported vintages, history clock, simulated broker filling market-on-close orders at the close, zero cost | APR 0.17824928 and Sharpe 0.96467289 to 5e-9, the 162 changes of unit, and the unit at each of its 1,461 decisions, from 2006-06-21, as [`chan.bollinger`](https://github.com/l3a0/quantitative-trading/blob/19aadbeb9730bf34f724af5688669b6bc208d136/src/chan/bollinger.py) computes it at `19aadbeb`. The comparison puts the replication's first 19 rows, which hold nothing, in front |
| 2. A replay emits pinned orders, through a missing feed, a stale price, a restart in the wrong order and an ack withheld across two decisions | Backtest | A recorded stretch of sessions with four faults injected. The restart kills a worker mid-run, and the host brings execution up before data. The simulated broker withholds one order's ack across two decisions. A second run sets every cause to hold in the hard caps blob, as live sets it, and adds a halt of a cause the set does not name, once with `release_needs_blob` set and once absent | The orders on each day, the expected orders and halts for each fault, a halt with no orders on each faulted day, and the second decision's order on the withheld order's instrument refused. In the second run, every decision after the first halt halts until it is released, by the release test in [Halts and drills](../halts-and-drills.md#how-a-sort-runs), and the unnamed cause holds in both cases |
| 3. The risk gate refuses an order over each limit, and nothing bypasses it | Unit and import | `RiskGate` against an order one step below, at and above each of the seven limits, including a market-on-close order past the cutoff, and a pair whose second leg alone breaks `max_net`, an order on an instrument with an order in doubt, an order that names no strategy, an order that only reduces while a limit is past, a replacement whose old order fills during its cancel, the import rule test, and the fake broker | Seven refusals, each naming its limit, its figure and its bound, where a figure at the limit passes, the pair's, the refusal beside an order in doubt, the refusal for want of a position to count the worst loss against, the reducing order approved and its verdict naming each limit that is past, a replacement cut by a fill that raced its cancel, no import of `execution.brokers` outside `runtime`, no order reaching the fake broker without a journaled approval and `sending` entry, twice, or other than as planned, and no cancel reaching it before its `cancel` entry is stored |
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
enough span, and shadow running measures the gap until then. A paper order also
needs whole shares, while test 1's account holds billionths of a share. So the
two runs differ in two ways.

1. **Timing of the signal.** The paper decision computes its signal on the
   price at close−20, twenty minutes before the close the backtest's signal
   uses, and a price that moves in those minutes can flip the unit.
2. **Rounding.** Portfolio construction rounds each leg toward zero to the
   account's share step, so a whole-share leg is off by less than one share.
   Whole shares miss the replication's pins, by 1.56e-3 on the APR and 4.99e-3
   on the Sharpe ratio at a G of $10,000, which is exploratory. So test 1's
   account alone has a step of a billionth, as
   [The step a share count moves in](05-building-blocks.md#the-step-a-share-count-moves-in) says.

Shadow running measures both gaps rather than leaving them to argument.

The charter's paper gate also asks for every alarm to fire in a drill, which
covers more than test 4 names. The reconciliation mismatch in [The book and the broker disagree](06-runtime.md#the-book-and-the-broker-disagree) is one such
alarm, and its drill is the row Book and broker disagree in
[halts-and-drills.md](../halts-and-drills.md#alarms-on-the-order-path), which [#11](https://github.com/l3a0/ithildin-runtime/issues/11) builds.

Which issue writes each piece. Where a piece is a shared type, it lives in
`core` whichever issue writes it, and `Journal` lives in the `journal`
package.

**Table:** Which issue writes each piece

| Piece | Written on |
| --- | --- |
| Importing every source into marketlake, provenance on each bar, the one bar query | [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938), closed 2026-11-04 |
| `core`'s shared types and every `Protocol`, the blob header, the decision result and the plan as types, the queue port, the file form of each type through the one codec, the key builders, the calendar function and the plan builder, the import rule test | [#22](https://github.com/l3a0/ithildin-runtime/issues/22) |
| The storage contract of `BlobStore`, create-only writes, the decision result and its halt, the S3 and memory-backed folder adapters with one contract test suite, a capture held through an S3 outage | [#79](https://github.com/l3a0/ithildin-runtime/issues/79) |
| The journal's append, the hash chain and the reader's checks, in the `journal` package | [#80](https://github.com/l3a0/ithildin-runtime/issues/80) |
| What a journal entry holds, an order's states and the book the entries replay to: `Journal.view`, `Journal.record`, `Journal.in_doubt` and `Journal.replay`, the platform's stamp, fills keyed by the broker's fill id and the broker's id mapped on each ack | [#142](https://github.com/l3a0/ithildin-runtime/issues/142) |
| The queue's SQS and named-pipe adapters and their contract suite, the routes generated from the table in `runtime`, the receive loop | [#81](https://github.com/l3a0/ithildin-runtime/issues/81) |
| The test environment's bucket, queues and role, which the store and queue suites run against on each push to `main` | [#78](https://github.com/l3a0/ithildin-runtime/issues/78) |
| The fields `Bar` gains for each price's kind, venues and delay, with [#50](https://github.com/l3a0/ithildin-runtime/issues/50), any field [#5](https://github.com/l3a0/ithildin-runtime/issues/5) adds to `Snapshot`, the read-only bucket port and its two adapters, the mirror, the point-in-time read of sealed daily bars, the snapshot and data's stage, the security master and the price-kind contract | [#5](https://github.com/l3a0/ithildin-runtime/issues/5) |
| Reading the day before marketlake seals it, and the fields `Quote` gains | [#100](https://github.com/l3a0/ithildin-runtime/issues/100) |
| The marketlake pin with its reader of an unsealed day, the pyarrow pin, the contract tests of both and the check that the pin differs | [#131](https://github.com/l3a0/ithildin-runtime/issues/131) |
| Bollinger band alpha model, the signal records, `Signal` and `ForecastSet`, the checks `Basket` and `Forecast` make, the form of each strategy's alpha section | [#6](https://github.com/l3a0/ithildin-runtime/issues/6) |
| `Constraints`' share caps by instrument, `HardCaps`, the caps blob's reader and the tightening of both, `Verdict`, `LossReading` and the contradictions they reject, the mark, the exposure and the valuation, which `core` holds once for every reader, the risk gate that implements `RiskGate`, the loss reading, the stale-data rule | [#7](https://github.com/l3a0/ithildin-runtime/issues/7) |
| The risk model `LimitRisk`, the `[risk]` section of a strategy's file, and the risk stage, which writes each decision's `Constraints`, with the steps `core` gives every model's stage | [#115](https://github.com/l3a0/ithildin-runtime/issues/115) |
| The form of each strategy's data section, and data's settings built from it | [#110](https://github.com/l3a0/ithildin-runtime/issues/110) |
| The stress move's calculation, `Verdict.stress_move`, the command that prints a record, the records' file and reader, and the hold of the caps to them by the gate, the risk model and portfolio construction | [#116](https://github.com/l3a0/ithildin-runtime/issues/116) |
| The gate's reading of a close after a broker's trade | [#117](https://github.com/l3a0/ithildin-runtime/issues/117) |
| The flat and linear cost model, `CostSchedule` and `CostTerm`, the form of each strategy's cost section | [#8](https://github.com/l3a0/ithildin-runtime/issues/8) |
| Equal-risk-weight portfolio construction and its stage, the `[portfolio]` section of a strategy's file, `Sizing`, the record of how a target was sized, `share_step` in the hard caps, and a stage's reads of a file of several entries and of an input's configuration hashes | [#9](https://github.com/l3a0/ithildin-runtime/issues/9) |
| `OrderPath` and `Account`, the controls read before a broker call, what each broker call answers, the fake broker and the sweep that nothing reaches a broker around the path | [#143](https://github.com/l3a0/ithildin-runtime/issues/143) |
| Order manager, a plan's cancels and replacements, the simulator and the Alpaca adapter around its SDK, the simulated account's blob, the account's lease | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) |
| `Monitoring`, reconciliation, reports, alert channel adapter, dead-man check, kill switch, startup order, the alarms and their drills, each worker's deadline, the third level of CI and its second required check, the journal entry for a capture lost when a worker's process dies, and the supervisor and the fake-broker test as proposed below | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) |

Each module issue also writes its own worker's entry point under `runtime`,
the code that picks that worker's adapters for a mode.

Equal risk weight sizes each forecast's basket so they all carry the same
risk. With one basket in slice 1, it reduces to sizing that basket, so its
gross is G, a fixed dollar amount the owner sets as `gross` in the `[portfolio]`
section of the strategy's tracked file. A limit can make the position smaller, as
[How a target is sized](05-building-blocks.md#how-a-target-is-sized) says.

Five pieces belong to no single module. Each is written on a sub-issue of
[#4](https://github.com/l3a0/ithildin-runtime/issues/4).

1. `core`, with the shared types and every `Protocol`, on [#22](https://github.com/l3a0/ithildin-runtime/issues/22).
2. The import rule test, which reads every module's imports, on [#22](https://github.com/l3a0/ithildin-runtime/issues/22).
3. The storage contract every worker uses, in `core`: the create-only write
   and the decision result and its halt, with the S3 and memory-backed folder
   adapters, on [#79](https://github.com/l3a0/ithildin-runtime/issues/79).
4. `Journal`, in the `journal` package, on [#80](https://github.com/l3a0/ithildin-runtime/issues/80).
5. The queue's adapters and the receive loop, on [#81](https://github.com/l3a0/ithildin-runtime/issues/81).

Every module issue waits on [#22](https://github.com/l3a0/ithildin-runtime/issues/22), and each worker's entry point also
waits on [#81](https://github.com/l3a0/ithildin-runtime/issues/81).

The supervisor imports no worker module, but it cannot run a day until every worker
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
runners are Linux VMs with Docker, so CI runs the real image, in three
levels, from fast to slow. [#79](https://github.com/l3a0/ithildin-runtime/issues/79) and [#81](https://github.com/l3a0/ithildin-runtime/issues/81) build the store and queue
suites of the second level, [#78](https://github.com/l3a0/ithildin-runtime/issues/78) the test bucket and queues they run
against, and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) the third level.

1. **Module tests, in one process.** Each module's logic, the risk gate's
   seven refusals, its refusal beside an order in doubt and its refusal of an order that names no strategy, the import rule and the journal's replay run as ordinary
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
     condition, and against the test environment's bucket on each push to `main`.
     The emulator is moto's S3 server, started inside the test run, so the
     suite needs no Docker and runs on every local `uv run pytest`. `uv.lock`
     pins moto's wheel by its SHA-256. A probe of moto 5.2.5 found it refuses
     a create on an existing key and a rewrite on a stale tag, and lets one of
     40 racing creates through. A second probe, with 8 processes rewriting one
     lease on one tag for 20 rounds, let two writers win in 4 of the 20. moto
     checks no SHA-256 a write sends and never answers 409. So the suite marks
     the SHA-256 case as failing on moto and skips its racing rewrites there,
     and botocore's stubbed answers show the S3 adapter's handling of S3's own
     checksum refusal and its 409.
   - **The store suite on the test bucket** runs in ci.yml's `aws` job, with
     the test role, on each push to `main`. Every key a case writes sits under
     `runs/ci/<run>/store/<case>/` or `leases/plans/ci/<run>-<case>-<n>.json`,
     which the role may create and read, and the suite refuses a key that a
     test route would hear. The bucket's client ignores any endpoint the
     environment sets and refuses a host outside AWS, so a green run cannot
     come from an emulator. The bucket checks the SHA-256, so that case runs
     there as an ordinary case. Processes racing rewrites of one lease on one
     tag can draw a 409 from S3, and the case checks that a 409 left nothing
     behind. A run that drew none ends as an expected failure naming each
     round's outcomes. The killed writer signs its PUT and stops partway
     through the body once S3 has answered `100 Continue`. The cases that need
     a folder run on the folder alone, and the half of one case that deletes
     a lease runs on the folder and moto. The job's `-v` names each case it
     ran with `[bucket]`.
   - **The queue suite** holds the SQS adapter and the named-pipe adapter
     to what both guarantee. A write reaches the queue of every reader its
     routes name and no other, and the suite loads the generated rules into
     the emulator rather than routing by hand. An event can arrive twice,
     late, out of order or not at all, and a reader still handles each
     decision once, in the plan's order, by checking its keys when it wakes
     and when it starts. Dropping events while a reader is down, by taking
     them off its queue on SQS and by the folder adapter's seeded fault,
     shows the recovery CI runs is the one live runs. A writer killed between
     its write and the folder adapter's delivery announces again when it
     restarts. The SQS adapter runs through moto's server, the same emulator
     the store suite starts inside the test run, which sends S3's events to
     EventBridge and on to SQS. A probe of moto 5.2.5 found it matches a
     `wildcard` pattern on the event's key, delivers only to the rule's own
     queue and honours a visibility of 0, so the suite needs no Docker, and
     `uv.lock` pins moto by its wheel's SHA-256. Events repeated, delayed,
     reordered and dropped at random come from the folder adapter's seeded
     fault, so that case runs over the folder. On moto the suite repeats an
     event while its decision is in a handler, and receives and deletes a
     reader's events while it is down. The routes file for the test
     environment's queues, `infra/test/routes.json`, is generated from the
     suite's own table by `tests/support/routes_file.py`, and a test fails
     when the committed copy differs from what it writes. The queue port's `done` deletes a message, `extend` lengthens
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
   - **The queue suite on the test queues** runs in ci.yml's `aws` job, with
     the test role, on each push to `main`, and checks what moto only
     imitates: S3's own delivery through EventBridge to queues encrypted
     with SSE-SQS, a reader that several rules feed, the real visibility
     timeout, and the real delay from a write to its message. Every case
     that both queues guarantee runs there, and so do the SQS cases, with
     four differences.
     1. Every case shares the test bucket and queues. So each case's keys
        sit under a run date built from the workflow's run number and
        attempt, in an hour of the case's own, and every read of a queue
        keeps the case's keys and deletes any other message, which an
        earlier case left.
     2. The case for events dropped while a reader is down receives and
        deletes them, on moto too, rather than purging. SQS allows one purge
        of a queue a minute, and the job's own purge has just spent it.
     3. The case that repeats an event while its decision is in a handler
        skips, because S3 cannot be made to repeat one and the test role may
        not send a message.
     4. Every wait for a delivery allows 60 seconds, the smoke check's
        bound, and a check that nothing else arrives reads every queue of
        the case together for as long. A wait that ends with nothing names
        the reader and the key and says the apply may not have finished,
        and every later case fails at once with that line rather than
        waiting out its own bound. The delivery case prints the median and
        largest delay from each event's time to SQS taking its message.

     The queues' own visibility timeout is 30 seconds on both sides, so the
     cases that need 2 seconds pass it on each receive and extend, as the
     SQS adapter does. The suite's clients there are the store suite's
     bucket client, `aws_client` in `tests/support/aws.py`, which ignores
     any endpoint the environment sets and refuses a host outside AWS. The
     job's `-v` names each case it ran with `[aws]`.
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

The required `test` check runs the first two levels. The OpenTofu configuration under `infra/` has a required check of its own, `infra`, from `.github/workflows/infra.yml`. It passes only when `tofu fmt`, `tofu validate` and `tofu test` against a mock provider pass, and it runs on every pull request whether or not the pull request changes `infra/`, because a required check that no workflow starts blocks the merge. The price is six short jobs on each pull request, about 40 seconds from start to end on [run 44972470102](https://github.com/l3a0/ithildin-runtime/actions/runs/44972470102), and a public repository pays nothing for runner minutes. The runs against the test bucket and queues are the exception. They need the test role, so they run in `ci.yml`'s `aws` job, only on a push to `main`, and no ruleset requires that check, because a check that reports only on a push would never report on a pull request and would block every merge. Those runs take turns in one concurrency group, because they share the test queues. The load is pushes to `main`. `gh run list --workflow ci.yml --event push --branch main --created 2026-11-03..2026-11-10 --limit 200` counted 27 runs over those eight days, and ten times that still fits one run at a time. The measure is the count of `aws` runs GitHub cancels because a newer push replaced them, which `gh run list --workflow ci.yml --event push --branch main --status cancelled --created <the last seven days> --limit 200` reads, and planning a rewrite starts when that count averages one a day over a week. No alarm reads that count. A person reads it instead, the one exception to building load alarms at the first load, because test holds nothing that cannot be made again. The price is that a rewrite is noticed late, and it saves the one scheduled check an alarm would cost. The rewrite would give each run its own queues and rules. The routes file's format and the two OpenTofu modules carry over, and nothing recorded needs to, because a test run's blobs are regenerable. Another required check, the image check, which `.github/rulesets/default.json` gains when [#11](https://github.com/l3a0/ithildin-runtime/issues/11) builds it, runs the third on the same
image, test 2 and the restart drills included, because the restart path is
what this design rests on. That check also runs the real unit files and the
host's watchdog under the runner's own systemd, which hosted runners appear
to allow with `sudo`, as the first build confirms. A local backtest has no
restart behaviour, so no test host that reimplements systemd exists.

What CI cannot cover runs in each environment that has the resource, as [When drills run](07-deployment.md#environment-rule-10-when-drills-run) says, as drills on
[#11](https://github.com/l3a0/ithildin-runtime/issues/11): the instance
metadata fence against AWS's own service, S3's bucket policy and CloudTrail,
and the account's real roles. The memory-backed folder and the emulator only
imitate S3's rules, which the real test bucket checks.

The price is a CI run that builds an image and starts several containers on
every pull request, a second required check, an S3 emulator and a test
bucket to keep, and drills that only the VM can run.
