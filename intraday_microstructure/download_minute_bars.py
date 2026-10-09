"""One-time (well, resumable) downloader for 1-minute OHLCV bars, caching into
data/minute/{TICKER}.csv — the data Phase 1 of the intraday momentum/reversal baseline needs.

WHY A STANDALONE SCRIPT, NOT INLINE IN THE BACKTEST NOTEBOOK (unlike the hourly downloader,
which lives inside relative_strength_rotation_ibkr_backtest.ipynb): 1-minute bars are far slower
to pull from IBKR than hourly ones. A single 1-month/1-ticker request measured ~55s in testing
(2026-10-07), and a 6-month request timed out entirely at ib_insync's default 60s request
timeout. Hourly bars, by contrast, pulled a full YEAR per request near-instantly. That makes the
full-universe pull a multi-hour job — not something to run inside an interactive notebook cell.
Run this once in the background (`nohup python3 download_minute_bars.py &`), then the backtest
notebook just reads whatever's cached, same "assumes the cache already exists" convention as
every other notebook in this project.

SCOPE, DELIBERATELY BOUNDED: LOOKBACK_MONTHS defaults to 1, not years. At ~55s/chunk this is
already a ~2 hour job for the full ~131-ticker halal universe; a year of history the way the
hourly cache has would be a multi-day pull. One month of 1-minute bars is still ~390 bars/day x
~21 trading days x 131 tickers ~= 1M+ bar-observations — plenty to validate whether a momentum/
reversal baseline shows anything at all before spending a multi-day pull on more history. Bump
LOOKBACK_MONTHS and rerun (it's resumable and chunked) if the baseline looks promising enough to
justify more.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

import pandas as pd

try:
    from ib_insync import IB, Stock, util
except ImportError as exc:
    raise ImportError('Install ib-insync first: pip install ib-insync') from exc

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EQUITIES_LIST_PATH = REPO_ROOT / 'equities-list.csv'
DATA_DIR = HERE / 'data' / 'minute'
LOG_PATH = HERE / 'data' / 'minute_download_log.csv'

IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 80  # distinct from every other script's client id in this project

EXCHANGE = 'SMART'
CURRENCY = 'USD'
BAR_SIZE = '1 min'
WHAT_TO_SHOW = 'TRADES'
USE_RTH = True

LOOKBACK_MONTHS = 1       # see module docstring — deliberately small to start
CHUNK_DURATION = '1 M'    # per-request duration; 1-min bars measured far slower than hourly per chunk
PACING_SLEEP_SECONDS = 2.0
CHUNK_SLEEP_SECONDS = 2.0


def normalize_bars(bars):
    frame = util.df(bars)
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame['date'] = pd.to_datetime(frame['date'], utc=True)
    frame = frame.set_index('date').sort_index()
    keep = ['open', 'high', 'low', 'close', 'volume', 'average', 'barCount']
    return frame[[column for column in keep if column in frame.columns]]


def fetch_ticker_minute(ib, contract, lookback_months):
    """Walk backward one CHUNK_DURATION at a time for lookback_months chunks, or until IBKR has
    no earlier bars — same adaptive stop-on-empty pattern as the hourly downloader."""
    chunks, cursor, empty_in_a_row = [], pd.Timestamp.now(tz='UTC'), 0
    for _ in range(lookback_months):
        bars = ib.reqHistoricalData(
            contract, endDateTime=cursor.strftime('%Y%m%d %H:%M:%S UTC'), durationStr=CHUNK_DURATION,
            barSizeSetting=BAR_SIZE, whatToShow=WHAT_TO_SHOW,
            useRTH=USE_RTH, formatDate=2, keepUpToDate=False,
        )
        chunk = normalize_bars(bars)
        if chunk.empty:
            empty_in_a_row += 1
            if chunks or empty_in_a_row >= 2:
                break
        else:
            chunks.append(chunk)
            empty_in_a_row = 0
            cursor = chunk.index.min() - pd.Timedelta(minutes=1)
        time.sleep(CHUNK_SLEEP_SECONDS)
    if not chunks:
        return pd.DataFrame()
    result = pd.concat(chunks).sort_index()
    return result.loc[~result.index.duplicated(keep='last')]


def main():
    with open(EQUITIES_LIST_PATH, newline='') as f:
        rows = list(csv.DictReader(f))
    universe = [row['Ticker'] for row in rows if row['Asset Type'] == 'Stock']
    print(f'{len(universe)} candidate halal stocks in the universe')

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_rows = list(csv.DictReader(open(LOG_PATH))) if LOG_PATH.exists() else []
    done = {row['ticker'] for row in log_rows if row['status'] == 'ok'}

    util.patchAsyncio()
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=20)

    for i, ticker in enumerate(universe, 1):
        cache_path = DATA_DIR / f'{ticker}.csv'
        if ticker in done and cache_path.exists():
            print(f'[{i}/{len(universe)}] {ticker}: already cached, skipping')
            continue
        status, n_bars, first_date, last_date, error = 'ok', 0, '', '', ''
        t0 = time.time()
        try:
            contract = Stock(ticker, EXCHANGE, CURRENCY)
            qualified = ib.qualifyContracts(contract)
            if not qualified or contract.conId == 0:
                raise RuntimeError('contract could not be qualified')
            frame = fetch_ticker_minute(ib, contract, LOOKBACK_MONTHS)
            if frame.empty:
                status = 'no_data'
            else:
                frame.to_csv(cache_path, index_label='date')
                n_bars = len(frame)
                first_date, last_date = str(frame.index.min()), str(frame.index.max())
        except Exception as exc:
            status, error = 'error', str(exc)
        print(f'[{i}/{len(universe)}] {ticker}: {status} n_bars={n_bars} {first_date}..{last_date} '
              f'{error} ({time.time() - t0:.0f}s)', flush=True)
        log_rows = [r for r in log_rows if r['ticker'] != ticker]
        log_rows.append({'ticker': ticker, 'status': status, 'n_bars': n_bars,
                          'first_date': first_date, 'last_date': last_date, 'error': error})
        with open(LOG_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['ticker', 'status', 'n_bars', 'first_date', 'last_date', 'error'])
            writer.writeheader()
            writer.writerows(log_rows)
        time.sleep(PACING_SLEEP_SECONDS)

    ib.disconnect()
    print('done')


if __name__ == '__main__':
    main()
