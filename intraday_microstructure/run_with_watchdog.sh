#!/bin/bash
# Runs download_trade_ticks.py under an external watchdog: if the log file goes STALE_MINUTES
# without a new line, the python process is killed and restarted. This replaces an earlier
# in-process timeout (asyncio.wait_for around reqHistoricalTicks) that was removed after it
# caused a worse bug on 2026-10-09 — see download_trade_ticks.py's module-level comment above
# _request_chunk_with_retries for why. A real hang (observed once, 2026-10-07, after a TWS
# connectivity blip) is rare enough that an external, OS-level watchdog is a simpler and safer
# fix than fighting asyncio cancellation semantics inside the script itself.
set -u
cd "$(dirname "$0")"

SCRIPT=download_trade_ticks.py
LOG=download_trade_ticks.log
PYTHON=/home/mmiah/.venv/bin/python3
STALE_MINUTES=15  # briefly raised to 25 on 2026-10-15 on the theory ORCL was just slow, not
                  # hung — it hung anyway (confirmed by a precise 1561s-stale measurement), so
                  # ORCL was added to SKIP_TICKERS instead and this reverted to 15: a longer
                  # threshold only delays catching a genuine hang, it doesn't rescue one.
CHECK_INTERVAL_SECONDS=60

while true; do
    "$PYTHON" "$SCRIPT" >> "$LOG" 2>&1 &
    PID=$!
    echo "$(date): started $SCRIPT as pid $PID" >> "$LOG"

    while kill -0 "$PID" 2>/dev/null; do
        sleep "$CHECK_INTERVAL_SECONDS"
        if [ -f "$LOG" ]; then
            LAST_MODIFIED=$(stat -c %Y "$LOG")
            NOW=$(date +%s)
            STALE_SECONDS=$((NOW - LAST_MODIFIED))
            if [ "$STALE_SECONDS" -gt $((STALE_MINUTES * 60)) ]; then
                echo "$(date): log stale for ${STALE_SECONDS}s, killing pid $PID and restarting" >> "$LOG"
                kill "$PID" 2>/dev/null
                sleep 2
                kill -9 "$PID" 2>/dev/null
                break
            fi
        fi
    done

    if ! kill -0 "$PID" 2>/dev/null; then
        wait "$PID" 2>/dev/null
        EXIT_CODE=$?
        if [ "$EXIT_CODE" -eq 0 ] && grep -q "^done$" "$LOG" 2>/dev/null; then
            echo "$(date): $SCRIPT finished cleanly (exit 0, 'done' seen) — watchdog exiting" >> "$LOG"
            break
        fi
        echo "$(date): $SCRIPT exited (code $EXIT_CODE) without a clean 'done' — restarting in 5s" >> "$LOG"
        sleep 5
    fi
done
