# What a person runs

This page is for the owner and the operator. It is reference and how-to: the one command line tool, its commands, how a person goes from an alarm to the command that answers it, the steps the owner takes once per account, the command that measures a stress move, and the one that counts how often recorded quotes would be refused. The reasoning behind each rule sits in [the architecture](architecture/00-README.md) and [design.md](design.md), and each drill's steps sit in [halts-and-drills.md](halts-and-drills.md).

## Contents

- [What a person runs](#what-a-person-runs)
  - [One command line tool, run off the VM](#one-command-line-tool-run-off-the-vm)
  - [The commands](#the-commands)
  - [From an alarm to a command](#from-an-alarm-to-a-command)
  - [Steps the owner takes once per account](#steps-the-owner-takes-once-per-account)
  - [Measure a stress move](#measure-a-stress-move)
  - [Write a row of the record of splits](#write-a-row-of-the-record-of-splits)
  - [Count how often quotes would be refused](#count-how-often-quotes-would-be-refused)
  - [Measure the capture lag](#measure-the-capture-lag)
  - [Set up the test environment](#set-up-the-test-environment)

## What a person runs

The platform halts rather than guess, so every halt that needs judgement
waits for a person, and the person's actions are as much a part of the
record as the platform's. The owner or
the operator clears a halt, and only the owner clears a live account's
kill switch, apart from the pull a live drill the owner approved makes and clears. This
section is this doc's proposal for what those commands are and how an alarm
leads to one, for [#11](https://github.com/l3a0/ithildin-runtime/issues/11).

### One command line tool, run off the VM

#### Where the tool runs

A person acts through one command line tool, `ithildin`. It runs from the owner's or the operator's own machine with their own AWS credential, never on the runtime's VM. It reads and writes S3 only, so it needs nothing from the VM and works while the VM is down.

#### How a person's write is checked

Its writes go through the same journal, in the `journal` package, and the same storage contract, in `core`, that the workers use. So a person's entry is checked, chained and refused on a stale number exactly as a worker's is. The tool fetches the journal's end before it writes, since every step fetches and syncs before changing state, as design.md's `## Processes and files` says.

#### How a false name in an entry is caught

The tool writes the identity AWS reports for the caller into each entry, but S3 does not stop a caller writing any name it likes. Two things cover that.

1. CloudTrail records every write to these places, in a bucket only the owner can change:

   - the journal
   - the switch
   - `stop.json`
   - each strategy's `off.json`
   - the leases
   - the overrides
   - `accounts/<account>/releases/`
   - the halt folders
   - the hard caps
   - the owner's records
   - `release/`

2. Monitoring alarms when an entry's named identity differs from the one CloudTrail recorded.

Every principal but the owner refuses `cloudtrail:StopLogging`, `DeleteTrail`, `UpdateTrail` and `PutEventSelectors`, and [the drill on CloudTrail](halts-and-drills.md#drills-with-no-alarm) tries each.

#### Which credentials the tool uses

Two credentials hold what the VM's credential deliberately cannot.

1. **The owner's**, which may do everything below on any account.
2. **The operator's**, which acts on paper and shadow accounts only, plus pulling a live account's switch and switching a live strategy off, since each can only stop trading. It cannot clear a live account's halt, switch or lease, or write a live account's override.

Only the owner resumes live trading. The charter makes the operator an agent role, and hard limit 1 keeps agents to simulated and paper brokers, apart from its one exception, a live pull and a live switch-off.

#### The owner's credential

The owner's credential is a role reached through IAM Identity Center or an MFA sign-in behind a hardware key, with sessions of an hour at most.

A hardware key guards the sign-in, not what the sign-in leaves behind. Three facts about an IAM Identity Center session show what it leaves.

1. It [lasts eight hours by default](https://docs.aws.amazon.com/singlesignon/latest/userguide/user-interactive-sessions.html).
2. Its token sits in `~/.aws/sso/cache`, where any process of that user can read it.
3. While it lasts, it mints credentials for every permission set the user holds.

Agent sessions run as the owner's user on the owner's machine, so the owner's live-writing permission set is never signed in where agents run. It lives under a separate OS user or device, with a session short enough to need a fresh key touch, and a [drill](halts-and-drills.md#drills-with-no-alarm) proves an agent session cannot use it.

#### The operator's credential

The operator's is a short-lived principal of its own. The operator is an agent role, so its credential cannot rest on a hardware key. It has no owner permission set, sessions of an hour or less and no path to the owner's cache. How an agent session receives it is a runbook item on [#11](https://github.com/l3a0/ithildin-runtime/issues/11).

#### Both credentials and each bucket's policy

Neither holds a long-lived secret in a file. Each environment's bucket policy names each role, and these rules hold.

- The live bucket's policy enforces the owner's split.
- Inside every bucket, overrides sit under a prefix of their own per account, apart from the journal.
- Only the owner's role may write a live account's overrides, and no runtime role may write any override.
- Journal entries are created, never replaced, by execution, monitoring and the supervisor, by the `ithildin` tool for the person running it, by an emergency rollback's credential for its own kill switch pull, and on live by the approved drill role for its own drill's pull and clear.
- On the live bucket, the switch, lease, override and release keys refuse every person's principal but the owner's, the operator's pull and strategy off switch aside.
- The runtime's own roles keep the grants [The runtime's credential does only what the rules need](architecture/07-deployment.md#rule-5-the-runtimes-credential-does-only-what-the-rules-need) gives them there.

#### Which duty each credential holds

A larger team would split these duties among several people. Here one person and one agent hold them, so every duty sits with the owner's credential, the operator's, or either one. A cell that names both means either may act alone.

Two credentials keep duties apart only if a different principal holds each. So a duty gets a credential of its own when a different principal takes it on, such as a second person on call or a read-only auditor.

[The commands](#the-commands) says what each clear, override and pin command does. The hard caps are the risk gate's fixed limits.

**Table:** Which credential holds each duty on each kind of account

| Duty | Shadow and paper | Live |
| --- | --- | --- |
| Release a build | The owner's merge deploys it | The owner, by hand |
| Change the infrastructure | Follows the owner's merge. A change to the paper VM's image, startup script or role waits for the owner's approval | The owner, behind live's reviewer |
| Roll back by the release's pin | The owner or the operator | The owner |
| Remove a block record with `ithildin pin unblock` | The owner | The owner |
| Pull the kill switch | The owner or the operator | The owner or the operator |
| Switch a strategy off | Any person's credential | Any person's credential |
| Switch a strategy back on | The owner, and whether the operator may too is undecided | The owner |
| Clear a halt, a fired loss stop or the kill switch | The owner or the operator | The owner. A live drill the owner approved also clears the kill switch it pulled itself |
| Clear an account's lease | The owner or the operator | The owner |
| Write an override | The owner or the operator | The owner |
| Change the hard caps | The owner | The owner |
| Return a tightened limit to its value at the sample's opening | Not decided | The owner |
| Use the switch that stops restarts and scaling | The operator | The owner |
| Confirm a trade as the broker's own | The owner | The owner |
| Read the log and state stores | The owner or the operator | The owner or the operator |

The operator approves no apply to any environment.

### The commands

#### The commands that read

Reading commands change nothing.

A halt's id takes one of two forms, because there are two kinds of halt and each id is where its halt is stored. So the tool goes straight to a halt without searching for it.

1. A halt in a result belongs to one decision, as a stage's result saying why that decision cannot go on. Its id is the key of that result, `<deployment>/<run>/<decision>/<stage>`.
2. A halt held in a folder is a blob of its own for the account or one strategy, which outlives a decision and lasts until a journaled release names it. Its id is the halt's name, `<component>-<cause>-<timestamp>-<count>`, one name for each firing. It belongs to no single decision, so it has no result key to use.

`ithildin halt clear` reduces a person's input to that id before it journals, so the tool finds the environment it lives in.

1. **`ithildin status`** shows, for every account, grouped by environment:

   - whether the kill switch is set
   - which strategies are switched off
   - whether a loss stop has fired
   - the last sort's outcome for each strategy switched off
   - who holds each lease
   - every open halt with its reason and age
   - the latest decision's result for each stage
   - each worker's and the supervisor's last heartbeat and restart count
   - any pair leg left open since the last close
   - when the heartbeat service last heard a ping

   It also shows:

   - each environment's pin
   - the digest each recent run used
   - each level of load reached, with its date and the issue filed for it, or "no issue filed"
   - the next gate's rows with no passing record

2. **`ithildin halt explain <haltid>`** shows one halt: its reason, the halt it passed on, the blobs it was made from, and for a restart, the exit status and the end of the error output.

#### The commands that write

Writing commands take more friction the more they can resume trading, so they are grouped by what they do to trading.

**Table:** The ten writing commands, grouped by what they do to trading

| # | Group | Command | What it does |
| --- | --- | --- | --- |
| 1 | Stop trading | `ithildin switch pull` | Writes a new version of the kill switch blob |
| 2 | Stop trading | `ithildin strategy off` | Creates a strategy's off switch |
| 3 | Correct the record | `ithildin override` | Records a correction the book cannot work out alone |
| 4 | Resume trading | `ithildin halt clear` | Records that a person has looked at a halt |
| 5 | Resume trading | `ithildin strategy on` | Deletes a strategy's off switch |
| 6 | Resume trading | `ithildin stop clear` | Journals the clear of a fired loss stop, then deletes its blob |
| 7 | Resume trading | `ithildin switch clear` | Deletes the switch |
| 8 | Recover from a dead host | `ithildin lease clear` | Rewrites a lease another VM holds to clear its holder |
| 9 | Change the release | `ithildin pin set` | Rolls an environment back |
| 10 | Change the release | `ithildin pin unblock` | Removes a block record |

##### Stop trading

These two take a default reason and ask for no confirmation, because stopping should never be slow.

###### `ithildin switch pull --account <a>`

This is one command with a default reason, because stopping should never be slow.

1. The tool writes the kill switch blob, as a new version even where the switch is already set, journals the pull and raises the alarm. This doc proposes, for [#11](https://github.com/l3a0/ithildin-runtime/issues/11), that on live the operator's credential creates no journal entry, so what journals an operator's pull there is open on that issue.
2. The blob's own event wakes execution.
3. Execution reads the switch itself and cancels every resting order in the account, stamped or not, as [The kill switch is a blob](design.md#rule-10-the-kill-switch-is-a-blob) says.
4. The tool reads the journal once, at a deadline set before the [cancel cutoff](architecture/06-runtime.md#the-cancel-cutoff), for execution's entry saying the cancels are done, which names the switch version the pull wrote, so an entry left by an earlier pull never counts.
5. If that entry is not there or cannot be read, the tool tells the person to cancel at the broker's own console.

A pull past that deadline names the console at once.

###### `ithildin strategy off --account <a> --strategy <s>`

This creates the strategy's off switch with a default reason and journals it, because stopping should never be slow. This doc proposes, for [#11](https://github.com/l3a0/ithildin-runtime/issues/11), that on live the operator's credential creates no journal entry, so what journals an operator's switch-off there is open on that issue.

A decision that finds the off switch already set plans no orders for the strategy, as [Prepare, once per decision](architecture/06-runtime.md#prepare-once-per-decision) says. For a switch set later, execution reads the off switch right before each of that strategy's orders, abandons them, and keeps sending the account's other strategies' orders. It never reads as a target of zero, so nothing is flattened. The strategy's cancels still go out.

As with the kill switch, this doc proposes that the blob's event wakes execution, which takes the account's lease and makes a pass called the sort. [How a sort runs](halts-and-drills.md#how-a-sort-runs) proposes the steps of this pass.

**Table:** What the sort does to the strategy's working and in-doubt orders, as this doc proposes

| The order | What execution does |
| --- | --- |
| It adds to a position | Cancels it, journaled first |
| It reduces a position | Keeps it |
| It belongs to a pair with one leg of each kind | The pair stays whole. Execution cancels the adding leg first, and the reducing leg only once that cancel is confirmed with nothing filled |

The command then reports in three steps.

1. It prints the orders resting and says execution's journaled sort decides which stay.
2. It reads the journal once, at a deadline set before the cancel cutoff, for the `sort_done` entry.
3. If the entry is there, it prints which orders went, which stayed and which were lost with a partner. If not, it tells the person to cancel at the broker's own console.

A kept order still fills unless the kill switch, which cancels every order in the account, or the broker's console cancels it before the cancel cutoff. A cancel at the console traces to nothing, so it halts all trading until the owner confirms it.

Past the cutoff, the command says no order can be stopped and lists the adding orders that will still fill.

##### Correct the record

This changes the book and never sends an order.

###### `ithildin override --account <a>`

This records a correction the book cannot work out alone, such as a fill the broker shows and the journal lacks after an outage, or an order the platform did not send. It never sends an order.

An override names these things:

- the strategy's book it changes, or the unmatched entry it brings in
- each position or open order it changes, with the broker's evidence and, for a fill, the broker's fill id
- the book values it was built against

The command writes the override's body under the overrides prefix and a journal entry naming that blob by its SHA-256.

**Table:** What execution's next sync does with an override

| What the sync finds | What it does |
| --- | --- |
| The blob exists under the overrides prefix, its hash matches and the replayed book has not moved from the values it names | Applies it |
| The blob is not under the overrides prefix, or its hash does not match | Does not apply it |
| The replayed book has moved from the values it names | Refuses it |

A change to a strategy's position leaves that strategy off and sorted, as [A strategy is judged over a sample](design.md#a-strategy-is-judged-over-a-sample-and-runs-unchanged-through-it) says, and the command prints so.

An override brings a held cash record into the book, as the case list in design.md's `### Strategies, schedules and decisions` says.

##### Resume trading

These run from the narrowest reach to the widest in the usual case: one halt, one strategy, the fired loss stop on the account's gate, then the kill switch on the whole account. A halt of all trading is account-wide, so clearing that one reaches as far as the last two.

###### `ithildin halt clear <haltid>`

This records that a person has looked at a halt and why it may be left behind. It never reruns the halted decision, and the next decision runs as usual.

A clear of recover's halt does not end the hold of an unmatched record, fill or order the journal keeps. The next recover halts on the same entry, because nothing releases one until [#151](https://github.com/l3a0/ithildin-runtime/issues/151) builds the release on [#150](https://github.com/l3a0/ithildin-runtime/issues/150)'s release test, as [Drill for an order or fill the platform did not send](halts-and-drills.md#drill-for-an-order-or-fill-the-platform-did-not-send) says.

A halt raised because S3 would not store a capture refuses the clear until the condition in [Rule 1: a write S3 refuses or does not answer](design.md#rule-1-a-write-s3-refuses-or-does-not-answer) holds.

On an account that sets `release_needs_blob`, it first writes the release blob, whose body names the halt's id, which only the owner's role can do, and the journal entry names its hash.

This doc proposes it as the confirmation after a stop that [A strategy is judged over a sample](design.md#a-strategy-is-judged-over-a-sample-and-runs-unchanged-through-it) asks for, and as the release of a mismatched request's halt, for [#33](https://github.com/l3a0/ithildin-runtime/issues/33).

A clear brings a held cash record into the book, as the case list in design.md's `### Strategies, schedules and decisions` says.

###### `ithildin strategy on --account <a> --strategy <s>`

This deletes the strategy's off switch and journals it. It refuses three kinds of strategy.

1. A strategy with an unreleased firing of a strategy halt, naming each id. `ithildin halt clear <haltid>` releases each firing, which ends its sort.
2. A strategy retired under design.md's sample rules that has no new registration.
3. A strategy whose close after a broker's trade is still under way, while any leg has no outcome, its order is unsettled or still working, or an entry is owed, as [A fill is measured from the decision](design.md#a-fill-is-measured-from-the-decision-and-a-position-depends-on-a-market-it-does-not-control) says.

On a live account only the owner may. On shadow and paper this doc proposes that the operator may too, read from the rule for a fired loss stop and the kill switch. It waits on a ruling in [Proposals awaiting a ruling](architecture/09-decisions.md#proposals-awaiting-a-ruling).

###### `ithildin stop clear --account <a>`

This journals the clear of a fired loss stop, then deletes its blob, so the gate takes new orders again. Deleting the blob by hand clears nothing, because the journal keeps the firing, as [When a clear counts](architecture/06-runtime.md#when-a-clear-counts) says. [#203](https://github.com/l3a0/ithildin-runtime/issues/203) builds the command, and until it does a fired stop holds the account. On a live account only the owner may, and on shadow and paper the operator may too.

###### `ithildin switch clear --account <a>`

This journals the clear, then deletes the switch.

##### Recover from a dead host

This clears the holder of a lease whose host is gone, so another instance can take the account over.

###### `ithildin lease clear --account <a>`

This rewrites a lease another VM holds to clear its holder, with `If-Match` on the tag it read, before it expires, for when a person knows the holder is gone sooner than the expiry would show. It asks EC2 whether the holder's instance is stopped or gone.

**Table:** What `ithildin lease clear` does on each answer from EC2

| EC2's answer | What the command does |
| --- | --- |
| The holder's instance is stopped or gone | Clears the lease |
| The holder's instance still runs | Refuses |
| EC2 cannot answer | Clears the lease only if the person says with `--holder-gone` that they have confirmed it another way, which the command allows only on this answer |

This doc proposes, for [#10](https://github.com/l3a0/ithildin-runtime/issues/10), that the clear's journal entry is durable before the rewrite, so a write with no acknowledgement leaves the lease untouched, and that a lease changed since that entry stays and the command reports it. [#10](https://github.com/l3a0/ithildin-runtime/issues/10) holds how, including what a rerun checks.

##### Change the release

These roll an environment back and free a blocked build.

###### `ithildin pin set --environment <e> --digest <d>`

This rolls an environment back.

- It refuses a digest that environment has not run, or whose attestation `gh attestation verify` rejects.
- It makes the person type the environment's name.
- It writes the pin and a block record for the digest it replaced, beside a record of who changed it and why.

The owner or the operator runs it on shadow and paper, and only the owner on live.

###### Roll a release back during a session

The owner rolls back, or on shadow and paper the operator, in three steps. [A rollback is a pin to an earlier digest](architecture/07-deployment.md#rule-6-a-rollback-is-a-pin-to-an-earlier-digest) says why.

1. Pull the kill switch of every account the environment holds, one `ithildin switch pull --account <a>` per account as `ithildin status` lists them, or wait until the cancel cutoff has passed, so nothing is left half sent.
2. Run `ithildin pin set --environment <e> --digest <d>` with the earlier digest. The session keeps its own digest, and the earlier release takes the next session.
3. A switch pulled for the rollback stays set, so no session trades until it is cleared. A clear during the rolled-back session restarts trading on the release being rolled back, because that session keeps its digest, and on paper a set switch also keeps the promotion job from moving paper's pin. Leave each switch set until `ithildin status` shows the earlier digest as the digest the next session's first run used. Then clear each with `ithildin switch clear --account <a>`, by the owner, or on shadow and paper by the operator.

###### `ithildin pin unblock --environment <e> --digest <d>`

This removes a block record, so a digest blocked by mistake can be promoted again. Only the owner runs it. Until then a stray block holds paper on its current digest, which keeps trading, and stalls promotion.

#### What a command outside the stop group asks of a person

Every writing command outside the stop group does these things.

- They take a required `--reason`.
- They print what they will do and to which account.
- They show a live account's name in capitals.
- For a live account, they make the person type the account's name to confirm.

When there is nothing to do, a command says what it found and who acted on it, rather than quietly succeeding, so a mistyped id is visible.

### From an alarm to a command

#### What an alarm says

Each line of an alarm has one job.

1. Its first line names the environment, the account, the decision and the reason, so a push on a phone says what happened without opening anything.
2. Its second line is the command to run first, almost always `ithildin halt explain` on the halt, or `ithildin status` for an alarm with no halt, such as one the heartbeat service raises, whose check's description carries that line.
3. Its third line names the alarm's row in [Every alarm and its drill](halts-and-drills.md#every-alarm-and-its-drill), where the runbook on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) says, row by row, what to check and which writing command, if any, follows.

Monitoring's report after each decision ends with the same summary `ithildin status` prints.

#### When a writing command is needed

Most alarms on shadow and paper need no writing command at all. A missing bar that has since arrived, most gate refusals or a pair leg left open clear themselves at the next decision, which plans again from the book, and the person's job is to read the reason and decide whether something larger is wrong. A refusal repeats while what caused it stands. Examples are a fired stop, which `ithildin stop clear` ends, an order past `max_order_qty` or a `max_position` it adds to, an order that adds where a limit it counts toward is already past, and a pair's exit that would take net past its cap and further from zero, as [What may go out while a limit is past](design.md#rule-9-what-may-go-out-while-a-limit-is-past) says. On live, every halt holds the account until the owner clears it.

The writing commands are for the cases the platform cannot resolve alone:

- a book that disagrees with the broker
- the causes that hold a shadow or paper account, which [A halt is final for its decision](design.md#rule-7-a-halt-is-final-for-its-decision) names: an order the platform did not send, a snapshot that cannot be stored or confirmed, a price of unstated kind and a quote that fails its age and status check
- a kill switch
- a fired loss stop
- a halt of all trading that waits for a person, as [How a sort runs](halts-and-drills.md#how-a-sort-runs) says
- any halt on live

#### What to do about a halt on the clock

A halt on the machine's clock is one decision's on shadow and paper, and the next decision reads the clock again, so a first one needs no command there. On live it holds the account until the owner clears it, as every halt does.

**Table:** The owner's step where a halt on the clock repeats

| The reason says | Step |
| --- | --- |
| A server's answer is too old, or it has given none | Wait while that server is out. Otherwise revert the last apply or release the halts began with, and where there was none, replace the VM, because the thread that asks is stuck |
| The clock is past its bound against a server | Replace the VM, because chrony is not holding the clock. Where a newer answer of the other server puts the clock well within its bound, wait instead, because the first server or the path to it is wrong |
| No time server was asked | Roll the release back with `ithildin pin set`, because it gave a trading run a clock that measures nothing |

Each step but waiting is taken between sessions, because nothing on slice 1's one VM serves a cancel or the kill switch while it is replaced. [Roll a release back during a session](#roll-a-release-back-during-a-session) says how that one is taken inside a session, and [#11](https://github.com/l3a0/ithildin-runtime/issues/11) has still to say how the other two are and who ends a VM.

[The time servers](architecture/03-context-and-scope.md#the-time-servers) says how each reason comes about.

#### What to do away from a computer

Away from a computer, the fallback is the runbook's: cancel at the broker's own app, and journal it with `ithildin override` once back. A page or a phone app that runs these commands waits for evidence that the command line is not enough.

#### What all of this costs

The price has four parts.

1. A tool to build and test.
2. A drill for each writing command on a paper account before the paper gate, which the runbook on [#11](https://github.com/l3a0/ithildin-runtime/issues/11) adds to [Every alarm and its drill](halts-and-drills.md#every-alarm-and-its-drill).
3. CloudTrail's data events on eleven prefixes.
4. Two credentials outside the VM, the owner's behind a hardware key and the operator's a short-lived principal of its own, neither with a long-lived secret in a file.

### Steps the owner takes once per account

#### Confirm an alarm email subscription

An alarm's email subscription sends nothing until it is confirmed. The owner confirms it by hand, once per account, in place of clicking the emailed link. [Which accounts send the email](architecture/07-deployment.md#which-accounts-send-the-email) says which accounts need it, and [How the email route is set up](architecture/07-deployment.md#how-the-email-route-is-set-up) says why the command replaces the link.

Every command below runs under the owner's credential, signed in to the account that holds the topic. `<region>` is the region the topic's ARN names. The address is typed only at the command line, so it enters no tracked file and no OpenTofu state, as [How the email route is set up](architecture/07-deployment.md#how-the-email-route-is-set-up) says.

1. Subscribe the address with `aws sns subscribe --region <region> --topic-arn <topic ARN> --protocol email --notification-endpoint <address>`, where `<topic ARN>` is the account's alarm topic, which on the test account is `ithildin-test-alarms`. It prints `pending confirmation` in place of the subscription's ARN, and SNS emails the address a confirmation link.
2. Copy the emailed link's address without following it, and take the value of its `Token` parameter. AWS's [Subscribe](https://docs.aws.amazon.com/sns/latest/api/API_Subscribe.html) page says a token is valid for two days, and the [email notifications](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html) page says SNS deletes a subscription still unconfirmed after 48 hours, so a token older than that means starting again at step 1.
3. Run `aws sns confirm-subscription --region <region> --topic-arn <topic ARN> --token <token> --authenticate-on-unsubscribe true`, as AWS's [ConfirmSubscription](https://docs.aws.amazon.com/sns/latest/api/API_ConfirmSubscription.html) page describes. It prints the subscription's ARN.
4. Run `aws sns get-subscription-attributes --region <region> --subscription-arn <subscription ARN>` and check that `ConfirmationWasAuthenticated` reads `true`. If it does not, the subscription was confirmed by the link instead. Remove it with `aws sns unsubscribe --region <region> --subscription-arn <subscription ARN>` and start again at step 1.

To move the email to another address, subscribe and confirm the new address by steps 1 to 4 first. Then find the old subscription's ARN with `aws sns list-subscriptions-by-topic --region <region> --topic-arn <topic ARN>` and remove it with `aws sns unsubscribe`, so the topic always has at least one confirmed subscription. Last, run the drill row for an email subscription on that account. The same `aws sns unsubscribe` command ends a subscription on purpose.

The drill rows for an email subscription under [Drills with no alarm](halts-and-drills.md#drills-with-no-alarm) read the subscription's `ConfirmationWasAuthenticated` attribute and expect `true`.

### Measure a stress move

The risk gate's net cap follows the stress move, m, and every order halts until the hard caps name a record of it. [How the stress move is measured](architecture/05-building-blocks.md#how-the-stress-move-is-measured) says what the command reads and why, and [The stress move's record](architecture/08-crosscutting-concepts.md#the-stress-moves-record) what each key holds.

#### Run the command

It reads a folder that holds a copy of marketlake's lake, so it needs no credential, and it writes nothing but what it prints.

```bash
uv run python -m ithildinruntime.runtime.stress_move --lake <folder> --strategy <name> --through <YYYY-MM-DD> --seam <YYYY-MM-DD>
```

1. `--through` is the last session, and `--seam` a session a new series starts on, given once for each in order. Each leg's start comes from the strategy's own file, and no option changes it.
2. A run takes minutes, and longer the more partitions the lake lists.
3. A run that refuses prints one line to stderr, nothing to stdout, and exits nonzero. The line names the lake's folder, the mirror's and the checkout's by a word for each.
4. A comment line above the record names each record of the strategy that is larger, or whose move this run did not count. Read why before committing the new one.

#### Commit a record

1. Append what the command printed to `src/ithildinruntime/core/stress_moves.toml`. Never change or remove a record that is there, apart from [withdrawing one](#withdraw-a-record).
2. Fill in each flag's empty `what`, which says what the flagged price was. The reader refuses a record with one empty.
3. Where the record's `series` hold captured bars, check that its `splits` names the SHA-256 of `src/ithildinruntime/data/splits.csv` at the commit you merge into, and that its `split_rows` are that file's split rows for its instruments. The command refuses a pair of captured bars that no covered row holds, so a record over them needs [a covered row](#write-a-row-of-the-record-of-splits) reaching its `through` first. While none does, the older record's age keeps running.
4. Read each move under `left_out`, which m does not count. Where a source holds both its sides, compare it with m.
5. A move at step 4 that is at or above m, or that no source measures, stops the commit and goes to the owner.
6. Pin the record in `tests/test_stress.py`'s `PINNED`, by hand.

#### Raise the stress move

A larger record binds as soon as the release that carries it runs, whatever the caps hold. It stops binding if that release is rolled back or the record is withdrawn, and the net cap then grows with nothing halted. So the owner takes three steps, the first two before the release that carries the record runs.

1. Compare the dollars of each held leg with the net cap the new m gives. Where one is over it, the gate can refuse an exit of the pair once m is raised, and where each is, both, until the target, a cap, V or the prices move. [#158](https://github.com/l3a0/ithildin-runtime/issues/158) holds what the owner may do about a position then.
2. Write the caps' `stress_move` up to the new record's, leaving `stress_vintage` and `stress_through` on the older record. Raise `stress_high` in the same write where the new m is above it, because the caps halt every order otherwise. A rollback then loses nothing.
3. Move `stress_vintage` and `stress_through` to the new record only once the release that carries it would not be rolled back. A release rolled back past the record the caps name halts every order, so before such a rollback move both fields back. Move to the new record before the older one's age passes, whether it is larger or not. For one that is no larger, this step alone is enough.

#### Withdraw a record

A withdrawal ends what the record held, so the net cap can grow with nothing halted, and caps that name the record halt every order once the release runs. So before the change merges, the owner writes `stress_move` to what should stand, and moves the caps to another record the running release already carries where they name this one. Where there is no other, a release that carries a new measurement runs first, and the withdrawal comes in a later one. The change adds `withdrawn` with the reason, does the same in `PINNED`, and says which `stress_move`, `stress_vintage` and `stress_through` the owner's caps hold. A record of the same strategy and vintage is refused even beside a withdrawn one, so a right one is measured through another session.

### Write a row of the record of splits

`src/ithildinruntime/data/splits.csv` records every split of every instrument the platform reads, and the spans of sessions over which it holds every split. Every decision halts on a split it does not hold, apart from the three kinds the design doc's rule names as still reading as a move, and on a pair of sessions no covered row and no quote covers, so a person writes a row before a split's first session and when the covered rows fall behind. The design doc's rule that [a split reads as neither a gain nor a loss](design.md#a-backtest-names-what-it-could-not-have-traded-and-each-model-states-how-it-fails) says why. Rows are only ever added, by a pull request the owner merges, and `tests/test_data_files.py` pins each by hand.

#### Fetch what a row is read from

1. Save every answer fetched for the record of splits, whether or not a row cites it, under `~/.config/ithildin-runtime/splits/<date>/`, where `<date>` is the UTC date of the fetch, never in the repository. Save an issuer's filing as fetched and a vendor's answer as its connector returned it, and list each file's address and UTC fetch time in a `README.md` there.
2. Write the SHA-256 of each file a row cites into the row's `read_from`, beside its address and fetch time. `read_from` holds no comma, because the loader splits the row on commas.

These answers are recorded data, and an issuer's filing can be fetched again while a vendor's answer of that day cannot. So two things must hold for every saved answer, and [#250](https://github.com/l3a0/ithildin-runtime/issues/250) builds how.

1. A saved answer, and the listing of its address and fetch time, is never replaced or deleted.
2. A copy off the machine of each answer and its listing, checked against each answer's SHA-256, exists before anything relies on the answer. Until [#11](https://github.com/l3a0/ithildin-runtime/issues/11) gives saved published texts a prefix the [replication rule](design.md#rule-1-the-replica-before-live) copies, the owner makes that copy by hand.

Today the folder is the only copy, nothing locks its files, nothing reads them back, and an answer no row cites has its SHA-256 written nowhere. Nothing deploys yet, so the first reads that rely on a row are a stress run or a backtest run by hand, and then the first shadow decision, once [#202](https://github.com/l3a0/ithildin-runtime/issues/202) deploys on merge. The price is that losing or changing a file leaves rows citing answers nobody holds, and no vendor gives those answers again. A connector's rendering of a vendor's answer is what is kept, so a connector that drops a row or a page also goes unseen in the saved file.

#### Write a split row

1. Read the issuer's filing on SEC EDGAR that announces the split, and its index page, which gives the time EDGAR accepted it.
2. Read Alpha Vantage's `SPLITS` and Massive's `GET /stocks/v1/splits` for the ticker on the same day.
3. Write the row only where all three agree on the ratio and the first session on the new scale. Where they disagree, write nothing and take the question to the owner.
4. Write `new_shares` and `old_shares` as whole numbers, such as 1 and 8 for a one-for-eight reverse split, and `known_at` as the UTC instant the filing became public. Where EDGAR accepted it before a session's open, write that open, as USO's row does for 2020-04-22.
5. Merge it before the split's first session. From `known_at` the gate refuses an order that adds on the instrument, and from the first session every decision that reads bars of it not already adjusted for it halts until [#190](https://github.com/l3a0/ithildin-runtime/issues/190) and [#191](https://github.com/l3a0/ithildin-runtime/issues/191) land.

#### Write a covered row

1. Read both vendor lists for the instrument on one day, and write that day as the row's `added`, since the loader measures the 5 sessions from it.
2. Where neither lists a split in the span other than the record's own rows, write a covered row from the span's first session to a last session at least 5 sessions before that day. A list that cannot be read stops the row.
3. Start an instrument's first covered row on its first raw bar, or on the last session of Schwab's import before it, never earlier and never later. A list shows no split only inside the span it reaches, and a split before the first covered row reads as one the platform held no captured bar across, so a later start would let a split's halt end while a book or a cap written before it is on the old scale. `tests/test_data_files.py` pins each instrument's start.

A halt that says the record of splits does not reach a pair of sessions is released by a covered row reaching the later one. A halt on a step of 30 percent down or 42.9 percent up is released by a split row, or by a covered row once both lists show the move was real. On the decision's own session no covered row can reach the pair yet, so a halt there that waits on one stays for that session.

#### Withdraw or correct a row

1. A wrong ratio is a new `split` row on the same instrument and first session, added on a later date.
2. A wrong date is a `withdrawn` row on the old key and a `split` row on the new one.
3. A false split is a `withdrawn` row, added on a later date than the split row it ends, whose `read_from` says why the row was wrong.

A release that changes the file while a Paper sample is open keeps the sample open only once a replay that reads the new file shows the strategy's orders unchanged.

### Count how often quotes would be refused

A decision before the close halts unless its strategy writes, under `[data.quotes]`, the oldest quote it may read and the widest spread. This command counts what each choice would have refused on past sessions, which bounds each limit from below. The bounds from above, and the steps it counts at, are on [#133](https://github.com/l3a0/ithildin-runtime/issues/133), and [When the age is measured](architecture/03-context-and-scope.md#when-the-age-is-measured) says why the age is judged again at the gate.

#### Run the command

It reads a folder that holds a copy of marketlake's lake, so it needs no credential, and it writes nothing but what it prints.

```bash
uv run python -m ithildinruntime.runtime.quote_refusals --lake <folder> --instruments <ticker> <ticker> --from <YYYY-MM-DD> --through <YYYY-MM-DD> [--normal-status <status>]
```

1. The folder needs `manifest.jsonl`, `quarantine.jsonl`, `actions/corporate_actions.jsonl`, each file under `reference/` that the manifest lists, and each instrument's sealed quotes, `quotes/ticker=<ticker>/date=<YYYY-MM-DD>.parquet`, for every session from `--from` to `--through`. Each file must have the SHA-256 the manifest records.
2. `--normal-status` is needed only for a session no row of `data/quote_kinds.csv` covers, and a run without it stops naming the first such session. It is the security status a stand-in row states for each such session. The stand-in also holds a placeholder for the price kind, the last sale's kind, the venues and the volume, so no session it covers is refused for an unstated kind. Once a row covers every session, leave it out.
3. A run reads each session once for each age, so it takes a minute or more.
4. A run that refuses prints one line to stderr, nothing to stdout, and exits nonzero. A session the manifest lists no quotes for, a file the manifest lists and the folder lacks, and a file whose bytes differ from the manifest, each refuse the run, because none is a refusal a decision would meet.

#### Read what it prints

Every table is exploratory, and none holds a price.

1. **What refuses a session at every step** lists the reasons no choice of age or spread lifts, such as a crossed quote or a status that is not normal, or says that none does. A reason a quote gives counts once for each instrument and session it refuses, and each kind of fault of the read once for each session.
2. **Refusals by age** counts the sessions refused at each age, at the decision, at a send 30 seconds after it and at the market-on-close cutoff design.md proposes, close−12. No send has been measured, so a quote refused at a send t seconds after the decision under an age a is refused at the decision under a − t. The cutoff bounds a market-on-close order under caps of close−12. Another order, or one on a simulated account, can meet the gate up to the close, where the gate refuses a quote past its age.
3. **Refusals by spread** counts the sessions refused at each spread, at the decision. In both tables a cell gives every session refused, then those refused for that step's own reason, which can include sessions refused for another reason too, then those reasons by instrument and session, or by session for a fault of the read.
4. **What the quotes read hold** counts, over each quote read at the widest age, stamps later than their capture, sides whose size is zero or missing, and the status and realtime flag of each, which the row of `data/quote_kinds.csv` states.
5. **Ages at the decision** gives the least, median and most age of each instrument's quote and of its capture at close−20.
6. **Faults of the read** lists each fault of the sealed read, by session and by the ages it fell at.

### Measure the capture lag

Every read of quotes keeps only captures that landed 15 seconds before its
decision, the constant `CAPTURE_LAG` in `data/cycles.py`. [Why every read cuts 15 seconds before its decision](architecture/03-context-and-scope.md#why-every-read-cuts-15-seconds-before-its-decision)
says what bounds it. The measure behind it is one day, so measure again after
anything that could slow an upload, such as more tickers captured.

#### Run the command

It is marketlake's, and runs on marketlake's VM, where the bucket's credential
is. It sends one listing request a page and writes nothing.

```bash
python -m lake.live_upload lag --date <YYYY-MM-DD>
```

1. It prints each segment's time from its cycle's start to the bucket, then a
   summary line with the count, the largest, and how many are over 90 seconds.
   That last count is marketlake's own bound, and the steps below read only the
   largest.
2. It reads the last seven days only. A day older than that lists no segments,
   because the bucket expires each segment seven days after upload.

#### Act on the largest

1. **13 seconds or less** needs nothing.
2. **Above 13 seconds** moves the constant up, in a pull request of its own,
   which is reviewed as a raised maximum age is. The new value stays above the
   largest measured with the second S3 rounds away to spare, so a backtest reads
   no row a live listing could not hold.
3. **A value that would stop a close−20 decision reading 15:39's quote** goes to
   the owner, since 15:39's youngest capture is 20.5 seconds old at 15:40.

Nothing runs this on a schedule. [#11](https://github.com/l3a0/ithildin-runtime/issues/11)
is asked for the alarm.

### Set up the test environment

The test environment is the bucket, queues and roles that CI's `aws` job runs the store and queue suites against, in real S3 and SQS. [#78](https://github.com/l3a0/ithildin-runtime/issues/78) holds the plan it was built from, and [What an environment holds](architecture/07-deployment.md#what-an-environment-holds) says what test is for. The configuration sits under `infra/`.

1. `infra/test/bootstrap/` holds what CI needs before it can run: the state bucket, GitHub's OIDC provider, the apply role, the test role, and a budget alarm with the account's alarm topic. The owner applies it.
2. `infra/test/` holds the test bucket and the queues and rules `infra/test/routes.json` lists. `.github/workflows/infra.yml` applies it after each merge that changes `infra/` or the workflow, and on a manual dispatch from `main`. The owner applies it once by hand, before the merge that adds it.

#### Reach the test account

Every command below runs as `OrganizationAccountAccessRole`, which AWS Organizations creates in each account it creates, and which the test bucket's policy admits. AWS refuses to let a root sign-in assume a role, so the role is reached from an IAM user in the management account, `ithildin-owner`.

1. The user holds two grants and no access key. One is `sts:AssumeRole` on the test account's `OrganizationAccountAccessRole`, and the other is AWS's managed `SignInLocalDevelopmentAccess`, which `aws login` needs. The owner sets its console password and MFA device in the IAM console.
2. `aws login --profile ithildin-owner` signs it in from the laptop, through the browser, with no long-lived secret on disk.
3. An AWS CLI profile in `~/.aws/config` assumes the role from it, used with `export AWS_PROFILE=<profile>`.

```ini
[profile <profile>]
role_arn = arn:aws:iam::<test account id>:role/OrganizationAccountAccessRole
source_profile = ithildin-owner
region = us-east-1
```

OpenTofu's AWS provider cannot read an `aws login` session through that profile, so run OpenTofu with the role's short-lived keys in the shell's environment, with `eval "$(aws configure export-credentials --profile <profile> --format env)"`. The keys last an hour, never enter a file, and the AWS CLI commands below work with them too.

#### Set it up, before the merge that adds it

The order matters. Each step needs the one before it.

1. Create the AWS organization, with a new, empty management account that holds nothing else, and the test account as its first member, with centralized root access on. Then, signed in to the management account's console, open Cost Explorer in Billing and Cost Management once, which turns it on for the organization. No API can turn it on, and until it is on AWS refuses the bootstrap's budget with "is a linked account".
2. Set the repository's OIDC subject template to include `repo`, `context` and `job_workflow_ref`, with `gh api -X PUT repos/l3a0/ithildin-runtime/actions/oidc/customization/sub -F use_default=false -f 'include_claim_keys[]=repo' -f 'include_claim_keys[]=context' -f 'include_claim_keys[]=job_workflow_ref'`. Read it back with `gh api repos/l3a0/ithildin-runtime/actions/oidc/customization/sub` and check that `use_immutable_subject` still reads `true`. The board deploys through OIDC too, so dispatch it with `gh workflow run board.yml --repo l3a0/ithildin-runtime` and check that the run deploys.
3. Create the state bucket under a name of the owner's choosing, with `aws s3api create-bucket --bucket <state bucket>`. A new bucket comes encrypted and with public access blocked, and the bootstrap turns its versioning on.
4. Write the backend's file outside the repository, with `mkdir -p ~/.config/ithildin-runtime` and then `printf 'bucket = "%s"\n' <state bucket> > ~/.config/ithildin-runtime/test.tfbackend`.
5. Apply the bootstrap from the build's branch, with `tofu -chdir=infra/test/bootstrap init -backend-config=$HOME/.config/ithildin-runtime/test.tfbackend` and then `tofu -chdir=infra/test/bootstrap apply -var state_bucket=<state bucket>`. The apply prints the two roles' ARNs and the alarm topic's ARN.
6. Subscribe an address to the alarm topic and confirm it, by the steps in [Confirm an alarm email subscription](#confirm-an-alarm-email-subscription), with `<topic ARN>` the alarm topic's ARN and `<region>` `us-east-1`. The address is typed only at the command line, so it enters no tracked file and no OpenTofu state.
7. Apply the environment from the same branch, with `tofu -chdir=infra/test init -backend-config=$HOME/.config/ithildin-runtime/test.tfbackend` and then `tofu -chdir=infra/test apply`.
8. Run the delivery check below. If no event arrives, stop and do not merge, and read the rule's metrics as that check says, because the fix for the likeliest cause has a monthly price the owner decides on. The environment applied so far can stay in place, unused, until the owner decides.
9. Run `scripts/setup-repo.sh l3a0/ithildin-runtime`, which creates the `test` GitHub environment, limited to `main` with no reviewer, and adds the `infra` check to the checks a merge to `main` requires. Until the build's pull request merges, no other pull request can pass that check, because only the build's branch holds the workflow that reports it. Once it merges, an open pull request reports the check after its next push, such as a rebase onto `main`.
10. Set the three repository secrets the workflows read, with `gh secret set AWS_TEST_ROLE_ARN`, `gh secret set AWS_TEST_APPLY_ROLE_ARN` and `gh secret set TF_STATE_BUCKET_TEST`. Each command asks for its value, so none enters the shell's history. `tofu -chdir=infra/test/bootstrap output` prints the two ARNs again.

Applying before the merge means the first `aws` run on `main` finds the environment already there, and a failed delivery reaches the owner before anything merges.

Two checks follow the merge.

1. The merge's own `Infra` run is the apply role's first real plan, because steps 5 and 7 ran as the owner's role. Check that it ends with no changes to apply. A grant the apply role lacks fails that run with AWS's "access denied" naming the action, while the environment steps 5 to 7 applied keeps running. The fix is to add the grant in `infra/test/bootstrap/roles.tf`, merge it, and re-apply the bootstrap from `main` as step 5 says. That merge's own `Infra` run fails the same way, because the grant is not applied yet, so after the bootstrap apply re-run it with `gh workflow run infra.yml --repo l3a0/ithildin-runtime --ref main` and check that its `tofu apply (test)` summary reads "No changes."
2. In a checkout of `main`, run step 5's `init` and then `tofu -chdir=infra/test/bootstrap plan -var state_bucket=<state bucket>`, and expect no changes. Step 5 applied the bootstrap from the build's branch, and no workflow compares it with what merged.

After every apply of the bootstrap, this rollback's and the budget drill's included, check that the alarm topic still has its subscription, with `aws sns list-subscriptions-by-topic --topic-arn <alarm topic ARN>`, and expect one confirmed email subscription. A change that replaces the topic drops it, because the subscription lives outside OpenTofu state. If there is none, repeat step 6.

#### Check that an event reaches its queue

The smoke route's queue receives the event of every key under `runs/ci/*/smoke/`. A write under `runs/` must carry `If-None-Match`, or the bucket refuses it.

1. Write a key with `aws s3api put-object --bucket ithildin-runtime-test --key runs/ci/owner-check-$(date -u +%Y%m%dT%H%M%SZ)/smoke/check.json --body "$(mktemp)" --if-none-match '*'`. The AWS CLI takes a body only from a file, and `mktemp` makes an empty one.
2. Find the queue with `aws sqs get-queue-url --queue-name ithildin-test-smoke`.
3. Receive with `aws sqs receive-message --queue-url <queue URL> --wait-time-seconds 20`, up to three times. One message names the key in its `detail.object.key`.

If no message arrives, wait five minutes after the write, because EventBridge's counts reach CloudWatch a minute or more after the event. Then read the rule's three counts for the last hour, with `aws cloudwatch get-metric-statistics --namespace AWS/Events --dimensions Name=RuleName,Value=$(aws events list-rules --name-prefix ithildin-test-smoke- --query 'Rules[0].Name' --output text) --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ) --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) --period 3600 --statistics Sum --metric-name <name>`, once each with `TriggeredRules`, `Invocations` and `FailedInvocations`. The `list-rules` call reads the smoke rule's deployed name, which is `ithildin-test-` and a hash, and prints `None` when no such rule exists. The `date -v-1H` form is macOS's.

CloudWatch answers a metric with no data with no datapoint rather than with zero.

- **`TriggeredRules` has no datapoint, or zero:** the rule never matched, so the bucket's EventBridge notifications or the route's pattern is at fault, and the queue's encryption is not.
- **`FailedInvocations` counts the write:** EventBridge matched and could not deliver. Delivery to a queue under SQS's own encryption works, as the smoke check showed in [run 45030575106](https://github.com/l3a0/ithildin-runtime/actions/runs/45030575106), so the likelier cause is a queue policy that does not list the rule. Read it with `aws sqs get-queue-attributes --queue-url <queue URL> --attribute-names Policy`, and expect the rule's ARN under `aws:SourceArn`. A merge that changed `infra/test/routes.json` and whose apply has not finished leaves the policy behind.
- **`TriggeredRules` and `Invocations` count the write and `FailedInvocations` has no datapoint:** the event was delivered and the receive missed it. Receive again, and read the queue's `ApproximateNumberOfMessages` with `aws sqs get-queue-attributes`.

The key's current version expires one to two days after the write, and the bucket is empty at least three days after the last write.

#### Clear a lock a failed apply leaves

An apply that fails or is cancelled halfway can leave the state locked, and the next apply then waits five minutes and fails with a message that names the lock's id.

1. Check in [the Infra workflow's runs](https://github.com/l3a0/ithildin-runtime/actions/workflows/infra.yml) that no apply is still running.
2. Clear the lock from the laptop with `tofu -chdir=infra/test force-unlock <lock id>`, after the same `init` as step 7 above.
3. Re-apply `main`'s current configuration with `gh workflow run infra.yml --repo l3a0/ithildin-runtime --ref main`. Re-running an old failed run would apply that run's older commit, and undo what merged after it.

#### Roll back a bad apply

- **`infra/test`:** revert the merge that changed it, in a pull request. The revert's merge starts an apply of the earlier configuration. A rule the revert removes from the `rules` list of `infra/test/routes.json` goes with it, and so do the queues of a reader it removes from the `queues` list, as [Environment rule 10](architecture/07-deployment.md#environment-rule-10-how-an-environments-infrastructure-is-applied) says of test.
- **`infra/test/bootstrap`:** no workflow applies it. Revert the bad change in a pull request, and once it merges, run step 5's `init` and `apply` again from `main`, from the laptop. Rolling back from an older commit alone would leave `main` holding the bad change, and the next apply from `main` would put it back. The revert's own `Infra` run happens under the bad bootstrap, so re-run it afterwards with `gh workflow run infra.yml --repo l3a0/ithildin-runtime --ref main` and check that its `tofu apply (test)` summary reads "No changes."

#### Drill the rollback

Run this once, after the first merge that applies `infra/test`, and record the result on [#78](https://github.com/l3a0/ithildin-runtime/issues/78), as [How a drill counts toward a gate](halts-and-drills.md#how-a-drill-counts-toward-a-gate) says.

1. Merge a pull request that changes the smoke queue's visibility timeout from 30 to 31. It changes the smoke entry of `QUEUES` in `tests/support/routes_file.py`, rewrites `infra/test/routes.json` with `uv run python -m tests.support.routes_file`, and changes the `visibility_timeout == 30` that `infra/test/tests/test.tftest.hcl` pins. A hand edit of the routes file fails the required `test` check, because a test there compares the file with what the generator writes. A new file without the new pin fails `tofu test`, and the required `infra` check then blocks the merge.
2. Check that the merge's `Infra` run ran its `tofu apply (test)` job, and that the job's summary lists `module.routes.aws_sqs_queue.this[…]` as an `update` of `visibility_timeout_seconds`. The summary hides each key, so the reader's name does not appear.
3. Read the value with `aws sqs get-queue-attributes --queue-url $(aws sqs get-queue-url --queue-name ithildin-test-smoke --query QueueUrl --output text) --attribute-names VisibilityTimeout`, and expect 31.
4. Merge the revert of that pull request, and expect step 2's job to have run again and step 3's read to show 30.

#### Drill the budget alarm

Run this once and record the result on [#98](https://github.com/l3a0/ithildin-runtime/issues/98), naming who saw the email and when, such as "the owner". The issue is public, so the record summarises what each command showed and never names the address, the account id, an ARN or a queue URL. The budget counts usage before AWS credits, so credits on the account do not hold the month's spend at zero and keep the drill from starting.

1. Wait until `aws budgets describe-budget --account-id <test account id> --budget-name ithildin-test-monthly` shows `CalculatedSpend.ActualSpend` above 0.01 US dollars.
2. In a checkout of `main`, run step 5's `init`, then `tofu -chdir=infra/test/bootstrap apply -var state_bucket=<state bucket> -var budget_limit=0.01`.
3. Wait up to a day for the email at the confirmed address. If none arrives, read `aws sns list-subscriptions-by-topic --topic-arn <alarm topic ARN>` and the topic's `NumberOfNotificationsFailed` metric in CloudWatch, and record the drill as failed.
4. Whether or not the email arrived, put the limit back with the same `apply` without `-var budget_limit=0.01`, and check the subscription as the setup section says.
5. Read `aws budgets describe-notifications-for-budget --account-id <test account id> --budget-name ithildin-test-monthly` until the notification's state reads `OK`, for up to a day. AWS Budgets refreshes its figures a few times a day. Record the drill as failed while it still reads `ALARM`.

#### Retire the test environment

The bucket module carries OpenTofu's `prevent_destroy`, and other environments will call it, so retiring test removes the bucket from test's state rather than editing the module. In this order:

1. Merge a pull request that removes the `aws` job from `ci.yml` and the `apply` job from `infra.yml`, so nothing writes to the bucket or applies to it again.
2. Wait until the lifecycle has emptied the bucket of its current versions, noncurrent versions and delete markers, which takes at least three days. `aws s3api list-object-versions --bucket ithildin-runtime-test --max-items 1` shows no `Versions` and no `DeleteMarkers` once it has. Until then, wait a day and check again.
3. Remove the bucket's resources from test's state, with `tofu -chdir=infra/test state list` and then `tofu -chdir=infra/test state rm` on each address under `module.bucket`. Then delete the empty bucket with `aws s3api delete-bucket --bucket ithildin-runtime-test`.
4. Destroy the rest of test, the queues and rules, with `tofu -chdir=infra/test destroy`.
5. The state bucket also carries `prevent_destroy`. Remove it from the bootstrap's state with `tofu -chdir=infra/test/bootstrap state rm aws_s3_bucket.state aws_s3_bucket_versioning.state`, and destroy the rest with `tofu -chdir=infra/test/bootstrap destroy -var state_bucket=<state bucket>`. The bootstrap's state stays in the bucket until the next step deletes it.
6. Empty the state bucket of every version and delete it, with `aws s3api delete-objects` on each version `aws s3api list-object-versions` names, and then `aws s3api delete-bucket`.
7. Delete the three secrets with `gh secret delete`, and the `test` GitHub environment under the repository's Settings.
