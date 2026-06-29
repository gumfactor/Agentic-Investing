"""Page 4 — Blotter Approval (C1 Gate).

Safety rules enforced: C1, C3, C5, C9.
IBKR connection: Not required (reads blotter artifact; Airflow handles submission).

This is the highest-priority page — it replaces the CLI confirmation workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError

from reporting.dashboards.components.circuit_breaker import get_circuit_breaker
from reporting.dashboards.components.env_banner import render_env_banner
from reporting.dashboards.db import get_engine
from reporting.dashboards.queries import (
    blotter_approval_history,
    insert_blotter_approval,
    pending_blotter,
)

# -- Page config --
st.set_page_config(
    page_title="Blotter Approval — RQIS",
    page_icon="📋",
    layout="wide",
)

# -- Shared components --
render_env_banner()

from reporting.dashboards.components.circuit_breaker import (
    render_circuit_breaker_sidebar,
    render_circuit_breaker_warning,
)

render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

st.title("Blotter Approval")

engine = get_engine()
artifact_dir_str = os.environ.get("RQIS_PAPER_ARTIFACT_DIR", "")
artifact_dir = Path(artifact_dir_str) if artifact_dir_str else None

# ── Helpers ──────────────────────────────────────────────────────────────────

def _compute_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_submit_enabled() -> tuple[bool, str]:
    """Check whether submission should be enabled. Returns (enabled, reason)."""
    cb = get_circuit_breaker()
    if cb.is_open:
        return False, "Circuit breaker is OPEN (C4)"

    paper = os.environ.get("PAPER_TRADING", "").lower() == "true"
    port = os.environ.get("IBKR_PORT", "")
    c8_cleared = os.environ.get("C8_CLEARED", "").lower() == "true"

    if paper and port == "7497":
        return True, ""

    if not paper and port == "7496" and c8_cleared:
        return True, ""

    if not paper and port == "7496" and not c8_cleared:
        return False, "Live trading requires C8 clearance (4-week paper qualification)"

    return False, "Environment misconfigured — check PAPER_TRADING and IBKR_PORT"


# ── Detect pending blotter ───────────────────────────────────────────────────

pending = None
if artifact_dir:
    pending = pending_blotter(artifact_dir, engine)

if pending is None:
    # State A — No pending blotter
    st.info(
        "No blotter is awaiting approval.  \n"
        "The Airflow DAG runs at 23:00 ET on trading days."
    )
else:
    # State B — Blotter awaiting approval
    artifact_path: Path = pending["path"]
    blotter: dict = pending["blotter"]

    # Step 2: SHA-256 verification at page load
    computed_hash = _compute_file_sha256(artifact_path)
    stored_hash = blotter.get("candidate_rows_sha256")

    if stored_hash and computed_hash != stored_hash:
        st.error(
            "INTEGRITY CHECK FAILED: Blotter file has been modified "
            "since it was generated."
        )
        st.stop()

    # Step 1: Blotter header
    st.subheader("Blotter Details")
    paper_flag = blotter.get("paper_only", True)
    env_label = "PAPER TRADING" if paper_flag else "LIVE TRADING"
    ibkr_port = os.environ.get("IBKR_PORT", "unknown")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Run ID", blotter.get("run_id", "N/A")[:12])
    col2.metric("Environment", env_label)
    col3.metric("IBKR Port", ibkr_port)
    col4.metric("Total Orders", len(blotter.get("candidate_rows", [])))

    col5, col6 = st.columns(2)
    col5.metric("Generated", blotter.get("generated_at_utc", "N/A"))
    total_notional = sum(
        r.get("estimated_notional", 0) for r in blotter.get("candidate_rows", [])
    )
    col6.metric("Total Notional", f"${total_notional:,.2f}")

    st.caption(
        f"SHA-256: `{computed_hash[:16]}...` "
        f"(full: `{computed_hash}`)"
    )
    st.caption(f"Strategy: {blotter.get('strategy_id', 'N/A')}")

    # Step 3: Order grid
    st.subheader("Order Grid")

    candidate_rows = blotter.get("candidate_rows", [])
    if not candidate_rows:
        st.warning("Blotter contains no candidate rows.")
        st.stop()

    original_quantities: dict[int, float] = {}
    grid_rows = []
    for row in candidate_rows:
        seq = row.get("sequence", 0)
        qty = row.get("quantity", row.get("estimated_shares", 0))
        original_quantities[seq] = qty
        limit_price = row.get("limit_price", row.get("reference_price", 0))
        est_notional = row.get("estimated_notional", qty * limit_price)

        grid_rows.append({
            "selected": True,
            "sequence": seq,
            "ticker": row.get("ticker", ""),
            "side": row.get("side", ""),
            "quantity": int(qty),
            "limit_price": round(limit_price, 2),
            "est_notional": round(est_notional, 2),
            "risk_flag": row.get("risk_flag", ""),
        })

    grid_df = pd.DataFrame(grid_rows)

    edited_df = st.data_editor(
        grid_df,
        column_config={
            "selected": st.column_config.CheckboxColumn("✓", default=True),
            "sequence": st.column_config.NumberColumn("Seq", disabled=True),
            "ticker": st.column_config.TextColumn("Ticker", disabled=True),
            "side": st.column_config.TextColumn("Side", disabled=True),
            "quantity": st.column_config.NumberColumn("Quantity", min_value=1),
            "limit_price": st.column_config.NumberColumn(
                "Limit Price", format="$%.2f", disabled=True,
            ),
            "est_notional": st.column_config.NumberColumn(
                "Est. Notional", format="$%.2f", disabled=True,
            ),
            "risk_flag": st.column_config.TextColumn("Risk Flag", disabled=True),
        },
        use_container_width=True,
        hide_index=True,
        key="blotter_grid",
    )

    # Quantity edit validation — reject increases, revert to original
    quantity_issues = []
    for _, row in edited_df.iterrows():
        seq = int(row["sequence"])
        edited_qty = int(row["quantity"])
        orig_qty = int(original_quantities.get(seq, edited_qty))
        if edited_qty > orig_qty:
            quantity_issues.append(
                f"Row {seq} ({row['ticker']}): quantity increased from "
                f"{orig_qty} to {edited_qty} — reverted."
            )

    if quantity_issues:
        for msg in quantity_issues:
            st.warning(msg)
        st.warning("Quantity may not be increased from the blotter value.")

    # Running totals
    selected_mask = edited_df["selected"] == True  # noqa: E712
    selected_rows = edited_df[selected_mask]
    deselected_rows = edited_df[~selected_mask]

    selected_notional = sum(
        int(r["quantity"]) * float(r["limit_price"]) for _, r in selected_rows.iterrows()
    )
    deselected_notional = sum(
        int(r["quantity"]) * float(r["limit_price"]) for _, r in deselected_rows.iterrows()
    )

    st.markdown(
        f"**Selected:** {len(selected_rows)} orders / "
        f"${selected_notional:,.2f} notional"
    )
    st.markdown(
        f"**Deselected (will be rejected):** {len(deselected_rows)} orders / "
        f"${deselected_notional:,.2f} notional"
    )

    # Step 4: Submission flow
    st.subheader("Submit")

    submit_enabled, disable_reason = _is_submit_enabled()
    no_rows_selected = len(selected_rows) == 0

    if not submit_enabled:
        st.error(f"Submission disabled: {disable_reason}")
    if no_rows_selected:
        st.warning("No rows selected — select at least one order to submit.")

    can_submit = submit_enabled and not no_rows_selected and not quantity_issues

    paper_flag_str = "PAPER" if paper_flag else "LIVE"

    if st.button(
        "Submit selected orders",
        disabled=not can_submit,
        type="primary",
    ):
        st.session_state["show_confirmation"] = True

    if st.session_state.get("show_confirmation", False):
        st.divider()
        st.markdown(
            f"You are about to submit **{len(selected_rows)}** orders "
            f"totalling **${selected_notional:,.2f}** notional to "
            f"IBKR **{paper_flag_str}** (port {ibkr_port}).  \n\n"
            f"**Strategy:** {blotter.get('strategy_id', 'N/A')}  \n"
            f"**Run ID:** {blotter.get('run_id', 'N/A')}  \n\n"
            f"Deselected orders ({len(deselected_rows)}) will be permanently "
            f"recorded as operator-rejected.  \n\n"
            f"**This action cannot be undone.**"
        )

        confirmed = st.checkbox(
            "I have reviewed all orders and confirm this submission.",
            key="confirm_checkbox",
        )

        if st.button(
            "CONFIRM SUBMISSION",
            disabled=not confirmed,
            type="primary",
            key="confirm_submit",
        ):
            # Re-verify SHA-256 at submission time
            confirmed_hash = _compute_file_sha256(artifact_path)
            if stored_hash and confirmed_hash != stored_hash:
                st.error(
                    "Blotter file changed between review and submission. Aborting."
                )
                st.stop()

            # Resolve selected order IDs
            selected_ids = [
                str(int(r["sequence"])) for _, r in selected_rows.iterrows()
            ]

            # Quantity overrides — only entries where operator changed the value
            quantity_overrides: dict[str, int] = {}
            for _, r in edited_df.iterrows():
                seq = int(r["sequence"])
                edited_qty = int(r["quantity"])
                orig_qty = int(original_quantities.get(seq, edited_qty))
                if edited_qty != orig_qty and edited_qty <= orig_qty:
                    quantity_overrides[str(seq)] = edited_qty

            # INSERT approval row (C3: append-only)
            try:
                insert_blotter_approval(
                    engine,
                    run_id=blotter["run_id"],
                    local_path=str(artifact_path),
                    blotter_sha256=confirmed_hash,
                    selected_ids=selected_ids,
                    approved_by=st.session_state["operator_email"],
                    confirmed_hash=confirmed_hash,
                    session_id=st.session_state["session_id"],
                    quantity_overrides=quantity_overrides if quantity_overrides else None,
                )
            except IntegrityError:
                st.error(
                    "This blotter has already been approved. "
                    "Refresh the page to see the receipt."
                )
                st.stop()

            # Step 5: Post-submission receipt
            st.session_state["show_confirmation"] = False
            st.success(
                f"Approval submitted at {pd.Timestamp.utcnow():%Y-%m-%d %H:%M:%S} UTC"
            )

            approved_summary = ", ".join(
                f"{r['ticker']} ({r['side']} {int(r['quantity'])})"
                for _, r in selected_rows.iterrows()
            )
            rejected_summary = ", ".join(
                f"{r['ticker']} ({r['side']} {int(r['quantity'])})"
                for _, r in deselected_rows.iterrows()
            ) or "None"

            st.markdown(
                f"**{len(selected_rows)}** orders approved / "
                f"**{len(deselected_rows)}** orders rejected  \n\n"
                f"**Approved:** {approved_summary}  \n"
                f"**Rejected:** {rejected_summary} — operator deselected  \n\n"
                f"The Airflow DAG will proceed with order submission.  \n"
                f"Confirmed SHA-256: `{confirmed_hash}`  \n"
                f"Approved by: {st.session_state['operator_email']}"
            )

st.divider()

# ── Approval history (always visible) ────────────────────────────────────────

st.subheader("Approval History")
try:
    history = blotter_approval_history(engine, limit=20)
    if history.empty:
        st.caption("No approval records yet.")
    else:
        display_df = history.copy()
        if "selected_order_ids" in display_df.columns:
            display_df["n_selected"] = display_df["selected_order_ids"].apply(
                lambda x: len(json.loads(x)) if isinstance(x, str) else len(x) if isinstance(x, list) else 0
            )
        if "confirmed_blotter_sha256" in display_df.columns:
            display_df["sha256_short"] = display_df[
                "confirmed_blotter_sha256"
            ].str[:16]
        cols_to_show = [
            c for c in [
                "blotter_run_id", "approved_at_utc", "approved_by",
                "n_selected", "sha256_short", "dashboard_session_id",
            ] if c in display_df.columns
        ]
        st.dataframe(display_df[cols_to_show], use_container_width=True)
except Exception:
    st.caption("Approval history unavailable (database not connected).")
