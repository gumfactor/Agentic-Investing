"""Tests for the strategy-selection protocol schema (Gate 04 slice 04-1,
docs/plans/04-strategy-selection-protocol-design.md §5.1, §7 row 04-1).

Follows this repo's established migration-testing convention (see
data/tests/universe/test_eligibility_migration.py's docstring): Alembic
migration 014 carries no Postgres-only DDL (unlike 009/013's
EXCLUDE-USING-gist extensions), so its ``upgrade``/``downgrade`` shape is
verified directly (revision chain + callable functions), and the actual
table/constraint behavior is exercised end-to-end via the mirrored ORM
models in ``strategy_registry/selection_models.py`` against SQLite
(``Base.metadata.create_all``/``drop_all``), standing in for
upgrade/downgrade. SQLite CHECK constraints and the dual
``postgresql_where``/``sqlite_where`` partial unique index both enforce
natively on SQLite, so every constraint asserted here is a genuine
enforcement proof, not merely a schema-shape check -- noted per constraint
below where it matters.

``PRAGMA foreign_keys=ON`` is required for SQLite to enforce the FK
constraints at all (SQLite's default is OFF); this mirrors
``strategy_registry/registry.py``'s own connect-event pragma.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from strategy_registry.models import Base, StrategyDefinition
from strategy_registry.selection_models import (
    PromotionDecision,
    ResearchDataWindow,
    StrategyHypothesis,
    StrategyTrial,
)

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")


# ── Migration shape ──────────────────────────────────────────────────────────


def _migration_module():
    return importlib.import_module(
        "infra.db.migrations.versions.014_strategy_selection_protocol"
    )


def test_revision_chain_follows_013() -> None:
    mod = _migration_module()
    assert mod.revision == "014"
    assert mod.down_revision == "013"


def test_upgrade_and_downgrade_functions_exist() -> None:
    mod = _migration_module()
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_no_other_migration_claims_revision_014() -> None:
    """Guards against a duplicate revision id the way 004/004b already shows
    can happen in this repo's history -- 014 must be unique among the
    versions directory."""
    versions_dir = (
        Path(__file__).resolve().parents[2] / "infra" / "db" / "migrations" / "versions"
    )
    claimants = []
    for path in sorted(versions_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("revision") and '"014"' in stripped:
                claimants.append(path.name)
                break
    assert claimants == ["014_strategy_selection_protocol.py"], claimants


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'selection_014.db'}", future=True)

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _make_definition(session: Session, *, strategy_id: str = "v1_test_strategy") -> StrategyDefinition:
    defn = StrategyDefinition(
        strategy_id=strategy_id,
        config_hash="a" * 64,
        name=strategy_id,
        version=1,
        config={"foo": "bar"},
        created_at=datetime.now(timezone.utc),
    )
    session.add(defn)
    session.commit()
    return defn


def _make_hypothesis(session: Session, *, strategy_id: str = "v1_test_strategy") -> StrategyHypothesis:
    hyp = StrategyHypothesis(
        strategy_id=strategy_id,
        hypothesis_text="momentum beats buy-and-hold in this universe",
        created_at=datetime.now(timezone.utc),
    )
    session.add(hyp)
    session.commit()
    return hyp


def _make_trial(
    session: Session,
    *,
    strategy_id: str,
    config_hash: str,
    hypothesis_id: int,
    run_type: str = "walk_forward",
    status: str = "completed",
    window: str = "train_oos",
) -> StrategyTrial:
    trial = StrategyTrial(
        strategy_id=strategy_id,
        config_hash=config_hash,
        hypothesis_id=hypothesis_id,
        window=window,
        run_type=run_type,
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status=status,
        started_at=datetime.now(timezone.utc),
    )
    session.add(trial)
    session.commit()
    return trial


# ── All four tables exist after "upgrade" (create_all) ─────────────────────────


def test_all_four_tables_exist_after_create_all(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "strategy_hypotheses" in table_names
    assert "strategy_trials" in table_names
    assert "research_data_windows" in table_names
    assert "promotion_decisions" in table_names


def test_all_four_tables_dropped_after_drop_all(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'drop_check.db'}", future=True)

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    Base.metadata.drop_all(eng)
    inspector = inspect(eng)
    remaining = set(inspector.get_table_names())
    assert "strategy_hypotheses" not in remaining
    assert "strategy_trials" not in remaining
    assert "research_data_windows" not in remaining
    assert "promotion_decisions" not in remaining


# ── research_data_windows: window-ordering CHECK ────────────────────────────────


def test_research_data_windows_rejects_misordered_range(session: Session) -> None:
    """ck_research_data_windows_order must reject a window whose OOS start
    precedes train_end (an overlapping/misordered range) -- genuine SQLite
    CHECK-constraint enforcement, not just a schema-shape assertion."""
    bad = ResearchDataWindow(
        strategy_id="v1_test_strategy",
        train_start=date(2022, 1, 1),
        train_end=date(2023, 1, 1),
        # oos_start before train_end -- violates the ordering CHECK.
        oos_start=date(2022, 6, 1),
        oos_end=date(2023, 6, 1),
        holdout_start=date(2023, 6, 1),
        holdout_end=date(2023, 12, 1),
        created_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_research_data_windows_rejects_overlapping_holdout(session: Session) -> None:
    """holdout_start must not precede oos_end -- an overlap between the OOS
    and holdout windows is exactly the multiple-testing leak §4.2 exists to
    prevent."""
    session.rollback()
    bad = ResearchDataWindow(
        strategy_id="v1_test_strategy",
        train_start=date(2022, 1, 1),
        train_end=date(2022, 7, 1),
        oos_start=date(2022, 7, 1),
        oos_end=date(2023, 7, 1),
        # holdout_start before oos_end -- violates the ordering CHECK.
        holdout_start=date(2023, 1, 1),
        holdout_end=date(2023, 12, 1),
        created_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_research_data_windows_accepts_well_ordered_range(session: Session) -> None:
    good = ResearchDataWindow(
        strategy_id="v1_test_strategy",
        train_start=date(2022, 1, 1),
        train_end=date(2022, 7, 1),
        oos_start=date(2022, 7, 1),
        oos_end=date(2023, 7, 1),
        holdout_start=date(2023, 7, 1),
        holdout_end=date(2023, 12, 31),
        created_at=datetime.now(timezone.utc),
    )
    session.add(good)
    session.commit()
    assert good.id is not None


# ── research_data_windows: scope-XOR CHECK ──────────────────────────────────────


def test_research_data_windows_rejects_both_family_and_strategy_set(session: Session) -> None:
    bad = ResearchDataWindow(
        strategy_family="momentum",
        strategy_id="v1_test_strategy",
        train_start=date(2022, 1, 1),
        train_end=date(2022, 7, 1),
        oos_start=date(2022, 7, 1),
        oos_end=date(2023, 7, 1),
        holdout_start=date(2023, 7, 1),
        holdout_end=date(2023, 12, 31),
        created_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_research_data_windows_rejects_neither_family_nor_strategy_set(session: Session) -> None:
    session.rollback()
    bad = ResearchDataWindow(
        strategy_family=None,
        strategy_id=None,
        train_start=date(2022, 1, 1),
        train_end=date(2022, 7, 1),
        oos_start=date(2022, 7, 1),
        oos_end=date(2023, 7, 1),
        holdout_start=date(2023, 7, 1),
        holdout_end=date(2023, 12, 31),
        created_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_research_data_windows_accepts_family_only_scope(session: Session) -> None:
    good = ResearchDataWindow(
        strategy_family="momentum",
        strategy_id=None,
        train_start=date(2022, 1, 1),
        train_end=date(2022, 7, 1),
        oos_start=date(2022, 7, 1),
        oos_end=date(2023, 7, 1),
        holdout_start=date(2023, 7, 1),
        holdout_end=date(2023, 12, 31),
        created_at=datetime.now(timezone.utc),
    )
    session.add(good)
    session.commit()
    assert good.id is not None


# ── strategy_trials: window / run_type / status CHECKs ──────────────────────────


def test_strategy_trials_rejects_invalid_window(session: Session) -> None:
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    bad = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="not_a_real_window",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_strategy_trials_rejects_invalid_run_type(session: Session) -> None:
    session.rollback()
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    bad = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="train_oos",
        run_type="not_a_real_run_type",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_strategy_trials_rejects_fk_to_nonexistent_definition(session: Session) -> None:
    """Proves the strategy_definitions(strategy_id, config_hash) composite
    FK is real, not just declared -- inserting a trial against a definition
    that was never created must fail under PRAGMA foreign_keys=ON."""
    hyp = _make_hypothesis(session)
    bad = StrategyTrial(
        strategy_id="does_not_exist",
        config_hash="b" * 64,
        hypothesis_id=hyp.id,
        window="train_oos",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


# ── strategy_trials: one-shot holdout-confirmation partial unique index ────────


def test_one_completed_holdout_confirmation_trial_is_allowed(session: Session) -> None:
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    trial = _make_trial(
        session,
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        run_type="holdout_confirmation",
        status="completed",
        window="holdout",
    )
    assert trial.id is not None


def test_second_completed_holdout_confirmation_trial_is_rejected(session: Session) -> None:
    """The partial unique index uix_strategy_trials_one_holdout_confirmation
    must reject a SECOND completed holdout_confirmation trial for the same
    strategy_id -- the one-shot seal enforced at the DB level (§4.2), not
    just in the future TrialRecorder application code."""
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    _make_trial(
        session,
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        run_type="holdout_confirmation",
        status="completed",
        window="holdout",
    )
    second = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="holdout",
        run_type="holdout_confirmation",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(second)
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("first_status", ["running", "errored", "completed"])
def test_second_holdout_confirmation_trial_rejected_regardless_of_first_status(
    session: Session, first_status: str
) -> None:
    """The one-shot seal must trip on ANY prior holdout_confirmation attempt,
    not only a completed one (§4.2: "no prior holdout_confirmation trial row
    exists"). Because TrialRecorder inserts the row before dispatch and the
    run reads the sealed holdout data during dispatch, a first attempt that is
    still 'running' or that 'errored' has already consumed its single
    permitted look at the holdout -- so a second attempt of any status must be
    rejected. Regression guard against the earlier completed-only predicate
    that left the failure/retry path able to re-read the sealed data."""
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    _make_trial(
        session,
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        run_type="holdout_confirmation",
        status=first_status,
        window="holdout",
    )
    second = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="holdout",
        run_type="holdout_confirmation",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(second)
    with pytest.raises(IntegrityError):
        session.commit()


def test_multiple_walk_forward_trials_are_unrestricted(session: Session) -> None:
    """Sanity check that the partial index is scoped to
    run_type='holdout_confirmation' only -- ordinary walk_forward trials
    (the common case) are never limited to one per strategy."""
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    for _ in range(3):
        _make_trial(
            session,
            strategy_id=defn.strategy_id,
            config_hash=defn.config_hash,
            hypothesis_id=hyp.id,
            run_type="walk_forward",
            status="completed",
            window="train_oos",
        )
    # No exception means unrestricted, as intended.


@pytest.mark.parametrize(
    "window,run_type",
    [
        ("holdout", "walk_forward"),            # holdout window, non-confirmation type
        ("holdout", "parameter_sweep_variant"),
        ("train_oos", "holdout_confirmation"),  # confirmation type, non-holdout window
    ],
)
def test_window_and_run_type_must_agree_on_holdout(
    session: Session, window: str, run_type: str
) -> None:
    """ck_strategy_trials_holdout_window_iff_confirmation: a row touches the
    holdout window IFF it is a holdout_confirmation run. This closes the hole
    where a holdout-window row labeled as a normal run_type would escape the
    run_type-keyed one-shot seal and allow unlimited looks at the sealed
    holdout data (Codex round-2 P1)."""
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    bad = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window=window,
        run_type=run_type,
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_holdout_seal_is_scoped_per_strategy_id_not_global(session: Session) -> None:
    """The one-shot seal is per-strategy_id, NOT global: two DISTINCT
    strategies must EACH be able to record their own completed
    holdout_confirmation. Mirrors
    test_second_completed_holdout_confirmation_trial_is_rejected but across
    two different (strategy_id, config_hash) definitions -- proving the
    partial unique index keys on strategy_id and does not accidentally cap
    the whole table at one holdout confirmation."""
    defn_a = _make_definition(session, strategy_id="v1_alpha_strategy")
    defn_b = _make_definition(session, strategy_id="v1_beta_strategy")
    hyp_a = _make_hypothesis(session, strategy_id="v1_alpha_strategy")
    hyp_b = _make_hypothesis(session, strategy_id="v1_beta_strategy")

    trial_a = _make_trial(
        session,
        strategy_id=defn_a.strategy_id,
        config_hash=defn_a.config_hash,
        hypothesis_id=hyp_a.id,
        run_type="holdout_confirmation",
        status="completed",
        window="holdout",
    )
    trial_b = _make_trial(
        session,
        strategy_id=defn_b.strategy_id,
        config_hash=defn_b.config_hash,
        hypothesis_id=hyp_b.id,
        run_type="holdout_confirmation",
        status="completed",
        window="holdout",
    )
    assert trial_a.id is not None
    assert trial_b.id is not None
    assert trial_a.strategy_id != trial_b.strategy_id


def test_trial_rejects_hypothesis_belonging_to_a_different_strategy(session: Session) -> None:
    """The composite FK (hypothesis_id, strategy_id) -> strategy_hypotheses
    (id, strategy_id) must reject a trial for strategy A that references a
    hypothesis pre-registered for strategy B -- otherwise the trial counts
    under A while its preregistration evidence describes B, corrupting the
    audit trail this schema exists to protect."""
    defn_a = _make_definition(session, strategy_id="v1_alpha_strategy")
    _make_definition(session, strategy_id="v1_beta_strategy")
    hyp_b = _make_hypothesis(session, strategy_id="v1_beta_strategy")

    mismatched = StrategyTrial(
        strategy_id=defn_a.strategy_id,          # strategy A ...
        config_hash=defn_a.config_hash,
        hypothesis_id=hyp_b.id,                  # ... citing strategy B's hypothesis
        window="train_oos",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(mismatched)
    with pytest.raises(IntegrityError):
        session.commit()


def test_trial_accepts_hypothesis_of_the_same_strategy(session: Session) -> None:
    """The composite hypothesis FK still accepts the normal case: a trial whose
    strategy_id matches its referenced hypothesis's strategy_id."""
    defn = _make_definition(session, strategy_id="v1_alpha_strategy")
    hyp = _make_hypothesis(session, strategy_id="v1_alpha_strategy")
    trial = _make_trial(
        session,
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
    )
    assert trial.id is not None


def test_trial_allows_null_hypothesis_id_legacy_backfill_path(session: Session) -> None:
    """A NULL hypothesis_id must skip composite-FK enforcement (MATCH SIMPLE),
    preserving the documented legacy-backfill path for pre-protocol trials that
    were never linked to a pre-registered hypothesis."""
    defn = _make_definition(session)
    trial = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=None,
        window="train_oos",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(trial)
    session.commit()
    assert trial.id is not None


# ── promotion_decisions: FK + informational-DSR shape (§8 Q3) ──────────────────


def test_promotion_decisions_requires_existing_definition(session: Session) -> None:
    bad = PromotionDecision(
        strategy_id="does_not_exist",
        config_hash="c" * 64,
        n_trials_used=5,
        funnel_passed=True,
        overall_passed=True,
        evidence_json={},
        created_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_promotion_decisions_dsr_value_is_not_gated(session: Session) -> None:
    """Q3 (resolved 2026-07-22): dsr_value is informational only. A row with
    a low/negative dsr_value and overall_passed=True must be permitted --
    proving no DB-level DSR floor exists on this table."""
    defn = _make_definition(session)
    decision = PromotionDecision(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        n_trials_used=12,
        dsr_value=-1.5,
        funnel_passed=True,
        sensitivity_verdict="robust",
        stress_verdict="solid",
        overall_passed=True,
        evidence_json={"gates": {"funnel": "PASS"}},
        created_at=datetime.now(timezone.utc),
    )
    session.add(decision)
    session.commit()
    assert decision.id is not None


def test_promotion_decisions_rejects_invalid_sensitivity_verdict(session: Session) -> None:
    defn = _make_definition(session)
    bad = PromotionDecision(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        n_trials_used=1,
        funnel_passed=True,
        sensitivity_verdict="not_a_real_verdict",
        overall_passed=False,
        evidence_json={},
        created_at=datetime.now(timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


# ── NaN backstop on numeric columns (Postgres CHECK; SQLite coercion) ──────────
#
# Design doc §5.1 originally (and wrongly) claimed Postgres numeric has no NaN.
# It does: `'NaN'::numeric` inserts and persists on Postgres. Migration 014
# now carries a Postgres-only CHECK (`col IS NULL OR col <> 'NaN'::numeric`)
# rejecting NaN while allowing NULL. This is a deliberate dialect divergence:
# the `::numeric` cast is a SQLite syntax error, so the CHECK is
# `ddl_if(postgresql)` and is NOT built on SQLite -- but SQLite coerces
# float('nan') to NULL at storage time (verified), so NaN still cannot persist
# on the SQLite test path. The tests below assert the correct behavior on
# whichever dialect the test session is bound to (SQLite in CI; Postgres if a
# scratch-DB engine is ever wired in), so the same test is a genuine proof on
# both. The Postgres-side rejection is additionally proven out-of-band against
# a scratch DB during the migration upgrade/downgrade check.


def _assert_nan_is_not_persisted(session: Session, obj, attr: str) -> None:
    """Dialect-aware NaN backstop assertion.

    Postgres: the CHECK rejects the insert (IntegrityError). SQLite: the
    engine coerces NaN to NULL, so the insert succeeds but the stored value
    is None -- either way, a NaN value never survives in the column.
    """
    dialect = session.bind.dialect.name
    session.add(obj)
    if dialect == "postgresql":
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    else:
        session.commit()
        session.refresh(obj)
        assert getattr(obj, attr) is None, (
            f"{attr} should be coerced to NULL on {dialect}, not persist NaN"
        )


def test_nan_oos_sharpe_is_not_persisted(session: Session) -> None:
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    trial = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="train_oos",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        oos_sharpe=float("nan"),
        started_at=datetime.now(timezone.utc),
    )
    _assert_nan_is_not_persisted(session, trial, "oos_sharpe")


def test_nan_oos_max_drawdown_is_not_persisted(session: Session) -> None:
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    trial = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="train_oos",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        oos_max_drawdown=float("nan"),
        started_at=datetime.now(timezone.utc),
    )
    _assert_nan_is_not_persisted(session, trial, "oos_max_drawdown")


def test_nan_dsr_value_is_not_persisted(session: Session) -> None:
    defn = _make_definition(session)
    decision = PromotionDecision(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        n_trials_used=3,
        dsr_value=float("nan"),
        funnel_passed=True,
        overall_passed=False,
        evidence_json={},
        created_at=datetime.now(timezone.utc),
    )
    _assert_nan_is_not_persisted(session, decision, "dsr_value")


def test_finite_and_null_numerics_are_accepted(session: Session) -> None:
    """The NaN backstop must NOT reject legitimate finite values (including
    0.0, which a naive CAST-based NaN test would wrongly trip) or NULL."""
    defn = _make_definition(session)
    hyp = _make_hypothesis(session)
    good = StrategyTrial(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        window="train_oos",
        run_type="walk_forward",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        status="completed",
        oos_sharpe=0.0,          # boundary value that must survive
        oos_max_drawdown=-0.2,
        started_at=datetime.now(timezone.utc),
    )
    session.add(good)
    session.commit()
    session.refresh(good)
    assert float(good.oos_sharpe) == 0.0
    assert float(good.oos_max_drawdown) == -0.2

    # NULL still allowed (the "not computed yet" case).
    running = _make_trial(
        session,
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        hypothesis_id=hyp.id,
        run_type="walk_forward",
        status="running",
        window="train_oos",
    )
    assert running.oos_sharpe is None
