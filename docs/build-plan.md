# Build plan — ithildin-runtime

This doc carries the slicing rule, the build order, and each slice's test
surface. It does not carry scope. An unbuilt deliverable's scope lives on its
issue, which is what [CLAUDE.md](../CLAUDE.md) makes authoritative, and the
entries below are links to those issues rather than restatements of them. A
plan entry that restates an issue goes stale the moment work lands.

## The slicing rule

A slice is the smallest set of deliverables that leaves the product usable by
someone at the end of it. Not a layer, and not a subsystem. A slice that ends
with a component nobody can run is a slice cut the wrong way.

Order the slices by what the ranking rule in CLAUDE.md decides: what is missing
from the shortest path to a product someone can use goes first. Then ship it,
use it, and let what breaks set the order after that.

A slice carries one milestone on the tracker, with the same name, so a filed
issue has a milestone to take.

## Slices

### Slice 1, walking skeleton

**What it leaves usable.** A platform that runs every trading day on paper,
end to end, with every module in its simplest form. One strategy goes through
data, alpha, risk, cost, portfolio construction and execution, and the day
ends with positions reconciled against the broker.

**Deliverables.** The issues are not filed yet. Filing them is the slice's
first piece of work, one issue per row of the MVP table in
[team-charter.md](team-charter.md#the-first-mvp), and this list becomes links
when they exist.

**Test surface.**

1. A strategy already replicated in the sibling
   [quantitative-trading](https://github.com/l3a0/quantitative-trading)
   repository, run through this platform's backtest, reproduces its pinned
   numbers.
2. A replay of recorded data through the full loop against the simulated
   broker emits pinned orders, including on a day with a missing feed, a stale
   price and a restart in the wrong order.
3. The risk gate refuses an order over each limit, and a test shows no order
   reaches the broker adapter without passing it.
4. The kill switch and the dead-man check have each fired in a drill.

## Dependencies between slices

There is one slice so far, so nothing depends on anything yet. The next slice
is chosen by what breaks while the skeleton runs on paper.
