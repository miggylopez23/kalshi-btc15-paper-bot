import csv, json, os, subprocess, sys, time
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "paper_state.json"
TRADES = ROOT / "paper_trades.csv"
PIDFILE = ROOT / "bot.pid"
LOGFILE = ROOT / "bot.log"
BOT = ROOT / "kalshi_btc15_paper_bot.py"

st.set_page_config(page_title="BTC 15m Demo Lab", page_icon="₿", layout="centered")
st.title("₿ Kalshi BTC 15-minute Bot")
st.caption("Cloud dashboard • SAFE MODE: local paper simulation only")

def read_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except Exception: return {}
    return {"bankroll": 100.0, "daily_pnl": 0.0, "position": None, "day": "Not started"}

def is_running():
    if not PIDFILE.exists(): return False
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def start_bot():
    if is_running(): return
    log = open(LOGFILE, "a", buffering=1)
    p = subprocess.Popen([sys.executable, str(BOT)], cwd=str(ROOT),
                         stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    PIDFILE.write_text(str(p.pid))

def stop_bot():
    if not PIDFILE.exists(): return
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 15)
    except Exception:
        pass
    PIDFILE.unlink(missing_ok=True)

state = read_state()
running = is_running()
c1, c2 = st.columns(2)
c1.metric("Bot status", "RUNNING" if running else "STOPPED")
c2.metric("Paper bankroll", f"${float(state.get('bankroll', 100)):.2f}")
c3, c4 = st.columns(2)
c3.metric("Today's paper P/L", f"${float(state.get('daily_pnl', 0)):+.2f}")
pos = state.get("position")
c4.metric("Open paper position", "Yes" if pos else "No")

st.warning("This build does NOT place orders in Kalshi Demo or Production. It only simulates trades from public market data. No API keys are requested or stored.")
a, b = st.columns(2)
with a:
    if st.button("▶ Start paper bot", use_container_width=True, type="primary", disabled=running):
        start_bot()
        st.rerun()
with b:
    if st.button("■ Stop bot", use_container_width=True, disabled=not running):
        stop_bot()
        st.rerun()

st.subheader("Risk settings in this build")
st.write("- Starting paper bankroll: $100")
st.write("- Max paper spend per trade: $2")
st.write("- Daily paper loss stop: $10")
st.write("- Maximum open positions: 1")
st.write("- BTC 15-minute series only; no new entries near settlement")

if pos:
    st.subheader("Open simulated position")
    st.json(pos)

st.subheader("Recent settled paper trades")
if TRADES.exists():
    try:
        with TRADES.open(newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            st.dataframe(rows[-20:][::-1], use_container_width=True, hide_index=True)
        else:
            st.info("No settled paper trades yet.")
    except Exception as e:
        st.info(f"Trade log not ready: {e}")
else:
    st.info("No paper trades yet. Start the bot and let it observe market windows.")

st.subheader("Bot log")
if LOGFILE.exists():
    lines = LOGFILE.read_text(errors="replace").splitlines()
    st.code("\n".join(lines[-35:]) or "Waiting for output…")
else:
    st.caption("Log will appear after starting the bot.")

st.caption("Strategy signals are heuristic, not proven predictive probabilities. Run for at least 7–14 days before evaluating results.")
if st.button("Refresh dashboard"):
    st.rerun()
