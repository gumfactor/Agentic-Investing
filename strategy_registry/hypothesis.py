"""Hypothesis write path (Gate 04 slice 04-3,
docs/plans/04-strategy-selection-protocol-design.md §4.1, §5.1, §7 row 04-3).

Pre-registers a ``strategy_hypotheses`` row -- a named research question plus
its pre-declared parameter-sensitivity grid (§4.4) -- BEFORE any candidate
run happens, and enforces that ``param_grid_json`` becomes immutable at the
application layer once ``frozen_at`` is non-null. ``frozen_at`` itself is set
as a side effect of recording the first linked ``strategy_trials`` row (see
``backtesting/validation/trial_recorder.py::TrialRecorder``); this module
never sets ``frozen_at`` directly -- only ``TrialRecorder`` does, in the same
transaction as the triggering trial insert.

Follows the same DB-access pattern as ``strategy_registry.registry.
StrategyRegistry``: one ``create_engine`` per instance, a SQLite ``PRAGMA
foreign_keys=ON`` connect-event when the URL is SQLite, and short-lived
``Session`` blocks per DB interaction. Application-layer enforcement style
(not a DB trigger) matches ``StrategyRegistry.verify_config_integrity()``,
per design doc §5.1's explicit note on ``frozen_at``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from strategy_registry.models import Base
from strategy_registry.selection_models import StrategyHypothesis

logger = structlog.get_logger(__name__)

# Mirrors strategy_registry.fingerprint's strategy_id format
# (resolve_strategy_id's _ID_PATTERN) and selection_models.py's
# ck_strategy_hypotheses_strategy_id_format Postgres CHECK. Duplicated here
# (rather than importing a private fingerprint symbol) so this module fails
# fast with a clear message BEFORE hitting the DB, on both SQLite (where the
# CHECK is not installed, per selection_models.py's ddl_if(postgresql)) and
# Postgres.
_STRATEGY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,99}$")


def _validate_param_grid(param_grid_json: Optional[dict[str, Any]]) -> None:
    """Fast-fail shape/serializability check for ``param_grid_json``.

    Applied in both ``register_hypothesis`` and ``update_param_grid``,
    BEFORE any DB write, so a malformed grid fails with a clear
    :class:`InvalidHypothesisError` instead of a raw driver
    ``StatementError`` at commit time. ``None`` is always allowed (the
    column is nullable).
    """
    if param_grid_json is None:
        return
    if not isinstance(param_grid_json, dict):
        raise InvalidHypothesisError(
            "param_grid_json must be a dict mapping dot-paths to candidate "
            f"values (got {type(param_grid_json).__name__}: "
            f"{param_grid_json!r})."
        )
    try:
        # allow_nan=False so NaN/Infinity fail fast here (Codex round-1 P2):
        # Python's default json.dumps emits non-standard NaN/Infinity tokens
        # that PostgreSQL JSONB rejects at write time (turning the promised
        # fast-fail into a raw driver StatementError) while SQLite may accept
        # a value production cannot store. Reject strictly, up front.
        json.dumps(param_grid_json, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidHypothesisError(
            f"param_grid_json must be strictly-JSON-serializable (no NaN/Infinity): {exc}"
        ) from exc


# ── Exceptions ────────────────────────────────────────────────────────────────


class HypothesisRegistryError(Exception):
    """Base class for hypothesis write-path errors."""


class InvalidHypothesisError(HypothesisRegistryError):
    """Raised for a malformed strategy_id or empty hypothesis_text."""


class HypothesisNotFoundError(HypothesisRegistryError):
    """Raised when a referenced strategy_hypotheses id does not exist."""


class HypothesisParamGridFrozenError(HypothesisRegistryError):
    """Raised when attempting to edit param_grid_json after frozen_at has
    been set (i.e. after the first linked strategy_trials row was recorded).

    Application-layer enforcement, matching verify_config_integrity()'s
    style -- there is no DB trigger backing this (design doc §5.1).
    """


# ── HypothesisRegistry ────────────────────────────────────────────────────────


class HypothesisRegistry:
    """Write path for pre-registering research hypotheses and enforcing
    ``param_grid_json`` immutability once frozen.
    """

    def __init__(self, db_url: str) -> None:
        self._engine = create_engine(db_url, future=True)
        if db_url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        Base.metadata.create_all(self._engine)

    def register_hypothesis(
        self,
        strategy_id: str,
        hypothesis_text: str,
        param_grid_json: Optional[dict[str, Any]] = None,
    ) -> StrategyHypothesis:
        """Pre-register a hypothesis before any candidate config is run
        (§4.0 step 1).

        Args:
            strategy_id: Matches the Strategy Registry's strategy_id format
                (``^[a-z][a-z0-9_]{2,99}$``). No FK to strategy_definitions/
                strategies -- a hypothesis can precede either (§5.1).
            hypothesis_text: Free-text description of what is being tested
                and why. Must be non-empty.
            param_grid_json: The pre-declared parameter-sensitivity grid
                (§4.4). May be edited freely until the first linked trial
                freezes it (see update_param_grid).

        Returns:
            The newly inserted StrategyHypothesis row (frozen_at is None).

        Raises:
            InvalidHypothesisError: strategy_id fails the format check, or
                hypothesis_text is empty/whitespace-only. Raised before any
                DB write so the caller gets a clear message regardless of
                whether the Postgres-only CHECK constraint would also have
                caught a bad strategy_id.
        """
        if not strategy_id or not _STRATEGY_ID_PATTERN.match(strategy_id):
            raise InvalidHypothesisError(
                f"strategy_id {strategy_id!r} must match "
                f"^[a-z][a-z0-9_]{{2,99}}$ (same format as the Strategy "
                "Registry's strategy_id)."
            )
        if not hypothesis_text or not hypothesis_text.strip():
            raise InvalidHypothesisError(
                "hypothesis_text must be a non-empty description of the "
                "research question this hypothesis pre-registers."
            )
        _validate_param_grid(param_grid_json)

        now = datetime.now(tz=timezone.utc)
        with Session(self._engine) as session:
            hyp = StrategyHypothesis(
                strategy_id=strategy_id,
                hypothesis_text=hypothesis_text.strip(),
                param_grid_json=param_grid_json,
                created_at=now,
            )
            session.add(hyp)
            session.commit()
            session.refresh(hyp)
            logger.info(
                "hypothesis_registered",
                hypothesis_id=hyp.id,
                strategy_id=strategy_id,
            )
            return hyp

    def get_hypothesis(self, hypothesis_id: int) -> StrategyHypothesis:
        with Session(self._engine) as session:
            hyp = session.get(StrategyHypothesis, hypothesis_id)
            if hyp is None:
                raise HypothesisNotFoundError(
                    f"strategy_hypotheses id={hypothesis_id} not found."
                )
            return hyp

    def list_hypotheses(self, strategy_id: str) -> list[StrategyHypothesis]:
        with Session(self._engine) as session:
            return list(
                session.scalars(
                    select(StrategyHypothesis)
                    .where(StrategyHypothesis.strategy_id == strategy_id)
                    .order_by(StrategyHypothesis.created_at.desc())
                )
            )

    def update_param_grid(
        self,
        hypothesis_id: int,
        param_grid_json: Optional[dict[str, Any]],
    ) -> StrategyHypothesis:
        """Edit ``param_grid_json`` for a hypothesis that has not yet been
        frozen (§5.1: ``frozen_at`` set on the first linked trial).

        Args:
            hypothesis_id: The strategy_hypotheses row to edit.
            param_grid_json: The replacement grid (may be None to clear it).

        Returns:
            The updated StrategyHypothesis row.

        Raises:
            HypothesisNotFoundError: No row with this id exists.
            HypothesisParamGridFrozenError: ``frozen_at`` is already set --
                the grid was frozen by the first linked strategy_trials row
                and can no longer be edited. Register a new hypothesis
                instead of trying to retroactively widen/narrow an
                already-tested grid (this is exactly the "no tuning after
                seeing the result" discipline §4.4 requires).

        Note (TOCTOU fix): the frozen-check and the write are ONE atomic
        conditional UPDATE (``WHERE id = :id AND frozen_at IS NULL``), not a
        read-then-write. A read-then-write would let a concurrent
        ``TrialRecorder`` freeze (see ``backtesting.validation.
        trial_recorder.TrialRecorder``) commit BETWEEN this method's read of
        ``frozen_at`` and its write of ``param_grid_json``, letting the edit
        slip through after the grid was frozen. The guard living in the
        UPDATE's WHERE predicate closes that race on both SQLite and
        Postgres without needing ``SELECT ... FOR UPDATE``.
        """
        _validate_param_grid(param_grid_json)
        with Session(self._engine) as session:
            result = session.execute(
                update(StrategyHypothesis.__table__)
                .where(
                    StrategyHypothesis.__table__.c.id == hypothesis_id,
                    StrategyHypothesis.__table__.c.frozen_at.is_(None),
                )
                .values(param_grid_json=param_grid_json)
            )
            if result.rowcount == 1:
                session.commit()
                hyp = session.get(StrategyHypothesis, hypothesis_id)
                logger.info(
                    "hypothesis_param_grid_updated",
                    hypothesis_id=hypothesis_id,
                    strategy_id=hyp.strategy_id,
                )
                return hyp

            # rowcount == 0: either the row does not exist, or it exists but
            # is already frozen. Distinguish with a re-query in the SAME
            # session (no write occurred, so nothing to roll back beyond
            # discarding this no-op transaction).
            session.rollback()
            hyp = session.get(StrategyHypothesis, hypothesis_id)
            if hyp is None:
                raise HypothesisNotFoundError(
                    f"strategy_hypotheses id={hypothesis_id} not found."
                )
            raise HypothesisParamGridFrozenError(
                f"strategy_hypotheses id={hypothesis_id} (strategy_id="
                f"{hyp.strategy_id!r}) param_grid_json is frozen "
                f"(frozen_at={hyp.frozen_at.isoformat()}). It was frozen "
                "when the first strategy_trials row linking this "
                "hypothesis was recorded, and can never be edited again "
                "-- register a new hypothesis instead of retroactively "
                "changing an already-tested grid."
            )
