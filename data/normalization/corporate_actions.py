"""Corporate action adjustment utilities.

Computes cumulative price adjustment factors from corporate action records
and applies them to produce split-adjusted and dividend-adjusted prices.

We store unadjusted prices in daily_prices (for auditability) and compute
adjustments on the fly or at batch time. This module handles the computation.

Adjustment methodology:
  - Splits : multiply all prices before the ex-date by (1 / split_ratio).
              e.g., a 2-for-1 split halves all prior prices.
  - Dividends : the 'cumulative adjustment factor' approach divides prior
                prices by (1 - dividend/price_on_ex_date). This produces
                prices as if dividends were reinvested — the standard for
                total return series used in signal computation.
  - Spinoffs  : not yet supported (requires paid data source).

For backtesting, always use adjusted prices derived from this module.
Never use source_adj_close directly for signal computation — its adjustment
methodology may differ from ours and is not auditable.

Corporate-action observability contract (BUG-009, design plan §2.3)
---------------------------------------------------------------------
``compute_adjustment_factors``/``apply_adjustment_factors`` above are the
**full-history** routine: given a set of actions, they adjust every date in
*prices* regardless of when each action became knowable. That is exactly
correct for the raw execution-price/total-return series used *today* (e.g. a
tearsheet built at time-of-report over the complete history) — and exactly
wrong for reconstructing what a score or a realized return could have known
at some point in the past, because it lets a future split/dividend leak into
a historical score feature.

Two separate, explicitly named interfaces below wrap the same factor engine
with an availability cutoff, per design plan §2.3-2.4:

- :func:`build_score_price_history_as_of` — only actions known by the score
  observation cutoff. The ONLY adjustment path permitted for a score feature
  at time t.
- :func:`build_realized_total_return_as_of` — only actions that occurred and
  were known by the return exit cutoff. Used for realized IC/total-return
  analytics — never for raw fill/order notional (orders and cash notional
  always use the raw tradable price; see execution/ for that path, which
  this module does not touch).

Both require *corporate_actions* to carry a ``known_at`` column (added by
migration 011); an action whose ``known_at`` is null/missing cannot be used
by either builder and is dropped with a logged warning rather than silently
included.

Same-date multi-action accumulation and quoting convention (BUG-037, design
plan §3.1)
---------------------------------------------------------------------------
``compute_adjustment_factors`` accumulates the **product** of every action's
per-action multiplier for a given ``(ticker, ex_date)``, regardless of how
many actions or which ``action_type`` values share that date — not the
last-write-wins single multiplier the pre-fix implementation kept. This is
shared automatically by both cutoff-aware builders below and the legacy
full-history routine, since all three call this function.

A same-date split and cash dividend cannot simply be multiplied together
without first agreeing on a quoting convention: a dividend's adjustment
factor is ``(ex_close - dividend_per_share) / ex_close``, and
``dividend_per_share`` is only well-defined relative to a share-count basis
— *pre-split* (dollars per share before the same-day split takes effect) or
*post-split* (dollars per share after it). The two bases are not
interchangeable and must be normalized to one convention before the product
is taken.

**Empirically verified convention for this project's ingestion source
(yfinance, `data/ingestion/market/yfinance_client.py`): POST_SPLIT.**
Verification method: `yfinance.Ticker.dividends` is not a raw as-declared
series — it is retroactively normalized against a ticker's *entire* split
history, including splits that occur strictly *after* the dividend's own
ex-date. Confirmed against AAPL: the $2.65/share dividend actually declared
on 2012-08-09 (pre-split) is returned by `yf.Ticker("AAPL").dividends` as
``0.094643 == 2.65 / (7 * 4)`` — divided by the combined ratio of the 2014
7-for-1 split *and* the 2020 4-for-1 split, both of which post-date
2012-08-09. This proves yfinance dividend values are always expressed in
*current* (maximally post-split) share-count terms, not the basis in effect
on the dividend's own ex-date.

At the exact same-ex-date boundary specifically, an S&P 500-constituent-wide
historical scan of `Ticker.dividends`/`Ticker.splits` date collisions (21
tickers found, e.g. DHR 2016-07-05, IRM 2014-09-26, TMUS 2013-05-01, EXPE
2011-12-21) turned up only spinoff-modeling artifacts — Yahoo represents a
spinoff as one large one-time "dividend" plus a compensating "split"-labeled
ratio — never a genuine simultaneous ordinary stock split + ordinary
periodic cash dividend. No same-date ordinary-split/ordinary-dividend pair
exists anywhere in that sample to test the boundary directly against a real
row. Given (a) the general retroactive-normalization behavior demonstrated
above holds uniformly across the whole dividend series with no documented or
observed boundary exception, and (b) yfinance is the only ingestion source
this project is configured for, this module adopts **POST_SPLIT** as the
declared convention: a same-date dividend `value` is assumed to already
reflect the post-split (i.e. inclusive of the same-day split) share count
and needs no further scaling before being combined with the split
multiplier.

This assumption is enforced defensively, not silently. Each corporate-action
row may optionally carry a ``dividend_quoting_convention`` column with value
``"post_split"`` (default/assumed when the column or value is absent) or
``"pre_split"`` (the row's dividend value is stated in the pre-split share
basis and is normalized — divided by the same-date net split ratio — before
its factor is computed). Any other non-null value, or a same-date dividend
whose net split ratio cannot be resolved to a well-defined positive number,
raises :class:`AmbiguousSameDateActionError` rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# Precision for adjustment factors — 8 decimal places matches Bloomberg convention.
FACTOR_PRECISION = Decimal("0.00000001")

# Declared same-date dividend quoting convention (see module docstring for the
# empirical verification against yfinance). This is the convention assumed
# when a corporate-action row does not carry an explicit
# ``dividend_quoting_convention`` override.
DEFAULT_DIVIDEND_QUOTING_CONVENTION = "post_split"
_KNOWN_DIVIDEND_QUOTING_CONVENTIONS = ("post_split", "pre_split")


class AmbiguousSameDateActionError(ValueError):
    """Raised when a same-date group of corporate actions cannot be safely
    normalized to one quoting convention before combining their multipliers
    (BUG-037, design plan §3.1).

    Raised when:
      - a same-date dividend row carries a ``dividend_quoting_convention``
        value other than ``"post_split"``/``"pre_split"``/null, or
      - a same-date dividend is quoted ``"pre_split"`` but the same-date net
        split ratio is missing, zero, or negative (there is no well-defined
        factor to normalize by).

    This module never silently guesses a convention in these cases — the
    caller must fix the ingested data or supply an explicit, recognized
    convention.
    """


def _combine_same_date_action_multipliers(
    ticker: str,
    ex_dt: date,
    day_actions: pd.DataFrame,
    ex_close: object,
) -> Optional[Decimal]:
    """Combine every action's multiplier for one (ticker, ex_date) into a
    single product (BUG-037, design plan §3.1).

    Splits are combined first into one net split ratio for the date, so
    that a same-date dividend can be normalized to the declared
    ``POST_SPLIT`` convention (or an explicit per-row ``pre_split``
    override) before its own factor is computed. Spinoffs are logged as
    not-implemented and contribute no multiplier, matching pre-existing
    behavior. Returns ``None`` if no action on the date produced a usable
    multiplier.
    """
    splits = day_actions[day_actions["action_type"] == "split"]
    dividends = day_actions[day_actions["action_type"] == "dividend"]
    spinoffs = day_actions[day_actions["action_type"] == "spinoff"]

    for atype in set(day_actions["action_type"]) - {"split", "dividend", "spinoff"}:
        logger.warning(
            "unrecognized_action_type_ignored",
            ticker=ticker,
            ex_date=str(ex_dt),
            action_type=atype,
        )

    if not spinoffs.empty:
        logger.warning(
            "spinoff_not_implemented",
            ticker=ticker,
            ex_date=str(ex_dt),
        )

    # Net split ratio for the date: product of every same-date split value.
    # A same-date dividend is normalized against this combined ratio, not
    # any single split in isolation, so a triple-split-plus-dividend day
    # still normalizes correctly.
    net_split_ratio = Decimal("1")
    has_split = False
    for _, action in splits.iterrows():
        value = Decimal(str(action["value"]))
        if value != 0:
            net_split_ratio *= value
            has_split = True

    combined = Decimal("1")
    has_component = False

    if has_split:
        # Prior prices / split_ratio = adjusted price.
        combined *= Decimal("1") / net_split_ratio
        has_component = True

    if not dividends.empty and ex_close is not None and ex_close != 0:
        ex_close_d = Decimal(str(ex_close))
        for _, action in dividends.iterrows():
            raw_value = Decimal(str(action["value"]))
            # NOTE (reachability, BUG-076): `dividend_quoting_convention` is a
            # FORWARD-LOOKING hook. It is NOT selected by any live DB read path
            # today — the Airflow score/simulation queries and
            # scripts/validate_signal_ic.py select only ticker, ex_date,
            # action_type, value, known_at, source_version, and no migration or
            # writer creates this column. So for every DB-sourced row the
            # convention is absent -> the POST_SPLIT default (operator
            # signed-off 2026-07-20), and the `pre_split` normalization and
            # AmbiguousSameDateActionError branches are exercised ONLY by
            # explicit in-memory callers/tests. Wiring this to real data (a
            # column migration + query updates + an actual dated pre_split
            # source) is tracked as future work in BUG-076; do not treat this
            # override as active on DB rows until then.
            convention = action.get("dividend_quoting_convention") if hasattr(action, "get") else None
            # Treat every "absent" marker uniformly as default (post_split):
            # None, float NaN, AND pandas' pd.NA (the missing-value sentinel of
            # the nullable "string"/StringDtype column that arrives after a
            # parquet round-trip). A bare `pd.NA in (...)`/`pd.NA not in (...)`
            # membership or comparison raises "boolean value of NA is
            # ambiguous", so guard with a scalar pd.isna() check — `convention`
            # is always a single cell value here, never an array.
            if convention is not None and pd.api.types.is_scalar(convention) and pd.isna(convention):
                convention = None
            normalized_value = _normalize_dividend_value(
                ticker=ticker,
                ex_dt=ex_dt,
                raw_value=raw_value,
                convention=convention,
                net_split_ratio=net_split_ratio if has_split else None,
            )
            # Factor = (ex_close - normalized_dividend) / ex_close
            factor = (ex_close_d - normalized_value) / ex_close_d
            if factor > 0:
                combined *= factor
                has_component = True

    return combined if has_component else None


def _normalize_dividend_value(
    ticker: str,
    ex_dt: date,
    raw_value: Decimal,
    convention: Optional[str],
    net_split_ratio: Optional[Decimal],
) -> Decimal:
    """Normalize one same-date dividend's per-share value to the declared
    ``POST_SPLIT`` convention (BUG-037, design plan §3.1) before its factor
    is combined with any same-date split.

    ``convention`` is the row's optional ``dividend_quoting_convention``
    override; ``None``/``"post_split"`` means the value already reflects
    the post-split share count (the empirically-verified default for this
    project's yfinance ingestion — see module docstring) and is returned
    unchanged. ``"pre_split"`` means the value is stated in the pre-split
    share basis and must be divided by the same-date net split ratio to
    convert it to post-split terms before use.
    """
    if convention is None:
        convention = DEFAULT_DIVIDEND_QUOTING_CONVENTION
    if convention not in _KNOWN_DIVIDEND_QUOTING_CONVENTIONS:
        raise AmbiguousSameDateActionError(
            f"{ticker} {ex_dt}: unrecognized dividend_quoting_convention "
            f"{convention!r}; expected one of {_KNOWN_DIVIDEND_QUOTING_CONVENTIONS} "
            "or null. Refusing to guess a normalization (BUG-037, design plan §3.1)."
        )
    if convention == "post_split":
        return raw_value
    # convention == "pre_split"
    if net_split_ratio is None or net_split_ratio <= 0:
        raise AmbiguousSameDateActionError(
            f"{ticker} {ex_dt}: dividend quoted 'pre_split' but no positive "
            "same-date net split ratio is available to normalize it against. "
            "Refusing to guess a normalization (BUG-037, design plan §3.1)."
        )
    return raw_value / net_split_ratio


def compute_adjustment_factors(
    corporate_actions: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.DataFrame:
    """Compute a cumulative price adjustment factor for each (ticker, date).

    Args:
        corporate_actions: DataFrame with columns:
            ticker, ex_date, action_type ('split'|'dividend'), value
        prices: DataFrame with columns:
            ticker, date, close
            Used to look up the closing price on dividend ex-dates.

    Returns:
        DataFrame with columns: ticker, date, adj_factor
        adj_factor is applied as: adjusted_price = unadjusted_price * adj_factor
        adj_factor = 1.0 for dates on or after the most recent action.

    The factor is computed by walking backwards from the most recent date
    so that today's prices are never adjusted — only historical prices change.
    """
    if corporate_actions.empty:
        # No actions: adj_factor = 1.0 everywhere
        if prices.empty:
            return pd.DataFrame(columns=["ticker", "date", "adj_factor"])
        result = prices[["ticker", "date"]].copy()
        result["adj_factor"] = Decimal("1")
        return result

    result_frames = []

    for ticker, price_group in prices.groupby("ticker"):
        ca_group = corporate_actions[corporate_actions["ticker"] == ticker].sort_values("ex_date")

        if ca_group.empty:
            factor_df = price_group[["ticker", "date"]].copy()
            factor_df["adj_factor"] = Decimal("1")
            result_frames.append(factor_df)
            continue

        price_group = price_group.sort_values("date").copy()
        dates = price_group["date"].values
        closes = {row["date"]: row["close"] for _, row in price_group.iterrows()}

        # Build a dict of ex_date -> cumulative multiplier, where the
        # multiplier for a given ex_date is the PRODUCT of every action's
        # individual multiplier on that date (BUG-037) — not the multiplier
        # of whichever action happened to be iterated last. Each date's
        # multiplier applies to all price dates strictly before the ex_date.
        multipliers: dict[date, Decimal] = {}

        for ex_dt, day_actions in ca_group.groupby("ex_date"):
            day_multiplier = _combine_same_date_action_multipliers(
                ticker=ticker,
                ex_dt=ex_dt,
                day_actions=day_actions,
                ex_close=closes.get(ex_dt),
            )
            if day_multiplier is not None:
                multipliers[ex_dt] = day_multiplier

        # Apply multipliers: for each date, the adj_factor is the product
        # of all multipliers for actions with ex_date > date (i.e., things
        # that happened after this date in history, which we must adjust for).
        cum_factor = Decimal("1")
        adj_factors = {}

        # Iterate dates in reverse (newest first); accumulate factors as we
        # pass each ex_date going backwards
        sorted_dates = sorted(dates, reverse=True)
        sorted_ex_dates = sorted(multipliers.keys(), reverse=True)
        ex_idx = 0

        for d in sorted_dates:
            # Accumulate any multipliers with ex_date > d
            while ex_idx < len(sorted_ex_dates) and sorted_ex_dates[ex_idx] > d:
                cum_factor *= multipliers[sorted_ex_dates[ex_idx]]
                ex_idx += 1
            adj_factors[d] = cum_factor.quantize(FACTOR_PRECISION, rounding=ROUND_HALF_UP)

        factor_df = price_group[["ticker", "date"]].copy()
        factor_df["adj_factor"] = factor_df["date"].map(adj_factors).fillna(Decimal("1"))
        result_frames.append(factor_df)

    if not result_frames:
        return pd.DataFrame(columns=["ticker", "date", "adj_factor"])

    return pd.concat(result_frames, ignore_index=True)


def apply_adjustment_factors(
    prices: pd.DataFrame,
    factors: pd.DataFrame,
) -> pd.DataFrame:
    """Merge adjustment factors into a price DataFrame and compute adjusted OHLCV.

    Returns the input DataFrame with additional columns:
        adj_open, adj_high, adj_low, adj_close, adj_factor
    """
    df = prices.merge(factors, on=["ticker", "date"], how="left")
    df["adj_factor"] = df["adj_factor"].fillna(Decimal("1"))

    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[f"adj_{col}"] = df.apply(
                lambda row, c=col: (
                    (Decimal(str(row[c])) * row["adj_factor"]).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP
                    )
                    if row[c] is not None
                    else None
                ),
                axis=1,
            )

    return df


# ─── Cutoff-aware, as-of adjustment interfaces (BUG-009 §2.3) ────────────────


@dataclass(frozen=True)
class AsOfAdjustmentMetadata:
    """Provenance recorded alongside every cutoff-aware adjusted series.

    Both `build_score_price_history_as_of` and
    `build_realized_total_return_as_of` return this metadata so the caller
    (and, ultimately, the research-run/methodology record — §4) can record
    exactly which action-source snapshot and availability policy produced
    the series.
    """

    builder: str  # "build_score_price_history_as_of" | "build_realized_total_return_as_of"
    cutoff: datetime
    action_source_versions: tuple[str, ...]
    availability_policy: str
    n_actions_considered: int
    n_actions_excluded_by_cutoff: int
    n_actions_excluded_missing_known_at: int


def _filter_actions_by_cutoff(
    corporate_actions: pd.DataFrame,
    cutoff: datetime,
) -> tuple[pd.DataFrame, int, int]:
    """Keep only actions that (a) had already occurred (`ex_date <= cutoff`'s
    date) AND (b) were knowable by `cutoff` (`known_at <= cutoff`). Returns
    (filtered, n_excluded_by_cutoff, n_excluded_missing).

    Both criteria are required, not just (b): an action that is pre-announced
    (known_at before its own ex_date — e.g. a split announced two weeks
    ahead of its effective date) must not adjust historical prices before it
    has actually occurred, even though it is already "known" in the sense of
    (b) alone. Requiring (a) as well is what makes `build_realized_total_
    return_as_of`'s docstring claim of "(a) occurred (ex_date) and (b) known
    by exit_cutoff" true in the code, not just in prose (adversarial review
    finding, BUG-009 P2). This is a pure additional restriction — it can
    only exclude MORE actions than checking (b) alone, never include one
    that (b)-only filtering would have excluded, so no interface that was
    already correct under (b)-only regresses.

    An action with a null/missing `known_at` cannot be certified as knowable
    by any cutoff and is dropped (not silently included) — per §2.3, "an
    action without a defensible availability timestamp ... cannot be used to
    qualify historical score inputs."
    """
    if corporate_actions.empty:
        return corporate_actions, 0, 0
    required = {"known_at", "ex_date"}
    missing_cols = required - set(corporate_actions.columns)
    if missing_cols:
        raise ValueError(
            f"corporate_actions is missing required column(s) {missing_cols} for "
            "the cutoff-aware adjustment interfaces (migration 011). The legacy "
            "compute_adjustment_factors() full-history routine does not enforce "
            "this and must not be substituted for historical score computation "
            "(BUG-009 §2.3)."
        )

    known_at = corporate_actions["known_at"]
    missing_mask = known_at.isna()
    n_missing = int(missing_mask.sum())
    if n_missing:
        logger.warning(
            "corporate_actions_missing_known_at_excluded",
            n_excluded=n_missing,
            note="actions without a defensible availability timestamp cannot "
            "qualify historical score/return inputs (BUG-009 section 2.3)",
        )

    # Normalize known_at to tz-aware UTC for comparison against cutoff.
    def _as_aware(value):
        if value is None or pd.isna(value):
            return None
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts

    cutoff_ts = pd.Timestamp(cutoff)
    if cutoff_ts.tzinfo is None:
        cutoff_ts = cutoff_ts.tz_localize("UTC")
    cutoff_date = cutoff_ts.date()

    known_by_cutoff = known_at.apply(lambda v: (_as_aware(v) is not None) and (_as_aware(v) <= cutoff_ts))
    occurred_by_cutoff = corporate_actions["ex_date"].apply(
        lambda d: pd.Timestamp(d).date() <= cutoff_date
    )
    knowable_mask = known_by_cutoff & occurred_by_cutoff
    n_excluded_by_cutoff = int((~missing_mask & ~knowable_mask).sum())

    filtered = corporate_actions[knowable_mask].copy()
    return filtered, n_excluded_by_cutoff, n_missing


def build_score_price_history_as_of(
    prices: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    score_cutoff: datetime,
) -> tuple[pd.DataFrame, AsOfAdjustmentMetadata]:
    """Total-return-adjusted price history using only actions that had
    occurred (`ex_date <= score_cutoff`) AND were known by `score_cutoff`.

    This is the ONLY corporate-action adjustment path permitted for a score
    feature at time t (design plan §2.3). A future split/dividend — even one
    that the legacy full-history `compute_adjustment_factors` routine would
    back-adjust into the past, and even one already pre-announced (known_at
    before its own ex_date) — is excluded here: a not-yet-effective action
    must not adjust historical prices before it has actually occurred.

    Args:
        prices: long-format ticker/date/close (raw, unadjusted).
        corporate_actions: must include a `known_at` column (migration 011).
        score_cutoff: the score observation cutoff (typically the session
            close of the score date, e.g. `data.universe.calendar
            .session_close_cutoff`).

    Returns:
        (adjusted_prices, metadata). `adjusted_prices` has the same columns
        as `apply_adjustment_factors` (`adj_close`, `adj_factor`, ...) plus
        the original raw columns — never a fill-price source; raw `close`
        remains available and unmodified.
    """
    filtered_actions, n_excluded_cutoff, n_excluded_missing = _filter_actions_by_cutoff(
        corporate_actions, score_cutoff
    )
    factors = compute_adjustment_factors(filtered_actions, prices)
    adjusted = apply_adjustment_factors(prices, factors)

    versions = tuple(sorted(set(filtered_actions.get("source_version", pd.Series(dtype=str)).dropna())))
    metadata = AsOfAdjustmentMetadata(
        builder="build_score_price_history_as_of",
        cutoff=score_cutoff,
        action_source_versions=versions,
        availability_policy="score_cutoff_known_at_v1",
        n_actions_considered=len(corporate_actions),
        n_actions_excluded_by_cutoff=n_excluded_cutoff,
        n_actions_excluded_missing_known_at=n_excluded_missing,
    )
    logger.info(
        "score_price_history_built",
        score_cutoff=score_cutoff.isoformat(),
        n_actions_used=len(filtered_actions),
        n_actions_excluded_by_cutoff=n_excluded_cutoff,
        n_actions_excluded_missing_known_at=n_excluded_missing,
    )
    return adjusted, metadata


def build_realized_total_return_as_of(
    prices: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    entry_date: date,
    exit_cutoff: datetime,
) -> tuple[pd.DataFrame, AsOfAdjustmentMetadata]:
    """Total-return-adjusted price history for realized-return analytics.

    Uses only actions that (a) occurred (ex_date) and (b) were known by
    `exit_cutoff`. Used for realized IC / total-return diagnostics — NEVER
    for raw fill/order notional, which must stay on the raw tradable price
    series (design plan §2.2; orders/cash notional are a portfolio/execution
    concern this module does not implement or touch).

    Args:
        prices: long-format ticker/date/close (raw, unadjusted).
        corporate_actions: must include a `known_at` column (migration 011).
        entry_date: the research entry date (t+1 under the baseline timing
            policy); recorded in metadata for traceability, not used to
            filter actions (an action prior to entry is still relevant to
            total-return construction, matching the semantics of the
            unrestricted full-history routine within the knowledge cutoff).
        exit_cutoff: the return exit observation cutoff.

    Returns:
        (adjusted_prices, metadata).
    """
    filtered_actions, n_excluded_cutoff, n_excluded_missing = _filter_actions_by_cutoff(
        corporate_actions, exit_cutoff
    )
    factors = compute_adjustment_factors(filtered_actions, prices)
    adjusted = apply_adjustment_factors(prices, factors)

    versions = tuple(sorted(set(filtered_actions.get("source_version", pd.Series(dtype=str)).dropna())))
    metadata = AsOfAdjustmentMetadata(
        builder="build_realized_total_return_as_of",
        cutoff=exit_cutoff,
        action_source_versions=versions,
        availability_policy="exit_cutoff_known_at_v1",
        n_actions_considered=len(corporate_actions),
        n_actions_excluded_by_cutoff=n_excluded_cutoff,
        n_actions_excluded_missing_known_at=n_excluded_missing,
    )
    logger.info(
        "realized_total_return_built",
        entry_date=str(entry_date),
        exit_cutoff=exit_cutoff.isoformat(),
        n_actions_used=len(filtered_actions),
        n_actions_excluded_by_cutoff=n_excluded_cutoff,
        n_actions_excluded_missing_known_at=n_excluded_missing,
    )
    return adjusted, metadata
