# Architecture, section 8: Crosscutting concepts

This page is section 8 of [the architecture](00-README.md), in the order of the arc42 template. It answers what is stored, where, and in which format, for every component.

## Contents

- [What is stored](#what-is-stored)
  - [Where each entity is kept](#where-each-entity-is-kept)
  - [How recorded blobs are checked](#how-recorded-blobs-are-checked)
  - [File formats and keys](#file-formats-and-keys)

## What is stored

An entity-relationship diagram shows what is kept and how the records point at
each other. A pair of bars at a line's end means exactly one, and a circle with a
crow's foot means zero or more.

The figure below is a C4 code diagram.

```mermaid
erDiagram
    VINTAGE ||--o{ BAR : "holds"
    INSTRUMENT ||--o{ BAR : "is priced by"
    RUN ||--o{ SNAPSHOT : "acts on, one per decision"
    RUN ||--o{ ACCOUNT_FILE : "captures, one per decision"
    SNAPSHOT }o--o{ BAR : "lists"
    RUN ||--o{ JOURNAL_ENTRY : "writes"
    JOURNAL_ENTRY }o--o| ORDER : "records a step of"
    ORDER ||--o{ FILL : "fills by"
    RUN ||--o{ REPORT : "reports each decision"
    RUN ||--o{ SIGNAL : "records"
    RUN ||--o{ DECISION_RESULT : "ends each stage's decision with"
    DECISION_RESULT ||--o{ OUTPUT_FILE : "lists"
    RUN ||--|| PLAN : "lists its decisions in"
    RUN ||--o{ HEARTBEAT : "is watched through"
    RUN ||--o{ RESTART_RECORD : "records each restart in"
    JOURNAL_ENTRY |o--o| OVERRIDE : "names by SHA-256"
    JOURNAL_ENTRY |o--o| RELEASE_BLOB : "names by SHA-256"
    JOURNAL_ENTRY }o--o| HALT_FILE : "names by its id"
    LEASE }o--|| RUN : "is held by a process of"

    SNAPSHOT {
        string key
        string sha256
        datetime decision_at
        bool provisional
        string segments "key, S3 version and SHA-256 of each"
        string bars "each with its vintage_id"
        string quotes "every Quote field"
        string rows "every other row read, as text"
        string faults "each reason the read failed"
        string quotes_read_at "the worker's clock, or null"
        string ledgers "each ledger's SHA-256"
    }
    ACCOUNT_FILE {
        string key
        string sha256
        datetime read_at
        string positions
        string open_orders
        decimal cash
        int replayed_through
    }
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
        decimal close
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
        decimal value
        date first_session
        date last_session
        string release
    }
    DECISION_RESULT {
        string key
        string sha256
        string stage
        datetime decision_at
        string kind
        string halt_reason
        string halt_cause
        string inputs
        string release
        string config_hashes
        datetime stored_at
    }
    OUTPUT_FILE {
        string key
        string sha256
        string stage
        datetime decision_at
        string name
        string inputs
        string release
        string config_hashes
        datetime stored_at
    }
    JOURNAL_ENTRY {
        int seq
        int format
        datetime built_at
        string prev_hash
        string body "holds kind, writer and the kind's fields"
        string kind "read from body"
        string client_order_id "read from body"
        datetime stored_at "S3's metadata"
    }
    ORDER {
        string client_order_id
        string strategy
        string instrument
        decimal qty
        string state
    }
    FILL {
        string fill_id
        string correction_id
        string broker_order_id
        string client_order_id
        string instrument
        decimal qty
        decimal price
        string at_text
        datetime at
        datetime received_at
        string excess_places
        string basis
    }
    PLAN {
        int format
        string deployment
        string run
        string mode
        string release
        string decisions
    }
    HEARTBEAT {
        int format
        string writer
        datetime at
    }
    RESTART_RECORD {
        int format
        string worker
        int restart_count
        int exit_status
        string signal
        string error_tail
    }
    LEASE {
        int format
        string key
        string holder "VM, boot, container, restart count and handler"
        string release
        string run
        string etag
        datetime taken
        datetime expires
        string status "only on a plan's lease"
    }
    OVERRIDE {
        string key
        string sha256
        string account
        string changes
        string evidence
        string built_against
    }
    RELEASE_BLOB {
        string key
        string sha256
        string account
        string halt_id
    }
    HALT_FILE {
        string key
        string account
        string component
        string cause
        string reason
    }
```

### Where each entity is kept

The entities live in eight stores, one of them kept by marketlake, and each keeps its own rule. Five of them are the cylinders in both diagrams under [Data flow](06-runtime.md#data-flow), by the same names.

1. [marketlake](#marketlake-holds-vintage-and-bar) holds `VINTAGE` and `BAR`.
2. [Run records](#run-records) hold `SNAPSHOT`, `ACCOUNT_FILE`, `SIGNAL`, `DECISION_RESULT` and `OUTPUT_FILE`.
3. [The security master](#the-security-master) holds `INSTRUMENT`.
4. [The journal](#the-journal) holds `JOURNAL_ENTRY`.
5. [Decision reports](#decision-reports) hold `RUN` and `REPORT`.
6. [The simulated broker's account](#the-simulated-brokers-account) holds the simulator's orders and positions, in a form this doc proposes for [#144](https://github.com/l3a0/ithildin-runtime/issues/144).
7. [Account controls](#account-controls), the account's own blobs, hold `LEASE`, `OVERRIDE`, `RELEASE_BLOB`, `HALT_FILE`, the kill switch, the strategies' off switches, a fired loss stop and the hard caps.
8. [Split records and saved broker statements](#split-records-and-saved-broker-statements) are recorded facts, whose place is not yet written.

#### marketlake holds `VINTAGE` and `BAR`

Every bar, captured or imported, lives there, with its source and checksum in marketlake's manifest.

A repair can overwrite a partition, whose old copy marketlake's bucket versioning keeps, so [#16](https://github.com/l3a0/ithildin-runtime/issues/16) says lake partitions are not vintages. A bar's `vintage_id` is therefore its partition's path and S3 version id, which together name one copy, and a re-read fetches by that version.

marketlake's lake is private and backed up off the machine, which is what the premise asks of captured bars, and it keeps them out of this public repository. The checksums this repository commits are the partition checksums a snapshot records, for any run a test or a report pins.

Imported history from quantitative-trading arrives with that repository's own checksums, from its [`data/vintages.jsonl`](https://github.com/l3a0/quantitative-trading/blob/19aadbeb9730bf34f724af5688669b6bc208d136/data/vintages.jsonl), so the import can prove it read the recorded bytes. [l3a0/marketlake#938](https://github.com/l3a0/marketlake/issues/938) built the import. Yahoo's two files of GLD and USO are in no repository. marketlake's import accepts each of them only at the SHA-256 that [l3a0/marketlake#1025](https://github.com/l3a0/marketlake/issues/1025) pins, and the files, kept off the lake, stay the record.

#### Run records

Run records serve two purposes. They are how the workers pass work to each other, since each stage's output is the next stage's input, and they are the record of what each decision saw and did.

Run records hold `SNAPSHOT`, `ACCOUNT_FILE`, `SIGNAL`, `DECISION_RESULT` and `OUTPUT_FILE`, as blobs in S3 under the run's prefix, each written once at a key fixed by the run, the stage, the decision and, for an output file, its name.

- A snapshot lists the marketlake bars one decision read, by partition, each with its checksum in the bar's `vintage_id`. A read before sealing is marked provisional, and the snapshot then names each segment opened with its checksum and holds every `Quote` field, every other row read and each reason the read failed.
- A signal record holds a value the alpha model fitted, such as the hedge ratio or the z-score, with the first and last session whose prices it read. A strategy's signals for one decision are the entries of one file, `signals.json`, beside `forecast.json`. The file's header names the snapshot by its key and SHA-256, and the snapshot's bars name their vintage, so each value says which window of which vintage it was fitted on.
- Each blob's header names the blobs it came from, and a halt says why a decision stopped.

**Table:** Which blobs can be regenerated

| Blobs | Can they be regenerated? |
| --- | --- |
| A deterministic component's blobs | Yes, from marketlake, the journal, the hard caps as recover stored them, by this doc's proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10), and the code |
| Data's snapshots, execution's account files, the hard caps as recover read them, by the same proposal, the send stage's broker reads, a takeover's broker order list and the order list that maps an order in doubt | No. They capture what was true at the moment, so all are recorded data |
| The restart records the host's stop handler writes | No, but they are diagnostics rather than recorded data. They are kept as long as the operator sets, as design.md's `### Data has a cost, except the data that cannot be recaptured` says. The price is that a record not yet uploaded when its VM is lost is lost with it |
| A backtest's blobs | Yes. They sit in a memory-backed folder and vanish when it ends, because a backtest can be regenerated |

The blobs also make a question such as "how did the backtest's z-score on a decision differ from the shadow run's" a lookup rather than a recomputation.

#### The security master

The security master holds `INSTRUMENT`. It is a small committed file, `src/ithildinruntime/data/security_master.csv`, that changes only through a pull request. The file ships in the release image, so the digest records the mapping. Each row holds the runtime's id, marketlake's ticker, and the first and last session the mapping holds. Rows only add, an id is never reused or removed, and a rename or a delisting is a new dated row, so a backtest's universe keeps the members that later failed. `tests/test_data_files.py` pins every row by hand, because CI's checkout holds no history to compare with. A decision that reads an instrument on a session no row covers halts. The price is that a renamed ticker needs a release.

#### The journal

The journal holds `JOURNAL_ENTRY`. It is the one numbered log: one per broker account, outside every run, because the book is replayed from it across runs.

Each entry is a blob at a key fixed by the account and its sequence number, and an account's first entry is number 1. It holds these, so the entry itself is the record.

- Its number.
- The time it was built, to the microsecond.
- Its body.
- The hash of the entry before it.

Its own hash is the SHA-256 of those bytes, as [File formats and keys](#file-formats-and-keys) says. `Journal`, in the `journal` package, writes through `BlobStore` for the account the broker adapter names. `ORDER` and `FILL` are read from journal entries rather than kept as records of their own. Each entry is stored before the step it records happens.

Six writers can append.

1. The `ithildin` tool.
2. The supervisor, when it clears a lease or takes one over, and when it journals as lost a capture whose worker's process died holding it, as [A write S3 refuses or does not answer](../design.md#rule-1-a-write-s3-refuses-or-does-not-answer) says.
3. Monitoring.
4. Execution.
5. In a VM image rollback the owner overrides for an emergency, the rollback's credential, for the journal entry of the kill switch pull that rollback makes.
6. In a live drill the owner approved, the approved drill role, for the journal entries of its own kill switch pull and its own clear.

Only execution, monitoring, the supervisor and the `ithildin` tool may import the `journal` package, so the rollback's credential and the approved drill role may write their entries only by running one of them. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) settles which, and how the drill's pull carries its run id and its clear stays bound to the tag of its own pull, as [Rule 10](../design.md#rule-10-the-kill-switch-is-a-blob) requires.

`Journal.record` is the one way any of them adds an entry, as [Nothing is stored that a replay would refuse](05-building-blocks.md#nothing-is-stored-that-a-replay-would-refuse) says. S3's refusal of an existing key is what keeps two of them from taking one number. `record` halts, storing nothing, on an entry the fold refuses or one that does not chain to the stored entry before it, and it never renumbers an entry. Where the key holds the same bytes, the append is the writer's own earlier attempt and is done, and where it holds other bytes, `record` raises `NumberTaken`. That refusal is two writers racing for a number, so it raises none of the alarms on a create-only write whose stored bytes differ. The writer that is refused then views the journal again, decides its step again, and takes the next number. An order's `intent` starts over, its `verdict` or `sending` that lost its number ends the order `abandoned`, and the entry after a broker's answer is appended until it lands, as [Send, once per order](06-runtime.md#send-once-per-order) says.

Who may write which kind is held by two things, and neither is the bucket's grant, which admits a key under `journal/<account>/` whatever the body says. Only `record` adds an entry in this code, and `tests/test_order_sweep.py` shows, in the spellings its tests plant, that only the order path names six of the kinds an order moves by: `intent`, `verdict`, `sending`, `ack`, `in_doubt` and `abandoned`. A `rejected` and an `expired` are recorded by the recover stage too ([#145](https://github.com/l3a0/ithildin-runtime/issues/145)), and a fill by it alone. [What a journal entry's body holds](#what-a-journal-entrys-body-holds-and-how-a-retry-is-recognised) names each kind's writer. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds the question of what a prefix grant cannot limit.

Changing an entry would break the link from the entry after it. Beside that link, the bucket does four things, so an edit cannot hide.

1. It refuses any replacement or delete by every principal, the owner's included.
2. It keeps every version.
3. It locks each entry with S3 Object Lock.
4. It alarms on a delete event.

Before live, the live journal, every override, every release, every halt and every recorded capture are replicated to a bucket in a second AWS account, as [The replica before live](../design.md#rule-1-the-replica-before-live) says.

#### Decision reports

Decision reports hold `RUN` and `REPORT`. A `REPORT` is the report monitoring writes for each decision. A report is computed from the journal and the snapshot, so losing one costs nothing. The run writes it to S3 for the owner to read, not as a record.

#### The simulated broker's account

A simulated account is a log of calls. Each call that changed the account stores one blob before it returns, outside every run, and a read that changed nothing stores none. The calls are numbered, and they are not an event log of the platform's, which [Processes and files](../design.md#processes-and-files) rules out beside the journal. They stand where a broker's own records stand: only the simulated broker's adapter reads them, and no book is replayed from them.

##### What a blob holds

Every process that writes a log draws a name afresh as it starts, 32 hex characters, which is the blob's `writer`. Two processes' calls therefore never have the same bytes.

The figure below is a C4 code diagram.

```mermaid
classDiagram
    direction LR

    class SimCall {
        +int format
        +str account
        +int call
        +str or None writer
        +datetime or None at
        +datetime or None clock
        +SimEvents events
        +SimState after
    }
    class SimEvents {
        +SimEdit[] edits
        +SimOrder[] placed
        +str[] missed
        +SimOrder[] delayed
        +SimClose[] closed
    }
    class SimState {
        +Decimal cash
        +Map~InstrumentId, Decimal~ positions
        +SimOrder[] open_orders
        +SimOrder[] delayed
    }
    class SimOrder {
        +str or None id
        +str or None client_order_id
        +InstrumentId instrument
        +Decimal qty
        +Decimal filled
        +OrderType type
        +TimeInForce time_in_force
        +BrokerState state
        +datetime submitted_at
        +date session
        +bool read_close
    }
    class SimClose {
        +str id
        +BrokerState state
        +datetime at
        +Fill or None fill
        +BlobRef or None priced_from
    }
    class SimEdit {
        +int count
        +Decimal or None cash
        +InstrumentId or None instrument
        +Decimal or None shares
        +SimOrder or None order
    }
    SimCall o-- SimEvents
    SimCall o-- SimState
    SimEvents o-- SimEdit
    SimEvents o-- SimOrder
    SimEvents o-- SimClose
    SimState o-- SimOrder
```

1. `events` is what the call did: what a fault had someone do by hand, the orders the account took, the ones that arrived too late to take, the ones now on their way, and each order that ended. `priced_from` names the stored snapshot that gave a fill its price.
2. `at` is the account's time, which never goes back, and `clock` the clock's own reading. They differ only on a cancel whose clock read earlier than the account's time, or no time.
3. `after` is the account then. It holds no order that ended before, because a blob that carried every order would cost the square of the run's length.
4. Call 1 opens the account flat with its cash, and names no writer and no time. Opening it again is the same bytes and is done. Nothing else opens an account, so a lost log never reads as a new one.

Format 1 stays readable in every later release, because a shadow account's log outlives them. `tests/fixtures/simulated` holds a committed log.

##### How a call reads and writes the log

Every call first reads the log forward from the newest call it knows, and takes in each blob that follows from the one before it, whoever wrote it. So two instances take turns on one account as they would on a broker's. A process's first call lists the whole log.

**Table:** What a call does with what it finds in the log

| What it finds | What the call does |
| --- | --- |
| A blob whose events, applied to the account before it, give the account it holds | Takes it in |
| A gap, a blob that is not canonical, or one that is not the call before it with its events applied | Halts, and so does every later call |
| Its own create refused, or with no answer | Raises, and nothing this process remembers changes. The next call finds whether the blob landed |
| Its number holding these same bytes | The create is done, since its first answer was lost |
| Its number holding another call, or a store that says the create raced another and did not land | Starts over on what it then reads, up to 100 times, and then raises with nothing changed |

**A lost newest call is not seen here.** The log then reads as the account before it, and gives that call's order id to the next order. Two things find it: recover's comparison with the journal, which is [#145](https://github.com/l3a0/ithildin-runtime/issues/145)'s, and the journal's refusal of an order id it already holds. The bucket refuses a delete under `sim/`.

The simulated broker's adapter reads and writes it through a `BlobStore` the entry point hands it, so execution's own outputs are the same whichever broker it holds. Backtest and shadow use the same form, so an order still resting when a shadow run ends is there in the next run, or on a new VM. Each simulated account has its own lease and kill switch, so a backtest never shares the paper account's.

That the account is kept in this form is this doc's proposal for [#144](https://github.com/l3a0/ithildin-runtime/issues/144), in [Proposals awaiting a ruling](09-decisions.md#proposals-awaiting-a-ruling). Its prices are four.

1. Each call costs one read for the next number. On S3 a key that is not there reads so only to a role that may list the bucket with no limit on the prefix. To any other role it reads as access denied, and every call after a process's first fails.
2. Two instances whose clocks differ by more than the time between their calls halt on the clock. One VM has one clock. Two VMs meet it, which the clock's bound has to hold below.
3. Nothing here keeps two processes from acting at once. The account's lease does, which is [#148](https://github.com/l3a0/ithildin-runtime/issues/148)'s.
4. A process that starts reads the whole log, in turn, and reports nothing to the receive loop while it does. Two orders a decision leave three blobs, as `tests/test_simulated_log.py` counts over 100 decisions, so the 1,461 decisions of acceptance test 1 leave at most 4,384. The journal's cold read has the same shape and more entries, and [#154](https://github.com/l3a0/ithildin-runtime/issues/154) holds both.

#### Account controls

Outside every run, account controls, the account's own blobs, hold `LEASE`, `OVERRIDE`, `RELEASE_BLOB`, `HALT_FILE`, the kill switch, each strategy's off switch, a fired loss stop and the hard caps blob. They sit at keys fixed by the account or the deployment under prefixes of their own, with the rules [The kill switch and the hard caps are blobs](07-deployment.md#rule-4-the-kill-switch-and-the-hard-caps-are-blobs) and [The runtime's credential does only what the rules need](07-deployment.md#rule-5-the-runtimes-credential-does-only-what-the-rules-need) give.

#### Where the plan, heartbeats and restart records sit, and what the lifecycle expires

`PLAN` sits under the run's prefix. `HEARTBEAT` and `RESTART_RECORD` sit under prefixes of their own, keyed by writer and time and by worker and restart count.

The bucket's lifecycle expires heartbeats and the noncurrent versions a lease's rewrites leave under `leases/`, and nothing else. A snapshot a decision read sits under its run's prefix with data's other outputs and is recorded data, so no lifecycle rule may match it, as design.md's `### Data has a cost, except the data that cannot be recaptured` says. This holds for the buckets of shadow, paper and live. The test bucket's lifecycle expires everything, because its blobs are regenerable.

#### Split records and saved broker statements

Split records and saved broker statements are recorded facts. Wherever they sit, nothing may overwrite or silently drop one, every read verifies its checksum, and a copy leaves the machine from the first session. Where that is, is not yet written. [#46](https://github.com/l3a0/ithildin-runtime/issues/46) starts the one place for split records from marketlake's ledger and asks [#5](https://github.com/l3a0/ithildin-runtime/issues/5) to close the ways that ledger can lose a record, or say why it cannot be the place. One way [#46](https://github.com/l3a0/ithildin-runtime/issues/46) does not list is the `actions` lifecycle rule in marketlake's `infra/live/bucket.tf`, which expires a replaced version of the ledger after 30 days. No issue holds the place for saved broker statements yet.

### How recorded blobs are checked

S3 checks every blob at rest on its own schedule and repairs corruption from redundant copies, so no job of the platform's reads blobs back. Three checks remain, as [Faults are expected](../design.md#faults-are-expected-and-the-platform-checks-itself-while-it-runs) says.

1. Every write through `BlobStore` sends its SHA-256, and S3 refuses an upload whose bytes do not match it.
2. Before live, a recorded blob not found in the second account's bucket within a written bound raises an alarm that reaches the owner.
3. A restore drill restores to a scratch place and checks counts and checksums, on a schedule whose maximum interval sits beside the code. A mismatch it finds pages at once, naming the key and both hashes or the prefix and both counts. A blob missing because of a removal under a legal duty to delete, such as a vendor's license terms, which appends its own record, is expected and no mismatch. Monitoring also pages when its last passing run is older than that interval, and a run that fails before it compares is no passing run.

This doc proposes, for [#11](https://github.com/l3a0/ithildin-runtime/issues/11), that it holds the restore drill and the replication alarm, their routes to the owner and their drills, which its body does not yet name. Each drill shows its alarm firing and leaves nothing in live's journal, captures or replica that a replay reads.

### File formats and keys

Two rules lean on exact bytes.

1. A deterministic stage that is run again must write the same bytes, because S3 refuses the second write and the worker then compares SHA-256s, alarming on a difference.
2. The journal chains each entry to the hash of the one before it.

Both fail if the same values can be written two ways, such as a different key order, a float printed with one more digit, or a time in another zone. So `core` defines the file form of every type, which [#22](https://github.com/l3a0/ithildin-runtime/issues/22) builds as this subsection describes.

#### Who writes and reads a blob

**One codec in `core` writes and reads every blob.** `core.encode` turns an object into bytes and `core.decode` turns bytes of a named kind back into an object. No worker, tool or test calls a JSON library itself. Two workers that each followed the standard library would differ: Python's `json` sorts keys by code point where RFC 8785, the JSON Canonicalization Scheme, sorts by UTF-16 units, and `Decimal(0.1)` carries fifty-five digits of the float behind it. Each field's type in `core` declares how the codec writes it, and `core`'s storage contract holds one key builder for each row of the key table below, so no module puts a key together from strings.

**The codec writes its bytes with a published RFC 8785 library rather than an encoder of its own.** The library is Trail of Bits' [`rfc8785`](https://github.com/trailofbits/rfc8785.py), a pure-Python implementation with no dependencies of its own, pinned at an exact version, 0.1.4, its latest release on 2026-11-08. A run of that version on Python 3.12 showed that it sorts keys by UTF-16 units, and that it refuses a `Decimal`, a NaN, an infinity and an integer whose magnitude is 2⁵³ or more, as [its source](https://github.com/trailofbits/rfc8785.py/blob/v0.1.4/src/rfc8785/_impl.py) also shows. It does print a float, so `core.encode` turns each decimal and each time into its string, and refuses a float before the library sees the value. The library only encodes, so `core.decode` parses with Python's `json` and checks the bytes by encoding what it parsed again.

The figure below is a component-level flowchart of the codec's two paths, in order.

```mermaid
flowchart TB
    subgraph writing["Writing a blob"]
        direction TB
        w1["A worker, tool or test hands core.encode an object"]
        w2["core.encode turns each decimal and each time into its string, and refuses a float"]
        w3["The rfc8785 library writes the bytes"]
        w1 --> w2 --> w3
    end
    subgraph reading["Reading a blob"]
        direction TB
        r1["core.decode parses the bytes with Python's json"]
        r2["core.decode encodes what it parsed again and compares the bytes"]
        r1 --> r2
    end
    writing ~~~ reading
```

The price is a third-party library on the path that writes the journal, where a change in its output would break the hash chain. A test encodes committed fixture values and compares the bytes, so an upgrade that changes them fails CI before it ships.

#### What a blob's bytes look like

**Every blob is canonical JSON.** The encoding follows RFC 8785, the JSON Canonicalization Scheme, for encoding, whitespace and key order, and adds rules of its own for numbers, times and empty fields.

1. UTF-8, with no whitespace outside strings, as RFC 8785 requires.
2. Object keys sorted by their UTF-16 code units, as RFC 8785 sorts them.
3. No JSON floats. RFC 8785 prints a number in its shortest double form, which cannot hold most decimal prices exactly, and it recommends a string for a number that needs more precision than a double holds. So every decimal quantity, such as a price, a share count, a dollar amount, a hedge ratio or a z-score, is a JSON string in plain decimal notation, such as `"123.4500"`. Integers below 2⁵³, such as a sequence number or a bar's volume, are JSON integers. Share counts are decimals, because a forecast's basket holds a fraction of a share of GLD, and test 1's account holds fractions too.
4. Every time the platform writes is UTC, in RFC 3339 with a `Z` and six fractional digits, truncated, such as `"2026-11-02T20:40:01.123456Z"`. A decision's time is UTC too, such as the 15:40 decision on 2026-11-02 at `"2026-11-02T20:40:00.000000Z"`. A session date is the exchange's trading date, such as `"2026-11-02"`, because it names a session rather than an instant.
5. A field with no value is `null`, never left out, so a reader can tell an empty field from a format that lacks it. A list keeps its order when the order means something, such as a plan's decisions, and is sorted by its key when it does not, such as a header's inputs, which is this doc's rule rather than RFC 8785's.

#### Why not another format

**Only canonical JSON passes both tests.** The first test is whether the same values give the same bytes across releases and languages, which both rules that lean on exact bytes need. A second test is whether a person can read a blob straight from S3.

**Table:** The formats weighed for the blobs, against the two tests

| Format | Same bytes for the same values | Readable from S3 |
| --- | --- | --- |
| Parquet | No. Its writer records its own version in each file, in [the footer's `created_by` field](https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift), so the same values change bytes across releases | No, it is binary |
| Protocol Buffers | No. [Its documentation](https://protobuf.dev/programming-guides/serialization-not-canonical/) says its serialization "is not (and cannot be) canonical" | No, it is binary |
| CBOR with [RFC 8949's deterministic encoding](https://www.rfc-editor.org/rfc/rfc8949#section-4.2) | Yes, with rules of the platform's own for decimals and times, as JSON needs too. RFC 8949 leaves the deterministic form of a decimal fraction to the application, and [the IETF's common profile](https://datatracker.ietf.org/doc/draft-ietf-cbor-cde/) expired as a draft | No, it is binary |
| Canonical JSON after RFC 8785, with the five rules above | Yes | Yes |

#### How a value received from outside is kept

**A value received from outside is kept exactly as it arrived.** A fill's quantity and price, a position, an open order's quantity, and marketlake's bar and quote values are stored as the decimal string the broker or marketlake sent, never passed through a float and never rounded, and so is a time received from outside, with its UTC instant written beside it.

The journal and the snapshot are the only records of what the broker and the market said, so a rounded copy would lose a value that cannot be recaptured: at four places, a leftover position of `0.00004` would record as zero, and the book replayed from rounded fills would drift from the broker's. A received value with more places than its field declares is flagged in the record, never rounded.

marketlake stores prices as 64-bit floats, so for its values "as sent" means the float's shortest round-trip text, `Decimal(repr(x))`, through one converter in `core` that never rounds. The converter refuses, rather than rewrites, a string in any form but ASCII digits with an optional minus, fraction and exponent, and the codec refuses a decimal past a size limit a test pins, so a hostile value cannot make a writer allocate gigabytes. A refused value halts its read, and the raw bytes beside it keep what arrived. `core.to_decimal` below is for computed values only, and a test pins that a five-place price and `0.00004` survive the round trip.

A received body is stored whole and unchanged, in a form canonical JSON can hold, a body that is not valid UTF-8 included. It is kept as exact bytes in one of two forms, with a field naming which.

1. A body that is valid UTF-8 is stored as text, which canonical JSON returns byte for byte.
2. Any other body is stored as base64.

A body that cannot be stored halts, and is held as a capture under [Rule 1: a write S3 refuses or does not answer](../design.md#rule-1-a-write-s3-refuses-or-does-not-answer).

#### Which clock a schedule follows, and how it becomes UTC

**Two rules are stated on the exchange's clock, and resolved to UTC once.** They follow New York's clock because the market does.

1. The host's timer, which starts the run before the session and stops it after the close.
2. The heartbeat check's cron expression.

A rule written in UTC would move by an hour against the close each time the clocks change. So each is written as the exchange's local time, and one calendar function in `core` turns it into a UTC instant for each session.

**The calendar function takes the trading sessions as data.** `core` imports no calendar library, because the usual one, `exchange_calendars` 4.13.2, would pull pandas and numpy into every worker. The table of sessions and their closes is a committed data file, checked by its SHA-256, which a script regenerates from `exchange_calendars` at a pinned version. An early close is the case that matters: on 2026-11-27 the close is 18:00 UTC, three hours before a normal day's, so close−20 falls at 17:40 UTC that day and at 20:40 UTC on 2026-11-30.

Five rules hold the table.

1. A session missing from the table halts.
2. The table runs from the first session any committed backtest reads through the end of the next calendar year.
3. The table names the range it covers, and a date outside it halts, since a missing input stops trading (hard limit 3).
4. The plan builder reports how many sessions the table has left, so an alarm can fire before a run meets its end. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) sets that bound and builds the alarm.
5. A closure announced after the table was cut reads as a session with no data, which halts, since a missing input stops trading (hard limit 3).

A time stated as minutes before the close resolves through the same function from that session's close. Two such times are a strategy's close−20 decision and the market-on-close cutoff, which is close−12 in the hard caps this doc recommends, under Alpaca's close−10. The plan, every key, every timestamp the platform writes and every comparison hold only that instant.

The promotion job's schedule is a GitHub Actions cron, which runs in UTC, so it runs at 22:00 UTC, after the close in both summer and winter.

#### How a computed value is rounded

**A value the platform computes is rounded once, in one place.** The types in `core` hold `Decimal`.

- A statistic is converted once, at the stage's output, by `core.to_decimal`, which rounds it half to even to the field's declared places. One computed in floats is taken in the float's shortest form first. The alpha model computes its hedge ratio and z-score in `Decimal` at 28 digits, inside a context of its own, and decides on the unrounded values.
- Portfolio construction rounds each leg's shares toward zero, to the account's `share_step` for a target and to nine places for what was wanted. It is one division of exact products, so a target exactly on a step is kept, as [How a target is sized](05-building-blocks.md#how-a-target-is-sized) says.
- The risk gate, the order manager, `Journal.replay` and reconciliation compute in `Decimal` throughout, and the quantity journaled for an order is the exact string the broker adapter sends and the gate checks.

A `dict` field's values take the declared places of their value type, such as a share count in `TargetPortfolio.shares`. A field's declared places never shrink in a later release.

The price is that a value computed in floats that falls exactly on a rounding boundary can round differently under another release's numeric library. A run never mixes releases, because a worker on another release halts with "release changed", so the difference can appear only between runs, where the header's `release` explains it.

#### What an output file holds

**Every output file has a header and a body.** Each output file is one object with two members, `header`, the `BlobHeader`, and `body`. The body holds two fields: `entries`, a list of the stage's own kind of record, and `trade_nothing`, the mark that means hold every position and send no order. Portfolio construction halts on an input that carries it, because a target portfolio has no form for that. A body that carries the mark holds no entries, and the codec refuses one that holds both. A reader halts on a body with no entries and no mark, as [An absent, stale or empty input halts](../design.md#rule-6-an-absent-stale-or-empty-input-halts) says. The header's `format` is a number per kind, raised when that kind's fields change. Of the types in `core`, a plan, a lease, a heartbeat, a restart record and a journal entry are each stored at a key of their own rather than as a stage's output file, and each carries a `format` field of its own instead. A `HaltRecord` travels inside a `DecisionResult`, whose header names its format. Any other blob stored at a key of its own, such as a book snapshot, a halt held in a folder, an override or a release, gets a format from the issue that defines it, because the data engineer in [the charter](../team-charter.md#data-engineer) vetoes a recorded format with no schema identifier. [A rollback is a pin to an earlier digest](07-deployment.md#rule-6-a-rollback-is-a-pin-to-an-earlier-digest) requires that each release writes only formats the release before it can read. Each hash is lowercase hex SHA-256, and each writer sends it as the upload's SHA-256 checksum, so S3 checks it before it stores the blob.

#### The hard caps blob

Each account's hard caps are one blob at `accounts/<account>/caps.json`, which a person writes by hand. So it is read by `core.parse_caps` rather than the codec, which refuses bytes that are not canonical, and spaces and any order of fields are allowed. The figures here are made up.

```json
{"format": 1, "account": "paper", "limits": {"max_order_qty": {"GLD": "10"}, "max_position": {"GLD": "5"},
  "max_gross": "1000", "max_open_orders": 4, "loss_stop": "50", "moc_cutoff": 15,
  "share": "0.02", "stress_move": "0.2", "stress_low": "0.1", "stress_high": "0.6",
  "stress_vintage": "<64 hex digits>", "stress_through": "2026-11-20", "stress_max_age_days": 365, "share_step": "1"}}
```

**Table:** Each field of `limits`, its form and what it holds

| Field | Form | Holds |
| --- | --- | --- |
| `max_order_qty`, `max_position` | An object of one decimal string per instrument, the same instruments in both, each named in at most 80 printable characters | Shares, the first at or above the second |
| `max_gross`, `loss_stop` | A decimal string | Dollars. The second is above zero |
| `max_open_orders` | A JSON integer | Orders |
| `moc_cutoff` | A JSON integer | Minutes before the close |
| `share` | A decimal string | The share of V one stranded leg may lose, strictly between 0 and 1 |
| `stress_move` | A decimal string | The stress move, as a fraction, at or above the one its record holds |
| `stress_low`, `stress_high` | Decimal strings | The sane range of the stress move, both ends allowed |
| `stress_vintage` | 64 lower case hex digits | The SHA-256 of the vintage the stress move was measured over, which names [its record](#the-stress-moves-record) |
| `stress_through` | `YYYY-MM-DD` | That vintage's last session, as the record holds it |
| `stress_max_age_days` | A JSON integer from 1 to 365 | How many days past that session a decision may still use it |
| `share_step` | One of the ten decimal strings `1`, `0.1` and so on to `0.000000001` | The step a share count moves in on the account, which is the broker's rule and no limit. Portfolio construction rounds each target to it |

A decimal string is plain digits with an optional minus and fraction, with no exponent. An integer is below 2**53 in size. The blob is at most 65,536 bytes. Any other section may sit beside `limits`, and this reader reads none of them. [#11](https://github.com/l3a0/ithildin-runtime/issues/11) reads `holds`, the causes that hold the account, and nothing reads `reasons`, why each value sits where it does.

A blob that cannot be read halts every order, naming what is wrong: bytes that are not JSON, a key given twice, another `format`, another account's blob, a missing or unknown field of `limits`, or a value out of its range. The halt shows little of a long value, and nothing of the account the blob names. A release rolled back past a field a newer one added halts on it too, so trading stops until the field goes.

#### The stress move's record

Each measurement of a strategy's stress move is one `[[record]]` table of `core/stress_moves.toml`, a tracked file the release carries. A record is never removed or changed, so `tests/test_stress.py` pins every one by hand. One found wrong gains `withdrawn` with the reason, and no longer counts toward the largest m.

A record is named by its strategy and its vintage, which is the SHA-256 of the bars the measurement read. The hash is over one UTF-8 line for each bar, in order of instrument and then of session. A line holds the instrument, the session, the partition's key and the partition's SHA-256, joined by tabs and ended by a newline. The bucket's version is left out, so a run on the bucket and one on a copy agree. The lines name the legs, so the vintage fixes them. The record keeps the hash and not the lines, so it can confirm which partitions a run read and cannot list them. The record holds no price, because the source's terms keep its bytes out of this repository.

**Table:** Each key of a record, what it holds and who reads it

| Key | Holds | Read by |
| --- | --- | --- |
| `strategy`, `instruments` | The strategy's name, and its legs in order of name | The hold |
| `vintage` | The vintage's SHA-256 | The hold |
| `through` | The last session, a date | The hold |
| `stress_move` | m, a decimal string of eight places | The hold and the command |
| `leg`, `moved_from`, `moved_to` | The move that set m | The hold reads `leg`, and the command all three |
| `one_session`, `one_session_leg`, `one_session_on` | The largest one-session move | A person |
| `series` | A table for each series of each leg: `instrument`, `first`, `last`, `basis`, and `source` and `source_as_of` where the bars have them | A person, and the reader its `instrument` and `last` |
| `left_out` | A table for each move left out: `instrument`, `sessions` (1 or 2), `moved_from`, `moved_to` | A person |
| `flagged` | A table for each flag the read raised: `instrument`, `session`, `field`, `rule`, and `what`, which the person who commits the record fills in | A person |
| `manifest`, `measured_on` | The SHA-256 of the manifest the run read, and the run's date | A person |
| `withdrawn` | The reason, on a record found wrong, and absent on any other | The hold |

The reader refuses each of these as a halt naming the file, and the record and the key where the fault is in one. No halt names the folder the file sits in.

1. A file that is absent or cannot be read, such as one that is not TOML.
2. A key it does not know, or one left out.
3. A value of another form, such as an m at or below zero or a flag with an empty `what`.
4. A `leg` that is none of the record's instruments, because the hold counts a record by its leg.
5. A `through` that is not the last session of each leg's `series`, because the stress move's age counts from it.
6. Two records of one strategy and vintage.

The risk and portfolio workers' entry points take the file's path and hand on the records or the halt. The price is that a partition marketlake rewrote with the same close makes a new vintage, whose record has to be measured again.

[How the stress move is measured](05-building-blocks.md#how-the-stress-move-is-measured) and [How the caps are held to the stress move's record](05-building-blocks.md#how-the-caps-are-held-to-the-stress-moves-record) say what writes a record and what reads it.

#### How a journal entry is hashed and checked

**A journal entry hashes its whole blob.** Each entry is one blob holding `seq`, `format`, `built_at`, `prev_hash` and `body`. Its hash is the SHA-256 of those bytes exactly as stored, which the next entry carries as its `prev_hash`, and the first entry of an account carries sixty-four zeros. A hash of the body alone would leave an entry's number and its link outside what the next entry vouches for.

The figure below is a C4 code diagram.

```mermaid
flowchart LR
    e1["An account's first entry carries sixty-four zeros as its prev_hash"]
    e2["Entry 2 carries the SHA-256 of entry 1's stored bytes as its prev_hash"]
    e3["Entry 3 carries the SHA-256 of entry 2's stored bytes as its prev_hash"]
    e1 --> e2 --> e3
```

A blob cannot hold its own hash, so `JournalEntry.hash()` computes it as the SHA-256 of the entry's canonical encoding, and it is the one function that does. A reader trusts that hash only because `core.decode` refuses bytes that are not the canonical encoding of what it parsed, so the hash of a decoded entry is the hash of the bytes S3 returned. A reader also checks three things, and halts otherwise.

1. That those bytes are the canonical encoding of what it parsed.
2. That each key's number equals the `seq` inside it.
3. That it knows the entry's `format`.

Beside those three, the reader halts on a `prev_hash` that is not the hash of the entry before it and on a gap in the numbers, as [Rule 8](../design.md#rule-8-the-journal-is-the-one-numbered-log) says, on a key listed at or below the entry last read, and on any key under the journal's prefix that is not an entry's. A number a listing left out is a gap where its own key holds nothing, because a folder listed beside a writer can miss an entry. It skips `journal/<account>/checkpoints/`, where the checkpoints this doc proposes for [#154](https://github.com/l3a0/ithildin-runtime/issues/154) sit.

Every journal format ever written stays readable by every later release, because the book can always be replayed from the first entry, even when a rebuild starts from a book snapshot, and each format has a committed fixture entry and a test that replays it.

#### What a journal entry's body holds, and how a retry is recognised

The `body` holds a `kind`, the `writer` that built the entry, and that kind's fields. It is the record's own canonical JSON object with `kind` and `writer` added, so each decimal, time and byte string has the codec's one form.

##### The kinds of entry

**Table:** The kinds built so far, who writes each, and what it records

| Kind | Fields | Written by | What it records |
| --- | --- | --- | --- |
| `opening` | `account`, `cash` | Execution for a backtest, by [#146](https://github.com/l3a0/ithildin-runtime/issues/146). A person on shadow and paper | The account's opening cash, flat. An account's entry 1 and no other |
| `read` | `stage`, `decision_at`, `ref`, `at` | Execution | The stored file that holds a broker read, named before any value in it is used, and when the read was made |
| `decision` | `deployment`, `run`, `decision_at`, `targets`, `orders`, `off`, `inputs`, `read`, `synced` | Execution | Every strategy's whole target, the planned orders in order, each strategy that was off, each input by name, and the entries it was planned on and synced through |
| `intent` | `decision`, `order` | Execution's order path | One planned order, under its `decision` entry's number |
| `verdict` | `client_order_id`, `verdict`, `now`, `stop`, `close_entry`, `book`, `caps`, `caps_sha256`, `snapshot`, `constraints`, `valuation` | Execution's order path | The gate's answer, with the stress move it used, and all it was handed but the order, which the `intent` holds, and the stress records |
| `abandoned` | `client_order_id`, `reason` | Execution's order path, and for an order that is `Sending` only the writer of its `sending` | Nothing was sent, and why |
| `sending` | `client_order_id` | Execution's order path | The order may leave now |
| `ack` | `client_order_id`, `broker_order_id`, `raw`, `cut`, `received_at` | Execution's order path | The broker has the order, with the bytes it sent |
| `rejected` | `client_order_id`, `reason`, `raw`, `cut`, `received_at` | Execution | The broker refused the order |
| `in_doubt` | `client_order_id`, `reason`, `raw`, `cut`, `received_at` | Execution's order path | No answer that settles the order, or one this release does not read. `raw` is null where no plain bytes came |
| `fill` | `fill`, `read` | Execution | One fill, and the number of the `read` entry whose file holds it |
| `disputed_fill` | `fill`, `read` | Execution | A repeat of a fill whose content differs, kept beside the first |
| `expired` | `client_order_id`, `filled`, `raw`, `cut`, `received_at` | Execution | The broker says the order ended with a part unfilled |

A `verdict` keeps the caps as the canonical bytes of the parsed `HardCaps`, with the SHA-256 of the blob they came from, because the owner rewrites that blob. The fold never decodes them, so a cap added later leaves old entries readable, and the blob's other sections stay out of entries nobody can delete.

The other kinds, such as `cancel`, `sort`, `sort_done`, `override`, `clear`, `switch_pull` and the outside and unmatched entries, each arrive with the part that writes it. A release reads a kind one release before any release writes it, so a pin back to the release before still reads what the journal holds, as [Each blob names what made it](../design.md#rule-5-each-blob-names-what-made-it) asks of a format.

##### What holds a kind still

1. **A kind's fields never change**, and neither do those of a `core` type inside it, an enumeration's members or a check its constructor makes, because any of them can make every old entry unreadable. The bounds below, the checks on an entry and the stamp's own rules run on every old entry too, so each may be loosened and never tightened. A changed shape is a new kind under a new name, and every kind ever written stays readable. Until the first journal outside a test is opened, a change rewrites that kind's committed entry instead. `tests/fixtures/orders` holds a committed journal of every kind.
2. **A body of an unknown kind, or whose fields are not its kind's, halts** with the entry's number and the kind. Skipping one would build a book on part of the record.
3. **Bounds hold what an entry keeps.** `raw` has at most 65,536 bytes. A longer answer keeps its first part with `cut` true, and the order path journals one to a submit as `in_doubt`. A broker's order id or fill id is one to 128 letters, digits, `:`, `_` and `-`. A `fill`'s price is above zero and its shares are not zero, and neither are a planned order's. A `disputed_fill` moves nothing, so it keeps the price and the shares that came, such as a fill sent again for zero shares. A blob is named by a key and a SHA-256 of 64 lower case hex digits, and a `verdict`'s caps are not empty, because an entry that names nothing can never be run again or repaired.

##### What the writer is for

The `writer` is 32 lower case hex characters, drawn afresh at each process start in every mode, for each `Journal` that records. It names no host, path, user or account. So two processes, and two `Journal`s of one process, never build the same bytes. Without it, two under the history clock did, and both held one `sending` entry's acknowledgement and called the broker. Test 1 alone passes fixed ones, one for each `Journal`, because it compares bytes. The test that compares two instances byte for byte leaves the `writer` out and computes the hashes again around it, as [How a component learns of a change](../design.md#how-a-component-learns-of-a-change) says.

##### How a retry is recognised

A writer builds an entry once and resends the same bytes on a retry, so a live process recognises its own earlier attempt by hash. A process that restarted builds new bytes, so before it appends it checks the field that names the step against the journal instead.

**Table:** The field a restarted writer checks against the journal before it appends, by kind of entry

| Kind of entry | The field it checks |
| --- | --- |
| `opening` | The entry's number, since an account's entry 1 is its only one |
| `read` | The stored file's key and SHA-256 |
| `decision` | The decision time |
| `intent`, `verdict`, `abandoned`, `sending`, `ack`, `rejected`, `in_doubt` and `expired` | The client order id, with the state the view shows the order in |
| `cancel` | The broker's order id and what tells this cancel from an earlier one on the same order, because an order the platform did not stamp has no client order id and one order can carry several cancels. [#10](https://github.com/l3a0/ithildin-runtime/issues/10) names that key. A `cancel` entry with no journaled answer is a cancel whose answer is lost, so a restarted writer looks it up before any new cancel, as rule 6 of [Faults are expected](../design.md#faults-are-expected-and-the-platform-checks-itself-while-it-runs) says |
| `fill` | The broker's fill id, with a repeat of the same content skipped and a differing one journaled as `disputed_fill`, after which the book halts |
| `disputed_fill` | The broker's fill id with its order, instrument, shares, price and time |
| `outside_fill` | The broker's fill id and correction identifier together, with an identical repeat skipped and a differing one journaled, which halts |
| `outside_order` | The broker's order id |
| `unmatched_fill` and `unmatched_order` | The broker's fill or order id |
| An outside entry with no order or fill | The broker's own record id |
| `sort` and `sort_done` | The off switch's or halt's id |
| `override` | The override blob's SHA-256 |
| `clear` | The halt's id with the release hash, counting only a valid release as already done |
| A halt's own entry | The firing's id |

A switch already pulled makes `switch_pull` say so rather than journal it twice.

#### Where each blob is stored

Every key below sits in its environment's bucket, as [Four environments](07-deployment.md#four-environments) says. Deployments and accounts appear by the alias the release's table gives them, never by a broker's account number.

A create-only write that S3 refuses, whose stored SHA-256 differs from the writer's, alarms, as [Every alarm and its drill](../halts-and-drills.md#every-alarm-and-its-drill) lists. For a capturing stage, data's snapshot and execution's recover stage, the differing capture is also stored at the next attempt number, by `core`'s key builder for that row, and named in the decision result, so a second read of a source that may not come again is kept rather than discarded.

**Table:** The key each store writes under

| Store | Key |
| --- | --- |
| A run's plan, which only the supervisor's role may create | `runs/<deployment>/<run>/plan.json` |
| The session-start comparison's result, holding the sequence number and hash it compared | `runs/<deployment>/<run>/comparison.json` |
| A halt of the run, and the run's stop | `runs/<deployment>/<run>/halt.json`, `runs/<deployment>/<run>/stop.json` |
| A stage's decision result, which holds its halt when it has one | `runs/<deployment>/<run>/<decision>/<stage>/result.json` |
| A stage's output file, including data's snapshot, alpha's forecast and its signal records, risk's constraints, portfolio construction's target and the record of how it was sized, and monitoring's report | `runs/<deployment>/<run>/<decision>/<stage>/<name>.json` |
| The account's `Valuation`, an output of each decision's recover stage, by this doc's proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10) | `runs/<deployment>/<run>/<decision>/execution/recover/valuation.json` |
| The reconciled `Book`, and the `HardCaps` as recover parsed them, which the risk and portfolio stages read, two outputs of each decision's recover stage, by this doc's proposal for [#10](https://github.com/l3a0/ithildin-runtime/issues/10) | `runs/<deployment>/<run>/<decision>/execution/recover/book.json`, `runs/<deployment>/<run>/<decision>/execution/recover/caps.json` |
| A send stage's broker read, an output file of the send stage, one for each attempt | `runs/<deployment>/<run>/<decision>/execution/send/broker-read-<n>.json` |
| A capturing stage's later capture that differs from its first | `runs/<deployment>/<run>/<decision>/<stage>/<name>.attempt-<n>.json`, with `n` counting from 2 |
| A takeover's broker order list, a capture named by the journaled takeover entry, whose form [#10](https://github.com/l3a0/ithildin-runtime/issues/10) holds | `runs/<deployment>/<run>/execution/takeovers/<UTC time>.json` |
| The order list that maps an order in doubt | [#10](https://github.com/l3a0/ithildin-runtime/issues/10) holds the key |
| A send stage's claim, naming the handler running it | `runs/<deployment>/<run>/<decision>/execution/send/claim.json` |
| A journal entry | `journal/<account>/<seq>.json`, with `seq` padded to twelve digits |
| A journal checkpoint | `journal/<account>/checkpoints/<seq>.json`, with `seq` padded to twelve digits |
| A book snapshot, derived | `accounts/<account>/books/<seq>.json`, with `seq` padded to twelve digits |
| The simulated account, one blob for each call that changed it, written before the call returns | `sim/<account>/<call>.json`, with the call padded to twelve digits |
| The plan's lease, holding the run's status | `leases/plans/<deployment>/<run>.json` |
| The account's lease | `leases/accounts/<account>.json` |
| The kill switch | `accounts/<account>/switch.json` |
| A fired loss stop, which holds until a person clears it | `accounts/<account>/stop.json` |
| A halt that names the account and outlives a session, such as the one [Faults are expected](../design.md#faults-are-expected-and-the-platform-checks-itself-while-it-runs) holds on an unsettled cancel | `accounts/<account>/halts/<component>-<cause>-<timestamp>-<count>.json` |
| A strategy's off switch | `accounts/<account>/strategies/<strategy>/off.json` |
| A strategy halt the platform raises, by this doc's proposal for [#33](https://github.com/l3a0/ithildin-runtime/issues/33) | `accounts/<account>/strategies/<strategy>/halts/<component>-<cause>-<timestamp>-<count>.json` |
| The hard caps, as the owner writes them | `accounts/<account>/caps.json` |
| An override | `accounts/<account>/overrides/<sha256>.json` |
| A live release | `accounts/<account>/releases/<sha256>.json` |
| An owner's record confirming a broker's trade, under a prefix [#11](https://github.com/l3a0/ithildin-runtime/issues/11) names | [#11](https://github.com/l3a0/ithildin-runtime/issues/11) holds the key |
| A rewrite-planning page's claim and its sent record, one for each load parameter and level in the environment | `load/<parameter>-<level>/claim.json`, `load/<parameter>-<level>/sent.json` |
| A heartbeat | `heartbeats/<deployment>/<writer>/<UTC time>.json` |
| A restart record | `restarts/<deployment>/<run>/<worker>/<boot>/<count>.json`, with the count padded to six digits |
| The release pin, deploy records, block records, promotion records and pin changes | `release/pin.json`, `release/deploys/<digest>.json`, `release/blocked/<digest>.json`, `release/promotions/<digest>.json`, `release/changes/<UTC time>.json` |

#### What each part of a key stands for

**Table:** What each part of a key stands for

| Part | What it stands for |
| --- | --- |
| `<run>` | The session date in shadow, paper and live, and the span's first and last session dates, as `2006-04-26_2012-04-09`, in a backtest, built by one function in `core` |
| `<decision>` | The decision's UTC time in the compact form `20261102T204000Z`, so it sorts in order |
| `<timestamp>` | A halt firing's UTC second in the compact form `20261105T204100Z`, so a halt's key sorts in order |
| `<UTC time>` | A UTC time to the microsecond, in the compact form `20261102T204001.123456Z`, so it sorts in order |
| `<boot>` | The boot id of the VM that wrote the record, so a replacement VM whose restart count starts again never meets an earlier record's key |
| `<seq>` | The journal entry's sequence number. Every key that carries it pads it to twelve digits, the journal entry's own key, a checkpoint's and a book snapshot's, so a listing sorts them in order. Twelve digits of sequence number cover a million million entries, which at thirty entries a minute around the clock last tens of thousands of years |

`core`'s key builders refuse a part that would make two rows' keys the same, such as a `-` inside a halt key's component or cause, or an output named `result`, `claim`, `broker-read-<n>` or like an attempt.

A halt's id is the key of the result that holds it, `<deployment>/<run>/<decision>/<stage>`, or for a halt held in a folder its name. The prefixes are the ones the grants of [The runtime's credential does only what the rules need](07-deployment.md#rule-5-the-runtimes-credential-does-only-what-the-rules-need) and the bucket's lifecycle name, with `release/` read by each VM's role for its own pin, as [Four environments](07-deployment.md#four-environments) says.
