from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from strategy_registry import fingerprint as fp_module
from strategy_registry.fingerprint import StrategyFingerprint
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


class InvalidTransitionError(StrategyRegistryError):
    pass


class ConflictingActiveStrategyError(StrategyRegistryError):
    pass


class MissingOperatorNotesError(StrategyRegistryError):
    pass


class ConfigDriftError(StrategyRegistryError):
    pass


class MissingDataVersionError(StrategyRegistryError):
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
            existing = session.get(StrategyDefinition, (fp.strategy_id, fp.config_hash))
            if existing is not None:
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
                if "uq_strategy_definitions_version" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise DuplicateVersionError(
                        f"Version {fp.version} is already registered for "
                        f"'{fp.strategy_id}' with a different config hash. "
                        f"Bump 'version' in the YAML to add a new config variant."
                    ) from exc
                raise
            session.refresh(defn)
            log.info("definition_added", strategy_id=fp.strategy_id, config_hash=fp.config_hash[:8], version=fp.version)
            return defn

    def get_definition(
        self,
        strategy_id: str,
        config_hash: str,
    ) -> StrategyDefinition:
        with Session(self._engine) as session:
            defn = session.get(StrategyDefinition, (strategy_id, config_hash))
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
                session.flush()

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
                if "uq_strategy_definitions_version" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise DuplicateVersionError(
                        f"Version {fp.version} is already registered for '{fp.strategy_id}' "
                        f"with a different config hash."
                    ) from exc
                raise

            session.refresh(strategy)
            log.info("strategy_registered", strategy_id=fp.strategy_id, config_hash=fp.config_hash[:8])
            return strategy

    def transition(
        self,
        strategy_id: str,
        to_status: StrategyStatus,
        operator_notes: Optional[str] = None,
    ) -> Strategy:
        """Execute a lifecycle status transition with full guard checks."""
        if to_status == StrategyStatus.LIVE and not operator_notes:
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
            session.commit()
            session.refresh(strategy)

            log.info("strategy_transition", strategy_id=strategy_id, from_status=from_status, to_status=to_status)
            return strategy

    def get(self, strategy_id: str) -> Strategy:
        with Session(self._engine) as session:
            return self._get_or_raise(session, strategy_id)

    def list(
        self,
        status: Optional[StrategyStatus] = None,
        strategy_family: Optional[str] = None,
    ) -> list[Strategy]:
        with Session(self._engine) as session:
            q = select(Strategy)
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
            if defn is None or not defn.source_path:
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
        if run_type in _REQUIRE_DATA_VERSION and not data_version:
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
