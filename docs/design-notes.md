# Design notes

This document explains the intentional choices in the paper triangular-arbitrage
simulator. It is meant for readers who want the *why*, not only the *how*.

## Why order-book depth, not trades

Triangular arbitrage here is an **execution** problem: can we convert hub → A → B
→ hub (or a longer wrap) at sizes that clear exchange filters, after taker fees?

That requires:

- directed **bid/ask** prices (not mid),
- **resting size** at each level (to walk the book for a proposed notional),
- a local book that stays consistent under high-frequency updates.

Public trades / aggTrades show prints, not the residual book. Reconstructing
liquidity from trades alone needs strong assumptions and still does not yield a
clean IOC simulator. Diff-depth + snapshot sync is the direct input.

This project consumes Binance’s authenticated **SBE** depth stream (~20 ms) and
keeps sequence-checked local books. REST supplies the initial depth snapshot and
`exchangeInfo` filters (tick, lot, notional).

## Book synchronization

Per symbol:

1. Buffer inbound depth diffs from the websocket.
2. REST-fetch a depth snapshot; set `update_id = lastUpdateId`.
3. Drop events already covered by the snapshot (`last_update_id <= snapshot_id`).
4. Accept the first event that **bridges** the snapshot
   (`first_update_id ≤ snapshot_id+1 ≤ last_update_id`).
5. After bridging, require contiguous update IDs. On gap, queue overflow, or
   excessive apply lag: invalidate that symbol and resync with a fresh snapshot.

Diff quantities are **absolute** sizes at a price. Quantity `0` deletes the level.

## Evaluation coalescing and freshness

Depth events arrive far faster than a full route scan should run. The market-data
layer marks symbols dirty and publishes a batch about every
`eval_coalesce_ms` (default 10 ms). The engine evaluates only routes that touch
dirty symbols.

Trading pauses when:

- the whole stream is silent longer than `max_stream_silence_ms`, or
- books are not synchronized / apply lag exceeds `max_apply_lag_ms`.

These knobs exist so the simulator does not “trade” on stale or half-applied
state.

## Why sequential IOC simulation

Binance spot does not offer an atomic multi-leg order spanning three symbols.
Any real strategy that crosses a triangle is sequential. Modeling the path as
one simultaneous mid-price product would hide the core risk: leg 1 can fill
while legs 2–3 become unprofitable or fail.

The paper executor:

- sleeps a configurable simulated latency,
- walks the local book like a taker IOC within a slippage cap,
- rounds prices/sizes to tick and lot constraints with `Decimal`,
- records fills without sending orders.

## Fees and leg count

Expected net factor scales roughly as \((1 - f)^{n}\) for \(n\) taker legs.
Longer wraps need a larger gross cycle ratio before they clear
`min_net_profit_rate`. That is why 4-leg / 5-leg routes are capped and treated
as experimental extensions, not the default universe.

## Universe construction

**USDC mode (default):** rank non-stable USDC markets by 24h quote volume, take
alts from roughly volume-rank place 70+, use BTC/ETH/BNB only as bridges when
cross markets exist, and skip pure mega-connector triangles
(`USDC-BTC-ETH-USDC`). Motivation: majors are extremely competitive; residual
fee-viable edges at demo size were not observed in live paper sessions.

**Hub mode:** hold small balances in several hubs (e.g. BTC/ETH/BNB/SOL) and
enumerate directed hub triangles on crypto–crypto pairs.

## Recovery

If a cycle breaks while holding non-hub inventory, the engine searches direct
or two-hop recovery routes back to the hub/anchor, with a separate (looser)
recovery slippage cap and chunking when exchange maxima require it. Recovery is
loss bounding, not profit seeking.

## Theoretical vs candidate vs traded

| Artifact | Meaning |
| --- | --- |
| Theoretical | Fee-adjusted **top-of-book** cycle ratio \(> 1\) for any infinitesimal size |
| Candidate audit | Shadow accounting when a theoretical episode opens (immediate vs delayed depth walk, filter/latency flags) |
| Paper episode | Actual simulated sequential cycles when a depth-complete quote clears the profit gate |

A positive theoretical ratio does **not** imply the demo size can execute.
Sample rows under `results/sample/` show a short-lived top-of-book edge that
failed depth/executability checks.

## What this project is intentionally not

- Not a live trading system (no order-placement client in the maintained path).
- Not a claim of persistent alpha.
- Not a full historical L2 backtester (recording/replay is future work).

See also [limitations.md](limitations.md) and [findings.md](findings.md).
