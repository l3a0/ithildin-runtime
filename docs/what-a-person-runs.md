# What a person runs

This page is for the owner and the operator. It is reference and how-to: the one command line tool, its commands, how a person goes from an alarm to the command that answers it, and the steps the owner takes once per account. The reasoning behind each rule sits in [the architecture](architecture/00-README.md) and [design.md](design.md), and each drill's steps sit in [halts-and-drills.md](halts-and-drills.md).

## Contents

- [What a person runs](#what-a-person-runs)
  - [One command line tool, run off the VM](#one-command-line-tool-run-off-the-vm)
  - [The commands](#the-commands)
  - [From an alarm to a command](#from-an-alarm-to-a-command)
  - [Steps the owner takes once per account](#steps-the-owner-takes-once-per-account)

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

Its writes go through the same journal and storage contract in `core` that the workers use. So a person's entry is checked, chained and refused on a stale number exactly as a worker's is. The tool fetches the journal's end before it writes, since every step fetches and syncs before changing state, as design.md's `## Processes and files` says.

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

Only the owner resumes live trading. The charter makes the operator an agent role, and hard limit 1 keeps agents to simulated and paper brokers.

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
- Journal entries are created, never replaced, by execution, monitoring and the supervisor, and by the `ithildin` tool for the person running it.
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
| Clear a halt, a fired loss stop or the kill switch | The owner or the operator | The owner |
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
| 6 | Resume trading | `ithildin stop clear` | Deletes a fired loss stop's blob |
| 7 | Resume trading | `ithildin switch clear` | Deletes the switch |
| 8 | Recover from a dead host | `ithildin lease clear` | Rewrites a lease another VM holds to clear its holder |
| 9 | Change the release | `ithildin pin set` | Rolls an environment back |
| 10 | Change the release | `ithildin pin unblock` | Removes a block record |

##### Stop trading

These two take a default reason and ask for no confirmation, because stopping should never be slow.

###### `ithildin switch pull --account <a>`

This is one command with a default reason, because stopping should never be slow.

1. The tool writes the kill switch blob, as a new version even where the switch is already set, journals the pull and raises the alarm.
2. The blob's own event wakes execution.
3. Execution reads the switch itself and cancels every resting order in the account, stamped or not, as [The kill switch is a blob](design.md#rule-10-the-kill-switch-is-a-blob) says.
4. The tool reads the journal once, at a deadline set before the [cancel cutoff](architecture/06-runtime.md#the-cancel-cutoff), for execution's entry saying the cancels are done, which names the switch version the pull wrote, so an entry left by an earlier pull never counts.
5. If that entry is not there, the tool tells the person to cancel at the broker's own console.

A pull past that deadline names the console at once.

###### `ithildin strategy off --account <a> --strategy <s>`

This creates the strategy's off switch with a default reason and journals it, because stopping should never be slow.

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

This doc proposes, for [#10](https://github.com/l3a0/ithildin-runtime/issues/10), that an override brings a held cash record into the book, as the case list in design.md's `### Strategies, schedules and decisions` says.

##### Resume trading

These run from the narrowest reach to the widest in the usual case: one halt, one strategy, the fired loss stop on the account's gate, then the kill switch on the whole account. A halt of all trading is account-wide, so clearing that one reaches as far as the last two.

###### `ithildin halt clear <haltid>`

This records that a person has looked at a halt and why it may be left behind. It never reruns the halted decision, and the next decision runs as usual.

On an account that sets `release_needs_blob`, it first writes the release blob, whose body names the halt's id, which only the owner's role can do, and the journal entry names its hash.

This doc proposes it as the confirmation after a stop that [A strategy is judged over a sample](design.md#a-strategy-is-judged-over-a-sample-and-runs-unchanged-through-it) asks for, and as the release of a mismatched request's halt, for [#33](https://github.com/l3a0/ithildin-runtime/issues/33).

This doc also proposes, for [#10](https://github.com/l3a0/ithildin-runtime/issues/10), that a clear brings a held cash record into the book, as the case list in design.md's `### Strategies, schedules and decisions` says.

###### `ithildin strategy on --account <a> --strategy <s>`

This deletes the strategy's off switch and journals it. It refuses two kinds of strategy.

1. A strategy with an unreleased firing of a strategy halt, naming each id. `ithildin halt clear <haltid>` releases each firing, which ends its sort.
2. A strategy retired under design.md's sample rules that has no new registration.

On a live account only the owner may. On shadow and paper the operator may too, which is this doc's reading of the rule for a fired loss stop and the kill switch rather than a rule of its own.

###### `ithildin stop clear --account <a>`

This deletes a fired loss stop's blob and journals the clear, so the gate takes new orders again. On a live account only the owner may, and on shadow and paper the operator may too.

###### `ithildin switch clear --account <a>`

This deletes the switch and journals the clear.

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

Most alarms on shadow and paper need no writing command at all. A missing bar, a gate refusal or a pair leg left open clears itself at the next decision, which plans again from the book, and the person's job is to read the reason and decide whether something larger is wrong. On live, every halt holds the account until the owner clears it.

The writing commands are for the cases the platform cannot resolve alone:

- a book that disagrees with the broker
- an order the platform did not send
- a kill switch
- any halt on live

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

The owner first writes the subscription by hand, as [How the email route is set up](architecture/07-deployment.md#how-the-email-route-is-set-up) says, so the address enters no tracked file.

1. Take the token from the emailed link without opening the link. The link's `Token` value is the token, and its `TopicArn` value names the topic.
2. Run `aws sns confirm-subscription --topic-arn <topic ARN> --token <token> --authenticate-on-unsubscribe true`.
3. Check that `aws sns get-subscription-attributes --subscription-arn <subscription ARN>` reports `ConfirmationWasAuthenticated` as `true`. The drill rows for an email subscription under [Drills with no alarm](halts-and-drills.md#drills-with-no-alarm) check the same.
4. If the subscription was confirmed by the link instead, remove it, subscribe it again, and repeat steps 1 to 3.
