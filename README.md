# halal-stock-strat

A collection of systematic equity trading strategies, built and researched end-to-end over a
Shariah-screened universe: signal design, backtesting, cost/feasibility analysis, and — for one
strategy — live, real-money execution. The two folders below are the actively developed,
most-documented projects; the notebooks at the repo root are earlier single-signal explorations
that came before them.

**Universe.** `equities-list.csv` lists ~131 candidate halal stocks (plus a few ETFs used as
benchmarks/hedges). This is a *candidate* list, not a live compliance screen — names are added
and removed over time, and every notebook here treats Shariah compliance as something to verify
independently, not something the ticker list guarantees.

## Featured projects

### [`relative_strength_rotation/`](relative_strength_rotation/) — cross-sectional momentum rotation

Ranks the universe by trailing return and rotates into an equal-weighted basket of the current
top performers, at two cadences: a monthly research variant and a 2-hour intraday variant that
has traded real money live since **2026-10-05**. The central question: does a monthly-horizon
momentum ranking rule still carry a tradeable edge when compressed to an intraday cadence, once
realistic transaction costs are priced in?

**Key finding:** no — a dedicated frequency experiment found **negative information coefficient
at every rebalance horizon tested** (1-24 hourly bars), and once a realistic 5bps round-trip
cost is modeled, only the *slowest* cadence tested stayed net-profitable. That result, not a
hunch, is what motivated the second project below.

### [`intraday_microstructure/`](intraday_microstructure/) — does order flow and price/volume microstructure predict near-term returns?

A direct response to the rotation finding above: rather than keep tuning the same cross-sectional
momentum signal at higher frequency, this asks a more basic question from scratch — does a
stock's own recent price, volume, and trade-level behavior predict its near-term return at all,
and in which direction? Answered empirically via information coefficient across three
increasingly data-rich phases (1-minute bars → feature-augmented bars → individual trade ticks),
each phase a direct response to the previous one's limiting factor.

**Key finding:** a real, short-horizon reversal effect exists and gets more tradeable as more
information is added — gross edge went from +1.76bps (cost-breakeven ~1bps) at the simplest
phase to +8.94bps (surviving ~5bps of cost) once order-flow imbalance was added — but the
largest jump in edge came on a sample concentrated in a few calendar days, flagged in that
project's own README as provisional pending a longer, re-run confirmation rather than reported
at face value.

## Repo layout

```
halal-stock-strat/
├── README.md                          — this file
├── equities-list.csv                  — the shared halal-candidate universe (ticker, asset type)
├── data/                               — shared daily/hourly bar cache (2006/2009-2026), used by the root-level notebooks
├── relative_strength_rotation/         — featured project: live cross-sectional momentum rotation (see above)
│   └── live/                          — the real-money execution code, Webull integration, dashboard
├── intraday_microstructure/            — featured project: order-flow/microstructure research (see above)
│   └── data/                          — separate, higher-granularity cache (1-min bars, trade ticks) — not the root data/
└── *.ibkr_backtest.ipynb / other root-level notebooks — earlier single-signal explorations, below
```

## Earlier explorations (root-level notebooks)

Before the two featured projects above, several single-signal strategies were backtested
independently over the same halal universe — each notebook is self-contained and documents its
own method and caveats:

- **`momentum_strategy_ibkr_backtest.ipynb`** — classic cross-sectional 12-1 momentum, monthly rebalance.
- **`momentum_trend_filter_ibkr_backtest.ipynb`** / **`momentum_trend_filter_intraday_backtest.ipynb`** — momentum narrowed to names also in a confirmed long-term uptrend, at monthly and intraday cadences.
- **`volatility_adjusted_momentum_ibkr_backtest.ipynb`** — momentum ranking scaled by realized volatility.
- **`breakout_trend_following_ibkr_backtest.ipynb`** — per-stock breakout/trend-following, evaluated independently rather than cross-sectionally.
- **`rsi_pullback_ibkr_backtest.ipynb`** / **`zscore_mean_reversion_ibkr_backtest.ipynb`** — mean-reversion entries (RSI dip, and a Z-score framing of the same idea) within a longer-term uptrend.
- **`buy_and_hold_ibkr_benchmark.ipynb`** — a single-name (AAPL) buy-and-hold benchmark for comparison.

## A note on live trading

`relative_strength_rotation/live/relative_strength_rotation_webull_intraday.py` places real
orders against a real brokerage account on a schedule. Everything else in this repo —
including the monthly variant of the same strategy — is backtest/dry-run only. If you're
adapting any of this, treat the live script's safety mechanisms (settled-cash-only NAV,
idempotency guard, kill switch, scheduled-time check, triple-gated live authorization — all
documented in `relative_strength_rotation/README.md`) as the minimum bar, not optional extras.
