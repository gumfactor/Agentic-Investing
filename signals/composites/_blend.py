"""Shared blending utility for composite signals.

All composites that blend pre-computed z-scored signal DataFrames use this
function. It handles per-row weight renormalization (missing signals get their
weight redistributed to available signals) and cross-sectional re-standardization
of the output.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def blend_scores(
    df: pd.DataFrame,
    weights: dict[str, float],
    output_col: str,
    date_col: str = "date",
) -> pd.DataFrame:
    """Blend pre-z-scored signal columns into a single composite score.

    Args:
        df: DataFrame containing all columns named in ``weights``.
        weights: Mapping of score column name → weight. Need not sum to 1;
            they are renormalized per row after excluding NaN positions.
        output_col: Name of the composite column to add.
        date_col: Column to group by for cross-sectional re-standardization.

    Returns:
        Copy of ``df`` with ``output_col`` added and cross-sectionally
        z-scored per ``date_col``. Rows where every input is NaN receive NaN.
    """
    score_cols = list(weights.keys())
    weights_arr = np.array([weights[c] for c in score_cols], dtype=float)

    vals = df[score_cols].to_numpy(dtype=float)          # (n, k)
    mask = ~np.isnan(vals)                               # True where valid

    active_w = mask * weights_arr                        # (n, k) — zero weight on NaN
    active_w_sum = active_w.sum(axis=1)                  # (n,)

    weighted_sum = np.where(mask, vals, 0.0) @ weights_arr  # (n,)

    with np.errstate(invalid="ignore", divide="ignore"):
        composite = np.where(active_w_sum > 0, weighted_sum / active_w_sum, np.nan)

    out = df.copy()
    out[output_col] = composite

    def _cs_zscore(s: pd.Series) -> pd.Series:
        std = s.std(ddof=1)
        if std == 0 or pd.isna(std):
            # All non-NaN values are tied (std=0) or only one exists (std=NaN).
            # Assign 0.0 to valid rows (they're all at the mean), keep NaN rows as NaN
            # so that downstream dropna still removes rows that had no signal at all.
            out = pd.Series(np.nan, index=s.index)
            out[s.notna()] = 0.0
            return out
        return (s - s.mean()) / std

    out[output_col] = out.groupby(date_col)[output_col].transform(_cs_zscore)
    return out
