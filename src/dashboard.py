"""Streamlit dashboard for the SportArbitrage scanner.

Run:  streamlit run src/dashboard.py
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import streamlit as st

from src import config
from src.db import (
    get_alerted_opportunities,
    get_connection,
    get_metrics_summary,
    get_opportunities,
    get_paper_trades,
    get_source_health_latest,
    init_db,
    upsert_paper_trade,
)
from src.models import PAPER_TRADE_STATUSES, PaperTrade
from src.stake import guaranteed_profit, stake_split

st.set_page_config(page_title="SportArbitrage Scanner", layout="wide")

conn = get_connection()
init_db(conn)

# ── Sidebar filters ──────────────────────────────────────────────────────────

st.sidebar.title("Filters")

sport_filter = st.sidebar.selectbox(
    "Sport",
    ["All"] + config.TARGET_SPORTS,
)
market_filter = st.sidebar.selectbox(
    "Market",
    ["All", "h2h", "spreads", "totals"],
)
min_margin_filter = st.sidebar.slider(
    "Min margin (%)", 0.0, 10.0, 1.0, step=0.1
)
bookmaker_filter = st.sidebar.text_input("Bookmaker (partial match)")

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_live, tab_alerted, tab_calc, tab_history, tab_paper, tab_metrics, tab_health = st.tabs(
    ["Current Opportunities", "Alerted Opportunities", "Stake Calculator",
     "History", "Paper Trading", "Analytics", "Source Health"]
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _load_opps(limit: int = 200) -> list[dict]:
    return get_opportunities(
        conn,
        sport_key=sport_filter if sport_filter != "All" else None,
        market_key=market_filter if market_filter != "All" else None,
        min_margin=min_margin_filter / 100 if min_margin_filter else None,
        bookmaker=bookmaker_filter or None,
        limit=limit,
    )


def _parse_best_odds(o: dict) -> list[dict]:
    raw = o["best_odds_json"]
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


# ── Tab 1: Current Opportunities ─────────────────────────────────────────────

with tab_live:
    st.header("Current Opportunities")

    opps = _load_opps()

    if not opps:
        st.info("No opportunities match the current filters. Run the scanner first: `python -m src.scanner`")
    else:
        rows = []
        for o in opps:
            best = _parse_best_odds(o)
            books = ", ".join(b["bookmaker"] for b in best)
            rows.append({
                "Sport": o["sport_key"],
                "Match": f"{o['home_team']} vs {o['away_team']}",
                "Market": o["market_key"],
                "Line": o["line"] or "—",
                "Margin": f"{o['margin'] * 100:.2f}%",
                "Profit ($100)": f"${o['guaranteed_profit']:.2f}",
                "Books": books,
                "Detected": str(o["detected_at"])[:19],
            })
        st.dataframe(rows, use_container_width=True)

# ── Tab 2: Alerted Opportunities ─────────────────────────────────────────────

with tab_alerted:
    st.header("Alerted Opportunities")

    alerted = get_alerted_opportunities(conn, limit=200)
    if not alerted:
        st.info("No alerts sent yet.")
    else:
        rows = []
        for o in alerted:
            best = _parse_best_odds(o)
            books = ", ".join(b["bookmaker"] for b in best)
            rows.append({
                "Sport": o["sport_key"],
                "Match": f"{o['home_team']} vs {o['away_team']}",
                "Market": o["market_key"],
                "Margin": f"{o['margin'] * 100:.2f}%",
                "Profit ($100)": f"${o['guaranteed_profit']:.2f}",
                "Books": books,
                "Alert Sent": str(o.get("alert_sent_at", ""))[:19],
                "Channel": o.get("alert_channel", ""),
            })
        st.dataframe(rows, use_container_width=True)

# ── Tab 3: Stake Calculator ──────────────────────────────────────────────────

with tab_calc:
    st.header("Stake Calculator")

    opps_for_calc = _load_opps()
    if not opps_for_calc:
        st.info("No opportunities available for calculation.")
    else:
        labels = [
            f"{o['home_team']} vs {o['away_team']} — {o['market_key']} ({o['margin']*100:.2f}%)"
            for o in opps_for_calc
        ]
        selected_idx = st.selectbox("Select opportunity", range(len(labels)), format_func=lambda i: labels[i])
        total = st.number_input("Total stake ($)", min_value=1.0, value=100.0, step=10.0)

        opp = opps_for_calc[selected_idx]
        best = _parse_best_odds(opp)
        odds = [b["decimal_odds"] for b in best]

        stakes = stake_split(odds, total)
        profit = guaranteed_profit(stakes, odds, total)

        st.subheader("Recommended Stakes")
        for b, s in zip(best, stakes):
            st.write(f"**{b['outcome']}** @ {b['bookmaker']}: odds {b['decimal_odds']:.3f} → stake **${s:.2f}**")

        col1, col2 = st.columns(2)
        col1.metric("Guaranteed Profit", f"${profit:.2f}")
        col2.metric("ROI", f"{(profit / total) * 100:.2f}%")

# ── Tab 4: History ───────────────────────────────────────────────────────────

with tab_history:
    st.header("Opportunity History")

    all_opps = get_opportunities(conn, limit=500)
    if not all_opps:
        st.info("No recorded opportunities yet.")
    else:
        rows = []
        for o in all_opps:
            rows.append({
                "ID": o["id"],
                "Sport": o["sport_key"],
                "Match": f"{o['home_team']} vs {o['away_team']}",
                "Market": o["market_key"],
                "Line": o["line"] or "—",
                "Margin": f"{o['margin'] * 100:.2f}%",
                "Detected": str(o["detected_at"])[:19],
            })
        st.dataframe(rows, use_container_width=True)

# ── Tab 5: Paper Trading ────────────────────────────────────────────────────

with tab_paper:
    st.header("Paper Trading Log")

    all_opps = get_opportunities(conn, limit=100)
    if not all_opps:
        st.info("No opportunities to paper-trade yet.")
    else:
        opp_labels = [f"#{o['id']} | {o['home_team']} vs {o['away_team']} — {o['market_key']}" for o in all_opps]
        sel = st.selectbox("Select opportunity", range(len(opp_labels)), format_func=lambda i: opp_labels[i], key="paper_sel")
        selected_opp = all_opps[sel]

        existing_trades = get_paper_trades(conn, opportunity_id=selected_opp["id"])

        if existing_trades:
            st.subheader("Existing Entries")
            st.dataframe(existing_trades, use_container_width=True)

        st.subheader("Log New Check")
        with st.form("paper_trade_form"):
            status = st.selectbox("Status", PAPER_TRADE_STATUSES)
            still_available = st.selectbox("Still available?", ["Unknown", "Yes", "No"])
            would_profit = st.number_input("Would-have profit ($)", value=0.0, step=0.01)
            notes = st.text_area("Notes")
            submitted = st.form_submit_button("Save")

        if submitted:
            avail_map = {"Unknown": None, "Yes": True, "No": False}
            trade = PaperTrade(
                opportunity_id=selected_opp["id"],
                checked_at=datetime.now(UTC),
                status=status,
                still_available=avail_map[still_available],
                would_have_profit=would_profit if would_profit else None,
                notes=notes,
            )
            upsert_paper_trade(conn, trade)
            st.success("Paper trade logged.")
            st.rerun()

# ── Tab 6: Analytics ─────────────────────────────────────────────────────────

with tab_metrics:
    st.header("Analytics")
    days = st.slider("Look-back period (days)", 7, 60, 28)
    metrics = get_metrics_summary(conn, days=days)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Scans", metrics["total_scans"])
    c2.metric("Theoretical Arbs Found", metrics["theoretical_arbs_found"])
    c3.metric("Alerts Sent", metrics["alerts_sent"])
    c4.metric("Opps / Week", f"{metrics['opportunities_per_week']:.1f}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Avg Margin", f"{metrics['avg_margin'] * 100:.2f}%" if metrics["avg_margin"] else "—")
    c6.metric("Checked Opportunities", metrics["checked_opportunities"])
    c7.metric("Verified Executable %", f"{metrics['verified_executable_pct'] * 100:.0f}%" if metrics["checked_opportunities"] else "—")
    c8.metric("Avg Verified Profit / $100", f"${metrics['avg_verified_profit_per_100']:.2f}" if metrics["avg_verified_profit_per_100"] else "—")

    c9, c10 = st.columns(2)
    c9.metric("Projected Monthly EV", f"${metrics['projected_monthly_ev']:.2f}")
    c10.metric("Manual Success Rate", f"{metrics['success_rate'] * 100:.0f}%" if metrics["checked_opportunities"] else "—")

    if metrics["paper_trade_counts"]:
        st.subheader("Paper Trade Breakdown")
        st.bar_chart(metrics["paper_trade_counts"])
    else:
        st.info("No paper trades recorded yet.")

# ── Tab 7: Source Health ─────────────────────────────────────────────────────

with tab_health:
    st.header("Source Health")

    health_rows = get_source_health_latest(conn)
    if not health_rows:
        st.info("No source health records yet. Run a scan to populate.")
    else:
        display = []
        for h in health_rows:
            display.append({
                "Source": h["source_key"],
                "Healthy": "Yes" if h["is_healthy"] else "No",
                "Fetches": h.get("fetch_count", 0),
                "Failures": h.get("failure_count", 0),
                "Last Success": str(h.get("last_success", ""))[:19] or "—",
                "Last Failure": str(h.get("last_failure", ""))[:19] or "—",
                "Latency (ms)": f"{h['latency_ms']:.0f}" if h.get("latency_ms") else "—",
                "Error": h.get("error_message") or "—",
                "Recorded": str(h.get("recorded_at", ""))[:19],
            })
        st.dataframe(display, use_container_width=True)
