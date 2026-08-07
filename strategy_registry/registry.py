from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from strategy_registry import fingerprint as fp_module
from strategy_registry.fingerprint import StrategyFingerprint
from strategy_registry import selection_models  # noqa: F401 -- import for side effect: registers
# StrategyHypothesis/StrategyTrial/ResearchDataWindow/PromotionDecision on Base.metadata
# so Base.metadata.create_all() below creates their tables too.
from strategy_registry.models import (
    Base,
    Strategy,
    StrategyDefinition,
    StrategyRun,
    StrategyStatusHistory,
)

log = structlog.get_logger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────


class StrategyRegistryError(Exception):
    pass


class StrategyAlreadyRegisteredError(StrategyRegistryError):
    pass


class StrategyNotFoundError(StrategyRegistryError):
    pass


class DefinitionNotFoundError(StrategyRegistryError):
    pass


class DuplicateVersionError(StrategyRegistryError):
    """strategy_id+version pair already exists with a different config hash."""
    pass


class EvaluationWindowConflictError(StrategyRegistryError):
    """
    An existing (strategy_id, config_hash) definition is being reused with an
    incoming config that is IDENTICAL in strategy identity but DIFFERS in
    evaluation context (the backtest date window, and any other field excluded
    from the identity hash — see strategy_registry/fingerprint.py).

    Since config_hash excludes the evaluation window, "same strategy, different
    window" collapses to one hash and hits the definition-reuse path. Silently
    reusing the stored definition would let its *stored* window govern
    downstream evaluation (PromotionPipeline dispatches ``defn.config``) instead
    of the window the caller supplied — the "value that ran diverges from the
    value supplied" defect class. The evaluation window is a per-measurement
    input, not part of the stored definition's identity; it must be threaded to
    the measurement (e.g. PromotionPipeline holdout window), never smuggled in
    by re-registering the same identity with different dates. Fail closed.
    """
    pass


class InvalidTransitionError(StrategyRegistryError):
    pass


class ConflictingActiveStrategyError(StrategyRegistryError):
    pass


class MissingOperatorNotesError(StrategyRegistryError):
    pass


class InsufficientPaperQualificationError(StrategyRegistryError):
    """Attempted live promotion without any passed paper runs (C8)."""
    pass


class ConfigDriftError(StrategyRegistryError):
    pass


class MissingDataVersionError(StrategyRegistryError):
    pass


class RunLifecycleMismatchError(StrategyRegistryError):
    """run_type is inconsistent with the strategy's current lifecycle status."""
    pass


# ── Status and transitions ────────────────────────────────────────────────────


class StrategyStatus(str, enum.Enum):
    BACKTESTING = "backtesting"
    PAPER = "paper"
    LIVE = "live"
    ARCHIVED = "archived"


_ALLOWED_TRANSITIONS: dict[StrategyStatus, set[StrategyStatus]] = {
    StrategyStatus.BACKTESTING: {StrategyStatus.PAPER, StrategyStatus.ARCHIVED},
    StrategyStatus.PAPER: {
        StrategyStatus.LIVE,
        StrategyStatus.BACKTESTING,
        StrategyStatus.ARCHIVED,
    },
    StrategyStatus.LIVE: {StrategyStatus.PAPER, StrategyStatus.ARCHIVED},
    StrategyStatus.ARCHIVED: set(),
}

_SINGLE_ACTIVE = {StrategyStatus.PAPER, StrategyStatus.LIVE}
_REQUIRE_DATA_VERSION = {"backtest", "walk_forward"}

# Paper/live run_type requires the strategy lifecycle row to exist and be in
# a matching active status. ARCHIVED is intentionally excluded: a terminal
# strategy must not accumulate new qualification records.
_RUN_TYPE_LIFECYCLE_GATE: dict[str, set[StrategyStatus]] = {
    "paper": {StrategyStatus.PAPER},
    "live": {StrategyStatus.LIVE},
}

_VALID_RUN_TYPES = frozenset({"unit", "signal_ic", "backtest", "walk_forward", "paper", "live"})
_VALID_RUN_STATUSES = frozenset({"running", "passed", "failed", "blocked"})


# ── StrategyRegistry ──────────────────────────────────────────────────────────


class StrategyRegistry:
    def __init__(self, db_url: str) -> None:
        self._engine = create_engine(db_url, future=True)
        if db_url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        Base.metadata.create_all(self._engine)

    # ── Definition layer ──────────────────────────────────────────────────────

    def fingerprint(
        self,
        config_path: str,
        explicit_strategy_id: Optional[str] = None,
    ) -> StrategyFingerprint:
        """Validate and hash a config without touching the DB."""
        return fp_module.fingerprint(config_path, explicit_strategy_id)

    @staticmethod
    def _assert_reuse_config_matches(
        existing: StrategyDefinition,
        fp: "StrategyFingerprint",
    ) -> None:
        """
        Guard the definition-reuse path. A stored definition may only be reused
        for an incoming fingerprint whose canonical config is IDENTICAL to the
        stored one. Because both sides share config_hash, every identity field
        already matches by construction; therefore any difference here lives
        entirely in the excluded evaluation-context fields (the backtest date
        window). Reusing the stored row in that case would let its stored window
        silently govern downstream evaluation instead of the caller's window.
        Fail closed rather than discard the incoming config.
        """
        if existing.config != fp.config:
            raise EvaluationWindowConflictError(
                f"strategy_id={fp.strategy_id!r} already has a stored definition "
                f"for config_hash={fp.config_hash[:12]!r} whose evaluation "
                f"context differs from the config supplied now (identity is "
                f"identical — the difference is in fields excluded from the "
                f"identity hash, i.e. the backtest date window). The evaluation "
                f"window is a per-measurement input, not part of the stored "
                f"definition; thread it to the measurement (e.g. "
                f"PromotionPipeline's holdout window) instead of re-registering "
                f"the same identity with different dates. Refusing to silently "
                f"reuse the stored definition."
            )

    def add_definition(
        self,
        config_path: str,
        explicit_strategy_id: Optional[str] = None,
    ) -> StrategyDefinition:
        """
        Validate, hash, and insert into strategy_definitions. Idempotent on
        (strategy_id, config_hash). Raises DuplicateVersionError if the same
        strategy_id+version pair already exists with a different hash.
        """
        fp = fp_module.fingerprint(config_path, explicit_strategy_id)
        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            # Eager-load runs so the returned object is usable after session close.
            existing = session.scalar(
                select(StrategyDefinition)
                .where(
                    StrategyDefinition.strategy_id == fp.strategy_id,
                    StrategyDefinition.config_hash == fp.config_hash,
                )
                .options(selectinload(StrategyDefinition.runs))
            )
            if existing is not None:
                self._assert_reuse_config_matches(existing, fp)
                log.debug("definition_already_exists", strategy_id=fp.strategy_id, config_hash=fp.config_hash[:8])
                return existing

            defn = StrategyDefinition(
                strategy_id=fp.strategy_id,
                config_hash=fp.config_hash,
                name=fp.name,
                version=fp.version,
                description=fp.description,
                portfolio_method=fp.portfolio_method,
                n_long=fp.n_long,
                rebalance_frequency=fp.rebalance_frequency,
                config=fp.config,
                source_path=fp.source_path,
                created_at=now,
            )
            session.add(defn)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                exc_str = str(exc).lower()
                if "uq_strategy_definitions_version" in exc_str or (
                    "unique" in exc_str and "version" in exc_str
                ):
                    raise DuplicateVersionError(
                        f"Version {fp.version} is already registered for "
                        f"'{fp.strategy_id}' with a different config hash. "
                        f"Bump 'version' in the YAML to add a new config variant."
                    ) from exc
                raise

            # Re-query with relationship loading before session closes.
            defn = session.scalar(
                select(StrategyDefinition)
                .where(
                    StrategyDefinition.strategy_id == fp.strategy_id,
                    StrategyDefinition.config_hash == fp.config_hash,
                )
                .options(selectinload(StrategyDefinition.runs))
            )
            log.info("definition_added", strategy_id=fp.strategy_id, config_hash=fp.config_hash[:8], version=fp.version)
            return defn  # type: ignore[return-value]

    def get_definition(
        self,
        strategy_id: str,
        config_hash: str,
    ) -> StrategyDefinition:
        with Session(self._engine) as session:
            defn = session.scalar(
                select(StrategyDefinition)
                .where(
                    StrategyDefinition.strategy_id == strategy_id,
                    StrategyDefinition.config_hash == config_hash,
                )
                .options(selectinload(StrategyDefinition.runs))
            )
            if defn is None:
                raise DefinitionNotFoundError(
                    f"No definition found for ('{strategy_id}', '{config_hash[:8]}…'). "
                    f"Call add_definition() first."
                )
            return defn

    def list_definitions(self, strategy_id: str) -> list[StrategyDefinition]:
        with Session(self._engine) as session:
            return list(
                session.scalars(
                    select(StrategyDefinition)
                    .where(StrategyDefinition.strategy_id == strategy_id)
                    .options(selectinload(StrategyDefinition.runs))
                    .order_by(StrategyDefinition.version)
                )
            )

    # ── Lifecycle layer ───────────────────────────────────────────────────────

    def register(
        self,
        config_path: str,
        strategy_family: Optional[str] = None,
        supersedes_strategy_id: Optional[str] = None,
        notes: Optional[str] = None,
        explicit_strategy_id: Optional[str] = None,
    ) -> Strategy:
        """
        Formally register a strategy for operational use. Calls add_definition()
        internally, then creates the strategies lifecycle row with status=backtesting.
        strategy_id values are permanent — use v{N+1}_… for new config versions.
        """
        fp = fp_module.fingerprint(config_path, explicit_strategy_id)
        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            if session.get(Strategy, fp.strategy_id) is not None:
                raise StrategyAlreadyRegisteredError(
                    f"'{fp.strategy_id}' is already registered. "
                    f"strategy_id values are permanent — create v{{N+1}}_… instead."
                )

            if supersedes_strategy_id is not None:
                if session.get(Strategy, supersedes_strategy_id) is None:
                    raise StrategyNotFoundError(
                        f"supersedes_strategy_id '{supersedes_strategy_id}' not found in the registry."
                    )

            # Ensure definition exists (idempotent). Flush before adding Strategy
            # so the composite FK (strategy_id, canonical_config_hash) is satisfied
            # at INSERT time — required when SQLite FK enforcement is enabled.
            defn = session.get(StrategyDefinition, (fp.strategy_id, fp.config_hash))
            if defn is not None:
                self._assert_reuse_config_matches(defn, fp)
            if defn is None:
                defn = StrategyDefinition(
                    strategy_id=fp.strategy_id,
                    config_hash=fp.config_hash,
                    name=fp.name,
                    version=fp.version,
                    description=fp.description,
                    portfolio_method=fp.portfolio_method,
                    n_long=fp.n_long,
                    rebalance_frequency=fp.rebalance_frequency,
                    config=fp.config,
                    source_path=fp.source_path,
                    created_at=now,
                )
                session.add(defn)
                # flush() must be wrapped: a pre-existing definition with the same
                # strategy_id+version but different hash fires uq_strategy_definitions_version
                # here, before the outer try/except commit block is reached.
                try:
                    session.flush()
                except IntegrityError as exc:
                    session.rollback()
                    exc_str = str(exc).lower()
                    if "uq_strategy_definitions_version" in exc_str or (
                        "unique" in exc_str and "version" in exc_str
                    ):
                        raise DuplicateVersionError(
                            f"Version {fp.version} is already registered for '{fp.strategy_id}' "
                            f"with a different config hash. "
                            f"Bump 'version' in the YAML to add a new config variant."
                        ) from exc
                    raise

            strategy = Strategy(
                strategy_id=fp.strategy_id,
                canonical_config_hash=fp.config_hash,
                status=StrategyStatus.BACKTESTING,
                strategy_family=strategy_family,
                supersedes_strategy_id=supersedes_strategy_id,
                registered_at=now,
                notes=notes,
            )
            session.add(strategy)

            session.add(StrategyStatusHistory(
                strategy_id=fp.strategy_id,
                from_status=None,
                to_status=StrategyStatus.BACKTESTING,
                transitioned_at=now,
                operator_notes=notes,
            ))

            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                exc_str = str(exc).lower()
                # PK collision on strategies.strategy_id from a concurrent register().
                if (
                    "strategies.strategy_id" in exc_str
                    or ("primary key" in exc_str and "strategies" in exc_str)
                    or (
                        "unique" in exc_str
                        and "strategy_id" in exc_str
                        and "version" not in exc_str
                    )
                ):
                    raise StrategyAlreadyRegisteredError(
                        f"'{fp.strategy_id}' was registered by a concurrent process. "
                        f"strategy_id values are permanent — create v{{N+1}}_… instead."
                    ) from exc
                if "uq_strategy_definitions_version" in exc_str or (
                    "unique" in exc_str and "version" in exc_str
                ):
                    raise DuplicateVersionError(
                        f"Version {fp.version} is already registered for '{fp.strategy_id}' "
                        f"with a different config hash."
                    ) from exc
                raise

            # Re-query with relationship loading before session closes.
            strategy = session.scalar(
                select(Strategy)
                .where(Strategy.strategy_id == fp.strategy_id)
                .options(selectinload(Strategy.status_history))
            )
            log.info("strategy_registered", strategy_id=fp.strategy_id, config_hash=fp.config_hash[:8])
            return strategy  # type: ignore[return-value]

    def transition(
        self,
        strategy_id: str,
        to_status: StrategyStatus,
        operator_notes: Optional[str] = None,
    ) -> Strategy:
        """Execute a lifecycle status transition with full guard checks."""
        if to_status == StrategyStatus.LIVE and not (operator_notes and operator_notes.strip()):
            raise MissingOperatorNotesError(
                "operator_notes is required when transitioning to 'live'. "
                "Document the C8 clearance basis."
            )

        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            strategy = self._get_or_raise(session, strategy_id)
            from_status = StrategyStatus(strategy.status)

            allowed = _ALLOWED_TRANSITIONS[from_status]
            if to_status not in allowed:
                raise InvalidTransitionError(
                    f"Cannot transition '{strategy_id}' from '{from_status}' to '{to_status}'. "
                    f"Allowed: {sorted(s.value for s in allowed) or 'none (terminal)'}."
                )

            # C8: require at least one passed paper run before live promotion.
            if to_status == StrategyStatus.LIVE:
                paper_run = session.scalar(
                    select(StrategyRun).where(
                        StrategyRun.strategy_id == strategy_id,
                        StrategyRun.run_type == "paper",
                        StrategyRun.status == "passed",
                    )
                )
                if paper_run is None:
                    raise InsufficientPaperQualificationError(
                        f"Cannot promote '{strategy_id}' to live: no passed paper runs found. "
                        f"C8 requires a 4-week automated paper-trading qualification. "
                        f"Record paper runs via record_run(run_type='paper', status='passed') first."
                    )

            if to_status in _SINGLE_ACTIVE:
                conflict = session.scalar(
                    select(Strategy).where(
                        Strategy.status == to_status,
                        Strategy.strategy_id != strategy_id,
                    )
                )
                if conflict is not None:
                    raise ConflictingActiveStrategyError(
                        f"'{conflict.strategy_id}' is already in '{to_status}' status. "
                        f"Transition it away first."
                    )

            strategy.status = to_status
            if to_status == StrategyStatus.PAPER and strategy.activated_paper_at is None:
                strategy.activated_paper_at = now
            elif to_status == StrategyStatus.LIVE and strategy.activated_live_at is None:
                strategy.activated_live_at = now
            elif to_status == StrategyStatus.ARCHIVED:
                strategy.archived_at = now

            session.add(StrategyStatusHistory(
                strategy_id=strategy_id,
                from_status=from_status,
                to_status=to_status,
                transitioned_at=now,
                operator_notes=operator_notes,
            ))

            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # Partial-unique index on status fires when a concurrent process races us.
                exc_str = str(exc).lower()
                if "uix_strategies_one" in exc_str or (
                    "unique" in exc_str and "status" in exc_str
                ):
                    raise ConflictingActiveStrategyError(
                        f"A concurrent transition placed another strategy into '{to_status}' status. "
                        f"Retry after resolving the conflict."
                    ) from exc
                raise

            # Re-query with relationship loading before session closes.
            strategy = session.scalar(
                select(Strategy)
                .where(Strategy.strategy_id == strategy_id)
                .options(selectinload(Strategy.status_history))
            )
            log.info("strategy_transition", strategy_id=strategy_id, from_status=from_status, to_status=to_status)
            return strategy  # type: ignore[return-value]

    def get(self, strategy_id: str) -> Strategy:
        with Session(self._engine) as session:
            strategy = session.scalar(
                select(Strategy)
                .where(Strategy.strategy_id == strategy_id)
                .options(selectinload(Strategy.status_history))
            )
            if strategy is None:
                raise StrategyNotFoundError(f"Strategy '{strategy_id}' not found in the registry.")
            return strategy

    def list(
        self,
        status: Optional[StrategyStatus] = None,
        strategy_family: Optional[str] = None,
    ) -> list[Strategy]:
        with Session(self._engine) as session:
            q = select(Strategy).options(selectinload(Strategy.status_history))
            if status is not None:
                q = q.where(Strategy.status == status)
            if strategy_family is not None:
                q = q.where(Strategy.strategy_family == strategy_family)
            return list(session.scalars(q.order_by(Strategy.registered_at)))

    def verify_config_integrity(self, strategy_id: str) -> bool:
        """
        Re-fingerprint the YAML at source_path and compare against
        canonical_config_hash. Raises ConfigDriftError if they differ (C6).
        """
        with Session(self._engine) as session:
            strategy = self._get_or_raise(session, strategy_id)
            defn = session.get(
                StrategyDefinition,
                (strategy_id, strategy.canonical_config_hash),
            )
            if defn is None:
                raise DefinitionNotFoundError(
                    f"No definition row found for '{strategy_id}' with its canonical config hash. "
                    f"The definition may have been removed from the DB."
                )
            if not defn.source_path:
                raise ConfigDriftError(
                    f"Cannot verify '{strategy_id}': source_path not recorded in definition."
                )
            current_hash = fp_module.recompute_hash(defn.source_path)
            if current_hash != strategy.canonical_config_hash:
                raise ConfigDriftError(
                    f"Config drift detected for '{strategy_id}'. "
                    f"Stored hash: {strategy.canonical_config_hash[:12]}… "
                    f"Current hash: {current_hash[:12]}… "
                    f"C6: create a new versioned YAML instead of modifying one used in paper/live."
                )
            log.info("config_integrity_verified", strategy_id=strategy_id)
            return True

    # ── Run recording layer ───────────────────────────────────────────────────

    def record_run(
        self,
        strategy_id: str,
        config_hash: str,
        run_type: str,
        status: str,
        metrics: Optional[dict[str, Any]] = None,
        data_version: Optional[str] = None,
        artifact_path: Optional[str] = None,
        mlflow_run_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> StrategyRun:
        """
        Append a run record. data_version required for backtest/walk_forward (C7).
        The (strategy_id, config_hash) must exist in strategy_definitions.
        """
        if run_type not in _VALID_RUN_TYPES:
            raise ValueError(
                f"run_type must be one of {sorted(_VALID_RUN_TYPES)}, got {run_type!r}"
            )
        if status not in _VALID_RUN_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_RUN_STATUSES)}, got {status!r}"
            )
        if run_type in _REQUIRE_DATA_VERSION and not (data_version and data_version.strip()):
            raise MissingDataVersionError(
                f"data_version is required for run_type='{run_type}' (C7). "
                f"Pass the MLflow manifest path."
            )

        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            if session.get(StrategyDefinition, (strategy_id, config_hash)) is None:
                raise DefinitionNotFoundError(
                    f"No definition for ('{strategy_id}', '{config_hash[:8]}…'). "
                    f"Call add_definition() or register() first."
                )

            # Guard: paper/live run_type requires a registered lifecycle row in a
            # matching active status. This prevents:
            # (a) fabricated qualification records when no lifecycle row exists
            # (b) runs inconsistent with the strategy's current status
            # (c) new paper/live records on archived (terminal) strategies
            if run_type in _RUN_TYPE_LIFECYCLE_GATE:
                lifecycle = session.get(Strategy, strategy_id)
                if lifecycle is None:
                    raise RunLifecycleMismatchError(
                        f"Cannot record a '{run_type}' run for '{strategy_id}': "
                        f"strategy is not registered. Call register() first, "
                        f"then transition to the appropriate status."
                    )
                current = StrategyStatus(lifecycle.status)
                allowed = _RUN_TYPE_LIFECYCLE_GATE[run_type]
                if current not in allowed:
                    raise RunLifecycleMismatchError(
                        f"Cannot record a '{run_type}' run for '{strategy_id}' "
                        f"which is currently in '{current}' status. "
                        f"Allowed statuses for '{run_type}' runs: "
                        f"{sorted(s.value for s in allowed)}."
                    )

            run = StrategyRun(
                strategy_id=strategy_id,
                config_hash=config_hash,
                run_type=run_type,
                status=status,
                data_version=data_version,
                metrics=metrics or {},
                artifact_path=artifact_path,
                mlflow_run_id=mlflow_run_id,
                notes=notes,
                started_at=now,
                completed_at=now if status != "running" else None,
            )
            session.add(run)
            session.commit()
            session.refresh(run)

            log.info("run_recorded", strategy_id=strategy_id, run_type=run_type, status=status)
            return run

    def get_runs(
        self,
        strategy_id: str,
        run_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[StrategyRun]:
        with Session(self._engine) as session:
            q = select(StrategyRun).where(StrategyRun.strategy_id == strategy_id)
            if run_type is not None:
                q = q.where(StrategyRun.run_type == run_type)
            if status is not None:
                q = q.where(StrategyRun.status == status)
            return list(session.scalars(q.order_by(StrategyRun.started_at.desc())))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_or_raise(self, session: Session, strategy_id: str) -> Strategy:
        strategy = session.get(Strategy, strategy_id)
        if strategy is None:
            raise StrategyNotFoundError(f"Strategy '{strategy_id}' not found in the registry.")
        return strategy
