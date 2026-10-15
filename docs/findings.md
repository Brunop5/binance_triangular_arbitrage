# Findings (paper sessions)

These notes summarize qualitative and sample quantitative observations from
live **paper** runs against Binance depth. They are **not** a performance
guarantee and are subject to the caveats in [limitations.md](limitations.md).

## Method

- Stream synchronized L2 books over Binance SBE depth.
- Score routes with fee-adjusted top-of-book ratios and depth-complete quotes
  at demo size (on the order of tens of USDC or small hub notionals).
- Log theoretical episodes, candidate shadow audits, and any paper trade
  episodes to CSV.

## Main observations

1. **Fee drag dominates.** At retail-like taker fees, most apparent gross
   triangles disappear after \((1-f)^{n}\). Lower fee tiers make more
   theoretical edges appear; that does not by itself make them executable.
2. **Mega-connector triangles were effectively sterile** for this demo size
   in multi-week paper monitoring (e.g. USDC–BTC–ETH style paths). The default
   universe therefore prefers quieter bridgeable alts and optional hub mode.
3. **Top-of-book ≠ executable.** Many fee-positive top-of-book flashes fail
   depth walks, filters, or expire within tens to hundreds of milliseconds.
4. **Short half-life.** Sample theoretical episode in
   `results/sample/theoretical_opportunities.csv` lasted ~102 ms, closed when
   the ratio fell back through 1, and was never bot-executable at demo size.
5. **Candidate audits catch optimism.** The paired sample candidate row is
   flagged `DEPTH_ILLUSION|EXPIRED_BEFORE_ARRIVAL`: projected top-of-book PnL
   was tiny/positive while immediate depth-aware output was already negative.
6. **Recovery is part of the design, not an edge case.** Sequential legs imply
   inventory risk whenever the residual cycle dies mid-path.

## Sample artifact

See `results/sample/` for one real paper-session observation (hub-style route
`BTC-IDR-TAO-BTC`, 2026-08-28). Numbers are truncated in the sample files for
readability; full-precision logs remain a local runtime concern.

| Field | Sample value | Interpretation |
| --- | --- | --- |
| Duration | ~102 ms | Edge disappeared almost immediately |
| Start fee-adjusted ratio | ~1.00207 | Barely positive after fees at top-of-book |
| Max executable ratio | ~0.99915 | Depth-complete path not profitable |
| `bot_executable_at_least_once` | `False` | Demo size would not have traded it |
| Candidate flags | `DEPTH_ILLUSION\|EXPIRED_BEFORE_ARRIVAL` | Book/depth and timing rejected the edge |

## What would be needed to claim more

- Longer recorded L2 sessions with replay.
- Explicit comparison of paper fills vs tiny live orders on the same timestamps
  (only with keys that cannot withdraw, and only if you accept the risk).
- Systematic sweeps over universe rank, fee tier, latency, and size.

Until then, the honest claim is: **the stack demonstrates microstructure-aware
paper simulation and logging**, and the early evidence suggests most
top-of-book triangles are not demo-executable after fees and depth.
