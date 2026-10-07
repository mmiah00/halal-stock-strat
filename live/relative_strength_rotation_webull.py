"""Live/paper* execution for the relative-strength rotation strategy on Webull — MONTHLY
rebalance (see relative_strength_rotation_webull_intraday.py for the 2-hour variant).

*There is no Webull paper/sandbox environment — verified by reading the installed SDK
(webull-openapi-python-sdk) source directly: no "sandbox"/"paper"/"demo" concept exists
anywhere in it. Every call this script makes, once DRY_RUN is off, hits your real funded
account. This is a materially different risk profile from relative_strength_rotation_live.py,
which could be (and was) tested extensively against a free IBKR paper account first.

DATA SOURCE VS. EXECUTION VENUE: this script still ranks the universe using IBKR historical
daily bars (via rotation_common, read-only, market-data only — same as the email script).
Only account state and order execution go through Webull (via webull_common.py, shared with
the intraday variant — see that module's docstring for what's confirmed vs. guessed).

SAFETY MODEL:
  - DRY_RUN defaults to True. In dry-run, this reads your REAL account balance/positions from
    Webull (safe, read-only) and computes what it would trade, but never calls preview_order
    or place_order.
  - Going live requires ALL THREE of: DRY_RUN = False below, --confirm-live, and
    CONFIRM_LIVE=YES in the environment.
  - The live path calls preview_order first and aborts that order (not the whole run) if the
    preview itself errors, before ever calling place_order — the closest substitute available
    for the missing sandbox.
  - Kill switch, idempotency (state file), per-order sanity cap, and trade logging all work
    identically to the IBKR live script — see that file's docstring for the reasoning.
  - Before this is ever authorized to go live: place ONE manual test order yourself first
    (small, in a liquid name, while watching) to confirm the integration's field names/types
    actually match what Webull's API expects. Don't let the first real order be an unattended
    automated one.
"""
from __future__ import annotations

import csv
import logging
import sys
import time
from pathlib import Path

import pandas as pd

try:
    from ib_insync import IB, util
except ImportError as exc:
    raise ImportError('Install ib-insync first: pip install ib-insync') from exc

from rotation_common import (
    LOOKBACK_MONTHS, TOP_N,
    is_first_trading_day_of_month, load_universe, fetch_recent_daily_closes,
    compute_target_basket, build_orders as _build_orders,
)
from webull_common import (
    build_trade_client, get_free_cash_and_positions, managed_nav, send_failure_popup,
    place_order as _place_order, confirm_live_authorized as _confirm_live_authorized,
)

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / 'relative_strength_rotation_webull.log'
STATE_PATH = HERE / 'relative_strength_rotation_webull_state.csv'
KILL_SWITCH_PATH = HERE / 'KILL_SWITCH_WEBULL'

TRADE_LOG_TXT_PATH = HERE / 'webull_trade_log.txt'
TRADE_LOG_CSV_PATH = HERE / 'webull_trade_log.csv'
TRADE_LOG_CSV_COLUMNS = ['timestamp', 'side', 'ticker', 'success', 'account_balance']

DRY_RUN = True  # <-- must be flipped to False here, AND --confirm-live, AND CONFIRM_LIVE=YES env var, to place real orders

# IBKR is used here ONLY to fetch historical daily closes for the ranking signal — read-only,
# never touches an account. Same paper connection the email script uses for this purpose.
IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 73

# Strategy parameters come from rotation_common. Execution parameters:
INVESTABLE_FRACTION = 0.98
MAX_ORDER_FRACTION_OF_NAV = 0.15
MIN_TRADE_NOTIONAL = 1.0        # small account — a $5 floor (used for the IBKR version) would ignore too much
FILL_WAIT_SECONDS = 20

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('rsr_webull')


# --------------------------------------------------------------------------------------
# State / idempotency — identical pattern to relative_strength_rotation_live.py
# --------------------------------------------------------------------------------------
def read_last_rebalance_month() -> str | None:
    if not STATE_PATH.exists():
        return None
    return STATE_PATH.read_text().strip() or None


def write_last_rebalance_month(month_key: str) -> None:
    STATE_PATH.write_text(month_key)


# --------------------------------------------------------------------------------------
# Trade logging
# --------------------------------------------------------------------------------------
def append_trade_log(timestamp: pd.Timestamp, side: str, ticker: str, status: str, balance: float | None) -> None:
    balance_str = f'${balance:,.2f}' if balance is not None else 'unknown'
    with open(TRADE_LOG_TXT_PATH, 'a') as f:
        f.write(f'{timestamp.isoformat()} | {side} {ticker} -> {status} | balance={balance_str}\n')
    csv_success = status if status in ('DRY_RUN', 'SUCCESS') else 'FAILED'
    is_new = not TRADE_LOG_CSV_PATH.exists()
    with open(TRADE_LOG_CSV_PATH, 'a', newline='') as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(TRADE_LOG_CSV_COLUMNS)
        writer.writerow([timestamp.isoformat(), side, ticker, csv_success,
                          f'{balance:.2f}' if balance is not None else ''])


def place_order(trade_client, account_id, order_spec, dry_run) -> None:
    _place_order(trade_client, account_id, order_spec, dry_run, log, append_trade_log)


def confirm_live_authorized(cli_confirm_live: bool) -> bool:
    return _confirm_live_authorized(DRY_RUN, cli_confirm_live)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> int:
    confirm_live_flag = '--confirm-live' in sys.argv
    now = pd.Timestamp.now(tz='UTC')

    if KILL_SWITCH_PATH.exists():
        log.warning('KILL_SWITCH_WEBULL present at %s — exiting without touching the account', KILL_SWITCH_PATH)
        return 0

    if not is_first_trading_day_of_month(now):
        log.info('not the first NYSE trading day of the month — nothing to do today (%s)', now.date())
        return 0

    month_key = f'{now.year:04d}-{now.month:02d}'
    if read_last_rebalance_month() == month_key:
        log.info('already rebalanced for %s — exiting (idempotency guard)', month_key)
        return 0

    live_authorized = confirm_live_authorized(confirm_live_flag)
    if not DRY_RUN and not live_authorized:
        log.error('DRY_RUN is False but live trading is not fully authorized '
                  '(need --confirm-live AND CONFIRM_LIVE=YES env var) — running as dry-run instead')
    dry_run = not live_authorized

    log.info('=== relative strength rotation Webull run: %s (dry_run=%s) ===', now.isoformat(), dry_run)

    try:
        trade_client, account_id = build_trade_client(log)
    except Exception as exc:
        log.exception('could not set up Webull trade client')
        send_failure_popup('Webull connection/auth', str(exc), LOG_PATH, log)
        return 1
    log.info('using Webull account_id=%s', account_id)

    util.patchAsyncio()
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=20)
    except Exception as exc:
        log.exception('could not connect to IBKR at %s:%d for ranking data', IB_HOST, IB_PORT)
        send_failure_popup('IBKR connection', str(exc), LOG_PATH, log)
        return 1

    try:
        universe = load_universe()
        universe_set = set(universe)
        log.info('%d candidate halal stocks in the universe', len(universe))
        daily_closes = fetch_recent_daily_closes(ib, universe)
        selected, ranked, last_price = compute_target_basket(daily_closes)
    finally:
        ib.disconnect()

    free_cash, current_positions = get_free_cash_and_positions(trade_client, account_id)
    log.info('Webull free cash=$%.2f, %d total positions in account (unmanaged ones left untouched)',
              free_cash, len(current_positions))

    if not selected:
        log.info('no rebalance action this month (see warning above if universe was too thin) — current positions left as-is')
        if not dry_run:
            write_last_rebalance_month(month_key)
        return 0

    log.info('selected top %d by trailing %d-month return: %s', TOP_N, LOOKBACK_MONTHS, selected)
    log.info('full ranking:\n%s', (ranked * 100).round(1).to_string())

    nav = managed_nav(free_cash, current_positions, last_price, universe_set)
    log.info('this strategy\'s own NAV (free cash + already-deployed rotation positions) = $%.2f', nav)

    orders = _build_orders(nav, current_positions, selected, last_price, universe_set,
                            INVESTABLE_FRACTION, MAX_ORDER_FRACTION_OF_NAV, MIN_TRADE_NOTIONAL)
    sells = [o for o in orders if o['side'] == 'SELL']
    buys = [o for o in orders if o['side'] == 'BUY']
    log.info('%d sell orders, %d buy orders to compute/place', len(sells), len(buys))

    for order_spec in sells:
        place_order(trade_client, account_id, order_spec, dry_run)
    if sells and not dry_run:
        time.sleep(FILL_WAIT_SECONDS)
        free_cash, current_positions = get_free_cash_and_positions(trade_client, account_id)
        nav = managed_nav(free_cash, current_positions, last_price, universe_set)
        orders = _build_orders(nav, current_positions, selected, last_price, universe_set,
                                INVESTABLE_FRACTION, MAX_ORDER_FRACTION_OF_NAV, MIN_TRADE_NOTIONAL)
        buys = [o for o in orders if o['side'] == 'BUY']
    for order_spec in buys:
        place_order(trade_client, account_id, order_spec, dry_run)

    if not dry_run:
        write_last_rebalance_month(month_key)
    log.info('=== run complete ===')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# --------------------------------------------------------------------------------------
# Crontab line — same pattern as the IBKR scripts, staggered to 8:45am America/Chicago so
# all three (IBKR live/dry-run, email, Webull) don't open connections at the same instant.
# Deliberately no --confirm-live / CONFIRM_LIVE here either — see module docstring.
#
# 45 8 * * 1-5 /home/mmiah/.venv/bin/python3 /home/mmiah/halal-stock-strat/live/relative_strength_rotation_webull.py >> /home/mmiah/halal-stock-strat/live/webull_cron.log 2>&1
# --------------------------------------------------------------------------------------
