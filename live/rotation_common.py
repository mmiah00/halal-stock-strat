"""Shared signal/data logic for the relative-strength rotation strategy, used by both
relative_strength_rotation_live.py (real IBKR account execution) and
relative_strength_rotation_email.py (email suggestions for a hypothetical $150 portfolio).

Kept in one place deliberately: the ranking/calendar logic took several rounds of real bug
fixes against live IBKR data to get right (duration-string limits, month-boundary handling,
the exposure-direction fix on the order-size cap), and duplicating it across two scripts would
let them silently drift out of sync with each other and with the backtest notebook."""
from __future__ import annotations

import csv
import logging
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from ib_insync import IB, Stock, util
except ImportError as exc:
    raise ImportError('Install ib-insync first: pip install ib-insync') from exc

try:
    import pandas_market_calendars as mcal
except ImportError as exc:
    raise ImportError('Install pandas_market_calendars first: pip install pandas_market_calendars') from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
EQUITIES_LIST_PATH = REPO_ROOT / 'equities-list.csv'

# Strategy parameters — identical to relative_strength_rotation_ibkr_backtest.ipynb.
LOOKBACK_MONTHS = 3
TOP_N = 10
MIN_UNIVERSE = 15

PACING_SLEEP_SECONDS = 2.0  # between per-ticker historical-data requests, to stay under IBKR's pacing limit

log = logging.getLogger('rotation_common')


# --------------------------------------------------------------------------------------
# Calendar: is today the first NYSE trading session of a new month?
# --------------------------------------------------------------------------------------
def is_first_trading_day_of_month(today: pd.Timestamp) -> bool:
    today_naive = pd.Timestamp(today).tz_localize(None).normalize()
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=today_naive - pd.Timedelta(days=10), end_date=today_naive + pd.Timedelta(days=3))
    sessions = schedule.index.normalize()  # tz-naive, matching today_naive
    if today_naive not in sessions:
        return False  # today isn't even a trading day
    prior_sessions = sessions[sessions < today_naive]
    if len(prior_sessions) == 0:
        return False
    prev_session = prior_sessions[-1]
    return prev_session.month != today_naive.month


# --------------------------------------------------------------------------------------
# Universe and signal
# --------------------------------------------------------------------------------------
def load_universe() -> list[str]:
    with open(EQUITIES_LIST_PATH, newline='') as f:
        rows = list(csv.DictReader(f))
    return [r['Ticker'] for r in rows if r['Asset Type'] == 'Stock']


def normalize_bars(bars) -> pd.DataFrame:
    frame = util.df(bars)
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame['date'] = pd.to_datetime(frame['date'], utc=True)
    frame = frame.set_index('date').sort_index()
    return frame[['close']]


def fetch_recent_daily_closes(ib: IB, universe: list[str]) -> dict[str, pd.Series]:
    """~13 months of daily closes per ticker, fetched fresh (small, fast requests)."""
    closes = {}
    for i, ticker in enumerate(universe, 1):
        try:
            contract = Stock(ticker, 'SMART', 'USD')
            qualified = ib.qualifyContracts(contract)
            if not qualified or contract.conId == 0:
                log.warning('skip %s: could not qualify contract', ticker)
                continue
            bars = ib.reqHistoricalData(
                # IBKR rejects 'D'/'M' durations over 12 months ("must be made in years"); '1 Y'
                # is the shortest years-unit request and comfortably covers the 3-month lookback.
                contract, endDateTime='', durationStr='1 Y', barSizeSetting='1 day',
                whatToShow='TRADES', useRTH=True, formatDate=2, keepUpToDate=False,
            )
            frame = normalize_bars(bars)
            if frame.empty:
                log.warning('skip %s: no bars returned', ticker)
                continue
            closes[ticker] = frame['close']
        except Exception:
            log.exception('skip %s: error fetching bars', ticker)
        ib.sleep(PACING_SLEEP_SECONDS)  # stay well under IBKR's historical-data pacing limit
        if i % 20 == 0:
            log.info('fetched %d/%d tickers', i, len(universe))
    return closes


def compute_target_basket(daily_closes: dict[str, pd.Series]) -> tuple[list[str], pd.Series, pd.Series]:
    """Returns (selected_tickers, score_table, last_price). Mirrors the notebook exactly:
    rank by trailing LOOKBACK_MONTHS-month return (last regular-session close of each
    month, no skip month), take the top TOP_N. If fewer than MIN_UNIVERSE names are
    scoreable, returns an empty selection (caller should then leave current holdings
    untouched — the same fallback the backtest uses)."""
    if not daily_closes:
        log.error('no tickers returned any bars — cannot compute a ranking this run')
        return [], pd.Series(dtype=float), pd.Series(dtype=float)

    price_panel = pd.DataFrame(daily_closes).sort_index()
    monthly_prices = price_panel.resample('ME').last()

    today = pd.Timestamp.now(tz='UTC')
    if len(monthly_prices) and (monthly_prices.index[-1].year, monthly_prices.index[-1].month) == (today.year, today.month):
        # Today's own session is this month's only bar so far; the notebook's rebalance
        # logic wants the *completed* prior month as "decision_idx", so drop it here too.
        monthly_prices = monthly_prices.iloc[:-1]

    if len(monthly_prices) <= LOOKBACK_MONTHS:
        return [], pd.Series(dtype=float), price_panel.iloc[-1]

    score = (monthly_prices.iloc[-1] / monthly_prices.iloc[-1 - LOOKBACK_MONTHS] - 1).dropna()
    last_price = price_panel.ffill().iloc[-1]

    if len(score) < MIN_UNIVERSE:
        log.warning('only %d scoreable names (< MIN_UNIVERSE=%d) — holding current basket unchanged', len(score), MIN_UNIVERSE)
        return [], score, last_price

    ranked = score.sort_values(ascending=False)
    selected = ranked.head(TOP_N).index.tolist()
    return selected, ranked, last_price


# --------------------------------------------------------------------------------------
# Order/suggestion construction — pure function, no IBKR/account dependency beyond its args.
# --------------------------------------------------------------------------------------
def build_orders(nav: float, current_positions: dict[str, float], selected: list[str],
                  last_price: pd.Series, universe: set[str],
                  investable_fraction: float, max_order_fraction_of_nav: float,
                  min_trade_notional: float) -> list[dict]:
    """One dict per order: {ticker, side, shares, ref_price, notional}.

    Only tickers in `universe` (the halal candidate list) are ever touched — an existing
    position in something outside it (e.g. a hedge or a trade from another strategy
    sharing this account) is left completely alone, on purpose."""
    target_value_per_name = (nav * investable_fraction / TOP_N) if selected else 0.0
    managed_positions = {t: s for t, s in current_positions.items() if t in universe}
    unmanaged = set(current_positions) - universe
    if unmanaged:
        log.info('leaving %d position(s) outside the managed universe untouched: %s', len(unmanaged), sorted(unmanaged))

    all_relevant = set(managed_positions) | set(selected)
    orders = []
    for ticker in sorted(all_relevant):
        price = last_price.get(ticker)
        if price is None or not np.isfinite(price) or price <= 0:
            log.warning('no valid price for %s this run — data gap, existing position (if any) left untouched', ticker)
            continue
        current_shares = managed_positions.get(ticker, 0.0)
        target_shares = (target_value_per_name / price) if ticker in selected else 0.0
        delta_shares = target_shares - current_shares
        notional = abs(delta_shares) * price
        if notional < min_trade_notional:
            continue
        # The cap only ever gates orders that INCREASE exposure to a name (buys, or a partial
        # top-up of an existing one). A sell that reduces or closes a position is de-risking —
        # blocking it would strand an unwanted position, which is worse, not safer.
        increases_exposure = abs(target_shares) > abs(current_shares)
        if increases_exposure and notional > max_order_fraction_of_nav * nav:
            log.error('REFUSING order for %s: notional $%.2f exceeds %.0f%% of NAV ($%.2f) — check NAV/price data',
                       ticker, notional, max_order_fraction_of_nav * 100, nav)
            continue
        orders.append({
            'ticker': ticker, 'side': 'BUY' if delta_shares > 0 else 'SELL',
            'shares': round(abs(delta_shares), 6), 'ref_price': round(price, 4), 'notional': round(notional, 2),
        })
    return orders
