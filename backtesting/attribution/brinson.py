"""Brinson-Hood-Beebower (BHB) performance attribution.

Decomposes portfolio excess return into:
  - Allocation effect:   (w_p,i - w_b,i) * (r_b,i - r_b)
  - Selection effect:    w_b,i * (r_p,i - r_b,i)
  - Interaction effect:  (w_p,i - w_b,i) * (r_p,i - r_b,i)

where i indexes groups (e.g. GICS sectors), w denotes weights, r denotes
group-level returns, and r_b is the total benchmark return.

Reference: Brinson, Hood & Beebower (1986), "Determinants of Portfolio Performance."
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class AttributionResult:
    """Full attribution output."""
    records: pd.DataFrame
    summary: pd.DataFrame
    total_allocation: float
    total_selection: float
    total_interaction: float
    total_excess_return: float


def compute_brinson_attribution(
    portfolio_weights: pd.DataFrame,
    benchmark_weights: pd.DataFrame,
    returns: pd.DataFrame,
    group_col: str = "sector",
) -> AttributionResult:
    """Compute Brinson-Hood-Beebower attribution.

    Args:
        portfolio_weights: DataFrame with columns (date, ticker, weight, group_col).
        benchmark_weights: DataFrame with columns (date, ticker, weight, group_col).
        returns: DataFrame with columns (date, ticker, return).
        group_col: Column name defining groups (e.g. 'sector').

    Returns:
        AttributionResult with per-group allocation/selection/interaction.
    """
    _require_cols(portfolio_weights, {"date", "ticker", "weight", group_col}, "portfolio_weights")
    _require_cols(benchmark_weights, {"date", "ticker", "weight", group_col}, "benchmark_weights")
    _require_cols(returns, {"date", "ticker", "return"}, "returns")

    all_dates = sorted(
        set(portfolio_weights["date"]) | set(benchmark_weights["date"])
    )

    records: list[dict] = []

    for dt in all_dates:
        pw = portfolio_weights[portfolio_weights["date"] == dt]
        bw = benchmark_weights[benchmark_weights["date"] == dt]
        rets = returns[returns["date"] == dt]

        pw_grp = _group_weights_and_returns(pw, rets, group_col, suffix="p")
        bw_grp = _group_weights_and_returns(bw, rets, group_col, suffix="b")

        merged = pd.merge(pw_grp, bw_grp, on=group_col, how="outer").fillna(0.0)
        r_b_total = float((merged["w_b"] * merged["r_b"]).sum())

        for _, row in merged.iterrows():
            w_p = float(row["w_p"])
            w_b = float(row["w_b"])
            r_p = float(row["r_p"])
            r_b = float(row["r_b"])
            allocation = (w_p - w_b) * (r_b - r_b_total)
            selection = w_b * (r_p - r_b)
            interaction = (w_p - w_b) * (r_p - r_b)
            records.append({
                "date": dt,
                "group": row[group_col],
                "portfolio_weight": w_p,
                "benchmark_weight": w_b,
                "portfolio_return": r_p,
                "benchmark_return": r_b,
                "allocation": allocation,
                "selection": selection,
                "interaction": interaction,
                "total": allocation + selection + interaction,
            })

    records_df = pd.DataFrame(records)
    if records_df.empty:
        empty = pd.DataFrame(columns=["group", "allocation", "selection", "interaction", "total"])
        return AttributionResult(records_df, empty, 0.0, 0.0, 0.0, 0.0)

    summary = (
        records_df.groupby("group")[["allocation", "selection", "interaction", "total"]]
        .sum()
        .reset_index()
    )

    return AttributionResult(
        records=records_df,
        summary=summary,
        total_allocation=float(records_df["allocation"].sum()),
        total_selection=float(records_df["selection"].sum()),
        total_interaction=float(records_df["interaction"].sum()),
        total_excess_return=float(records_df["total"].sum()),
    )


def _group_weights_and_returns(
    weights_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    group_col: str,
    suffix: str,
) -> pd.DataFrame:
    """Aggregate security-level weights and returns to group level."""
    merged = weights_df.merge(returns_df[["ticker", "return"]], on="ticker", how="left")
    merged["return"] = merged["return"].fillna(0.0)

    def _agg(g: pd.DataFrame) -> pd.Series:
        w_sum = g["weight"].sum()
        r = (g["weight"] * g["return"]).sum() / w_sum if w_sum > 0 else 0.0
        return pd.Series({f"w_{suffix}": w_sum, f"r_{suffix}": r})

    agg = merged.groupby(group_col).apply(_agg, include_groups=False).reset_index()
    return agg


def _require_cols(df: pd.DataFrame, required: set[str], name: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{name} DataFrame missing columns: {missing}")
