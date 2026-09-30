"""Live/paper execution for the relative-strength rotation strategy on IBKR.

Same signal as relative_strength_rotation_ibkr_backtest.ipynb: rank the halal candidate
universe by trailing 3-month return, hold the top 10 equal-weighted, rebalance monthly.
This script is meant to run once on each month's first trading day, via cron (see the
bottom of this file for a crontab line). It decides for itself whether today is a
rebalance day and exits immediately if not, so it's safe to schedule it to run daily.

SAFETY MODEL — read before pointing this at a funded account:

  - DRY_RUN defaults to True. In dry-run, the script does everything (fetches data, ranks
    the universe, computes target trades against your *real* current IBKR positions) but
    only prints/logs the orders it would place — it never calls placeOrder.
  - Going live requires ALL THREE of: DRY_RUN = False below, the --confirm-live CLI flag,
    and the environment variable CONFIRM_LIVE=YES set in the environment that invokes it
    (e.g. in the crontab line itself). Any one of these missing means no order is sent.
    This is deliberate friction: a cron job with a stale/edited env, or a config file
    that silently changed, should not be enough on its own to start sending real orders.
  - A kill switch: if KILL_SWITCH_PATH exists on disk, the script logs that and exits
    before touching the account or placing any order. `touch` that path any time you
    want to guarantee it does nothing on its next scheduled run.
  - Idempotency: STATE_PATH records the last month this script actually rebalanced.
    If cron fires twice in one day, or the box reboots and cron catches up, it will not
    rebalance twice for the same month.
  - Per-order sanity check: refuses to place any single order worth more than
    MAX_ORDER_FRACTION_OF_NAV of account NAV (protects against a bad NAV/price read
    turning into an oversized order; equal weighting across 10 names should never
    legitimately hit this).
  - Reconciliation, not replay: current holdings and NAV are read fresh from IBKR
    (ib.positions(), ib.accountSummary()) every run. This script never trusts its own
    memory of what it did last time — only STATE_PATH's rebalance-month marker persists.
  - Trade log: every attempted order — dry-run or real — is appended to trade_log.txt
    (one line each) and trade_log.csv (timestamp, side, ticker, success, account_balance),
    with the account's NAV re-read from IBKR immediately after the attempt. 'success' in
    the CSV is 'DRY_RUN' for simulated attempts, 'SUCCESS' once IBKR has *accepted* a real
    order (not necessarily filled yet — it's a marketable limit order, not guaranteed to
    fill), or 'FAILED' with the reason in trade_log.txt. Both files are gitignored since
    they contain real account balances.

WHAT THIS DOES NOT YET DO (documented, not implemented — extend before relying on this
unattended for real money):
  - No watchdog/auto-restart of IB Gateway itself (use IBC: https://github.com/IbcAlpha/IBC).
    If Gateway isn't logged in when cron fires, this script will just fail to connect,
    log an error, and exit — it will not page you.
  - No slippage/fill-quality tracking against the notebook's assumptions.
  - No max-daily-loss circuit breaker at the account level.
  - No alerting (email/SMS) on errors — check the log file.
"""
from __future__ import annotations

import csv
import logging
import os
import sys
from pathlib import Path

import pandas as pd

try:
    from ib_insync import IB, Stock, LimitOrder, util
except ImportError as exc:
    raise ImportError('Install ib-insync first: pip install ib-insync') from exc

from rotation_common import (
    LOOKBACK_MONTHS, TOP_N,
    is_first_trading_day_of_month, load_universe, fetch_recent_daily_closes,
    compute_target_basket, build_orders as _build_orders,
)

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
LOG_PATH = Path(__file__).resolve().parent / 'relative_strength_rotation_live.log'
STATE_PATH = Path(__file__).resolve().parent / 'relative_strength_rotation_live_state.csv'
KILL_SWITCH_PATH = Path(__file__).resolve().parent / 'KILL_SWITCH'

# Every attempted order (dry-run or real) gets appended to both of these — one line of plain
# text per attempt, and one row in the CSV. Both are append-only and never overwritten.
TRADE_LOG_TXT_PATH = Path(__file__).resolve().parent / 'trade_log.txt'
TRADE_LOG_CSV_PATH = Path(__file__).resolve().parent / 'trade_log.csv'
TRADE_LOG_CSV_COLUMNS = ['timestamp', 'side', 'ticker', 'success', 'account_balance']

DRY_RUN = True  # <-- must be flipped to False here, AND --confirm-live, AND CONFIRM_LIVE=YES env var, to place real orders

# Paper: 172.27.224.1:7497 (TWS paper) — this is what's connected right now.
# Live: change PORT to your live TWS/Gateway port (7496 TWS live, 4001 Gateway live) when ready.
IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 61

# LOOKBACK_MONTHS, TOP_N, MIN_UNIVERSE come from rotation_common (shared with the email script).

# Execution parameters.
INVESTABLE_FRACTION = 0.98      # leave a small cash buffer for commissions/price drift between the read and the fill
MAX_ORDER_FRACTION_OF_NAV = 0.15  # sanity ceiling per order; equal-weight-of-10 should never legitimately approach this
MIN_TRADE_NOTIONAL = 5.0        # skip rebalancing dust smaller than this
LIMIT_OFFSET_BPS = 50           # marketable limit: buy at last*(1+offset), sell at last*(1-offset)
FILL_WAIT_SECONDS = 20          # how long to wait/poll for sells to fill before sizing buys off the freed-up cash
# PACING_SLEEP_SECONDS for historical-data fetches lives in rotation_common (shared with the email script).

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('rsr_live')


# --------------------------------------------------------------------------------------
# IBKR account state
# --------------------------------------------------------------------------------------
def get_account_state(ib: IB) -> tuple[float, dict[str, float]]:
    summary = {v.tag: v.value for v in ib.accountSummary() if v.currency == 'USD' or v.tag == 'NetLiquidation'}
    nav = float(summary['NetLiquidation'])
    positions = {}
    for p in ib.positions():
        if p.contract.secType == 'STK' and p.contract.currency == 'USD':
            positions[p.contract.symbol] = float(p.position)
    return nav, positions


def read_last_rebalance_month() -> str | None:
    if not STATE_PATH.exists():
        return None
    return STATE_PATH.read_text().strip() or None


def write_last_rebalance_month(month_key: str) -> None:
    STATE_PATH.write_text(month_key)


# --------------------------------------------------------------------------------------
# Order construction and placement
# --------------------------------------------------------------------------------------
def build_orders(nav: float, current_positions: dict[str, float], selected: list[str],
                  last_price: pd.Series, universe: set[str]) -> list[dict]:
    return _build_orders(nav, current_positions, selected, last_price, universe,
                          INVESTABLE_FRACTION, MAX_ORDER_FRACTION_OF_NAV, MIN_TRADE_NOTIONAL)


def append_trade_log(timestamp: pd.Timestamp, side: str, ticker: str, status: str, balance: float | None) -> None:
    """Append one line to trade_log.txt and one row to trade_log.csv for a single order attempt.

    `status` is one of 'DRY_RUN', 'SUCCESS', or 'FAILED: <reason>'. `balance` is the account's
    NetLiquidation value re-read from IBKR immediately after the attempt (None if that read
    itself failed, which is logged as its own row rather than silently dropped)."""
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


def place_order(ib: IB, order_spec: dict, dry_run: bool) -> None:
    ticker, side, shares, ref_price = order_spec['ticker'], order_spec['side'], order_spec['shares'], order_spec['ref_price']
    timestamp = pd.Timestamp.now(tz='UTC')

    if dry_run:
        log.info('DRY-RUN %s %s shares=%.6f ref_price=%.4f notional=$%.2f', side, ticker, shares, ref_price, order_spec['notional'])
        status = 'DRY_RUN'
    else:
        try:
            contract = Stock(ticker, 'SMART', 'USD')
            ib.qualifyContracts(contract)
            offset = 1 + LIMIT_OFFSET_BPS / 10_000 * (1 if side == 'BUY' else -1)
            limit_price = round(ref_price * offset, 2)
            order = LimitOrder(side, shares, limit_price, tif='DAY')
            trade = ib.placeOrder(contract, order)
            log.info('submitted order id=%s %s %s shares=%.6f limit=%.2f', trade.order.orderId, side, ticker, shares, limit_price)
            ib.sleep(2)  # give IBKR a moment to acknowledge (or reject) the order before we log its status
            order_status = trade.orderStatus.status
            if order_status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                status = f'FAILED: order {order_status}'
            else:
                # Not necessarily filled yet (this is a marketable limit, not a guaranteed fill) —
                # 'SUCCESS' here means IBKR accepted the order, not that it has filled. Check
                # trade_log.txt / IBKR's own fill reports for actual execution.
                status = 'SUCCESS'
        except Exception as exc:
            log.exception('order failed for %s %s', side, ticker)
            status = f'FAILED: {exc}'

    try:
        balance, _ = get_account_state(ib)
    except Exception:
        log.exception('could not re-read account balance after attempting %s %s', side, ticker)
        balance = None
    append_trade_log(timestamp, side, ticker, status, balance)


def confirm_live_authorized(cli_confirm_live: bool) -> bool:
    return (not DRY_RUN) and cli_confirm_live and os.environ.get('CONFIRM_LIVE') == 'YES'


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> int:
    confirm_live_flag = '--confirm-live' in sys.argv
    now = pd.Timestamp.now(tz='UTC')

    if KILL_SWITCH_PATH.exists():
        log.warning('KILL_SWITCH present at %s — exiting without touching the account', KILL_SWITCH_PATH)
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

    log.info('=== relative strength rotation live run: %s (dry_run=%s) ===', now.isoformat(), dry_run)

    util.patchAsyncio()
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=dry_run, timeout=20)
    except Exception:
        log.exception('could not connect to IBKR at %s:%d', IB_HOST, IB_PORT)
        return 1

    try:
        universe = load_universe()
        universe_set = set(universe)
        log.info('%d candidate halal stocks in the universe', len(universe))

        nav, current_positions = get_account_state(ib)
        log.info('account NAV=$%.2f, %d existing USD stock positions', nav, len(current_positions))

        daily_closes = fetch_recent_daily_closes(ib, universe)
        selected, ranked, last_price = compute_target_basket(daily_closes)

        if not selected:
            log.info('no rebalance action this month (see warning above if universe was too thin) — '
                     'current positions left as-is')
            if not dry_run:
                write_last_rebalance_month(month_key)
            return 0

        log.info('selected top %d by trailing %d-month return: %s', TOP_N, LOOKBACK_MONTHS, selected)
        log.info('full ranking:\n%s', (ranked * 100).round(1).to_string())

        orders = build_orders(nav, current_positions, selected, last_price, universe_set)
        sells = [o for o in orders if o['side'] == 'SELL']
        buys = [o for o in orders if o['side'] == 'BUY']
        log.info('%d sell orders, %d buy orders to compute/place', len(sells), len(buys))

        for order_spec in sells:
            place_order(ib, order_spec, dry_run)
        if sells and not dry_run:
            ib.sleep(FILL_WAIT_SECONDS)
            nav, current_positions = get_account_state(ib)  # re-read freed-up cash before sizing buys
            orders = build_orders(nav, current_positions, selected, last_price, universe_set)
            buys = [o for o in orders if o['side'] == 'BUY']
        for order_spec in buys:
            place_order(ib, order_spec, dry_run)

        if not dry_run:
            write_last_rebalance_month(month_key)  # dry runs never mark the month done — see module docstring
        log.info('=== run complete ===')
        return 0
    finally:
        ib.disconnect()


if __name__ == '__main__':
    raise SystemExit(main())

# --------------------------------------------------------------------------------------
# Crontab line to run daily near market open (9:35am ET) and let is_first_trading_day_of_month
# decide whether it's actually a rebalance day. Adjust the time for your server's local
# timezone — this assumes the box is set to America/New_York.
#
# CONFIRM_LIVE=YES 35 9 * * 1-5 /home/mmiah/.venv/bin/python3 /home/mmiah/halal-stock-strat/live/relative_strength_rotation_live.py --confirm-live >> /home/mmiah/halal-stock-strat/live/cron.log 2>&1
#
# Leaving CONFIRM_LIVE unset (or DRY_RUN = True above) keeps every run in dry-run mode
# regardless of this cron line, which is the safer way to first watch it operate for a
# few months before it's authorized to place anything.
# --------------------------------------------------------------------------------------
