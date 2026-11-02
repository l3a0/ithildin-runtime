# Design — ithildin-runtime

This doc carries the reasoning. The tracker carries unbuilt scope, and this
doc links to an issue rather than restating it.
[team-charter.md](team-charter.md) carries the roles and the lifecycle, and
the module table this doc reasons about.

## Contents

- [Premise](#premise)
- [One runtime](#one-runtime)
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
run exercise the same lines of code.

## Design principles

A principle earns a place here only when a design can pass or fail it, which is the test *The Art of Scalability* sets for its own (Abbott and Fisher, 1st ed., Addison-Wesley, 2009, Kindle location 4588, cited below as AoS L4588). A principle like "scales infinitely" fails that test, because no design can be checked against it. Each rule below names the check a review runs, and the reason for it.

The owner's standing directive is to design for 100 times the first load and build for the first load. The book states the same idea as designing every major system so it can be split along at least two axes, while building only the split that today's load needs (AoS L4684, L8205). Its three axes are clones of the whole system (x), a split by function or by kind of data (y), and a split by a key such as account or symbol (z) (AoS L7432).

So these rules are checks on a design, and a review applies all of them. A module builds the mechanism a rule names when the path it guards first runs, which the walking skeleton does for the order path, and no sooner. Three kinds of mechanism are built from the first run whatever the count, because CLAUDE.md's ranking rule exempts them.

1. Whatever keeps data that cannot be recaptured from being lost: disk headroom for capture and the journal, capture's place outside every halt, and the guard that keeps cleanup rules off recorded data.
2. Alarms and halts, which read zero while they are healthy.
3. The guards on the order path, whose price is paid by building them late: the sizing rule, the single sender and the block on unknown orders.

### Design ahead, and write down where the build stops

1. **The next scale step exists as a written design, and the only code built ahead of it is configuration.** The book's example is a data-access layer designed so it could reach several databases while one runs. Its migration scripts are written when the need arises, and the design is what lets a second database take weeks rather than months (AoS L1078). Here the only code built ahead is a configuration value, such as a partition count, whose other settings a test runs. A script or path offered later as the next step has run once, in a drill on a copy, before anything relies on it.
2. **Every implementation states its written scale limit, and an alarm or a test trips at 75% of it.** Code built for "any number of read nodes" that handles five has a limit of five (AoS L1174). The limit is stated beside the code that enforces it, and 75% is the headroom ceiling in rule 4. On the order path, reaching the limit halts.
3. **A partition key and its count are configuration from the first commit.** Raising the count of a split by key is a configuration change. The book finds a split by function "not so easy" to design room for in advance (AoS L7781), so module boundaries and data ownership are settled early. Captured data is keyed by its source, the vendor's identifier as received, and the date it was recorded. The key only routes a record and never identifies it, and a write into a key that already holds data adds a new vintage rather than replacing one. The trading loop is keyed by account.
4. **Plan headroom per component, at 50% of measured capacity and never above 75%.** The measured maximum can be wrong, growth estimates can be wrong, and behaviour near the limit turns erratic (AoS L4449, L4475, L4480). Headroom is computed for each component, never for the system as a whole (AoS L4496). Here that means capture disk, journal volume and the broker's API rate limits. A share of the rate limit is reserved for cancels and the kill switch, and new orders never draw on it.

### Keep the order path isolated

The **order path** runs from the target portfolio through the risk gate and the journal to the broker adapter. A **synchronous dependency** on it is a call that leaves the process or waits on input or output. A call between modules inside the process, through their shared interfaces, is not one.

1. **Nothing that does not earn money can stall the order path.** The book's first rule of fault isolation is to protect whatever is closest to the money (AoS L7130). Monitoring, reports, dashboards and research reach the order path only asynchronously, and are allowed to fail. The book's own case is a monitoring database whose slowness backed up every user query until the site went down (AoS L8616). The loss stop and the kill switch live in the risk gate and are read on every order, never in monitoring.
2. **Count the synchronous dependencies on the order path and keep them to the minimum.** Availability multiplies along a chain. One hundred parts at 99.999% each give about 99.9% together, which turns about 5 minutes of downtime a year into over 500 (AoS L8698). The minimum here is the input data, the risk gate, the journal and the broker. The journal write is synchronous on purpose, because hard limit 4 puts it before the send.
3. **A fault boundary is drawn around the whole loop, never around a layer, and every crossing is listed.** In the book, a unit isolated from faults shares nothing with its neighbours, takes no calls across its boundary, and carries every transaction from start to finish inside itself (AoS L7088, L7101, L7113). A boundary drawn around all the databases, or all the risk checks, only groups like parts, and one failure there still reaches every transaction (AoS L7117). The book lets one boundary hold one tenant or many (AoS L7147). So the first build is one boundary holding every strategy sleeve, and a split by account comes when load or risk calls for it. Two strategies in one broker account share buying power, so they never sit in different boundaries. Three crossings are allowed, and any other breaks the boundary.
   1. Captured data flows in, read only and one way, and a stale feed halts the sleeves that read it.
   2. Monitoring reads outward only.
   3. The kill switch flows inward to every boundary, and a boundary that cannot read it halts.

### Every fact has one writer

1. **Each fact has one authoritative store, and a copy of a value that changes is derived.** The book requires a single point of truth for any value copied across a system, updated in one place (AoS L8153). Here the journal records every intent, order, acknowledgement, fill and cancel when it happens, and the broker is the truth for holdings and cash at the moment the platform asks. The two are independent records that reconciliation compares and never overwrites, and a mismatch halts trading. Positions and cash held anywhere else are derived and rebuilt once the two agree. Captured data, every backup of it or of the journal, and every broker statement the platform saved are recorded facts, not derived copies.
2. **The quantity that sizes an order counts every order already sent.** The book asks three questions of any replicated read, and one of them decides this case: will the value feed a later write (AoS L7901)? Here the gate and the sizing both measure against the broker's holdings plus every order the journal shows as sent, working or unknown, counted as if fully filled. A strategy's own position comes from the journal's fills, and reconciliation checks the strategies' sum against the account. Risk limits, instrument metadata and configuration may be cached with a maximum age. Their refresh, like every session boundary, reads the shared clock rather than the wall clock, so a backtest refreshes the same way live trading does.
3. **One process sends for an account, and an order with no answer blocks the next.** State that must survive a restart is rebuilt from the journal and the broker, and a rebuild that cannot read either leaves the platform halted. The book's order of preference is to avoid state, then to move it out of the process (AoS L8393, L8827). Only the process holding the account's lease writes the journal or sends to the broker, and a standby takes the lease only after the old one has expired or been revoked, then reconciles before it sends. Each journal entry carries a client order id fixed by the decision before the send, the broker receives the same id, and a resend reuses it. An order the broker never acknowledged is unknown rather than failed. It blocks every further order on its account and instrument until reconciliation settles it, and if it stays unknown past a set time, trading halts and the owner is alerted. The simulated broker can withhold an acknowledgement, so replay runs this path. These rules go beyond the book, which discards a timed-out call (AoS L8698).

### Every change can be undone or switched off

1. **A strategy can be switched off without a deploy.** The book's rule is to design anything released so it can be turned off, which is faster than a rollback and leaves the rest of the release running (AoS L4800, L6358, L6996). Here the switch is a per-strategy halt that leaves the other strategies trading. Like every halt under hard limit 3, it stops new orders and never reads as a target of zero. Switching a strategy off is anyone's to do, while switching it back on in live trading is the owner's, on the owner's ruling of 2026-11-02.
2. **A change to a stored format ships in two releases: the code first, the data later.** Old fields and tables stay for as long as a rollback may need them (AoS L6327, L6335). The book puts the price of rollback at under 1% of a release for most changes, and finds the large releases that skip it are the ones that need it most (AoS L6340, L6348). Captured data and the journal are never migrated. A new format applies to new writes, every reader keeps reading every format ever written, and a test replays a journal holding each of them.
3. **A release is compared with the one before it, and a sudden gain is a finding too.** A jump in throughput can mean a code path or a filter was dropped (AoS L5876). Replays of the same recorded day must match on their counters, such as orders, rejects and fills. Each timing carries a written tolerance in both directions, set from this platform's own run-to-run spread, and the book's worked example uses 5% (AoS L11260). Any change past those bounds without an intended cause goes to the builder (AoS L11226, L11254).

### Monitoring answers whether there is a problem first

1. **The first monitors answer "is there a problem", and there are between three and ten of them.** Monitoring built bottom-up from host metrics produces alerts that teams learn to ignore, and then a customer reports the outage first (AoS L10210, L10223, L10355). Here the first monitors are these nine.
   1. P&L against expectation.
   2. Exposure against limits.
   3. Fill and reject rates.
   4. Slippage against the cost model.
   5. Data freshness.
   6. Orders in an unknown state, and reconciliation mismatches.
   7. Capture-disk and journal headroom.
   8. Whether the platform is halted, and why.
   9. The heartbeat.

   "Where is the problem" and "what is the problem" come after, in that order (AoS L10335).
2. **Every halt is a fixed rule, and statistics only escalate.** The book alerts when a value leaves several standard deviations of its own history at the same time on similar days (AoS L10168, L10313). Here that applies only to counters that behave like rates, such as fills, rejects and message rate, and only after the baseline holds a stated number of sessions. Until then a fixed threshold stands in, and the statistical alert is drilled on recorded history. A limit breach, the loss stop, a reconciliation mismatch and missing or stale input each halt by a fixed rule, because hard limit 3 cannot wait for a statistic.

### Data has a cost, except the data that cannot be recaptured

1. **Every class of derived data has an owner and an end date.** Data costs more than its storage, and the book lists six costs that grow with it (AoS L8947). Most of its value falls with age, so keeping it forever needs a stated value and a time limit (AoS L8963, L9168). Diagnostic logs, intermediate backtest output and feature tables follow that rule. Recorded data is exempt as a class: captured data, the journal, every backup of either, and every broker statement the platform saved. No lifecycle or cleanup rule may match a path that holds it. The operator's change log and incident evidence are kept as long as the operator sets on purpose, rather than by this rule.

### Buy what does not set the venture apart

1. **A component is built here only when it passes the book's four questions.** Does it set the venture apart from competitors? Is this team the best owner of it? How soon would a competitor catch up? Can it be built and supported for less than buying (AoS L5500)? The book finds it "not uncommon" to underestimate support cost by an order of magnitude for infrastructure that runs around the clock (AoS L5320). Here support lands on the owner's hours, so a build is priced in operator hours a year, not in tokens. The four answers and that price go on the issue, and the call is the owner's. The charter's build-or-buy note names the alpha models and the research platform as what the team builds. A bought component inside the loop runs in process, through the same interface, in every environment. One that runs only at the broker sits behind the broker adapter, and its simulated stand-in is named as a gap in the model.
2. **A vendor's own features sit behind the adapter at its edge, with an issue to retire them.** Deep integration with one provider raises the cost of leaving it (AoS L6727). When a crisis forces one in, the book's rule is to wrap it and commit to removing it (AoS L6794).

The price of these rules is that every path carries its mechanisms from its first run, and a design names its next split before any code needs it. What they buy is that load reaching 100 times the first takes configuration and more machines rather than a rewrite, which is what the owner's directive asks.

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
| **order path** | The path from the target portfolio through the risk gate and the journal to the broker adapter. Its synchronous dependencies are the calls on it that leave the process or wait on input or output |
| **written scale limit** | The most load an implementation handles as built, stated beside the code that enforces it, as distinct from the most its design allows |
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
| One standing agent per role | Every agent runs on the same model, so a role adds something only as an objective held during one piece of work. Standing agents idle and cost tokens. The charter's `## Why a team needs a charter` gives the reasoning. The `l3a0` plugin's `/l3a0:ask-team` role agents are not standing agents, because each runs once for one review and then ends |
| A build-plan doc listing each slice's deliverables and tests | It restated scope the tracker holds, so the two would drift. A slice's tests are its parent issue's acceptance criteria, and what each deliverable waits on is its sub-issue's "blocked by" links. The rule for cutting a slice is the one part GitHub cannot hold, and it lives in [How work is cut and ordered](#how-work-is-cut-and-ordered) |
| A copy of the build board's generator and page in this repo | The owner ruled on 2026-10-28 to call repo-template's reusable workflow at a pinned commit instead. The generator and page run to about 2,900 lines that still change often, and a copy per repo drifts. [#12](https://github.com/l3a0/ithildin-runtime/issues/12) carries the ruling |
| A copy of the board skills under `.claude/skills/` | The owner moved `update-build-board` and `sync-prune-next` into the `l3a0` plugin on 2026-10-29, as one union of every repository's copy, and [#18](https://github.com/l3a0/ithildin-runtime/issues/18) deleted the copies here. The price is that the skills move with each plugin release while `.github/workflows/board.yml` pins the generator to one commit. The plugin's `## Generator compatibility` rules keep the two in step, and `CLAUDE.md`'s `## Keep the build board current` names the release they were checked against |
| The template's cross-surface sweep module | Nothing in the repo is regenerated from code yet, and it holds no figure. The board's tab icon is committed as a finished file, per the next row. Take the module from [repo-template](https://github.com/l3a0/repo-template) when the first generated artifact lands |
| A script that renders the board's tab icon | It reads the owner's brand folder, which lives outside the repo, and it needs Cairo, a dependency nothing else here uses. The icon changes only when the brand does, so `.github/board/favicon.ico` is committed as a finished file and a new icon is a new file. [PR #17](https://github.com/l3a0/ithildin-runtime/pull/17) carries how it was made |
| The book's N+1 design, rule of three and multiple live sites | Redundant compute and a second live site guard availability the platform does not yet need, since it runs one paper account on one machine at 1x. The owner moved a three-VM cluster out of the first slice on 2026-11-02. They return when an availability target the platform has measured calls for them. What cannot wait is the copy of what cannot be recaptured, which the premise and the design principles already cover |
| The book's "scale out, not up" and commodity hardware | For a sole operator a bigger machine is often the cheapest correct next step, and the hardware argument dates from 2009. The design principles keep the part that holds: each design names its next split, so moving to more machines takes configuration rather than a rewrite |
