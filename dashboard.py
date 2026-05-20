"""
SolMoon Dashboard — visualize your bot's performance.

Usage:
    pip install streamlit pandas plotly
    streamlit run dashboard.py

Then open http://localhost:8501 in your browser.

The dashboard reads from these files (auto-created by the bot):
  - stats.json                  : W/L counters, daily PnL, circuit breaker
  - balance_history.json        : balance snapshots over time
  - wallet_signal_stats.json    : per-wallet signal performance

Auto-refreshes every 30 seconds.
"""

import json
import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SolMoon Dashboard",
    page_icon="🌙",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Custom CSS for darker Solana-aesthetic theme
st.markdown("""
<style>
    .stApp {
        background-color: #0B0E11;
    }
    [data-testid="stMetricValue"] {
        font-size: 2rem;
        color: #FFFFFF;
    }
    [data-testid="stMetricLabel"] {
        color: #B6BCC6;
    }
    [data-testid="stMetricDelta"] {
        font-weight: 600;
    }
    h1, h2, h3 { color: #FFFFFF; }
    .stProgress > div > div > div > div {
        background-color: #9945FF;
    }
</style>
""", unsafe_allow_html=True)

# Auto-refresh every 30s
st.markdown('<meta http-equiv="refresh" content="30">', unsafe_allow_html=True)

CHALLENGE_START_BALANCE = 0.800607   # SOL — Day 0 anchor
CHALLENGE_START_DATE = datetime(2026, 5, 19)
PALIERS = [
    {"name": "Foundation", "target": 1.5, "icon": "🌱"},
    {"name": "Acceleration", "target": 3.0, "icon": "🚀"},
    {"name": "Compounding", "target": 6.0, "icon": "⚡"},
    {"name": "Scale", "target": 12.0, "icon": "🔥"},
]


# ─────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


stats = load_json("stats.json", {})
history = load_json("balance_history.json", [])
wallet_stats = load_json("wallet_signal_stats.json", {})


# ─────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────
col_title, col_status = st.columns([3, 1])
with col_title:
    st.title("🌙 SolMoon Dashboard")
    st.caption(
        f"Personal Challenge — Day 0: {CHALLENGE_START_DATE.strftime('%Y-%m-%d')} "
        f"· Last update: {datetime.now().strftime('%H:%M:%S')}"
    )
with col_status:
    cb_until = stats.get("circuit_breaker_until", 0)
    import time
    if cb_until > time.time():
        h = int((cb_until - time.time()) / 3600)
        st.error(f"🛑 CIRCUIT BREAKER\nactive {h}h")
    else:
        st.success("✅ Bot ACTIVE")


# ─────────────────────────────────────────────────────────────────────
# Top metrics — Current capital & key stats
# ─────────────────────────────────────────────────────────────────────
current_balance = history[-1]["balance_sol"] if history else CHALLENGE_START_BALANCE
delta_sol = current_balance - CHALLENGE_START_BALANCE
delta_pct = (delta_sol / CHALLENGE_START_BALANCE * 100) if CHALLENGE_START_BALANCE > 0 else 0

wins = stats.get("wins", 0)
losses = stats.get("losses", 0)
total_trades = stats.get("total_trades", 0)
win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
days_elapsed = (datetime.now() - CHALLENGE_START_DATE).days + 1

m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.metric(
        "💰 Capital",
        f"{current_balance:.4f} SOL",
        f"{delta_sol:+.4f} ({delta_pct:+.1f}%)",
    )
with m2:
    st.metric("✅ Wins", wins)
with m3:
    st.metric("❌ Losses", losses)
with m4:
    st.metric("🎯 Win Rate", f"{win_rate:.0f}%")
with m5:
    st.metric("📅 Day", f"{days_elapsed} / 45")


# ─────────────────────────────────────────────────────────────────────
# Challenge progress — paliers
# ─────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🎯 Challenge progress")

current_palier_idx = 0
for i, p in enumerate(PALIERS):
    if current_balance < p["target"]:
        current_palier_idx = i
        break
else:
    current_palier_idx = len(PALIERS) - 1

prev_target = (
    CHALLENGE_START_BALANCE if current_palier_idx == 0
    else PALIERS[current_palier_idx - 1]["target"]
)
next_target = PALIERS[current_palier_idx]["target"]
progress = (
    (current_balance - prev_target) / (next_target - prev_target)
    if next_target > prev_target else 1.0
)
progress = max(0.0, min(1.0, progress))

st.write(
    f"**{PALIERS[current_palier_idx]['icon']} Palier #{current_palier_idx + 1} — {PALIERS[current_palier_idx]['name']}** "
    f"· {prev_target:.2f} SOL → **{next_target:.2f} SOL** "
    f"· Progress: {progress*100:.0f}%"
)
st.progress(progress)

# All paliers overview
cols = st.columns(4)
for i, p in enumerate(PALIERS):
    with cols[i]:
        status = "✅" if current_balance >= p["target"] else ("🟡" if i == current_palier_idx else "⬜")
        st.markdown(
            f"<div style='text-align:center; padding:10px; "
            f"background:{'#9945FF22' if i == current_palier_idx else '#161A1E'}; "
            f"border-radius:8px;'>"
            f"<div style='font-size:1.5rem'>{p['icon']}</div>"
            f"<div style='color:#B6BCC6'>{status} {p['name']}</div>"
            f"<div style='color:#FFF; font-weight:600'>{p['target']:.1f} SOL</div>"
            f"</div>",
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────
# Capital chart
# ─────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📈 Capital over time")

if len(history) > 1:
    df = pd.DataFrame(history)
    df["datetime"] = pd.to_datetime(df["iso"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["datetime"], y=df["balance_sol"],
        mode="lines+markers",
        line=dict(color="#9945FF", width=2),
        marker=dict(size=5, color="#14F195"),
        fill="tozeroy",
        fillcolor="rgba(153, 69, 255, 0.1)",
        name="Balance",
    ))
    # Add reference lines
    fig.add_hline(
        y=CHALLENGE_START_BALANCE, line_dash="dash",
        line_color="#B6BCC6", annotation_text="Start",
    )
    fig.add_hline(
        y=0.50, line_dash="dot",
        line_color="#F6465D", annotation_text="Kill switch",
    )
    fig.add_hline(
        y=next_target, line_dash="dash",
        line_color="#14F195", annotation_text=f"Next palier ({next_target:.1f})",
    )
    fig.update_layout(
        plot_bgcolor="#0B0E11", paper_bgcolor="#0B0E11",
        font_color="#FFFFFF",
        height=350, margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(gridcolor="#2A3038"),
        yaxis=dict(gridcolor="#2A3038", title="SOL"),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info(
        "📊 Pas encore assez de snapshots pour afficher la courbe. "
        "Le bot sauvegarde un point toutes les 30 min. Reviens dans 1-2 heures."
    )


# ─────────────────────────────────────────────────────────────────────
# Wallet performance leaderboard
# ─────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🔁 Wallet performance leaderboard")

if wallet_stats:
    rows = []
    for addr, s in wallet_stats.items():
        sent = s.get("sent", 0)
        resolved = s.get("resolved", 0)
        expired = s.get("expired", 0)
        if sent == 0:
            continue
        rate = resolved / sent * 100
        emoji = "🟢" if rate >= 50 else ("🟡" if rate >= 25 else "🔴")
        rows.append({
            "": emoji,
            "Wallet": f"{addr[:8]}...{addr[-4:]}",
            "Full address": addr,
            "Signals sent": sent,
            "Resolved": resolved,
            "Expired": expired,
            "Resolution %": round(rate, 1),
        })
    if rows:
        df_w = pd.DataFrame(rows).sort_values("Resolution %", ascending=False)
        st.dataframe(
            df_w, use_container_width=True, hide_index=True,
            column_config={
                "Resolution %": st.column_config.ProgressColumn(
                    "Resolution %", format="%.0f%%",
                    min_value=0, max_value=100,
                ),
            },
        )
        cold = df_w[(df_w["Signals sent"] >= 5) & (df_w["Resolution %"] < 25)]
        if not cold.empty:
            st.warning(
                f"⚠️ {len(cold)} cold wallet(s) (≥5 sent, <25% resolved) — "
                f"consider removing from `wallet_tracker.py`:"
            )
            for _, r in cold.iterrows():
                st.code(r["Full address"], language=None)
    else:
        st.info("Pas encore de signaux trackés.")
else:
    st.info(
        "🔍 Aucun fichier `wallet_signal_stats.json` trouvé. "
        "Il sera créé dès que les alpha wallets émettent leur premier signal."
    )


# ─────────────────────────────────────────────────────────────────────
# Today's status — circuit breaker & daily PnL
# ─────────────────────────────────────────────────────────────────────
st.markdown("---")
col_a, col_b = st.columns(2)
with col_a:
    st.subheader("📅 Today")
    daily_pnl = stats.get("daily_pnl_sol", 0.0)
    daily_start = stats.get("daily_start_balance", 0.0)
    daily_pct = (daily_pnl / daily_start * 100) if daily_start > 0 else 0
    st.metric(
        "Daily PnL",
        f"{daily_pnl:+.4f} SOL",
        f"{daily_pct:+.2f}%",
    )
    breaker_threshold = -10.0
    st.write(
        f"Circuit breaker triggers at **{breaker_threshold}%** intraday. "
        f"Current: **{daily_pct:+.2f}%**"
    )
    if daily_pct <= breaker_threshold:
        st.error("🚨 Circuit breaker should fire!")
    elif daily_pct <= -5:
        st.warning("⚠️ Approaching circuit breaker")
    else:
        st.success("✅ Safe zone")

with col_b:
    st.subheader("📜 Bot info")
    st.write(f"**Total trades:** {total_trades}")
    last_update = stats.get("last_updated_human", "—")
    st.write(f"**Last bot update:** {last_update}")
    if history:
        snapshots_taken = len(history)
        first_iso = history[0].get("iso", "—")[:19]
        st.write(f"**Balance snapshots:** {snapshots_taken} (since {first_iso})")


# ─────────────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "🌙 SolMoon Bot · "
    "[github.com/0xMchd/solmoon-bot](https://github.com/0xMchd/solmoon-bot) · "
    "[Premium](https://0xmchd.gumroad.com/l/afsebf) · "
    "Built by [@0xMchd](https://twitter.com/0xMchd) · "
    "Auto-refresh 30s"
)
