# Intraday Microstructure Research

A deliberate pivot away from pure relative-strength rotation as the source of intraday alpha.
The rotation frequency experiment (`../relative_strength_rotation/relative_strength_rotation_frequency_experiment.ipynb`)
found negative information coefficient at every rebalance horizon tested, calling into question
whether that signal has genuine intraday predictive power at all. This folder builds a different,
microstructure-aware approach from finer-grained data instead, in increasing order of data
requirements and modeling complexity. **See `STRATEGY.md` for the full phase-by-phase plan,
status, and real numeric findings** — this README covers the formulas and file layout; that file
covers the research narrative and results in depth.

## The question, and the method

Rather than assume momentum or reversal and go looking for it, every phase here asks a single,
direction-agnostic question: does a stock's own recent behavior predict its near-term future
return, and in which direction? The **information coefficient (IC)** — the correlation between a
signal and the forward return it's meant to predict — is the common measurement across all three
phases, so results are comparable:

```
IC(k, h) = corr( signal computed from a k-period lookback,  forward return over the next h periods )
```

A positive IC means the signal predicts *continuation* (momentum); negative means it predicts a
*reversal* (contrarian). Letting the data's own sign decide, rather than assuming one, is a
deliberate discipline — see Phase 1's "Key findings" for a backtest sign-bug this exact
discipline caught before it reached a conclusion.

## Phase 1 — momentum/reversal baseline

**Signal**: the trailing return over the last `k` minutes, for `k` and forward horizon `h` each
swept over `{1, 2, 5, 10, 15, 30, 60}` minutes:

```
trail_return(k)  = close[t] / close[t-k] - 1
fwd_return(h)    = close[t+h] / close[t] - 1
```

Pooled IC is computed across all (ticker, minute) pairs at once, for every (k, h) combination,
within each trading session only (a session boundary is never crossed — an overnight gap is a
different phenomenon from intraday continuation/reversal).

**Finding**: reversal dominated at every (k, h) tested (IC -0.013 to -0.041, strongest at k=h=1
minute). A long-short decile backtest — long recent losers, short recent winners, following the
IC's own sign — showed a real gross edge of **+1.76 bps per decision**, but it flips negative at
just 1 bps of assumed round-trip cost.

## Phase 2 — feature-augmented, selective reversal

Conditions Phase 1's reversal signal (re-oriented so a higher score always means a higher
predicted forward return: `reversal_score = -trail_return(1)`) on three features, all computed
from the same 1-minute OHLCV data:

```
session_VWAP      = cumsum(bar_avg_price × bar_volume) / cumsum(bar_volume)     (within session)
vwap_dist         = close / session_VWAP - 1

volume_surprise   = volume / baseline_volume_at_this_minute_of_session - 1
                    (baseline = this ticker's own average volume at that minute-of-session,
                     across the full cached sample)

relative_strength = ( trail_return(1) - cross_sectional_mean_at_this_minute )
                     / cross_sectional_std_at_this_minute
                    (a z-score against all other tickers at that exact minute — the rotation
                     strategy's own signal, demoted from primary driver to one input among several)
```

**Finding**: conditioning on the high tercile of `relative_strength` alone lifts IC from +0.052
to +0.081 (it does almost all the work; `vwap_dist` and `volume_surprise` add little on top of
it). Restricting the backtest to minutes passing all three filters raised the gross edge to
**+2.83 bps**, pushing the cost breakeven out to 1-2 bps — real progress, still not enough to
survive realistic costs unconditionally.

## Phase 3 — trade imbalance

Adds a fourth feature from genuinely new data (individual trade ticks, not bars): **trade
imbalance (TOI)**, using the *tick rule* for buy/sell classification (an uptick is a buy, a
downtick is a sell, an unchanged price inherits the previous classification — a standard, simpler
alternative to Lee-Ready, which would need matching quote data too):

```
direction(trade) = +1 if price > previous_price
                    -1 if price < previous_price
                    direction(previous_trade) if unchanged

TOI(minute) = ( Σ volume where direction=+1  -  Σ volume where direction=-1 )
              / ( Σ volume where direction=+1  +  Σ volume where direction=-1 )
```

**Status**: the engineering (a historical-tick downloader, tick-rule classification, the merge
pipeline) all works, but the first analysis run was inconclusive — the multi-day tick download
left different tickers' cached windows on different calendar dates than the frozen minute-bar
cache, so only 11 of 28 tickers overlapped with any usable signal. As of 2026-10-19 the tick
download finished cleanly for the full universe (126 tickers, minus 5 skipped — see below), so a
properly re-aligned re-run (refresh the minute-bar cache to the same date range, or derive bars
directly from the tick data) is the natural next step before trusting any trade-imbalance
finding. See `STRATEGY.md` for the detailed postmortem.

## Data pipeline notes

- **`download_minute_bars.py`**: 1-minute OHLCV bars, ~1 month of history (Sept 8 - Oct 7 2026),
  131 tickers. A single pass; not currently re-run on a schedule.
- **`download_trade_ticks.py`**: individual trade ticks, ~1.5 days of history per ticker (pulled
  fresh relative to whenever each ticker's fetch actually runs — see Phase 3's status above for
  why that caused a data-alignment problem). Resumable and idempotent — safe to stop and restart.
  Five tickers are permanently skipped (`SKIP_TICKERS`): `NVDA`, `MRVL`, `META`, `ORCL`, `AMZN` —
  each hung reproducibly deep into its own historical-tick request on every attempt (confirmed via
  isolated testing, not a pacing/throttling issue), with no per-chunk checkpointing yet to make
  retrying them worthwhile. See the inline comments above `SKIP_TICKERS` for the full diagnostic
  history of each, including one case (`ORCL`) where an initial short manual test looked clean and
  wrongly suggested the hang wasn't real — a lesson in not trusting a short sample for this kind
  of intermittent failure.
- **`run_with_watchdog.sh`**: runs `download_trade_ticks.py` under an external supervisor that
  restarts it if its log goes 15 minutes without a new line. This replaced an earlier in-process
  `asyncio` timeout that caused a *worse* bug (silent empty results instead of a hang) — see the
  comment above `_request_chunk_with_retries` in `download_trade_ticks.py`.

## File structure

```
intraday_microstructure/
├── README.md                              — this file
├── STRATEGY.md                            — full phase-by-phase plan, status, and findings (read this for results)
├── momentum_reversal_baseline.ipynb       — Phase 1
├── phase2_feature_augmented_reversal.ipynb— Phase 2
├── phase3_trade_imbalance.ipynb           — Phase 3
├── download_minute_bars.py                — Phase 1/2 data: 1-minute OHLCV bars
├── download_trade_ticks.py                — Phase 3 data: individual trade ticks, tick-rule classification
├── run_with_watchdog.sh                   — external process supervisor for download_trade_ticks.py
└── data/
    ├── minute/{TICKER}.csv                — cached 1-minute bars (131 tickers)
    ├── minute_download_log.csv            — per-ticker download status/resumability log
    ├── ticks/{TICKER}.csv                 — cached trade ticks (126 tickers as of 2026-10-19)
    └── ticks_download_log.csv             — per-ticker download status/resumability log
```

This folder's `data/` is entirely separate from the `data/` directory at the repo root (daily/
hourly bars shared by every other strategy, going back to 2006-2009) — different granularity,
much shorter history, and nothing here is read by or written to that shared cache.
