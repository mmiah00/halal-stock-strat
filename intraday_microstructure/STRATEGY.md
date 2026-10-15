# Intraday microstructure strategy — plan and status

This folder is a deliberate pivot away from pure relative-strength rotation as the source of
intraday alpha. The rotation frequency experiment
(`../relative_strength_rotation/relative_strength_rotation_frequency_experiment.ipynb`) found
negative information coefficient (IC) at every rebalance horizon tested (1-24 hourly bars),
calling into question whether that signal has genuine intraday predictive power at all,
independent of cadence. Rather than keep tuning the rotation strategy's rebalance interval, the
plan below builds a different, microstructure-aware approach from finer-grained data, in
increasing order of data requirements and modeling complexity.

## The 6-phase plan

| Phase | What it adds | Data needed | Status |
|---|---|---|---|
| 1 | Intraday momentum + reversal baseline — does a stock's own trailing k-minute return predict its forward h-minute return, and in which direction? | 1-min OHLCV | **Done** |
| 2 | Add VWAP distance, volume surprise, and relative strength as conditioning features on top of the Phase 1 reversal signal | 1-min OHLCV (same data, new features) | **Done (initial pass)** |
| 3 | Add trade imbalance (buy volume vs. sell volume) | Trade-level tick data | **Infrastructure done, first analysis inconclusive** |
| 4 | Add Level 2 order-book imbalance (bid/ask depth) | L2 order book data | Not started |
| 5 | Train a model (e.g. XGBoost) predicting 5-minute forward excess return from all of the above features combined | All of the above | Not started |
| 6 | Trade only when predicted return exceeds estimated transaction cost by a meaningful margin | Model from Phase 5 + a real cost model | Not started |

Phases 3-4 require data this project doesn't currently pull (trade-tick and L2 book data are a
materially bigger lift than historical bars — new IBKR subscriptions/entitlements, new
ingestion code, book-reconstruction logic). Nothing here commits to building those; they're
listed because the plan treats order flow as the more interesting long-run source of edge, with
Phases 1-2 (price/volume only) as the cheaper, faster-to-validate starting point.

## Phase 1 — momentum/reversal baseline (done)

**Files**: `download_minute_bars.py` (resumable 1-minute bar downloader from IBKR, caches to
`data/minute/{TICKER}.csv`), `momentum_reversal_baseline.ipynb`.

**What it found**: pooled across all 131 halal-universe tickers and one month of 1-minute bars,
reversal dominates at every lookback/horizon combination tested (1-60 minutes) — IC ranged from
-0.013 to -0.041, strongest at the shortest horizon (1-minute trailing return vs. 1-minute
forward return). No momentum zone was found anywhere in the grid. A corrected long-short decile
backtest (long recent losers, short recent winners, following the IC's actual sign) showed a
real gross edge of +1.76bps per decision — but it flips negative at just 1bps of assumed
round-trip trading cost, and is deeply negative by 5-10bps. **Conclusion: the reversal effect is
statistically real but not economically viable to trade unconditionally, on every single
minute's cross-section.** That's what motivates Phase 2: instead of trading every minute, find a
filter that identifies which reversal setups are worth taking.

## Phase 2 — feature-augmented, selective reversal (done, initial pass)

**Files**: `phase2_feature_augmented_reversal.ipynb`.

**Goal**: condition the Phase 1 reversal signal on three additional features computed purely
from the same 1-minute OHLCV data (no new data pull needed):

- **VWAP distance** — how far the current price has drifted from the session's volume-weighted
  average price. The intuition from the pasted research plan: a reversal is more likely to be a
  genuine snap-back, rather than a repricing that sticks, when it's moved unusually far from
  where most of the session's volume has actually traded.
- **Volume surprise** — current volume relative to that ticker's own typical volume at that same
  point in the trading session (intraday volume is strongly U-shaped — see the original research
  note — so "high volume" has to be judged relative to time-of-day, not an all-day average).
  The intuition: a price move on unusually high volume is more likely to reflect real
  (if temporary) liquidity imbalance of the kind that produces a genuine reversal, versus a move
  on thin volume that may just be noise.
- **Relative strength** — the same trailing-return score the rotation strategy uses, but
  computed as a cross-sectional z-score at each minute rather than compared only to the stock's
  own history. This is deliberately a step down from being the primary signal (as it was for
  rotation) to being one input among several, per the pasted plan: "your rotational-strength
  score becomes one input among several."

**Method**: bucket the Phase 1 reversal signal by each new feature (and combinations of them)
and re-measure IC within each bucket, looking for a subset of conditions where the reversal
effect is meaningfully stronger than the unconditional baseline — i.e., where being selective
(trading less often, but only the "real" setups) recovers enough edge to survive realistic
costs. This is the same "measure often, trade selectively" principle the rotation hysteresis-band
experiment already validated in a different context.

**What it found** (`phase2_feature_augmented_reversal.ipynb`): selectivity works, but modestly,
and relative strength is doing almost all of it. Conditioning on the high tercile of
relative_strength alone lifts IC from an unconditional +0.052 to +0.081 (the low tercile
actually flips negative); vwap_dist and volume_surprise each help a little on their own
(+0.059 and +0.058 at their high terciles) but add almost nothing once relative_strength is
already in the filter — a three-way "all high" filter only reaches +0.082, barely above
relative_strength alone. The backtest payoff: restricting the long-short decile spread to
minutes passing the combined filter raised the gross edge from +1.76bps (Phase 1, unconditional)
to **+2.83bps**, pushing the cost breakeven from under 1bps out to between 1-2bps — real
progress, but still not a strategy that survives a realistic (2bps+) cost assumption. See the
notebook's Key Findings for the full numbers and caveats (one month of data, mild look-ahead in
the volume-surprise baseline, multiple-comparisons risk from bucketing three features on a
single month).

## Phase 3 — trade imbalance (infrastructure done, first analysis inconclusive)

**Files**: `download_trade_ticks.py` (resumable historical-tick downloader, tick-rule
buy/sell classification, caches to `data/ticks/{TICKER}.csv`), `run_with_watchdog.sh` (external
process supervisor — restarts the downloader if its log goes stale, since a real TWS hang can't
always be caught from inside the script itself), `phase3_trade_imbalance.ipynb`.

**Goal**: add trade imbalance (buy-initiated vs. sell-initiated volume, via the tick rule — see
`download_trade_ticks.py`'s docstring for why not full Lee-Ready) as a fourth conditioning
feature alongside Phase 2's three, to see whether it adds genuinely new selectivity beyond what
price/volume-derived features already capture.

**What actually happened**: the tick download turned into the hardest engineering problem in
this whole project so far — not because the idea was hard, but because of a string of real bugs
and an unusually unstable TWS stretch over 2026-10-07 through 2026-10-10:

- A connectivity blip left a request hanging forever (no timeout on the sync call) — fixed with
  chunk-level retries.
- A hard IB session drop ("Peer closed connection") cascaded into ~20 wasted tickers before a
  per-ticker reconnect check was added.
- An attempt to add a timeout via `asyncio.wait_for` around the request introduced a *worse* bug:
  once a timeout fired, every subsequent request silently returned empty results instead of
  raising — 20+ real tickers logged as "no data" with no error at all. Reverted to the plain
  synchronous call and moved timeout protection to an external watchdog script instead, which
  restarts the whole process if its log goes stale rather than fighting asyncio cancellation
  semantics in-process.
- Three tickers (NVDA, MRVL, META) hang reproducibly on every attempt at some chunk deep into
  their fetch — confirmed NOT a pacing/clientId issue (both work fine in isolated single-chunk
  tests) — and are skipped for now (see `SKIP_TICKERS`), since there's no per-chunk checkpointing
  yet to avoid re-walking from scratch on every retry.
- On 2026-10-10 (a Saturday), the IB connection itself stopped completing its handshake entirely
  — consistent with IBKR's weekend maintenance window — and the download was paused rather than
  left crash-looping every 5 seconds.

**First analysis result (2026-10-10, 28 tickers cached)**: inconclusive, for a data reason more
than a finding about trade imbalance itself. The multi-day download means different tickers'
"most recent ~1.5 days" of tick data now land on different calendar windows, and only 11 of 28
tickers had any overlap at all with the (older, frozen-as-of-Oct-7) minute-bar cache — over
just ~1 day. Numbers from that narrow sample are noisy and partly contradict Phase 2's own
findings on the same names (e.g. relative_strength's bucket pattern inverted), which is itself
evidence the sample is too small and misaligned to trust yet, not a real trade-imbalance result.
See the notebook's Key Findings for the full numbers and the data-alignment explanation.

**Next step**: once TWS is confirmed stable again, either refresh the minute-bar cache to the
same date range as the tick cache, or derive 1-minute bars directly from the tick data for the
overlapping tickers, then re-run the Phase 3 notebook on a properly aligned, full-width sample
before drawing any real conclusion about trade imbalance.

## A note on what's NOT being chased yet

The original research plan ranked order-flow-confirmed strategies (#1-#2 in its table) above
everything being built here. That's deliberately deferred: those need trade-tick/L2 data this
project has no pipeline for yet, and the plan's own Phase 1 recommended starting with 1-minute
OHLCV momentum/reversal baselines first — which is what's been done. If Phase 2's feature-based
filtering recovers a cost-surviving edge, that's a much stronger case for investing in trade/L2
data ingestion (Phases 3-4) than jumping there on priors would have been.
