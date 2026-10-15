"""Resumable downloader for historical trade ticks, caching into data/ticks/{TICKER}.csv — the
data Phase 3 (trade imbalance) needs on top of the 1-minute bars Phases 1-2 used.

TRADES ONLY, NOT BID_ASK, DELIBERATELY: proper Lee-Ready classification (buy/sell based on
whether a trade printed closer to the prevailing bid or ask) needs matching quote ticks too,
roughly doubling the data pulled. This first pass instead uses the simpler "tick rule" (an
uptick from the previous trade price is classified a buy, a downtick a sell, an unchanged price
inherits the previous classification) — a standard, well-established classification method on
its own, just less precise than Lee-Ready. If Phase 3's results look promising enough to justify
it, adding BID_ASK ticks and switching to proper Lee-Ready classification is the natural
refinement — not assumed necessary up front.

SCOPE, DELIBERATELY BOUNDED: LOOKBACK_TRADING_DAYS defaults to 1, not a full month or even a
full week. Feasibility was checked first (2026-10-07): a short burst of 15 consecutive
reqHistoricalTicks calls for AAPL showed ~0.2s/request with no pacing issue, but a sustained
~1,445-chunk pull (5 trading days of AAPL) measured a real sustained rate closer to ~1.8s/chunk
— i.e. AAPL alone would take ~45 minutes for 5 days. The 131-ticker universe has very different
liquidity across names (several other mega-caps are roughly as tick-dense as AAPL), and nothing
at this granularity had been pulled before, so this starts at a single trading day across the
full universe rather than guessing at a longer window. A single day of tick data still gives
enormous statistical power per name (liquid names alone can produce hundreds of thousands of
trades in one session), which is why this is a reasonable first-pass scope despite being much
shorter than the 1-month minute-bar cache.
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
DATA_DIR = HERE / 'data' / 'ticks'
LOG_PATH = HERE / 'data' / 'ticks_download_log.csv'

IB_HOST = '172.27.224.1'
IB_PORT = 7497
IB_CLIENT_ID = 85  # distinct from every other script's client id in this project

EXCHANGE = 'SMART'
CURRENCY = 'USD'
USE_RTH = True

SKIP_TICKERS = {'NVDA', 'MRVL', 'META'}  # deliberately deferred:
                               # NVDA — during an unusually unstable TWS stretch on 2026-10-07,
                               # its exceptionally high tick density meant its fetch window was
                               # long enough to catch nearly every connectivity blip, repeatedly
                               # costing retries/time without finishing.
                               # MRVL — observed 2026-10-09: its first chunk fetches fine in
                               # isolation (confirmed directly, both with a fresh clientId and
                               # with this script's own clientId=85 — not a pacing/throttling
                               # issue), but fetch_ticker_ticks's walk-back loop hangs on it
                               # reproducibly every single time, at some chunk deep enough that it
                               # isn't the first request. Since there's no per-chunk checkpointing,
                               # every watchdog-triggered restart re-walks MRVL from scratch and
                               # re-hits the same hang — wasting 15+ minutes per cycle with zero
                               # progress. Revisit once the rest of the universe is done — likely
                               # worth adding per-chunk checkpointing or a smaller CHUNK_SIZE for
                               # this specific name rather than retrying the same approach again.
                               # META — same signature as MRVL (3 consecutive 15+ min stalls on
                               # the exact same ticker, 2026-10-09), skipped for the same reason.
                               # Two names with this same symptom makes a per-chunk checkpointing
                               # fix more worth doing than first assumed — see the MRVL note.

LOOKBACK_TRADING_DAYS = 1     # see module docstring — deliberately small to start
LOOKBACK_CALENDAR_BUFFER_DAYS = 1.5  # ~1 trading day's worth; a touch generous rather than precise
CHUNK_SIZE = 1000             # IBKR's per-request tick cap
CHUNK_SLEEP_SECONDS = 0.3     # safety margin; 15 back-to-back requests showed no pacing issue at all
PACING_SLEEP_SECONDS = 1.0    # between tickers
MAX_CHUNKS_PER_TICKER = 2000  # safety cap so a bug can't loop forever on one name
MAX_CHUNK_RETRIES = 5           # retries for a SINGLE chunk on an exception, not the whole ticker —
                                # TWS connectivity blips were observed recurring every few minutes
                                # on 2026-10-07, each resolving within seconds; without this, one
                                # blip mid-download cost 48 minutes of wasted progress on NVDA
                                # alone (every chunk already fetched for that ticker was discarded
                                # when the whole-ticker try/except in main() caught the error)
CHUNK_RETRY_SLEEP_SECONDS = 5   # pause between retries, giving TWS time to actually reconnect

# NOTE: an earlier version of this wrapped the plain sync reqHistoricalTicks call in
# asyncio.wait_for(reqHistoricalTicksAsync(...), timeout=...) to guard against a connectivity
# blip leaving the call hanging forever. That introduced a WORSE bug (observed 2026-10-09): once
# wait_for's cancellation fired on a slow request, every subsequent reqHistoricalTicks call for
# the rest of the run silently returned an empty list instead of raising — 20+ real, liquid
# tickers (META, CRM, ADBE, ...) logged as 'no_data' with no error at all, which is far more
# dangerous than a hang because nothing flags it. Reverted to the plain synchronous call (the
# same one every manual probe in this project used successfully); genuine hangs are instead
# meant to be caught by an external watchdog (see run_with_watchdog.sh) that restarts the whole
# process if the log goes stale, rather than fighting asyncio cancellation semantics in-process.


def _request_chunk_with_retries(ib, contract, cursor):
    last_exc = None
    for attempt in range(MAX_CHUNK_RETRIES):
        try:
            return ib.reqHistoricalTicks(
                contract, startDateTime='', endDateTime=cursor, numberOfTicks=CHUNK_SIZE,
                whatToShow='TRADES', useRth=USE_RTH,
            )
        except Exception as exc:
            last_exc = exc
            time.sleep(CHUNK_RETRY_SLEEP_SECONDS)
    raise last_exc


def fetch_ticker_ticks(ib, contract, stop_date):
    """Walk backward in CHUNK_SIZE-tick requests until stop_date is reached, IBKR returns
    nothing more, or MAX_CHUNKS_PER_TICKER is hit (safety cap, not expected to bind normally)."""
    chunks = []
    cursor = pd.Timestamp.now(tz='UTC').strftime('%Y%m%d %H:%M:%S') + ' UTC'  # IBKR needs an explicit endDateTime, not blank, for the first chunk
    for _ in range(MAX_CHUNKS_PER_TICKER):
        ticks = _request_chunk_with_retries(ib, contract, cursor)
        if not ticks:
            break
        chunks.append(ticks)
        oldest = ticks[0].time
        if oldest <= stop_date:
            break
        cursor = oldest.strftime('%Y%m%d %H:%M:%S') + ' UTC'
        time.sleep(CHUNK_SLEEP_SECONDS)
    if not chunks:
        return pd.DataFrame()
    rows = [(t.time, t.price, t.size) for chunk in chunks for t in chunk]
    frame = pd.DataFrame(rows, columns=['time', 'price', 'size']).drop_duplicates(subset='time')
    frame = frame.sort_values('time')
    return frame.loc[frame['time'] >= stop_date]


def main():
    with open(EQUITIES_LIST_PATH, newline='') as f:
        rows = list(csv.DictReader(f))
    universe = [row['Ticker'] for row in rows if row['Asset Type'] == 'Stock' and row['Ticker'] not in SKIP_TICKERS]
    print(f'{len(universe)} candidate halal stocks in the universe')

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_rows = list(csv.DictReader(open(LOG_PATH))) if LOG_PATH.exists() else []
    done = {row['ticker'] for row in log_rows if row['status'] == 'ok'}

    stop_date = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=LOOKBACK_CALENDAR_BUFFER_DAYS)

    util.patchAsyncio()
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=20)

    for i, ticker in enumerate(universe, 1):
        cache_path = DATA_DIR / f'{ticker}.csv'
        if ticker in done and cache_path.exists():
            print(f'[{i}/{len(universe)}] {ticker}: already cached, skipping')
            continue

        if not ib.isConnected():
            # Observed 2026-10-09: a hard session drop ("Peer closed connection") left every
            # subsequent ticker failing instantly with "Not connected" — 16 tickers were wasted
            # in seconds before this was caught. The chunk-level retry only covers transient
            # data-farm blips where the IB session itself stays up; a real session drop needs an
            # actual reconnect, not just waiting.
            print(f'[{i}/{len(universe)}] ib disconnected — reconnecting before continuing...', flush=True)
            ib.disconnect()
            time.sleep(2)
            ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=20)

        status, n_ticks, first_time, last_time, error = 'ok', 0, '', '', ''
        t0 = time.time()
        try:
            contract = Stock(ticker, EXCHANGE, CURRENCY)
            qualified = ib.qualifyContracts(contract)
            if not qualified or contract.conId == 0:
                raise RuntimeError('contract could not be qualified')
            frame = fetch_ticker_ticks(ib, contract, stop_date)
            if frame.empty:
                status = 'no_data'
            else:
                frame.to_csv(cache_path, index=False)
                n_ticks = len(frame)
                first_time, last_time = str(frame['time'].min()), str(frame['time'].max())
        except Exception as exc:
            status, error = 'error', str(exc)
        print(f'[{i}/{len(universe)}] {ticker}: {status} n_ticks={n_ticks} {first_time}..{last_time} '
              f'{error} ({time.time() - t0:.0f}s)', flush=True)
        log_rows = [r for r in log_rows if r['ticker'] != ticker]
        log_rows.append({'ticker': ticker, 'status': status, 'n_ticks': n_ticks,
                          'first_time': first_time, 'last_time': last_time, 'error': error})
        with open(LOG_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['ticker', 'status', 'n_ticks', 'first_time', 'last_time', 'error'])
            writer.writeheader()
            writer.writerows(log_rows)
        time.sleep(PACING_SLEEP_SECONDS)

    ib.disconnect()
    print('done')


if __name__ == '__main__':
    main()
