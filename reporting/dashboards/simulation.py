"""Forward simulation helpers for shadow strategy comparison.

Computes simulated NAV and returns from strategy target weights and daily prices.
Used by queries.py and the Airflow forward-simulation task.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def compute_simulated_return(
    target_weights: dict[str, float],
    prices_today: dict[str, float],
    prices_yesterday: dict[str, float],
) -> float:
    """Compute a single-day weighted return from target weights and prices."""
    total_return = 0.0
    total_weight = 0.0
    for ticker, weight in target_weights.items():
        p_today = prices_today.get(ticker)
        p_yesterday = prices_yesterday.get(ticker)
        if p_today is None or p_yesterday is None or p_yesterday <= 0:
            continue
        daily_ret = (p_today / p_yesterday) - 1.0
        total_return += weight * daily_ret
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return total_return


def build_simulated_nav_series(
    sim_df: pd.DataFrame,
    initial_nav: float = 10000.0,
) -> pd.Series:
    """Build a cumulative NAV series from strategy_simulations rows.

    If `simulated_nav` column exists and is populated, use it directly.
    Otherwise, reconstruct from `simulated_return`.
    """
    if sim_df.empty:
        return pd.Series(dtype=float)

    if "simulated_nav" in sim_df.columns and sim_df["simulated_nav"].notna().all():
        return sim_df.set_index("sim_date")["simulated_nav"].astype(float)

    returns = sim_df.set_index("sim_date")["simulated_return"].astype(float)
    nav = initial_nav * (1 + returns).cumprod()
    return nav


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Compute Jaccard similarity between two sets."""
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def alpha_overlap_matrix(
    engine: "Engine",
    strategy_ids: list[str],
    top_n: int = 20,
) -> pd.DataFrame:
    """Compute Jaccard similarity matrix of top-N alpha positions across strategies.

    Returns a DataFrame with strategy_ids as both index and columns,
    with Jaccard similarity values in [0, 1].
    """
    from sqlalchemy import text

    top_tickers: dict[str, set[str]] = {}
    with engine.connect() as conn:
        for sid in strategy_ids:
            rows = conn.execute(
                text("""
                    SELECT ticker FROM alpha_scores
                    WHERE score_date = (
                        SELECT MAX(score_date) FROM alpha_scores WHERE strategy_id = :sid
                    )
                    AND strategy_id = :sid
                    ORDER BY rank ASC
                    LIMIT :n
                """),
                {"sid": sid, "n": top_n},
            ).fetchall()
            top_tickers[sid] = {r[0] for r in rows}

    n = len(strategy_ids)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 1.0
            else:
                matrix[i][j] = jaccard_similarity(
                    top_tickers.get(strategy_ids[i], set()),
                    top_tickers.get(strategy_ids[j], set()),
                )

    return pd.DataFrame(matrix, index=strategy_ids, columns=strategy_ids)
