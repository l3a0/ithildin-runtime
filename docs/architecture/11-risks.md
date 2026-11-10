# Architecture, section 11: Risks and technical debt

This page is section 11 of [the architecture](00-README.md), in the order of the arc42 template. It answers which prices the design accepts, so a reader can review them together.

Every choice that carries a cost names it where the choice is made, as a sentence that starts "The price". This page lists each section that holds one, by page, and states none of them again, so the price is read where it is decided. `tests/test_risks_page.py` fails when a section of an architecture page, design.md, what-a-person-runs.md or halts-and-drills.md holds a sentence starting "The price is", "The price of" or "The price has" and is not listed here. [Proposals awaiting a ruling](09-decisions.md#proposals-awaiting-a-ruling) also gives the price of each open proposal in its own column.

## Prices in Building blocks

- [Package layout and the import rule](05-building-blocks.md#package-layout-and-the-import-rule)

## Prices in Deployment

- [What the separate accounts cost](07-deployment.md#what-the-separate-accounts-cost)
- [What the containers cost](07-deployment.md#what-the-containers-cost)
- [What rolls back a VM image that fails after its refresh](07-deployment.md#what-rolls-back-a-vm-image-that-fails-after-its-refresh)
- [The price of four environments](07-deployment.md#the-price-of-four-environments)

## Prices in Crosscutting concepts

- [Run records](08-crosscutting-concepts.md#run-records)
- [The security master](08-crosscutting-concepts.md#the-security-master)
- [Who writes and reads a blob](08-crosscutting-concepts.md#who-writes-and-reads-a-blob)
- [How a computed value is rounded](08-crosscutting-concepts.md#how-a-computed-value-is-rounded)

## Prices in Quality requirements

- [How CI runs them](10-quality.md#how-ci-runs-them)

## Prices in what a person runs

- [What all of this costs](../what-a-person-runs.md#what-all-of-this-costs)

## Prices in halts-and-drills.md

- [How a sort runs](../halts-and-drills.md#how-a-sort-runs)

## Prices in design.md

- [Design for 10x, plan the rewrite before 100x, build at 1x](../design.md#design-for-10x-plan-the-rewrite-before-100x-build-at-1x)
- [One runtime](../design.md#one-runtime)
- [Rule 7: how a run stops](../design.md#rule-7-how-a-run-stops)
- [Rule 9: what a lease is and when it is held](../design.md#rule-9-what-a-lease-is-and-when-it-is-held)
- [Rule 9: who may send, the account's lease](../design.md#rule-9-who-may-send-the-accounts-lease)
- [Rule 9: who takes over a lease](../design.md#rule-9-who-takes-over-a-lease)
- [Orders the platform did not send](../design.md#orders-the-platform-did-not-send)
- [How a component learns of a change](../design.md#how-a-component-learns-of-a-change)
- [A worker works through a span of decisions](../design.md#a-worker-works-through-a-span-of-decisions)
- [Strategies, schedules and decisions](../design.md#strategies-schedules-and-decisions)
- [Faults are expected, and the platform checks itself while it runs](../design.md#faults-are-expected-and-the-platform-checks-itself-while-it-runs)
- [Every change can be undone or switched off](../design.md#every-change-can-be-undone-or-switched-off)
- [A release is checked where it runs, and nothing heals by hiding a fault](../design.md#a-release-is-checked-where-it-runs-and-nothing-heals-by-hiding-a-fault)
- [A strategy is judged over a sample, and runs unchanged through it](../design.md#a-strategy-is-judged-over-a-sample-and-runs-unchanged-through-it)
- [A backtest names what it could not have traded, and each model states how it fails](../design.md#a-backtest-names-what-it-could-not-have-traded-and-each-model-states-how-it-fails)
- [A fill is measured from the decision, and a position depends on a market it does not control](../design.md#a-fill-is-measured-from-the-decision-and-a-position-depends-on-a-market-it-does-not-control)
- [A result is measured against the risk it took, and size answers to what the owner can bear](../design.md#a-result-is-measured-against-the-risk-it-took-and-size-answers-to-what-the-owner-can-bear)
- [Data has a cost, except the data that cannot be recaptured](../design.md#data-has-a-cost-except-the-data-that-cannot-be-recaptured)
- [What these rules cost, and what they buy](../design.md#what-these-rules-cost-and-what-they-buy)
- [2026-10-28](../design.md#2026-10-28)
- [2026-10-29](../design.md#2026-10-29)
- [2026-10-30](../design.md#2026-10-30)
- [2026-11-01](../design.md#2026-11-01)
- [2026-11-02](../design.md#2026-11-02)
- [2026-11-04](../design.md#2026-11-04)
- [2026-11-05](../design.md#2026-11-05)
- [2026-11-06](../design.md#2026-11-06)
- [2026-11-07](../design.md#2026-11-07)
- [2026-11-09](../design.md#2026-11-09)
- [2026-11-10](../design.md#2026-11-10)
- [Considered and rejected](../design.md#considered-and-rejected)
