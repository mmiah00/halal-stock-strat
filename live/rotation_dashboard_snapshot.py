"""Builds live/dashboard_snapshot.json: a read-only snapshot of the relative-strength rotation
strategy's hypothetical $150 portfolio, for the website's live dashboard tab.

This NEVER places an order and NEVER mutates rotation_email_state.json — it only reads that
state (written by relative_strength_rotation_email.py) and fetches fresh prices for whatever
tickers are currently relevant, via a read-only IBKR market-data connection.

Meant to be run periodically (e.g. every few minutes during market hours) by a recurring job
that also pushes this file's contents into the published website. As of 2026-09-30 that push
step doesn't exist yet — the Artifact tool needed to wire it up was unavailable when this was
built. Until that's connected, this file is only useful for local testing of the dashboard UI.

Output shape:
{
  "generated_at": ISO timestamp,
  "portfolio_value": float,
  "cash": float,
  "holdings": [{"ticker", "shares", "price", "value", "weight_pct"}, ...],
  "ranking": [{"ticker", "score_pct", "selected"}, ...]   (top 20),
  "last_suggestion": {"month", "computed_at", "orders": [...]} or null
}
"""
from __future__ import annotations

import json
import sys
import logging
from pathlib import Path

import pandas as pd

try:
    from ib_insync import IB, util
except ImportError as exc:
    raise ImportError('Install ib-insync first: pip install ib-insync') from exc

from rotation_common import load_universe, fetch_recent_daily_closes, compute_target_basket

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / 'rotation_dashboard_snapshot.log'
STATE_PATH = HERE / 'rotation_email_state.json'
SNAPSHOT_PATH = HERE / 'dashboard_snapshot.json'

IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 72

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('rsr_dashboard_snapshot')


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {'cash': 150.0, 'holdings': {}, 'last_rebalance_month': None, 'last_suggestion': None}


def build_snapshot() -> dict:
    state = load_state()
    now = pd.Timestamp.now(tz='UTC')

    util.patchAsyncio()
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=20)
    try:
        universe = load_universe()
        log.info('%d candidate halal stocks in the universe', len(universe))
        daily_closes = fetch_recent_daily_closes(ib, universe)
        selected, ranked, last_price = compute_target_basket(daily_closes)
    finally:
        ib.disconnect()

    holdings_out = []
    portfolio_value = state['cash']
    for ticker, info in state['holdings'].items():
        price = last_price.get(ticker)
        if price is None or not pd.notna(price):
            price = info['last_price']
        value = info['shares'] * price
        portfolio_value += value
        holdings_out.append({'ticker': ticker, 'shares': info['shares'], 'price': float(price), 'value': value})

    for h in holdings_out:
        h['weight_pct'] = (h['value'] / portfolio_value * 100) if portfolio_value else 0.0
    holdings_out.sort(key=lambda h: -h['weight_pct'])

    ranking_out = [
        {'ticker': ticker, 'score_pct': float(score * 100), 'selected': ticker in selected}
        for ticker, score in ranked.head(20).items()
    ]

    return {
        'generated_at': now.isoformat(),
        'portfolio_value': portfolio_value,
        'cash': state['cash'],
        'holdings': holdings_out,
        'ranking': ranking_out,
        'last_suggestion': state.get('last_suggestion'),
    }


def main() -> int:
    try:
        snapshot = build_snapshot()
    except Exception:
        log.exception('failed to build dashboard snapshot')
        return 1
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2))
    log.info('wrote snapshot: portfolio_value=$%.2f, %d holdings, %d ranked',
              snapshot['portfolio_value'], len(snapshot['holdings']), len(snapshot['ranking']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
