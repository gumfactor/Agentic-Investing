"""Trade journal — append-only fill store with FIFO P&L and wash-sale history.

Phase 5, M5.2.  Unblocks:
  - ComplianceEngine._check_wash_sale() (previously a stub — ctx['recent_loss_sells']
    was never populated because this module did not exist)
  - Realized P&L tracking for performance attribution
  - Open-position cost-basis reconstruction for tearsheets

Safety rule C3: TradeJournal.record_fill() is INSERT-only.  No UPDATE or DELETE
is issued against trade_fills by any method in this module.

Production schema is managed by Alembic migration 004_trade_journal_schema.py.
Tests call TradeJournal.create_schema(engine) to create the table in SQLite.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import MetaData, Table, Column
import structlog

from execution.oms.order import Order, OrderSide, OrderStatus

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = structlog.get_logger(__name__)

# ── SQLAlchemy table definition ───────────────────────────────────────────────
# Uses standard SA types for cross-dialect compatibility: SQLite in tests,
# PostgreSQL/TimescaleDB in production.  The Alembic migration may use
# PostgreSQL-native UUID or TIMESTAMPTZ; both accept the string/datetime values
# that this module inserts.

_metadata = MetaData()

_trade_fills = Table(
    "trade_fills",
    _metadata,
    Column("fill_id", sa.String(36), primary_key=True),
    Column("order_id", sa.String(36), nullable=False),
    Column("broker_order_id", sa.Text()),
    Column("ticker", sa.Text(), nullable=False),
    Column("strategy_id", sa.Text(), nullable=False),
    Column("side", sa.Text(), nullable=False),
    # filled_quantity is the INCREMENTAL quantity filled in this event (not cumulative).
    # For the first fill of an order this equals order.filled_quantity; for subsequent
    # events (PARTIALLY_FILLED → FILLED) it is the delta.  FIFO calculations use
    # SUM(filled_quantity) which correctly totals incremental shares.
    Column("filled_quantity", sa.Numeric(18, 6), nullable=False),
    # cumulative_filled_quantity mirrors order.filled_quantity at the time of recording.
    # Used for dedup (unique with order_id) and for operator display / audit.
    Column("cumulative_filled_quantity", sa.Numeric(18, 6), nullable=False),
    Column("avg_fill_price", sa.Numeric(18, 6), nullable=False),
    Column("limit_price", sa.Numeric(18, 6)),
    Column("fill_timestamp", sa.DateTime(timezone=True), nullable=False),
    Column("order_status_at_record", sa.Text(), nullable=False),
    Column("realized_pnl", sa.Numeric(18, 6)),
    Column("cost_basis_per_share", sa.Numeric(18, 6)),
    Column("wash_sale_disallowed", sa.Boolean(), nullable=False),
    Column("notes", sa.Text(), nullable=False),
    Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class FillRecord:
    """Immutable snapshot of a persisted fill row."""

    fill_id: str
    order_id: str
    broker_order_id: str | None
    ticker: str
    strategy_id: str
    side: str                              # 'BUY' | 'SELL'
    filled_quantity: float                 # incremental shares in this event
    cumulative_filled_quantity: float      # running total for this order
    avg_fill_price: float
    limit_price: float | None
    fill_timestamp: datetime
    order_status_at_record: str
    realized_pnl: float | None             # None for BUYs; FIFO P&L for SELLs (incremental)
    cost_basis_per_share: float | None
    wash_sale_disallowed: bool
    notes: str
    ingested_at: datetime


def _coerce_datetime(val: object) -> datetime | None:
    """Coerce raw DB value to timezone-aware datetime.

    SQLite returns ISO strings; PostgreSQL returns datetime objects.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if isinstance(val, str):
        # SQLite: "2024-01-15 12:00:00.000000+00:00" or "2024-01-15T12:00:00+00:00"
        val = val.replace(" ", "T", 1)
        if val.endswith("+00:00") or "+" in val[10:] or val.endswith("Z"):
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        return datetime.fromisoformat(val).replace(tzinfo=timezone.utc)
    raise TypeError(f"Cannot coerce {type(val)} to datetime")


def _row_to_fill_record(row: sa.engine.Row) -> FillRecord:
    return FillRecord(
        fill_id=row.fill_id,
        order_id=row.order_id,
        broker_order_id=row.broker_order_id,
        ticker=row.ticker,
        strategy_id=row.strategy_id,
        side=row.side,
        filled_quantity=float(row.filled_quantity),
        cumulative_filled_quantity=float(row.cumulative_filled_quantity),
        avg_fill_price=float(row.avg_fill_price),
        limit_price=float(row.limit_price) if row.limit_price is not None else None,
        fill_timestamp=_coerce_datetime(row.fill_timestamp),  # type: ignore[arg-type]
        order_status_at_record=row.order_status_at_record,
        realized_pnl=float(row.realized_pnl) if row.realized_pnl is not None else None,
        cost_basis_per_share=(
            float(row.cost_basis_per_share) if row.cost_basis_per_share is not None else None
        ),
        wash_sale_disallowed=bool(row.wash_sale_disallowed),
        notes=row.notes,
        ingested_at=_coerce_datetime(row.ingested_at),  # type: ignore[arg-type]
    )


class TradeJournal:
    """Append-only fill store with FIFO P&L and wash-sale history.

    Parameters
    ----------
    engine:
        SQLAlchemy engine.  In tests, use ``sa.create_engine("sqlite:///:memory:")``
        and call ``TradeJournal.create_schema(engine)`` before instantiating.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    # ── Schema bootstrap (tests only) ─────────────────────────────────────────

    @staticmethod
    def create_schema(engine: "Engine") -> None:
        """Create trade_fills table.  Tests only — production uses Alembic."""
        _metadata.create_all(engine)

    # ── Write ─────────────────────────────────────────────────────────────────

    def record_fill(self, order: Order) -> FillRecord:
        """Persist a fill from an order that has transitioned to FILLED or
        PARTIALLY_FILLED.

        Stores the INCREMENTAL quantity filled in this event (not cumulative) so
        that FIFO lot reconstruction remains correct across PARTIALLY_FILLED→FILLED
        progressions.  ``cumulative_filled_quantity`` preserves the running total
        for display and audit.

        For SELL orders, computes FIFO realized P&L on the incremental quantity
        against prior BUY fills for the same (ticker, strategy_id).

        Raises
        ------
        ValueError
            - Order has no fill data (filled_quantity=0 or avg_fill_price=None).
            - Order is not in FILLED or PARTIALLY_FILLED state.
            - No new incremental fill: order.filled_quantity <= previously recorded
              cumulative (duplicate call or non-advancing fill).
            - SELL incremental quantity exceeds open long quantity (long-only
              violation).
        """
        if order.filled_quantity <= 0 or order.avg_fill_price is None:
            raise ValueError(
                f"order {order.order_id[:8]} has no fill data "
                f"(filled_quantity={order.filled_quantity}, "
                f"avg_fill_price={order.avg_fill_price})"
            )
        if order.status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError(
                f"order {order.order_id[:8]} is not in a filled state "
                f"(status={order.status.value}); only FILLED or PARTIALLY_FILLED "
                "orders should be recorded"
            )

        now = datetime.now(timezone.utc)

        with self._engine.begin() as conn:
            # Fetch the most recent recorded row for this order (if any) so we
            # can compute the incremental quantity and back-calculate the exact
            # price for the incremental shares.
            prior_row = conn.execute(
                sa.select(
                    _trade_fills.c.cumulative_filled_quantity,
                    _trade_fills.c.avg_fill_price,
                )
                .where(_trade_fills.c.order_id == order.order_id)
                .order_by(_trade_fills.c.cumulative_filled_quantity.desc())
                .limit(1)
            ).one_or_none()

            if prior_row is not None:
                prior_max_cumulative = float(prior_row.cumulative_filled_quantity)
                prior_avg_fill_price = float(prior_row.avg_fill_price)
            else:
                prior_max_cumulative = 0.0
                prior_avg_fill_price = 0.0

            incremental_qty = order.filled_quantity - prior_max_cumulative
            if incremental_qty <= 1e-9:
                raise ValueError(
                    f"no new fill for order {order.order_id[:8]}: "
                    f"order reports {order.filled_quantity} shares cumulative, "
                    f"journal already has {prior_max_cumulative:.4f} recorded"
                )

            # Derive the exact price for this specific fill event.  When prior
            # fill records exist, the broker's avg_fill_price is a cumulative
            # weighted average; back-calculating isolates the incremental price:
            #   incremental_price = (new_cumul_avg × new_qty − old_cumul_avg × old_qty)
            #                       / incremental_qty
            # This prevents attributing the blended average to incremental shares
            # when a partial fill and a final fill execute at different prices.
            if prior_max_cumulative > 1e-9:
                incremental_fill_price = (
                    order.avg_fill_price * order.filled_quantity
                    - prior_avg_fill_price * prior_max_cumulative
                ) / incremental_qty
            else:
                incremental_fill_price = order.avg_fill_price

            realized_pnl: float | None = None
            cost_basis_per_share: float | None = None
            if order.side == OrderSide.SELL:
                # P&L computed on incremental quantity and exact incremental price
                # so successive partial-fill records don't double-count prior lots.
                cost_basis_per_share, realized_pnl = self._fifo_pnl(
                    order.ticker,
                    order.strategy_id,
                    incremental_qty,
                    incremental_fill_price,
                )

            fill_id = str(uuid.uuid4())
            row = {
                "fill_id": fill_id,
                "order_id": order.order_id,
                "broker_order_id": order.broker_order_id,
                "ticker": order.ticker,
                "strategy_id": order.strategy_id,
                "side": order.side.value,
                "filled_quantity": incremental_qty,
                "cumulative_filled_quantity": order.filled_quantity,
                # Store the exact incremental price, not the cumulative average.
                "avg_fill_price": incremental_fill_price,
                "limit_price": order.limit_price,
                "fill_timestamp": order.updated_at,
                "order_status_at_record": order.status.value,
                "realized_pnl": realized_pnl,
                "cost_basis_per_share": cost_basis_per_share,
                "wash_sale_disallowed": False,
                "notes": order.notes,
                "ingested_at": now,
            }
            conn.execute(_trade_fills.insert().values(**row))

        logger.info(
            "fill_recorded",
            order_id=order.order_id[:8],
            ticker=order.ticker,
            side=order.side.value,
            incremental_qty=incremental_qty,
            cumulative_qty=order.filled_quantity,
            incremental_fill_price=incremental_fill_price,
            realized_pnl=realized_pnl,
        )

        return FillRecord(
            fill_id=fill_id,
            order_id=order.order_id,
            broker_order_id=order.broker_order_id,
            ticker=order.ticker,
            strategy_id=order.strategy_id,
            side=order.side.value,
            filled_quantity=incremental_qty,
            cumulative_filled_quantity=order.filled_quantity,
            avg_fill_price=incremental_fill_price,
            limit_price=order.limit_price,
            fill_timestamp=order.updated_at,
            order_status_at_record=order.status.value,
            realized_pnl=realized_pnl,
            cost_basis_per_share=cost_basis_per_share,
            wash_sale_disallowed=False,
            notes=order.notes,
            ingested_at=now,
        )

    def recover_missed_fills(self, orders: list[Order]) -> list[FillRecord]:
        """Replay record_fill() for FILLED/PARTIALLY_FILLED orders not yet in the journal.

        Use this to recover from a DB connection failure that caused
        _record_fill_to_journal() to swallow an exception during
        reconcile_fills().  Safe to call with all orders — those already
        recorded at their current cumulative quantity are skipped automatically
        (the incremental-qty check treats them as no-ops).

        Returns the list of newly recorded FillRecords.
        """
        recovered: list[FillRecord] = []
        for order in orders:
            if order.filled_quantity <= 0 or order.avg_fill_price is None:
                continue
            if order.status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
                continue
            try:
                rec = self.record_fill(order)
                recovered.append(rec)
                logger.info(
                    "fill_recovered",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    incremental_qty=rec.filled_quantity,
                )
            except ValueError as exc:
                # No new fill for this order — already recorded or no fill data.
                logger.debug(
                    "fill_recovery_skipped",
                    order_id=order.order_id[:8],
                    reason=str(exc),
                )
        return recovered

    # ── Wash-sale context ─────────────────────────────────────────────────────

    def wash_sale_context(
        self,
        tickers: list[str],
        window_days: int = 30,
        as_of: date | None = None,
    ) -> dict[str, date]:
        """Return {ticker: last_loss_sell_date} for tickers with a loss-realizing
        SELL fill within ``window_days`` of ``as_of``.

        This populates ``ctx['recent_loss_sells']`` for
        ``ComplianceEngine._check_wash_sale()``, which blocks replacement BUY
        orders within 30 days of a loss-realizing SELL of the same ticker.

        Parameters
        ----------
        tickers:
            Tickers to query.  Typically the BUY-side tickers in the pending
            order batch (the candidate replacement buys).
        window_days:
            Look-back window in calendar days (default 30 matches the IRS
            wash-sale window).
        as_of:
            Reference date; defaults to today (UTC).
        """
        if not tickers:
            return {}

        as_of_dt = as_of or datetime.now(timezone.utc).date()
        cutoff = datetime(
            as_of_dt.year, as_of_dt.month, as_of_dt.day, tzinfo=timezone.utc
        ) - timedelta(days=window_days)

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    _trade_fills.c.ticker,
                    sa.func.max(_trade_fills.c.fill_timestamp).label("last_ts"),
                ).where(
                    (_trade_fills.c.ticker.in_(tickers))
                    & (_trade_fills.c.side == "SELL")
                    & (_trade_fills.c.realized_pnl < 0)
                    & (_trade_fills.c.fill_timestamp >= cutoff)
                ).group_by(_trade_fills.c.ticker)
            ).fetchall()

        result: dict[str, date] = {}
        for row in rows:
            ts = _coerce_datetime(row.last_ts)
            if ts is not None:
                result[row.ticker] = ts.date()
        return result

    # ── Queries ───────────────────────────────────────────────────────────────

    def fill_history(
        self,
        ticker: str | None = None,
        strategy_id: str | None = None,
        since: date | None = None,
    ) -> list[FillRecord]:
        """Return fill records matching the given filters, ordered by
        fill_timestamp ascending."""
        conditions = []
        if ticker is not None:
            conditions.append(_trade_fills.c.ticker == ticker)
        if strategy_id is not None:
            conditions.append(_trade_fills.c.strategy_id == strategy_id)
        if since is not None:
            since_ts = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
            conditions.append(_trade_fills.c.fill_timestamp >= since_ts)

        stmt = sa.select(_trade_fills).order_by(_trade_fills.c.fill_timestamp.asc())
        if conditions:
            stmt = stmt.where(sa.and_(*conditions))

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()

        return [_row_to_fill_record(r) for r in rows]

    def realized_pnl_summary(
        self,
        strategy_id: str | None = None,
        since: date | None = None,
    ) -> dict[str, float]:
        """Return cumulative realized P&L by ticker, filtered to SELL fills only.

        Returns {ticker: total_realized_pnl}.
        """
        conditions = [
            _trade_fills.c.side == "SELL",
            _trade_fills.c.realized_pnl.isnot(None),
        ]
        if strategy_id is not None:
            conditions.append(_trade_fills.c.strategy_id == strategy_id)
        if since is not None:
            since_ts = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
            conditions.append(_trade_fills.c.fill_timestamp >= since_ts)

        stmt = (
            sa.select(
                _trade_fills.c.ticker,
                sa.func.sum(_trade_fills.c.realized_pnl).label("total_pnl"),
            )
            .where(sa.and_(*conditions))
            .group_by(_trade_fills.c.ticker)
        )

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()

        return {row.ticker: float(row.total_pnl) for row in rows}

    def open_position_cost_basis(
        self,
        strategy_id: str | None = None,
    ) -> dict[str, tuple[float, float]]:
        """Return {ticker: (net_quantity, avg_cost_basis)} for open positions.

        Uses FIFO lot reconstruction to determine the remaining open lots and
        their weighted-average cost basis.  Tickers with zero net quantity are
        excluded.
        """
        conditions = []
        if strategy_id is not None:
            conditions.append(_trade_fills.c.strategy_id == strategy_id)

        where = sa.and_(*conditions) if conditions else sa.true()

        with self._engine.connect() as conn:
            ticker_rows = conn.execute(
                sa.select(_trade_fills.c.ticker)
                .where(where)
                .distinct()
            ).fetchall()

            result: dict[str, tuple[float, float]] = {}
            for (ticker,) in ticker_rows:
                lots = self._open_lots(ticker, strategy_id, conn)
                if not lots:
                    continue
                net_qty = sum(q for q, _ in lots)
                if net_qty < 1e-9:
                    continue
                total_cost = sum(q * p for q, p in lots)
                avg_cost = total_cost / net_qty
                result[ticker] = (net_qty, avg_cost)

        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fifo_pnl(
        self,
        ticker: str,
        strategy_id: str,
        sell_qty: float,
        sell_price: float,
    ) -> tuple[float, float]:
        """Compute FIFO cost basis and realized P&L for a SELL.

        Returns (cost_basis_per_share, realized_pnl).

        Raises ValueError if open long quantity < sell_qty (long-only violation).
        """
        with self._engine.connect() as conn:
            buy_rows = conn.execute(
                sa.select(
                    _trade_fills.c.filled_quantity,
                    _trade_fills.c.avg_fill_price,
                )
                .where(
                    (_trade_fills.c.ticker == ticker)
                    & (_trade_fills.c.strategy_id == strategy_id)
                    & (_trade_fills.c.side == "BUY")
                )
                .order_by(_trade_fills.c.fill_timestamp.asc())
            ).fetchall()

            prior_sell_scalar = conn.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.sum(_trade_fills.c.filled_quantity),
                        sa.literal(0),
                    )
                ).where(
                    (_trade_fills.c.ticker == ticker)
                    & (_trade_fills.c.strategy_id == strategy_id)
                    & (_trade_fills.c.side == "SELL")
                )
            ).scalar()

        lots = [
            (float(r.filled_quantity), float(r.avg_fill_price)) for r in buy_rows
        ]
        prior_sold = float(prior_sell_scalar or 0)

        # Consume prior sells from lots (FIFO) to find remaining open lots
        remaining: list[tuple[float, float]] = []
        to_consume = prior_sold
        for lot_qty, lot_price in lots:
            if to_consume >= lot_qty - 1e-9:
                to_consume = max(0.0, to_consume - lot_qty)
            elif to_consume > 1e-9:
                remaining.append((lot_qty - to_consume, lot_price))
                to_consume = 0.0
            else:
                remaining.append((lot_qty, lot_price))

        total_open = sum(q for q, _ in remaining)
        if sell_qty > total_open + 1e-6:
            raise ValueError(
                f"long-only violation: cannot sell {sell_qty:.4f} shares of "
                f"{ticker!r} (strategy={strategy_id!r}); "
                f"only {total_open:.4f} shares open after FIFO reconstruction"
            )

        # Consume current sell from remaining lots (FIFO)
        cost_total = 0.0
        to_sell = sell_qty
        for lot_qty, lot_price in remaining:
            if to_sell <= 1e-9:
                break
            consumed = min(to_sell, lot_qty)
            cost_total += consumed * lot_price
            to_sell -= consumed

        cost_basis_per_share = cost_total / sell_qty
        realized_pnl = (sell_price - cost_basis_per_share) * sell_qty
        return cost_basis_per_share, realized_pnl

    def _open_lots(
        self,
        ticker: str,
        strategy_id: str | None,
        conn: sa.engine.Connection,
    ) -> list[tuple[float, float]]:
        """Return remaining open buy lots as [(qty, price), ...] using FIFO."""
        conditions = [
            _trade_fills.c.ticker == ticker,
            _trade_fills.c.side == "BUY",
        ]
        if strategy_id is not None:
            conditions.append(_trade_fills.c.strategy_id == strategy_id)

        buy_rows = conn.execute(
            sa.select(
                _trade_fills.c.filled_quantity,
                _trade_fills.c.avg_fill_price,
            )
            .where(sa.and_(*conditions))
            .order_by(_trade_fills.c.fill_timestamp.asc())
        ).fetchall()

        sell_conditions = [
            _trade_fills.c.ticker == ticker,
            _trade_fills.c.side == "SELL",
        ]
        if strategy_id is not None:
            sell_conditions.append(_trade_fills.c.strategy_id == strategy_id)

        prior_sold = float(
            conn.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.sum(_trade_fills.c.filled_quantity),
                        sa.literal(0),
                    )
                ).where(sa.and_(*sell_conditions))
            ).scalar()
            or 0
        )

        remaining: list[tuple[float, float]] = []
        to_consume = prior_sold
        for row in buy_rows:
            lot_qty = float(row.filled_quantity)
            lot_price = float(row.avg_fill_price)
            if to_consume >= lot_qty - 1e-9:
                to_consume = max(0.0, to_consume - lot_qty)
            elif to_consume > 1e-9:
                remaining.append((lot_qty - to_consume, lot_price))
                to_consume = 0.0
            else:
                remaining.append((lot_qty, lot_price))

        return remaining
