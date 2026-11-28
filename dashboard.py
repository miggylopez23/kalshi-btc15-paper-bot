import csv
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from collections import defaultdict

import streamlit as st

ROOT = Path(__file__).resolve().parent
STATE, TRADES = ROOT / "paper_state.json", ROOT / "paper_trades_v2.csv"
PIDFILE, LOGFILE = ROOT / "bot.pid", ROOT / "bot.log"
BOT = ROOT / "kalshi_btc15_paper_bot.py"

st.set_page_config(page_title="BTC 15m Paper Lab", page_icon="₿", layout="centered")
st.title("₿ Kalshi BTC 15-minute Paper Lab")
st.caption("Public market data • paper simulation only • no Kalshi orders")


def read_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            st.error("Could not read paper_state.json. Do not start or reset the bot until the file is checked.")
    return {
        "starting_bankroll": 100.0, "cash": 100.0, "bankroll": 100.0,
        "daily_pnl": 0.0, "position": None, "day": "Not started"
    }


def is_running():
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
        return False
    except (ValueError, PermissionError, OSError):
        return False


def start_bot():
    if is_running():
        return
    if not BOT.exists():
        st.error(f"Bot file not found: {BOT.name}")
        return
    log = open(LOGFILE, "a", buffering=1)
    process = subprocess.Popen(
        [sys.executable, "-u", str(BOT)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True
    )
    PIDFILE.write_text(str(process.pid))


def stop_bot():
    if not PIDFILE.exists():
        return
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        st.info("Stop signal sent. The bot should save its state and stop shortly.")
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
    except (ValueError, PermissionError, OSError) as error:
        st.error(f"Could not stop bot: {error}")


def load_trades():
    if not TRADES.exists():
        return []
    try:
        with TRADES.open(newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as error:
        st.error(f"Could not read trade ledger: {error}")
        return []


s = read_state()
running = is_running()
pos = s.get("position")
cash = float(s.get("cash", s.get("bankroll", 100)))
reserved = (float(pos.get("cost", 0)) + float(pos.get("fee", 0))) if pos else 0.0
equity = round(cash + reserved, 2)
start = float(s.get("starting_bankroll", 100))
net = round(equity - start, 2)

st.metric("Bot status", "RUNNING" if running else "STOPPED")
c1, c2 = st.columns(2)
c1.metric("Available cash", f"${cash:.2f}")
c2.metric("Book equity", f"${equity:.2f}", delta=f"${net:+.2f} vs start")
c3, c4 = st.columns(2)
c3.metric("Realized P/L", f"${float(s.get('realized_pnl', net)):+.2f}")
c4.metric("Today's settled P/L", f"${float(s.get('daily_pnl', 0)):+.2f}")
st.info(
    "Open-position value is carried at entry cost plus estimated fee until settlement; "
    "this is NOT a live resale valuation."
)
st.warning("Paper-only. This app does not submit Kalshi Demo or Production orders.")

st.subheader("📦 Download Backups")
st.caption("Download these files before code changes. Save them somewhere safe.")
backup_files = [
    ("paper_state.json", STATE, "application/json"),
    ("paper_trades_v2.csv", TRADES, "text/csv"),
    ("bot.log", LOGFILE, "text/plain"),
]
for filename, filepath, mime in backup_files:
    if filepath.exists():
        try:
            st.download_button(
                label=f"⬇️ Download {filename}",
                data=filepath.read_bytes(),
                file_name=filename,
                mime=mime,
                use_container_width=True,
                key=f"backup_{filename}",
            )
        except Exception as error:
            st.error(f"Could not prepare {filename}: {error}")
    else:
        st.caption(f"{filename}: Not created yet")

st.warning(
    "Streamlit Community Cloud storage is not a permanent backup. "
    "Download your state and trade files regularly and keep copies outside the app."
)

a, b = st.columns(2)
with a:
    if st.button("▶ Start paper bot", use_container_width=True, type="primary", disabled=running):
        start_bot()
        st.rerun()
with b:
    if st.button("■ Stop bot", use_container_width=True, disabled=not running):
        stop_bot()
        st.rerun()

st.subheader("Risk controls")
st.write("- Starting bankroll: as stored in paper_state.json")
st.write("- Maximum contract cost per trade: $2 (estimated fees additional)")
st.write("- Daily realized-loss pause: $10; open positions continue to be checked for settlement")
st.write("- One open position maximum")
st.write("- One entry per 15-minute ticker")

if pos:
    st.subheader("Open paper position")
    st.write(
        f"**{pos.get('side', '').upper()}** · {pos.get('contracts')} contracts · "
        f"entry ${float(pos.get('price', 0)):.2f}"
    )
    st.write(f"Contract cost: ${float(pos.get('cost', 0)):.2f}")
    st.write(f"Estimated fee: ${float(pos.get('fee', 0)):.2f}")
    st.write(f"Cash committed: ${reserved:.2f}")
    st.caption(
        f"Ticker: {pos.get('ticker', '')} · heuristic score "
        f"{pos.get('confidence_heuristic', 'n/a')} (not a probability)"
    )

trades = load_trades()
st.subheader("Performance tracker")
if trades:
    pnls, fees = [], []
    for trade in trades:
        try:
            pnls.append(float(trade.get("pnl") or 0))
        except (TypeError, ValueError):
            pass
        try:
            fees.append(float(trade.get("entry_fee_estimate") or 0))
        except (TypeError, ValueError):
            pass

    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    flats = sum(value == 0 for value in pnls)
    count = len(pnls)
    win_rate = wins / count * 100 if count else 0
    average = sum(pnls) / count if count else 0
    best = max(pnls) if pnls else 0
    worst = min(pnls) if pnls else 0
    net_pnl = sum(pnls)

    m1, m2 = st.columns(2)
    m1.metric("Settled trades", count)
    m2.metric("Win rate", f"{win_rate:.1f}%")
    m3, m4 = st.columns(2)
    m3.metric("Net P/L", f"${net_pnl:+.2f}")
    m4.metric("Average P/L / trade", f"${average:+.2f}")
    m5, m6 = st.columns(2)
    m5.metric("Estimated fees", f"${sum(fees):.2f}")
    m6.metric("Wins / losses", f"{wins} / {losses}")
    m7, m8 = st.columns(2)
    m7.metric("Best trade", f"${best:+.2f}")
    m8.metric("Worst trade", f"${worst:+.2f}")
    st.caption(
        f"{flats} breakeven trades. Net P/L uses recorded trade P/L, "
        "which already subtracts the estimated entry fee."
    )

    by_bucket = defaultdict(list)
    for trade in trades:
        bucket = (trade.get("entry_timing_bucket") or "").strip()
        try:
            value = float(trade.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if bucket:
            by_bucket[bucket].append(value)

    st.markdown("**Results by entry timing**")
    if by_bucket:
        timing_rows = []
        for bucket in ["First 3 min", "Minutes 3–7", "Minutes 7–11", "Final 4 min"]:
            values = by_bucket.get(bucket, [])
            if values:
                timing_rows.append({
                    "Entry timing": bucket,
                    "Trades": len(values),
                    "Win rate": f"{sum(value > 0 for value in values) / len(values) * 100:.1f}%",
                    "Net P/L": f"${sum(values):+.2f}",
                    "Average P/L": f"${sum(values) / len(values):+.2f}",
                })
        st.dataframe(timing_rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No entry-timing data is available in the recorded trades yet.")

    st.markdown("**Recent settled trades**")
    st.dataframe(trades[-25:][::-1], use_container_width=True, hide_index=True)
else:
    st.caption("No settled trades recorded yet. Performance stats appear after settlement.")

st.subheader("Bot log")
if LOGFILE.exists():
    try:
        lines = LOGFILE.read_text(errors="replace").splitlines()
        st.code("\n".join(lines[-40:]) or "Waiting for output…")
    except OSError as error:
        st.error(f"Could not read bot log: {error}")
else:
    st.caption("Log appears after the bot starts.")

if st.button("Refresh dashboard"):
    st.rerun()
