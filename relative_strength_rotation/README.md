# Relative Strength Rotation

A systematic momentum/rotation strategy over a halal-screened equity universe. It ranks stocks
by trailing return, holds an equal-weighted basket of the top performers, and periodically
rotates out of laggards and into whatever is currently strongest. Two variants of it exist here:
a slower **monthly** version (research-only / dry-run) and a faster **2-hour intraday** version
that trades real money live.

## How it works

**Universe.** All tickers marked `Stock` (not ETF) in `equities-list.csv` at the repo root — 131
candidates as of this writing, shared with every other strategy in this project. This is a list
of halal *candidates* only; it is not a live compliance screen, and names can be added/removed
over time (see `STRATEGY.md`-style notes in `intraday_microstructure/` for an example of a name
being removed mid-flight).

**Signal — trailing return.** For each ticker, score it by its trailing price return over a
lookback window, with no "skip" period:

```
score(ticker) = price[t] / price[t - lookback] - 1
```

- Monthly variant: lookback = `LOOKBACK_MONTHS` = 3 months, evaluated once on each month's first
  NYSE trading session.
- Intraday variant: lookback = `LOOKBACK_BARS` = 6 hourly bars (~1 trading day), evaluated every
  `REBALANCE_INTERVAL_HOURS` = 2 hours during market hours. Six bars over a two-bar rebalance
  keeps the same 3:1 lookback-to-rebalance ratio as the monthly version, just rescaled to hours.

**Selection.** Rank all scoreable names and take the top `TOP_N` = 10, equal-weighted. If fewer
than `MIN_UNIVERSE` = 15 names have a valid score at decision time (e.g. a data gap), the
strategy **holds the previous basket unchanged** rather than trade into a thin, unreliable
ranking — this fallback has fired many times in live operation (see the `.log` files) and is a
deliberate safety behavior, not a bug.

**Position sizing.** The strategy tracks its own net asset value — not the whole brokerage
account, only the capital it has deployed itself:

```
managed_NAV = settled_free_cash + Σ (shares_held[t] × last_price[t])   for t in universe
```

Any pre-existing or outside position in the account (another strategy's holdings, a position in
a ticker later removed from the universe, etc.) is deliberately excluded from this sum and left
completely untouched — see `managed_nav()` / `build_orders()` in `rotation_common.py`.

Target size per selected name, then an order per ticker to close the gap:

```
target_value_per_name = managed_NAV × INVESTABLE_FRACTION / TOP_N      (INVESTABLE_FRACTION = 0.98)
target_shares[t]       = target_value_per_name / price[t]   if t is selected, else 0
order_shares[t]        = target_shares[t] - current_shares[t]
```

A buy/top-up order is refused outright if its notional would exceed `MAX_ORDER_FRACTION_OF_NAV`
(15%) of NAV — a sanity cap against acting on bad price/NAV data. Sell orders are never capped
this way, since a sell only *reduces* risk; blocking one would strand an unwanted position.
Orders below `MIN_TRADE_NOTIONAL` ($1) are skipped as not worth the trade.

**Execution (intraday/live only).** Orders are limit orders priced off the ranking-fetch price,
offset by `LIMIT_OFFSET_BPS` (100bps as of 2026-10-15 — buy up to 1% above last, sell down to 1%
below) to make them reasonably likely to fill without chasing the market. Webull's fractional-
share order rules impose their own quirks on top of this — see `webull_common.py`'s module
docstring for everything that was reverse-engineered by trial against the real API (field names,
the 5-decimal share-quantity limit, the "must be marketable" rule for sub-1-share orders, etc.).

**Realized P&L.** Computed once per cycle, only from that cycle's sells, against Webull's own
reported cost basis (the source of truth for what was actually paid, fetched fresh *before* any
sell executes that cycle):

```
realized_PnL = Σ (sell_price[t] - cost_basis[t]) × shares_sold[t]      over sells this cycle
```

**Safety mechanisms**, all in `webull_common.py` / the live scripts, briefly:
- *Settled-cash-only NAV* — the strategy only ever counts settled cash as spendable, never
  unsettled proceeds from a recent sale, which makes a Good-Faith Violation structurally
  impossible rather than merely unlikely (at the cost of some capital sitting idle in
  post-sale settlement limbo — see the dashboard's data notes for a worked example).
- *Idempotency* — time-based for the intraday script (won't re-rebalance until
  `REBALANCE_INTERVAL_HOURS` have passed), month-based for the monthly script.
- *Kill switch* — touch `KILL_SWITCH_WEBULL_INTRADAY` (or the monthly equivalent) to pause a
  script without touching its code.
- *Scheduled-time sanity check* — the intraday script refuses to trade if the wall-clock time
  it's running at isn't within 10 minutes of one of its four intended slots (8:30/10:30/12:30/
  14:30 CT), added after a WSL virtual-clock drift issue caused cron to fire it off-schedule
  (see the comment above `SCHEDULED_TIMES_CT` in the script).
- *Triple-gate live authorization* — placing a real order requires `DRY_RUN = False` in the
  script **and** `--confirm-live` on the command line **and** `CONFIRM_LIVE=YES` in the
  environment, all three at once, deliberately redundant against an accidental live run.

## What's been learned (see linked notebooks for full detail)

The 2-hour intraday cadence was chosen by analogy, not validated — a later backtest
(`relative_strength_rotation_frequency_experiment.ipynb`) found **negative information
coefficient at every rebalance horizon tested** (1-24 hourly bars) and that, once realistic
transaction costs are modeled, only the *slowest* cadence tested stayed profitable — faster
rebalancing did not help. This finding is what motivated the pivot to the microstructure-based
research in `../intraday_microstructure/` (see that folder's own README).

## File structure

```
relative_strength_rotation/
├── README.md                                        — this file
├── relative_strength_rotation_ibkr_backtest.ipynb    — monthly strategy backtest (2009-2026 hourly bars)
├── relative_strength_rotation_intraday_backtest.ipynb— single-cadence (2h) intraday backtest (1 month of minute bars)
├── relative_strength_rotation_frequency_experiment.ipynb — tests 5 rebalance cadences + signal-decay (IC) analysis; the negative-IC finding above
└── live/
    ├── rotation_common.py                — shared signal/ranking/order-sizing logic (both monthly & intraday)
    ├── webull_common.py                  — Webull API integration: auth, balances/positions, order placement, Windows toast notifications
    ├── relative_strength_rotation_live.py        — monthly strategy, IBKR execution (DRY_RUN only, never gone live)
    ├── relative_strength_rotation_email.py       — monthly strategy, emails suggested trades for a hypothetical $150 portfolio (no real orders, ever)
    ├── relative_strength_rotation_webull.py      — monthly strategy, Webull execution (DRY_RUN only, not currently scheduled)
    ├── relative_strength_rotation_webull_intraday.py — ⚠ the live, real-money script — 2-hour cadence, Webull execution
    ├── rotation_dashboard_snapshot.py    — builds a JSON snapshot for the (separate, hypothetical-portfolio) website dashboard
    ├── build_dashboard.py / dashboard_template.html / dashboard.html — real-money performance dashboard for the live strategy (see below)
    ├── *_credentials.env(.example)       — Webull/email API credentials (the non-`.example` files are gitignored)
    ├── conf/token.txt                    — cached Webull auth token (gitignored)
    ├── *.log, *_cron.log, *_state.*, *trade_log.*  — runtime logs/state (all gitignored; the trade logs are the closest thing to a ledger)
    └── trade_popups/                     — one HTML detail page per live trade cycle, opened by clicking the Windows toast notification
```

**The live dashboard** (`live/dashboard.html`) shows the strategy's own managed capital only —
portfolio value, free cash, total P&L since inception, a buy/sell log, and key stats (max
drawdown, a small-sample Sharpe) — explicitly excluding every other holding in the brokerage
account. It regenerates every 30 minutes via cron (`build_dashboard.py`) and auto-refreshes in
the browser every 5 minutes. Open it directly in a browser; no server required.
