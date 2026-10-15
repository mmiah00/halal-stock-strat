"""Builds dashboard.html: a standalone, real-money performance dashboard for the relative
strength rotation strategy's OWN managed capital only — explicitly excludes every other holding
in the Webull account (the pre-existing positions like SPUS/UBER/NIO/ADT/JMKE/SLV/MDLN/TEM, and
now PLTR too, orphaned after it was removed from the halal universe on equities-list.csv — see
that change's own discussion for why it's now unmanaged).

WHY A LOCAL FILE, NOT A PUBLISHED ARTIFACT: the Artifact publish tool was unavailable in the
session this was built (2026-10-09) — same blocker hit on 2026-09-30 when the LIVE tab in
momentum_bench.html was first built (see that work's project memory). This produces a
self-contained HTML file with the data baked in as a JS object (same pattern), openable directly
in a browser with no server needed. Re-run this script and reopen the file for a fresh snapshot;
publishing it as a real Artifact (for a shareable link, or a page that stays live without
rerunning this script) is future work once that tool is available.

DATA SOURCES, ALL REAL (no synthetic data anywhere):
  - NAV time series: extracted from this strategy's OWN "this strategy's own NAV ... = $X.XX"
    log lines in relative_strength_rotation_webull_intraday.log (the exact same number the live
    script computes before every rebalance decision) — not reconstructed or estimated.
  - One known-bad reading is dropped (2026-10-07 ~12:35 CT): the free-cash component that fed it
    was negative (-$16.90), which is impossible for this cash account (settled-cash-only by
    design — see webull_common.get_free_cash_and_positions) and traces to a 429-rate-limited
    stale balance read, not a real NAV. See DROPPED_BAD_NAV_TIMESTAMPS below.
  - Trade log: webull_intraday_trade_log.csv, pre-go-live DRY_RUN rows excluded. Each trade's
    notional is matched from the corresponding preview_order log line (same ticker/side within 5
    seconds) — 98/98 real trades matched cleanly as of this build.
  - Current state (today's portfolio value, free cash, positions): a fresh read-only call to the
    real Webull account via webull_common, at build time.

A DELIBERATE METHODOLOGY NOTE, surfaced in the dashboard's own "data notes" section too: the
live script's own NAV figure only ever counts SETTLED cash (by design, to make good-faith
violations structurally impossible), not cash that's pending settlement after a recent sell
(typically clears T+1). That means a NAV reading taken shortly after a sell can look like a
sharp, temporary drop that isn't a real loss — it reappears once the cash settles. This dashboard
handles that explicitly: the historical chart uses the strategy's own consistent method
throughout (comparable at every point), while the "Portfolio Value" summary card uses total
value including pending-settlement cash, for an accurate answer to "what do I actually have right
now" — the two are reconciled in the data notes rather than silently disagreeing.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from rotation_common import load_universe
from webull_common import build_trade_client, _as_json

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / 'relative_strength_rotation_webull_intraday.log'
TRADE_LOG_CSV = HERE / 'webull_intraday_trade_log.csv'
OUTPUT_HTML = HERE / 'dashboard.html'

# NOTE: this MUST be computed fresh from the current equities-list.csv, not hardcoded — the
# rotation strategy's whole point is rotating into a different top-10 basket every ~2 hours, so a
# fixed snapshot of "currently held" tickers goes stale within one cycle. A hardcoded 9-ticker set
# here (bug, fixed 2026-10-15) silently misclassified every actual current holding as "unmanaged"
# once the real basket rotated away from it, making the dashboard report a bogus ~55% loss that
# was actually a dashboard bug, not a real one.
ORPHANED_TICKERS = {'PLTR'}  # bought while in the universe, since removed (not halal) — see dashboard notes
DROPPED_BAD_NAV_TIMESTAMPS = {
    '2026-10-07T17:35:00+00:00': 'free cash read as -$16.90 (impossible for this cash account) — stale 429-rate-limited balance read',
}
STRATEGY_DESCRIPTION = (
    "Ranks a halal-screened universe of stocks by trailing price return and holds the top 10, "
    "equal-weighted, rebalancing every 2 hours during market hours. Names that fall out of the "
    "top ranks are sold and replaced by whatever is currently strongest — the same mechanic as "
    "classic relative-strength rotation, just run on hourly bars instead of monthly ones."
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('build_dashboard')


def parse_preview_notionals(log_text: str) -> pd.DataFrame:
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO preview_order response for (BUY|SELL) (\w+): "
        r"\{'estimated_cost': '([\d.]+)'",
        re.MULTILINE,
    )
    rows = []
    for m in pattern.finditer(log_text):
        ts_utc = (pd.Timestamp(m.group(1)) + pd.Timedelta(hours=5)).tz_localize('UTC')  # local (CDT) -> UTC
        rows.append({'ts_utc': ts_utc, 'side': m.group(2), 'ticker': m.group(3), 'notional': float(m.group(4))})
    return pd.DataFrame(rows)


def parse_nav_history(log_text: str) -> list[dict]:
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO this strategy's own NAV .*= \$([\d.]+)",
        re.MULTILINE,
    )
    points = []
    for m in pattern.finditer(log_text):
        ts_utc = (pd.Timestamp(m.group(1)) + pd.Timedelta(hours=5)).tz_localize('UTC')
        points.append({'timestamp': ts_utc.isoformat(), 'nav': float(m.group(2))})
    # drop the duplicate pre-go-live reading (identical value, logged minutes before the first real trade)
    deduped = []
    seen_values_at_start = set()
    for i, p in enumerate(points):
        if i == 0 and len(points) > 1 and p['nav'] == points[1]['nav']:
            continue  # the pre-trade snapshot, same value as go-live
        deduped.append(p)
    clean = [p for p in deduped if p['timestamp'] not in DROPPED_BAD_NAV_TIMESTAMPS]
    dropped = [p for p in deduped if p['timestamp'] in DROPPED_BAD_NAV_TIMESTAMPS]
    return clean, dropped


def build_trade_table(log_text: str) -> tuple[list[dict], dict]:
    rows = list(csv.DictReader(open(TRADE_LOG_CSV)))
    real_rows = [r for r in rows if r['success'] != 'DRY_RUN']
    previews = parse_preview_notionals(log_text)

    used = set()
    enriched = []
    for r in real_rows:
        ts = pd.Timestamp(r['timestamp'])
        notional = None
        if not previews.empty:
            cand = previews[(previews['side'] == r['side']) & (previews['ticker'] == r['ticker'])]
            cand = cand[(cand['ts_utc'] - ts).abs() < pd.Timedelta(seconds=5)]
            cand = cand[~cand.index.isin(used)]
            if len(cand):
                idx = (cand['ts_utc'] - ts).abs().idxmin()
                notional = float(previews.loc[idx, 'notional'])
                used.add(idx)
        enriched.append({
            'timestamp': r['timestamp'], 'side': r['side'], 'ticker': r['ticker'],
            'status': r['success'], 'notional': notional,
        })

    summary = {
        'total': len(enriched),
        'buys': sum(1 for t in enriched if t['side'] == 'BUY' and t['status'] == 'SUCCESS'),
        'sells': sum(1 for t in enriched if t['side'] == 'SELL' and t['status'] == 'SUCCESS'),
        'failed': sum(1 for t in enriched if t['status'] == 'FAILED'),
    }
    return enriched, summary


def fetch_current_state() -> dict:
    current_universe = set(load_universe())  # fresh read of equities-list.csv, not a stale snapshot

    trade_client, account_id = build_trade_client(log)
    positions_resp = _as_json(trade_client.account_v2.get_account_position(account_id))
    balance_resp = _as_json(trade_client.account_v2.get_account_balance(account_id))
    usd = balance_resp['account_currency_assets'][0]

    settled_cash = float(usd['settled_cash'])
    settled_cash_warning = None
    if settled_cash < 0:
        # Impossible for a cash account — a stale/rate-limited balance read (same signature as the
        # 2026-10-07 bad NAV point this dashboard already drops from history). Surface it rather
        # than silently show a number that can't be true; rerunning this script usually clears it.
        settled_cash_warning = (
            f'Settled cash just read as ${settled_cash:.2f}, which is impossible for a cash '
            'account — almost certainly a stale/rate-limited API read, not a real negative '
            'balance. Rerun build_dashboard.py for a corrected reading.'
        )

    managed, orphaned, unmanaged = [], [], []
    for p in positions_resp:
        entry = {
            'ticker': p['symbol'], 'shares': float(p['quantity']), 'cost_price': float(p['cost_price']),
            'last_price': float(p['last_price']), 'market_value': float(p['market_value']),
            'unrealized_pl': float(p['unrealized_profit_loss']),
        }
        if p['symbol'] in current_universe:
            managed.append(entry)
        elif p['symbol'] in ORPHANED_TICKERS:
            orphaned.append(entry)
        else:
            unmanaged.append(entry)

    return {
        'settled_cash': settled_cash,
        'settled_cash_warning': settled_cash_warning,
        'unsettled_cash': float(usd['unsettled_cash']),
        'managed_positions': managed,
        'orphaned_positions': orphaned,
        'unmanaged_count': len(unmanaged),
        'generated_at': pd.Timestamp.now(tz='UTC').isoformat(),
    }


def compute_stats(nav_clean: list[dict]) -> dict:
    navs = [p['nav'] for p in nav_clean]
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    running_max = np.maximum.accumulate(navs)
    drawdowns = [(navs[i] - running_max[i]) / running_max[i] for i in range(len(navs))]
    max_dd = min(drawdowns)
    max_dd_at = nav_clean[drawdowns.index(max_dd)]['timestamp']
    mean_ret = float(np.mean(rets)) if rets else float('nan')
    std_ret = float(np.std(rets, ddof=1)) if len(rets) > 1 else float('nan')
    return {
        'n_observations': len(navs),
        'max_drawdown_pct': max_dd,
        'max_drawdown_at': max_dd_at,
        'mean_cycle_return_pct': mean_ret,
        'std_cycle_return_pct': std_ret,
        'naive_sharpe_per_cycle': (mean_ret / std_ret) if std_ret else float('nan'),
    }


def main() -> int:
    log_text = LOG_PATH.read_text(errors='replace')
    nav_clean, nav_dropped = parse_nav_history(log_text)
    trades, trade_summary = build_trade_table(log_text)
    current = fetch_current_state()
    stats = compute_stats(nav_clean)

    current_positions_mv = sum(p['market_value'] for p in current['managed_positions'])
    current_true_value = current['settled_cash'] + current['unsettled_cash'] + current_positions_mv
    current_consistent_value = current['settled_cash'] + current_positions_mv  # same method as nav_clean

    inception_nav = nav_clean[0]['nav']
    inception_ts = nav_clean[0]['timestamp']

    data = {
        'strategy_name': 'Relative Rotation Strategy',
        'strategy_description': STRATEGY_DESCRIPTION,
        'inception_date': inception_ts[:10],
        'inception_timestamp': inception_ts,
        'inception_nav': inception_nav,
        'generated_at': current['generated_at'],
        'nav_history': nav_clean,
        'nav_dropped': [{'timestamp': ts, 'reason': DROPPED_BAD_NAV_TIMESTAMPS[ts]} for ts in DROPPED_BAD_NAV_TIMESTAMPS],
        'current': {
            **current,
            'managed_positions_market_value': current_positions_mv,
            'true_total_value': current_true_value,
            'consistent_method_value': current_consistent_value,
        },
        'total_pnl_dollars': current_true_value - inception_nav,
        'total_pnl_pct': (current_true_value - inception_nav) / inception_nav,
        'stats': stats,
        'trades': sorted(trades, key=lambda t: t['timestamp'], reverse=True),
        'trade_summary': trade_summary,
    }

    template = (HERE / 'dashboard_template.html').read_text()
    html = template.replace('/*__DASHBOARD_DATA__*/', json.dumps(data, indent=2))
    OUTPUT_HTML.write_text(html)
    log.info('wrote %s — portfolio true value $%.2f, total P&L $%.2f (%.1f%%)',
              OUTPUT_HTML, current_true_value, data['total_pnl_dollars'], data['total_pnl_pct'] * 100)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# --------------------------------------------------------------------------------------
# Crontab line — regenerates dashboard.html every 30 minutes during market hours (8am-4pm CT,
# weekdays), so a tab left open (auto-refreshing every 5 min, see dashboard_template.html) stays
# reasonably current without needing a manual rerun. Read-only: this never places an order or
# touches rotation_email_state.json, only reads the live Webull account and the intraday script's
# own log, so it's a much lower-risk cron addition than anything trading-related in this project.
#
# */30 8-15 * * 1-5 /home/mmiah/.venv/bin/python3 /home/mmiah/halal-stock-strat/relative_strength_rotation/live/build_dashboard.py >> /home/mmiah/halal-stock-strat/relative_strength_rotation/live/dashboard_build_cron.log 2>&1
# --------------------------------------------------------------------------------------
