"""Shared alert feed widget — renders unacknowledged alerts with per-alert Ack buttons."""
from __future__ import annotations

import streamlit as st

from reporting.dashboards.components.circuit_breaker import get_alert_manager


def render_alert_feed(*, show_acknowledged: bool = True, page_key: str = "default") -> None:
    """Render unacknowledged alerts with Ack buttons and optional acknowledged history."""
    am = get_alert_manager()
    unacked = am.unacknowledged()

    if not unacked:
        st.success("No active alerts.")
        return

    for alert in unacked:
        acol1, acol2 = st.columns([4, 1])
        if alert.severity == "hard":
            acol1.error(
                f"**{alert.metric}** = {alert.value:.4f} "
                f"(threshold: {alert.threshold:.4f}) — {alert.fired_at:%H:%M UTC}"
            )
        else:
            acol1.warning(
                f"**{alert.metric}** = {alert.value:.4f} "
                f"(threshold: {alert.threshold:.4f}) — {alert.fired_at:%H:%M UTC}"
            )
        if acol2.button("Ack", key=f"ack_{page_key}_{alert.alert_id}"):
            am.acknowledge(alert.alert_id)
            st.rerun()

    if show_acknowledged:
        all_alerts = am.all_alerts()
        acked = [a for a in all_alerts if a.acknowledged]
        if acked:
            with st.expander(f"Acknowledged Alerts ({len(acked)})"):
                for alert in acked[-20:]:
                    st.caption(
                        f"{alert.fired_at:%Y-%m-%d %H:%M} — {alert.severity}: "
                        f"{alert.metric} = {alert.value:.4f}"
                    )
