# Known limitations

Honest constraints of this experiment. Useful for interviews and for anyone
tempted to treat paper CSVs as live expectancy.

## Market data and timing

- **No queue position.** Paper IOC assumes you take displayed size; live fills
  compete with other takers and cancellations.
- **Mixed-time books.** Legs of a route can be updated at slightly different
  event times even inside a coalesce batch.
- **SBE / API availability.** The authenticated depth feed and schema are
  exchange-controlled; decoder support is pinned to the schema used here
  (`schemas/stream_1_0.xml`).
- **Silence and lag gates** reduce trading on stale data but cannot invent
  atomic cross-book snapshots.

## Execution model optimism

- Simulated latency is a constant sleep, not exchange RTT distribution.
- Partial fills, self-trade prevention, and rate limits are simplified.
- Fee model assumes a fixed taker rate (BNB-discount style knobs in config);
  your real tier may differ.
- Recovery paths bound inventory risk in simulation; live recovery can be
  worse under moving markets.

## Strategy / research scope

- No full historical L2 replay yet; online paper sessions are the evidence
  source.
- Universe heuristics (volume-rank floor, hub set, max extended routes) are
  hand-chosen, not systematically optimized.
- Extended 4/5-leg wraps increase fee drag and failure surface.
- “Theoretical” edges are top-of-book and often vanish before demo size can
  complete a cycle.

## Operational

- Requires a Binance API key **identifier** for SBE (create a key with no
  trading/withdrawal permission; keep private keys offline).
- Stream URL length and `max_symbols` constrain how large a universe you can
  subscribe to in one session.
- Paper results in `results/` are local artifacts; only `results/sample/` is
  meant for the repository.

## Safety

- Archived live-order experiments under `archive/` are incomplete and must not
  be run.
- Never commit `.env`, PEM files, or `config.json` with secrets.
