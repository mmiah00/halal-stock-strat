"""Email suggestions (not automated orders) for the relative-strength rotation strategy,
sized for a hypothetical $150 total investment.

This NEVER places an order anywhere — it only reads market data (read-only IBKR connection,
purely for historical daily closes to compute the ranking) and emails you what to do about it.
There is no real brokerage account behind this: it tracks a hypothetical $150 portfolio in
STATE_PATH, assuming every past suggestion was followed exactly at the suggested reference
price. If you didn't follow a suggestion (skipped a trade, used a different price, whatever),
tell me in chat and I'll correct rotation_email_state.json to match what you actually hold —
there's no other way for this script to find out, since it never talks to your real account.

Runs once on each month's first NYSE trading day (via cron — see the bottom of this file),
and no-ops harmlessly every other day it's invoked, so it's safe to schedule daily.

WHY A HYPOTHETICAL PORTFOLIO INSTEAD OF READING YOUR REAL WEBULL ACCOUNT: this script predates
any Webull API integration — it has no way to read your real Webull balance or holdings. It
tracks its own ledger instead. Once/if a Webull API connection exists, this can be pointed at
your real account state the same way relative_strength_rotation_live.py reads real IBKR state.

Ranking/signal logic (rotation_common.py) is shared with relative_strength_rotation_live.py —
see that file's docstring for why. This script never authorizes any real order; there is no
DRY_RUN flag here because there is no live mode to accidentally enable.
"""
from __future__ import annotations

import json
import smtplib
import sys
import logging
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd

try:
    from ib_insync import IB, util
except ImportError as exc:
    raise ImportError('Install ib-insync first: pip install ib-insync') from exc

from rotation_common import (
    LOOKBACK_MONTHS, TOP_N,
    is_first_trading_day_of_month, load_universe, fetch_recent_daily_closes,
    compute_target_basket, build_orders,
)

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / 'relative_strength_rotation_email.log'
STATE_PATH = HERE / 'rotation_email_state.json'
PAUSE_PATH = HERE / 'EMAIL_PAUSE'  # touch this file to skip sending on the next scheduled run
CREDENTIALS_PATH = HERE / 'email_credentials.env'  # gitignored; see email_credentials.env.example

# Market-data-only IBKR connection — this script never calls positions(), accountSummary(),
# or placeOrder(). Paper host/port is fine here regardless of where your real money lives,
# since it's only ever used to fetch historical daily closes for the ranking.
IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 71

INVESTABLE_TOTAL = 150.0        # the hypothetical total this tracker starts from
INVESTABLE_FRACTION = 0.99      # small cash buffer for price drift between the email and when you actually place the trade
MAX_ORDER_FRACTION_OF_NAV = 0.15  # sanity ceiling; see rotation_common.build_orders
MIN_TRADE_NOTIONAL = 1.0        # skip suggesting rebalancing dust smaller than this (small portfolio, small threshold)
LIMIT_OFFSET_BPS = 50           # suggested marketable limit: buy at last*(1+offset), sell at last*(1-offset)

EMAIL_TO = 'maishm357@gmail.com'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('rsr_email')


# --------------------------------------------------------------------------------------
# Credentials — never hardcoded, never typed into chat. See email_credentials.env.example.
# --------------------------------------------------------------------------------------
def load_email_credentials() -> tuple[str, str] | None:
    """Returns (smtp_user, app_password), or None if not configured. Checks OS environment
    variables first (EMAIL_SMTP_USER, EMAIL_SMTP_APP_PASSWORD), then a local KEY=VALUE file
    at CREDENTIALS_PATH, so this can be set up without touching the crontab environment."""
    import os
    user = os.environ.get('EMAIL_SMTP_USER')
    password = os.environ.get('EMAIL_SMTP_APP_PASSWORD')
    if not password and CREDENTIALS_PATH.exists():
        values = {}
        for line in CREDENTIALS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            values[key.strip()] = val.strip()
        user = user or values.get('EMAIL_SMTP_USER')
        password = password or values.get('EMAIL_SMTP_APP_PASSWORD')
    if not password:
        return None
    return (user or EMAIL_TO), password


def send_email(subject: str, body: str) -> bool:
    creds = load_email_credentials()
    if creds is None:
        log.error('no email credentials configured (see email_credentials.env.example) — cannot send, suggestion NOT recorded as delivered')
        return False
    smtp_user, app_password = creds
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = EMAIL_TO
    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, app_password)
            server.sendmail(smtp_user, [EMAIL_TO], msg.as_string())
        log.info('email sent to %s', EMAIL_TO)
        return True
    except Exception:
        log.exception('email send failed')
        return False


# --------------------------------------------------------------------------------------
# Hypothetical $150 portfolio state
# --------------------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {'cash': INVESTABLE_TOTAL, 'holdings': {}, 'last_rebalance_month': None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def portfolio_value(state: dict, last_price: pd.Series) -> float:
    """Cash plus each held ticker marked at this run's fresh price, falling back to the
    price stored at the time it was bought if this run's fetch has a gap for that ticker."""
    total = state['cash']
    for ticker, info in state['holdings'].items():
        price = last_price.get(ticker)
        if price is None or not pd.notna(price):
            price = info['last_price']
            log.warning('no fresh price for held ticker %s this run — valuing at its last known price $%.2f', ticker, price)
        total += info['shares'] * price
    return total


def apply_orders_to_state(state: dict, orders: list[dict], selected: list[str], last_price: pd.Series) -> dict:
    """Returns a NEW state dict reflecting the suggested trades as if they filled exactly
    at ref_price. Only mutated/returned in memory here — the caller persists it, and only
    after the suggestion email has actually been delivered (see main())."""
    new_holdings = {t: dict(info) for t, info in state['holdings'].items()}
    cash = state['cash']
    for order in orders:
        ticker, side, shares, ref_price = order['ticker'], order['side'], order['shares'], order['ref_price']
        current_shares = new_holdings.get(ticker, {'shares': 0.0})['shares']
        if side == 'BUY':
            new_shares = current_shares + shares
            cash -= order['notional']
        else:
            new_shares = current_shares - shares
            cash += order['notional']
        if abs(new_shares) < 1e-9:
            new_holdings.pop(ticker, None)
        else:
            new_holdings[ticker] = {'shares': new_shares, 'last_price': ref_price}
    # Refresh the stored price for anything held/selected that didn't need a trade this
    # month, so next month's fallback valuation (if data is missing) is still recent.
    for ticker in set(new_holdings) | set(selected):
        price = last_price.get(ticker)
        if ticker in new_holdings and price is not None and pd.notna(price):
            new_holdings[ticker]['last_price'] = price
    return {'cash': cash, 'holdings': new_holdings, 'last_rebalance_month': state['last_rebalance_month']}


# --------------------------------------------------------------------------------------
# Email composition
# --------------------------------------------------------------------------------------
def format_order_line(order: dict) -> str:
    ticker, side, shares, ref_price, notional = (order['ticker'], order['side'], order['shares'],
                                                   order['ref_price'], order['notional'])
    offset = 1 + LIMIT_OFFSET_BPS / 10_000 * (1 if side == 'BUY' else -1)
    suggested_limit = round(ref_price * offset, 2)
    return (
        f'{side} {ticker}\n'
        f'    Fractional shares: {shares:.4f}\n'
        f'    Amount: ${notional:,.2f}  (at last price ${ref_price:,.2f})\n'
        f'    Suggested order: LIMIT {side} at ${suggested_limit:,.2f} '
        f'({"+" if side == "BUY" else "-"}{LIMIT_OFFSET_BPS / 100:.2f}% through the last price, '
        f'to fill promptly without chasing the price)\n'
        f'    Simpler alternative: a MARKET (a.k.a. "spot") order fills immediately at whatever the '
        f'current price is when it reaches the exchange, with less price control than a limit order\n'
    )


def render_email(month_key: str, orders: list[dict], selected: list[str], ranked: pd.Series,
                  nav: float, new_state: dict) -> tuple[str, str]:
    sells = [o for o in orders if o['side'] == 'SELL']
    buys = [o for o in orders if o['side'] == 'BUY']

    if not orders:
        subject = f'Rotation strategy — no changes needed ({month_key})'
    else:
        subject = f'Rotation strategy — {len(sells)} sell(s), {len(buys)} buy(s) suggested ({month_key})'

    lines = [
        f'Relative-strength rotation strategy — signal for {month_key}',
        f'Hypothetical portfolio value: ${nav:,.2f} (started at ${INVESTABLE_TOTAL:,.2f})',
        '',
    ]

    if not orders:
        lines.append("No trades needed — your current top-10 basket hasn't changed this month.")
    else:
        lines.append('Suggested trades (place sells before buys, so you have the cash on hand):')
        lines.append('')
        for order in sells:
            lines.append(format_order_line(order))
        for order in buys:
            lines.append(format_order_line(order))

    lines += [
        '',
        'Resulting target holdings:',
    ]
    for ticker, info in sorted(new_state['holdings'].items()):
        weight = info['shares'] * info['last_price'] / nav * 100 if nav else 0.0
        lines.append(f"  {ticker}: {info['shares']:.4f} shares (~${info['shares'] * info['last_price']:,.2f}, {weight:.1f}% of portfolio)")
    lines.append(f"  Cash: ${new_state['cash']:,.2f}")

    lines += [
        '',
        f'Full ranking this month (trailing {LOOKBACK_MONTHS}-month return, top 20 of the halal candidate universe):',
    ]
    for i, (ticker, score) in enumerate(ranked.head(20).items(), 1):
        marker = ' <- selected' if ticker in selected else ''
        lines.append(f'  {i:>2}. {ticker:<6} {score * 100:+.1f}%{marker}')

    lines += [
        '',
        '---',
        'This assumes every previous suggestion from this system was followed exactly, at the',
        'reference price shown. If you skipped a trade or got a different fill, tell Claude in',
        'chat so the tracker can be corrected — it has no way to check your real account itself.',
        '',
        'Every ticker here is a halal candidate only, not a verified-compliant list — verify',
        'independently. This is not investment advice. Price-only signal, no dividends, no',
        'transaction costs modeled beyond the small cash buffer above.',
    ]
    return subject, '\n'.join(lines)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> int:
    now = pd.Timestamp.now(tz='UTC')

    if PAUSE_PATH.exists():
        log.warning('EMAIL_PAUSE present at %s — skipping this run', PAUSE_PATH)
        return 0

    if not is_first_trading_day_of_month(now):
        log.info('not the first NYSE trading day of the month — nothing to do today (%s)', now.date())
        return 0

    month_key = f'{now.year:04d}-{now.month:02d}'
    state = load_state()
    if state['last_rebalance_month'] == month_key:
        log.info('already sent a suggestion for %s — exiting (idempotency guard)', month_key)
        return 0

    log.info('=== relative strength rotation email run: %s ===', now.isoformat())

    util.patchAsyncio()
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=20)
    except Exception:
        log.exception('could not connect to IBKR at %s:%d (market data only — no account touched)', IB_HOST, IB_PORT)
        return 1

    try:
        universe = load_universe()
        universe_set = set(universe)
        log.info('%d candidate halal stocks in the universe', len(universe))

        daily_closes = fetch_recent_daily_closes(ib, universe)
        selected, ranked, last_price = compute_target_basket(daily_closes)
    finally:
        ib.disconnect()

    current_shares = {t: info['shares'] for t, info in state['holdings'].items()}
    nav = portfolio_value(state, last_price)
    log.info('hypothetical portfolio value=$%.2f, %d existing holdings', nav, len(current_shares))

    if not selected:
        log.info('no rebalance action this month (see warning above if universe was too thin)')
        subject, body = render_email(month_key, [], [], ranked, nav,
                                      {'cash': state['cash'], 'holdings': state['holdings']})
        if send_email(subject, body):
            state['last_rebalance_month'] = month_key
            state['last_suggestion'] = {'month': month_key, 'computed_at': now.isoformat(), 'orders': []}
            save_state(state)
        return 0

    log.info('selected top %d by trailing %d-month return: %s', TOP_N, LOOKBACK_MONTHS, selected)
    log.info('full ranking:\n%s', (ranked * 100).round(1).to_string())

    orders = build_orders(nav, current_shares, selected, last_price, universe_set,
                           INVESTABLE_FRACTION, MAX_ORDER_FRACTION_OF_NAV, MIN_TRADE_NOTIONAL)
    log.info('%d suggested orders', len(orders))

    new_state = apply_orders_to_state(state, orders, selected, last_price)
    subject, body = render_email(month_key, orders, selected, ranked, nav, new_state)

    if send_email(subject, body):
        new_state['last_rebalance_month'] = month_key
        new_state['last_suggestion'] = {'month': month_key, 'computed_at': now.isoformat(), 'orders': orders}
        save_state(new_state)
        log.info('=== run complete, state advanced to %s ===', month_key)
    else:
        log.error('email did not send — state NOT advanced, will retry in full on next invocation')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# --------------------------------------------------------------------------------------
# Crontab line — runs daily at 8:40am America/Chicago (staggered 5 min after the live
# script's 8:35 slot, so the two don't open IBKR connections at the same instant) and
# no-ops on every day except each month's first NYSE trading session.
#
# 40 8 * * 1-5 /home/mmiah/.venv/bin/python3 /home/mmiah/halal-stock-strat/live/relative_strength_rotation_email.py >> /home/mmiah/halal-stock-strat/live/email_cron.log 2>&1
# --------------------------------------------------------------------------------------
