"""Shared Webull broker-interaction logic, used by both relative_strength_rotation_webull.py
(monthly rebalance) and relative_strength_rotation_webull_intraday.py (2-hour rebalance).

Every request field name here was verified by reading the installed SDK's source
(webull-openapi-python-sdk). Every RESPONSE shape was verified against the real account on
2026-10-02 — see the comments on each extractor for exactly what was confirmed. There is no
Webull sandbox; this is the only verification available short of a real order.

Kept in one place deliberately: these functions required real debugging against the live API
(raw requests.Response objects needing .json(), a broken /trade/security endpoint needing the
market-data side's list_instruments instead, real balance/position field names) and duplicating
that across two scripts would let them silently drift apart.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pandas as pd

try:
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
except ImportError as exc:
    raise ImportError('Install the SDK first: pip install webull-openapi-python-sdk') from exc

HERE = Path(__file__).resolve().parent
CREDENTIALS_PATH = HERE / 'webull_credentials.env'

WEBULL_REGION_ID = 'us'
WEBULL_MARKET = 'US'
LIMIT_OFFSET_BPS = 50  # suggested marketable limit: buy at last*(1+offset), sell at last*(1-offset)


# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------
def load_webull_credentials() -> dict | None:
    """Returns {'app_key', 'app_secret', 'account_id'} (account_id may be ''), or None if
    the app key/secret aren't configured. Checks OS environment variables first
    (WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_ACCOUNT_ID), then a local KEY=VALUE file at
    CREDENTIALS_PATH — never type these into chat, drop them directly into the file on disk."""
    values = {
        'app_key': os.environ.get('WEBULL_APP_KEY'),
        'app_secret': os.environ.get('WEBULL_APP_SECRET'),
        'account_id': os.environ.get('WEBULL_ACCOUNT_ID'),
    }
    if not (values['app_key'] and values['app_secret']) and CREDENTIALS_PATH.exists():
        file_values = {}
        for line in CREDENTIALS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            file_values[key.strip()] = val.strip()
        values['app_key'] = values['app_key'] or file_values.get('WEBULL_APP_KEY')
        values['app_secret'] = values['app_secret'] or file_values.get('WEBULL_APP_SECRET')
        values['account_id'] = values['account_id'] or file_values.get('WEBULL_ACCOUNT_ID')
    if not (values['app_key'] and values['app_secret']):
        return None
    return values


def _as_json(response):
    """Confirmed by testing against the real API: every TradeClient/DataClient method returns
    a raw requests.Response, not pre-parsed data — .json() must be called explicitly."""
    if hasattr(response, 'json') and callable(response.json):
        return response.json()
    return response


def build_trade_client(log) -> tuple[TradeClient, str]:
    """Connects to Webull and resolves the account_id to trade against. Returns
    (trade_client, account_id). `log` is the caller's logger, so these messages land in the
    right log file.

    There used to be a DataClient here too, for ticker -> instrument_id lookup before placing
    an order. Removed once live order testing on 2026-10-05 showed 'symbol' alone is accepted
    by the order endpoint and instrument_id isn't needed for that — see place_order."""
    creds = load_webull_credentials()
    if creds is None:
        raise RuntimeError('Webull credentials not configured — see webull_credentials.env.example')
    api_client = ApiClient(creds['app_key'], creds['app_secret'], WEBULL_REGION_ID)
    trade_client = TradeClient(api_client)

    account_id = creds.get('account_id')
    if not account_id:
        accounts = _as_json(trade_client.account_v2.get_account_list())
        log.warning('WEBULL_ACCOUNT_ID not pinned in webull_credentials.env — '
                    'account list response (pin one of these to be safe): %s', accounts)
        account_id = _first_account_id(accounts)
        if not account_id:
            raise RuntimeError('could not determine an account_id from get_account_list() — pin WEBULL_ACCOUNT_ID manually')
    return trade_client, account_id


def _first_account_id(accounts_response) -> str | None:
    data = accounts_response
    if isinstance(data, dict):
        rows = data.get('data') or data.get('accounts') or data.get('account_list') or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, dict):
        return first.get('account_id') or first.get('accountId') or first.get('id')
    return None


# --------------------------------------------------------------------------------------
# Account state — field names CONFIRMED against the real account (2026-10-02): balance has
# settled_cash/buying_power inside account_currency_assets[0]; positions is a bare list of
# dicts with 'symbol' and 'quantity' (both strings).
# --------------------------------------------------------------------------------------
def get_free_cash_and_positions(trade_client: TradeClient, account_id: str) -> tuple[float, dict[str, float]]:
    """Returns (free_cash, real_positions). free_cash is this account's SETTLED cash only —
    never unsettled funds, which structurally prevents good-faith violations, since a GFV
    requires buying with unsettled funds and this can never do that. real_positions is
    {ticker: shares} for EVERY position in the account, managed-universe or not — the caller's
    universe filter (see rotation_common.build_orders) decides what's actually touched."""
    balance = _as_json(trade_client.account_v2.get_account_balance(account_id))
    free_cash = _extract_free_cash(balance)

    positions_resp = _as_json(trade_client.account_v2.get_account_position(account_id))
    positions = _extract_positions(positions_resp)
    return free_cash, positions


def _extract_free_cash(balance_response) -> float:
    assets = balance_response.get('account_currency_assets', [])
    usd = next((a for a in assets if a.get('currency') == 'USD'), assets[0] if assets else None)
    if usd is None:
        raise RuntimeError(f'no account_currency_assets in balance response: {balance_response!r}')
    for key in ('settled_cash', 'buying_power', 'cash_balance'):
        if key in usd:
            return float(usd[key])
    raise RuntimeError(f'could not find a settled-cash field — inspect and fix _extract_free_cash: {balance_response!r}')


def _extract_positions(positions_response) -> dict[str, float]:
    rows = positions_response if isinstance(positions_response, list) else []
    positions = {}
    for row in rows:
        symbol = row.get('symbol')
        qty = row.get('quantity')
        if symbol and qty is not None:
            positions[symbol] = float(qty)
    return positions


def managed_nav(free_cash: float, real_positions: dict[str, float], last_price, universe: set[str]) -> float:
    """This strategy's own NAV: free cash plus the market value of whatever it has ALREADY
    bought (positions inside its managed universe only). Deliberately excludes every other
    position in the account — those were never this strategy's capital. Without including
    already-deployed capital here, the next rebalance would try to buy an entirely new
    allocation on top of what's already held, snowballing forever."""
    deployed = sum(shares * last_price[ticker] for ticker, shares in real_positions.items()
                   if ticker in universe and ticker in last_price.index and pd.notna(last_price[ticker]))
    return free_cash + deployed


# --------------------------------------------------------------------------------------
# Order placement
# --------------------------------------------------------------------------------------
def place_order(trade_client: TradeClient, account_id: str, order_spec: dict,
                 dry_run: bool, log, append_trade_log) -> None:
    """append_trade_log(timestamp, side, ticker, status, balance) is injected by the caller so
    each script's trade log stays separate (different files for the monthly vs. intraday
    scripts), while the placement logic itself stays identical."""
    ticker, side, shares, ref_price = order_spec['ticker'], order_spec['side'], order_spec['shares'], order_spec['ref_price']
    timestamp = pd.Timestamp.now(tz='UTC')

    if dry_run:
        log.info('DRY-RUN %s %s shares=%.6f ref_price=%.4f notional=$%.2f', side, ticker, shares, ref_price, order_spec['notional'])
        status = 'DRY_RUN'
    else:
        offset = 1 + LIMIT_OFFSET_BPS / 10_000 * (1 if side == 'BUY' else -1)
        limit_price = round(ref_price * offset, 2)
        # Field names below were confirmed by live trial against the real order/preview
        # endpoint on 2026-10-05 — several differ from what the SDK's own bundled classes
        # suggest (e.g. 'qty'/'tif' there vs. 'quantity'/'time_in_force' here), and
        # 'entrust_type'/'support_trading_session' aren't in the SDK's builders at all.
        # 'symbol' alone is sufficient — instrument_id (from list_instruments) is NOT
        # required for placing an order, only for other lookups.
        new_order = {
            'client_order_id': str(uuid.uuid4()),
            'market': WEBULL_MARKET,
            'instrument_type': 'EQUITY',
            'symbol': ticker,
            'side': side,
            'order_type': 'LIMIT',
            'limit_price': limit_price,
            'entrust_type': 'QTY',
            'quantity': round(shares, 5),  # confirmed limit (2026-10-05): fractional order quantity allows at most 5 decimal places
            'time_in_force': 'DAY',
            'support_trading_session': 'N',  # regular trading hours only, matching useRTH=True on the IBKR side
        }
        try:
            preview = _as_json(trade_client.order_v3.preview_order(account_id, [new_order]))
            log.info('preview_order response for %s %s: %s', side, ticker, preview)
            trade = _as_json(trade_client.order_v3.place_order(account_id, [new_order]))
            log.info('place_order response for %s %s: %s', side, ticker, trade)
            status = 'SUCCESS'
        except Exception as exc:
            log.exception('order failed for %s %s', side, ticker)
            status = f'FAILED: {exc}'

    try:
        free_cash, _ = get_free_cash_and_positions(trade_client, account_id)
    except Exception:
        log.exception('could not re-read free cash after attempting %s %s', side, ticker)
        free_cash = None
    append_trade_log(timestamp, side, ticker, status, free_cash)


def confirm_live_authorized(dry_run_flag: bool, cli_confirm_live: bool) -> bool:
    return (not dry_run_flag) and cli_confirm_live and os.environ.get('CONFIRM_LIVE') == 'YES'


# --------------------------------------------------------------------------------------
# Trade pop-up (Windows toast + full-detail HTML) — fires once per rebalance cycle, not per
# order. Resilience is deliberate: every entry point here is meant to be wrapped by the
# caller in a try/except, since a notification failure must never be allowed to affect
# trading logic. See the module-level note in relative_strength_rotation_webull_intraday.py
# for why this exists and what was verified (Windows toast notifications genuinely cap at 3
# lines of text — confirmed by testing, not a library quirk — hence the toast-plus-HTML split).
# --------------------------------------------------------------------------------------
POPUP_DIR = HERE / 'trade_popups'
MAX_POPUP_FILES = 200  # keep this bounded; each cycle writes a new file, this runs unattended


def get_cost_basis(trade_client: TradeClient, account_id: str) -> dict[str, float]:
    """{ticker: average cost per share}, straight from Webull's own position data — this is
    the broker's real cost basis, not something this project tracks itself, so it stays
    correct even if a position was opened before this popup feature existed."""
    positions_resp = _as_json(trade_client.account_v2.get_account_position(account_id))
    rows = positions_resp if isinstance(positions_resp, list) else []
    basis = {}
    for row in rows:
        symbol = row.get('symbol')
        cost_price = row.get('cost_price')
        if symbol and cost_price is not None:
            basis[symbol] = float(cost_price)
    return basis


def compute_realized_pnl(sells: list[dict], cost_basis: dict[str, float], log) -> float:
    """Sum of (sell price - cost basis) x shares, across this cycle's sell orders only — buys
    don't realize anything yet, they're just capital being deployed. This is the standard
    meaning of 'profit on a trade': gain or loss on what was actually closed, not cash flow."""
    total = 0.0
    for order in sells:
        ticker = order['ticker']
        basis = cost_basis.get(ticker)
        if basis is None:
            log.warning('no cost basis found for %s — excluding it from the realized P&L total', ticker)
            continue
        total += (order['ref_price'] - basis) * order['shares']
    return total


def _cleanup_old_popups() -> None:
    files = sorted(POPUP_DIR.glob('trade_*.html'), key=lambda p: p.stat().st_mtime)
    for stale in files[:-MAX_POPUP_FILES]:
        stale.unlink(missing_ok=True)


def build_trade_detail_html(timestamp: pd.Timestamp, sells: list[dict], buys: list[dict], net_profit: float) -> Path:
    """Writes one HTML file per cycle (never overwritten) and returns its path, so clicking
    an older toast notification later always opens the numbers that were true at that time,
    not whatever the most recent cycle happens to be."""
    POPUP_DIR.mkdir(parents=True, exist_ok=True)

    def rows(orders):
        return ''.join(
            f'<tr><td class="t">{o["ticker"]}</td><td class="n">{o["shares"]:.4f}</td>'
            f'<td class="n">${o["notional"]:,.2f}</td></tr>'
            for o in orders
        ) or '<tr><td colspan="3" class="empty">none this cycle</td></tr>'

    pnl_class = 'pos' if net_profit >= 0 else 'neg'
    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Rotation trade — {timestamp.isoformat()}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #faf9f5; color: #141413;
         padding: 32px; max-width: 700px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .sub {{ color: #898781; font-size: 13px; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
  th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
        color: #898781; padding: 6px 10px; border-bottom: 2px solid #e1e0d9; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #e1e0d9; font-size: 14px; }}
  td.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.empty {{ color: #898781; font-style: italic; text-align: center; }}
  .cols {{ display: flex; gap: 24px; }}
  .col {{ flex: 1; }}
  .sell th {{ color: #d03b3b; }}
  .buy th {{ color: #0ca30c; }}
  .pnl {{ font-size: 15px; padding: 16px 20px; border-radius: 10px; background: #f2f1ec; }}
  .pnl .amt {{ font-size: 28px; font-weight: 600; font-family: ui-monospace, monospace; }}
  .pos {{ color: #0ca30c; }}
  .neg {{ color: #d03b3b; }}
</style></head><body>
  <h1>Relative strength rotation — trade executed</h1>
  <div class="sub">{timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')}</div>
  <div class="cols">
    <div class="col">
      <table class="sell"><thead><tr><th>Sell</th><th>Shares</th><th>Amount</th></tr></thead>
        <tbody>{rows(sells)}</tbody></table>
    </div>
    <div class="col">
      <table class="buy"><thead><tr><th>Buy</th><th>Shares</th><th>Amount</th></tr></thead>
        <tbody>{rows(buys)}</tbody></table>
    </div>
  </div>
  <div class="pnl">Net realized P&amp;L on this cycle's sells
    <div class="amt {pnl_class}">{'+' if net_profit >= 0 else ''}{net_profit:,.2f}</div>
    <div style="color:#898781;font-size:12px;margin-top:4px">
      Only sells realize a gain/loss — buys are capital being deployed, not profit yet.</div>
  </div>
</body></html>'''

    path = POPUP_DIR / f'trade_{timestamp.strftime("%Y%m%dT%H%M%SZ")}.html'
    path.write_text(html)
    _cleanup_old_popups()
    return path


def send_trade_popup(timestamp: pd.Timestamp, sells: list[dict], buys: list[dict],
                      net_profit: float, dry_run: bool, log) -> None:
    """Writes the detail HTML, then fires a 3-line Windows toast (the hard limit on a Windows
    toast's text content — confirmed by testing, see relative_strength_rotation_webull_intraday.py)
    whose click action opens that HTML. Requires running where WSL-to-Windows interop works
    (powershell.exe reachable) — if it isn't, this logs a warning and does nothing else; a
    notification failure must never block or fail the actual trading run."""
    import subprocess

    try:
        detail_path = build_trade_detail_html(timestamp, sells, buys, net_profit)
        win_path = subprocess.run(['wslpath', '-w', str(detail_path)], capture_output=True, text=True, check=True).stdout.strip()

        prefix = '[DRY RUN] ' if dry_run else ''
        line1 = f'{prefix}{timestamp.strftime("%Y-%m-%d %H:%M %Z")}'
        line2 = f'{len(sells)} sell(s), {len(buys)} buy(s) this cycle'
        sign = '+' if net_profit >= 0 else ''
        line3 = f'Net realized P&L: {sign}${net_profit:,.2f} (click for detail)'

        ps_script = f'''
Import-Module BurntToast
$t1 = New-BTText -Text {_ps_quote(line1)}
$t2 = New-BTText -Text {_ps_quote(line2)}
$t3 = New-BTText -Text {_ps_quote(line3)}
$binding = New-BTBinding -Children $t1, $t2, $t3
$visual = New-BTVisual -BindingGeneric $binding
$content = New-BTContent -Visual $visual -Launch {_ps_quote('file:///' + win_path)} -ActivationType Protocol
Submit-BTNotification -Content $content
'''
        result = subprocess.run(
            ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning('trade popup toast failed (non-fatal): %s', result.stderr[:500])
        else:
            log.info('trade popup sent, detail at %s', detail_path)
    except Exception:
        log.exception('could not send trade popup (non-fatal, trading was not affected)')


def send_failure_popup(stage: str, error_message: str, log_path: Path, log) -> None:
    """Fires a 3-line Windows toast when a scheduled cycle fails before it ever gets to placing
    orders (e.g. Webull auth/connection failed, IBKR unreachable) — the silent-failure case that
    send_trade_popup doesn't cover, since that one only fires once a cycle actually has sells or
    buys to report. Clicking the toast opens the script's own log file (no separate detail page
    needed here — the log already has the full traceback). Same resilience contract as
    send_trade_popup: wrapped in try/except so a notification problem never raises into the
    caller, which is already in its own failure path when this is called."""
    import subprocess

    try:
        win_log_path = subprocess.run(['wslpath', '-w', str(log_path)], capture_output=True, text=True, check=True).stdout.strip()

        timestamp = pd.Timestamp.now(tz='UTC')
        line1 = f'{timestamp.strftime("%Y-%m-%d %H:%M %Z")}'
        line2 = f'TRADING CYCLE FAILED: {stage}'
        line3 = f'{error_message[:120]} (click for log)'

        ps_script = f'''
Import-Module BurntToast
$t1 = New-BTText -Text {_ps_quote(line1)}
$t2 = New-BTText -Text {_ps_quote(line2)}
$t3 = New-BTText -Text {_ps_quote(line3)}
$binding = New-BTBinding -Children $t1, $t2, $t3
$visual = New-BTVisual -BindingGeneric $binding
$content = New-BTContent -Visual $visual -Launch {_ps_quote('file:///' + win_log_path)} -ActivationType Protocol
Submit-BTNotification -Content $content
'''
        result = subprocess.run(
            ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning('failure popup toast failed (non-fatal): %s', result.stderr[:500])
        else:
            log.info('failure popup sent for stage=%s', stage)
    except Exception:
        log.exception('could not send failure popup (non-fatal)')


def _ps_quote(s: str) -> str:
    """Single-quoted PowerShell string literal — doubling embedded single quotes is PowerShell's
    own escaping rule, not a security workaround, so this is safe for the trusted, locally-built
    strings this module passes through it (tickers, dollar amounts, a local file path)."""
    return "'" + s.replace("'", "''") + "'"
