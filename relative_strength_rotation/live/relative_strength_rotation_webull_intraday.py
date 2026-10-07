"""2-HOUR rebalance variant of the relative-strength rotation strategy on Webull.

THIS IS A DIFFERENT, UNVALIDATED STRATEGY, NOT A FASTER VERSION OF THE MONTHLY ONE. The
monthly script (relative_strength_rotation_webull.py) ranks by trailing 3-MONTH return,
computed from daily closes, and re-evaluating that signal more often changes nothing — it only
updates once a day at most. This script instead ranks by trailing return over the last
LOOKBACK_BARS hourly bars (6 bars ~= one trading day, chosen to keep the same 3:1
lookback:rebalance ratio the monthly strategy uses — 3 months of lookback for a 1-month
rebalance), which actually can produce a different top-10 every 2 hours.

NOTHING ABOUT THIS FREQUENCY HAS EVER BEEN BACKTESTED. The monthly strategy's CAGR/drawdown/
Sharpe numbers you've seen on the website say nothing about how this version would perform —
different lookback, different rebalance cadence, no transaction costs modeled, no historical
simulation run at all. Treat this as an experiment with real (if small) money, not a validated
strategy running faster.

GOOD-FAITH VIOLATIONS: structurally prevented, not just avoided. get_free_cash_and_positions
(webull_common.py) reads SETTLED cash only, never unsettled funds. A GFV requires buying with
unsettled funds and selling before they settle — since this never buys with unsettled funds,
that specific violation cannot happen. What this does NOT prevent: trading very frequently in
ways that are simply uneconomical once real spread/commissions exist, or depleting settled
cash fast enough that most 2-hour cycles end up doing nothing because no cash has freed up yet
(which is likely, and is the system naturally throttling itself, not a bug).

WHY WEBULL ONLY, NO IBKR-DRY-RUN-STYLE EQUIVALENT: IBKR is still used here, but only for
intraday ranking bars (read-only market data, via rotation_common), exactly like the monthly
Webull script. Execution is Webull-only. There is no intraday equivalent of
relative_strength_rotation_live.py (the IBKR paper-account version) because that would be a
third, separate thing to build and validate; this script's DRY_RUN default is what stands in
for that caution here.

SAFETY MODEL — same shape as the monthly Webull script:
  - DRY_RUN defaults to True.
  - Going live requires ALL THREE of: DRY_RUN = False below, --confirm-live, and
    CONFIRM_LIVE=YES in the environment.
  - Kill switch: KILL_SWITCH_WEBULL_INTRADAY (separate from the monthly script's, so pausing
    one doesn't silently pause the other).
  - Idempotency is TIME-based here, not month-based: STATE_PATH stores the timestamp of the
    last real rebalance, and a run does nothing unless both the market is open right now AND
    at least REBALANCE_INTERVAL_HOURS have passed since that timestamp. Dry runs never advance
    this timestamp, same reasoning as the monthly scripts.
  - Trade log, per-order sanity cap, preview-before-place: identical to the monthly script.

TRADE POP-UP: fires once per cycle (not per order) whenever sells or buys actually happen,
dry-run or real — see webull_common.send_trade_popup. A Windows toast notification can only
show 3 lines of text (confirmed by testing two different APIs on 2026-10-05, not a library
limitation), so the toast shows a timestamp, a sell/buy count, and net realized P&L; clicking
it opens a full HTML table (one file per cycle, kept in relative_strength_rotation/live/trade_popups/, never overwritten
so an old toast always opens the numbers that were true when it fired). Realized P&L only
counts this cycle's sells against Webull's own reported cost basis — buys aren't profit yet,
they're capital being deployed. This requires WSL-to-Windows interop (powershell.exe) to be
reachable; if it isn't, the popup silently logs a warning and the trading logic is unaffected.

FAILURE POP-UP: fires when a cycle dies before it ever gets to placing orders — Webull
connection/auth failure, or IBKR unreachable (the latter is the single most common real failure
mode so far: TWS simply wasn't running on the Windows host when cron fired, e.g. the missed
8:30am CT cycle on 2026-10-06). Unlike the trade pop-up there's no separate detail HTML; clicking
the toast opens this script's own log file directly, since the traceback is already there. See
webull_common.send_failure_popup.
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
    TOP_N, MIN_UNIVERSE,
    is_market_open_now, load_universe, fetch_recent_intraday_closes,
    compute_target_basket_intraday, build_orders as _build_orders,
)
from webull_common import (
    build_trade_client, get_free_cash_and_positions, managed_nav, get_cost_basis,
    compute_realized_pnl, send_trade_popup, send_failure_popup,
    place_order as _place_order, confirm_live_authorized as _confirm_live_authorized,
)

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / 'relative_strength_rotation_webull_intraday.log'
STATE_PATH = HERE / 'relative_strength_rotation_webull_intraday_state.txt'
KILL_SWITCH_PATH = HERE / 'KILL_SWITCH_WEBULL_INTRADAY'

TRADE_LOG_TXT_PATH = HERE / 'webull_intraday_trade_log.txt'
TRADE_LOG_CSV_PATH = HERE / 'webull_intraday_trade_log.csv'
TRADE_LOG_CSV_COLUMNS = ['timestamp', 'side', 'ticker', 'success', 'account_balance']

DRY_RUN = False  # flipped live 2026-10-05, after a single manually-supervised real test trade confirmed the order path works

IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 74  # distinct from the monthly script's (73) and the email/dashboard scripts'

REBALANCE_INTERVAL_HOURS = 2
BAR_SIZE = '1 hour'
BAR_MINUTES = 60
LOOKBACK_BARS = 6        # ~1 trading day of hourly bars; keeps the monthly strategy's 3:1 lookback:rebalance ratio
FETCH_DURATION = '2 D'   # enough bars for LOOKBACK_BARS + buffer, small/fast request

# Same execution parameters as the monthly script — same account, same small-balance reasoning.
INVESTABLE_FRACTION = 0.98
MAX_ORDER_FRACTION_OF_NAV = 0.15
MIN_TRADE_NOTIONAL = 1.0
FILL_WAIT_SECONDS = 20

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('rsr_webull_intraday')


# --------------------------------------------------------------------------------------
# State / idempotency — TIME-based, not month-based (see module docstring)
# --------------------------------------------------------------------------------------
def read_last_rebalance_time() -> pd.Timestamp | None:
    if not STATE_PATH.exists():
        return None
    text = STATE_PATH.read_text().strip()
    return pd.Timestamp(text) if text else None


def write_last_rebalance_time(now: pd.Timestamp) -> None:
    STATE_PATH.write_text(now.isoformat())


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
        log.warning('KILL_SWITCH_WEBULL_INTRADAY present at %s — exiting without touching the account', KILL_SWITCH_PATH)
        return 0

    if not is_market_open_now(now):
        log.info('market is not open right now — nothing to do (%s)', now.isoformat())
        return 0

    last_rebalance = read_last_rebalance_time()
    if last_rebalance is not None and (now - last_rebalance) < pd.Timedelta(hours=REBALANCE_INTERVAL_HOURS):
        log.info('last rebalance was %s ago (< %dh) — exiting (idempotency guard)',
                  now - last_rebalance, REBALANCE_INTERVAL_HOURS)
        return 0

    live_authorized = confirm_live_authorized(confirm_live_flag)
    if not DRY_RUN and not live_authorized:
        log.error('DRY_RUN is False but live trading is not fully authorized '
                  '(need --confirm-live AND CONFIRM_LIVE=YES env var) — running as dry-run instead')
    dry_run = not live_authorized

    log.info('=== relative strength rotation Webull INTRADAY run: %s (dry_run=%s) ===', now.isoformat(), dry_run)

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
        intraday_closes = fetch_recent_intraday_closes(ib, universe, duration=FETCH_DURATION, bar_size=BAR_SIZE)
        selected, ranked, last_price = compute_target_basket_intraday(
            intraday_closes, LOOKBACK_BARS, TOP_N, MIN_UNIVERSE, bar_minutes=BAR_MINUTES)
    finally:
        ib.disconnect()

    free_cash, current_positions = get_free_cash_and_positions(trade_client, account_id)
    log.info('Webull free cash=$%.2f, %d total positions in account (unmanaged ones left untouched)',
              free_cash, len(current_positions))

    if not selected:
        log.info('no rebalance action this cycle (see warning above if universe was too thin) — current positions left as-is')
        if not dry_run:
            write_last_rebalance_time(now)
        return 0

    log.info('selected top %d by trailing %d-bar (%s) return: %s', TOP_N, LOOKBACK_BARS, BAR_SIZE, selected)
    log.info('full ranking:\n%s', (ranked * 100).round(1).to_string())

    nav = managed_nav(free_cash, current_positions, last_price, universe_set)
    log.info('this strategy\'s own NAV (free cash + already-deployed rotation positions) = $%.2f', nav)

    orders = _build_orders(nav, current_positions, selected, last_price, universe_set,
                            INVESTABLE_FRACTION, MAX_ORDER_FRACTION_OF_NAV, MIN_TRADE_NOTIONAL)
    sells = [o for o in orders if o['side'] == 'SELL']
    buys = [o for o in orders if o['side'] == 'BUY']
    log.info('%d sell orders, %d buy orders to compute/place', len(sells), len(buys))

    # Cost basis for the realized-P&L popup must be read BEFORE any sell executes — Webull's
    # own position data is the source of truth for what was actually paid, not anything this
    # script tracks itself.
    try:
        cost_basis = get_cost_basis(trade_client, account_id)
    except Exception:
        log.exception('could not read cost basis — trade popup will show $0.00 realized P&L this cycle')
        cost_basis = {}

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

    if sells or buys:
        net_profit = compute_realized_pnl(sells, cost_basis, log)
        send_trade_popup(now, sells, buys, net_profit, dry_run, log)

    if not dry_run:
        write_last_rebalance_time(now)
    log.info('=== run complete ===')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# --------------------------------------------------------------------------------------
# Crontab line — runs every 2 hours, every day (the script's own is_market_open_now check
# throws out every invocation outside real trading hours, so an imprecise cron window is
# fine). Deliberately no --confirm-live / CONFIRM_LIVE here either.
#
# 0 */2 * * * /home/mmiah/.venv/bin/python3 /home/mmiah/halal-stock-strat/relative_strength_rotation/live/relative_strength_rotation_webull_intraday.py >> /home/mmiah/halal-stock-strat/relative_strength_rotation/live/webull_intraday_cron.log 2>&1
# --------------------------------------------------------------------------------------
