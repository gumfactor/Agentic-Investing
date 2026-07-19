"""Tests for the daily signal DAG's SQLAlchemy-1.4-safe PIT filter.

Codex PR #34 P1: the Airflow runtime image pins SQLAlchemy 1.4.51, while
data.universe.models uses SQLAlchemy 2-only APIs. The DAG's membership
filter therefore must not import data.universe.runtime/models, and its
plain-SQL eligibility predicate must stay in semantic lockstep with
data.universe.runtime.PITUniverseLookup.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

_REPO_ROOT = Path(__file__).parent.parent
_DAG_PATH = _REPO_ROOT / "airflow" / "dags" / "daily_signal_pipeline.py"


@pytest.fixture(scope="module")
def dag_module():
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location("daily_signal_pipeline_test", _DAG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def universe_db(tmp_path_factory):
    """SQLite DB with a published fixture universe import."""
    from data.universe.import_pipeline import run_import
    from data.universe.providers.fixture_provider import (
        FIXTURE_COVERAGE_START,
        FixtureSP500Provider,
    )

    tmp = tmp_path_factory.mktemp("dag_pit_db")
    db_path = tmp / "u.db"
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp / "artifacts",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    return f"sqlite:///{db_path}"


class TestImportIsolation:
    def test_dag_source_never_imports_sqlalchemy2_universe_modules(self) -> None:
        # The packaged Airflow environment (SQLAlchemy 1.4.51 per
        # infra/docker/Dockerfile.airflow) cannot import
        # data.universe.models/runtime; the DAG must not import them
        # anywhere (module level or inside task functions). Docstring
        # references are allowed, so inspect actual import statements.
        import ast

        tree = ast.parse(_DAG_PATH.read_text(encoding="utf-8"))
        # BUG-009 section 4 follow-up (adversarial review, same-session):
        # data.research.models/identity are just as SQLAlchemy-2-only as
        # data.universe.models/runtime (DeclarativeBase/Mapped/mapped_column)
        # — an earlier draft of the research-run lookup imported
        # data.research.identity directly and would have raised ImportError
        # the moment _write_scores actually ran inside the packaged Airflow
        # image, despite passing every test in this repo's SQLAlchemy-2.x
        # dev environment. Banned here so a regression fails loudly in CI-
        # equivalent tests instead of silently in production.
        banned = (
            "data.universe.runtime",
            "data.universe.models",
            "data.universe.import_pipeline",
            "data.research.identity",
            "data.research.models",
        )
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.startswith(banned)]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(banned) or node.module in ("data.universe", "data.research"):
                    offenders.append(node.module)
        assert offenders == [], f"DAG imports SQLAlchemy-2-only modules: {offenders}"

    def test_dag_module_imports_cleanly(self, dag_module) -> None:
        assert hasattr(dag_module, "_pit_eligible_tickers_sql")
        assert hasattr(dag_module, "_pit_membership_filter")
        assert hasattr(dag_module, "_get_active_research_run_id_sql")


class TestSemanticParityWithRuntime:
    """The plain-SQL predicate must produce exactly the runtime's universe.

    The fixture DAG helper is hardcoded to universe_id='sp500'; the fixture
    import publishes under 'sp500_fixture', so parity is checked by pointing
    the SQL at the fixture rows directly.
    """

    def _sql_eligible(self, dag_module, db_url: str, as_of: date) -> set | None:
        # Re-point the hardcoded 'sp500' at the fixture universe by
        # temporarily rewriting the fixture rows' universe_id.
        from sqlalchemy import text

        eng = create_engine(db_url, future=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE universe_import_batches SET universe_id='sp500'"))
            conn.execute(text("UPDATE universe_membership SET universe_id='sp500'"))
        try:
            return dag_module._pit_eligible_tickers_sql(db_url, as_of)
        finally:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE universe_import_batches SET universe_id='sp500_fixture'")
                )
                conn.execute(
                    text("UPDATE universe_membership SET universe_id='sp500_fixture'")
                )
            eng.dispose()

    @pytest.mark.parametrize(
        "as_of",
        [
            date(2020, 3, 2),  # DDD first stint active
            date(2020, 7, 1),  # BBB active, DDD gone
            date(2021, 1, 1),  # BBB removal effective but not yet knowable
            date(2021, 1, 4),  # BBB removal knowable
            date(2021, 6, 1),  # CCC effective but not yet knowable
            date(2021, 6, 3),  # CCC knowable
            date(2022, 6, 1),  # steady state
        ],
    )
    def test_sql_predicate_matches_runtime(self, dag_module, universe_db, as_of) -> None:
        from data.universe.runtime import PITUniverseLookup

        runtime = PITUniverseLookup(universe_db, "sp500_fixture")
        expected = set(runtime.load_universe_as_of(as_of).eligible_tickers)
        actual = self._sql_eligible(dag_module, universe_db, as_of)
        assert actual == expected

    def test_returns_none_outside_coverage(self, dag_module, universe_db) -> None:
        assert self._sql_eligible(dag_module, universe_db, date(2030, 1, 1)) is None

    def test_returns_none_without_published_import(self, dag_module, tmp_path) -> None:
        from data.universe.models import Base

        db_url = f"sqlite:///{tmp_path / 'empty.db'}"
        Base.metadata.create_all(create_engine(db_url, future=True))
        assert dag_module._pit_eligible_tickers_sql(db_url, date(2022, 6, 1)) is None


class TestActiveResearchRunLookup:
    """_get_active_research_run_id_sql must stay in semantic lockstep with
    data.research.identity.get_active_research_run (BUG-009 section 4 /
    adversarial-review follow-up): same answer, same fail-closed behavior,
    without the DAG importing the SQLAlchemy-2-only ORM to get it."""

    @pytest.fixture
    def research_db(self, tmp_path):
        from data.research.models import Base

        db_url = f"sqlite:///{tmp_path / 'research.db'}"
        Base.metadata.create_all(create_engine(db_url, future=True))
        return db_url

    def _register_and_activate(self, db_url: str, methodology_name: str, activate: bool = True):
        from sqlalchemy import create_engine as _create_engine
        from sqlalchemy.orm import Session

        from data.research.identity import (
            MethodologySpec,
            activate_run,
            register_methodology,
            register_run,
        )

        engine = _create_engine(db_url, future=True)
        with Session(engine) as session:
            methodology = register_methodology(
                session,
                MethodologySpec(
                    name=methodology_name,
                    universe_import_policy="test",
                    timing_policy_id="t_plus_1_close_v1",
                    score_action_availability_policy="score_cutoff_known_at_v1",
                    realized_return_action_availability_policy="exit_cutoff_known_at_v1",
                    action_source_version="test",
                    return_adjustment_policy="total_return_adjusted_v1",
                    missing_data_policy="pct_change_fill_none_v1",
                    code_config_hash="test",
                ),
            )
            run = register_run(session, methodology.id, data_version="2026-07-18")
            session.commit()
            if activate:
                activate_run(session, run.id, activated_by="test")
                session.commit()
            return run.id

    def test_matches_orm_lookup_when_active(self, dag_module, research_db) -> None:
        from sqlalchemy import create_engine as _create_engine
        from sqlalchemy.orm import Session

        from data.research.identity import get_active_research_run

        run_id = self._register_and_activate(research_db, "dag_test_methodology")

        sql_result = dag_module._get_active_research_run_id_sql(research_db, "dag_test_methodology")
        assert sql_result == run_id

        engine = _create_engine(research_db, future=True)
        with Session(engine) as session:
            orm_result = get_active_research_run(session, "dag_test_methodology")
        assert sql_result == orm_result.id

    def test_raises_when_no_active_run(self, dag_module, research_db) -> None:
        self._register_and_activate(research_db, "dag_test_methodology_inactive", activate=False)
        with pytest.raises(RuntimeError, match="No active research run"):
            dag_module._get_active_research_run_id_sql(research_db, "dag_test_methodology_inactive")

    def test_raises_when_methodology_unknown(self, dag_module, research_db) -> None:
        with pytest.raises(RuntimeError, match="No active research run"):
            dag_module._get_active_research_run_id_sql(research_db, "does_not_exist")


class TestFilterDegradation:
    def test_filter_degrades_to_provisional_on_lookup_failure(
        self, dag_module, monkeypatch
    ) -> None:
        # Point DATABASE_URL at a DB with no tables at all: the filter must
        # return the frame unchanged (provisional), never raise.
        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
        df = pd.DataFrame({"ticker": ["AAA", "XXX"], "score": [1.0, 2.0]})
        out = dag_module._pit_membership_filter(df, date(2022, 6, 1))
        pd.testing.assert_frame_equal(out, df)

    def test_filter_applies_membership_when_available(
        self, dag_module, universe_db, monkeypatch
    ) -> None:
        from sqlalchemy import text

        eng = create_engine(universe_db, future=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE universe_import_batches SET universe_id='sp500'"))
            conn.execute(text("UPDATE universe_membership SET universe_id='sp500'"))
        try:
            monkeypatch.setenv("DATABASE_URL", universe_db)
            df = pd.DataFrame({"ticker": ["AAA", "NOPE"], "score": [1.0, 2.0]})
            out = dag_module._pit_membership_filter(df, date(2022, 6, 1))
            assert out["ticker"].tolist() == ["AAA"]
        finally:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE universe_import_batches SET universe_id='sp500_fixture'")
                )
                conn.execute(
                    text("UPDATE universe_membership SET universe_id='sp500_fixture'")
                )
            eng.dispose()


class TestLoadPricesPreFilter:
    """Codex PR #34 round-5 P2: the PIT eligible set must define the factor
    cross-section BEFORE scoring — _load_prices filters the price panel by
    ticker (keeping full lookback history) before any factor task runs."""

    class _FakeTI:
        def __init__(self):
            self.pushed = {}

        def xcom_push(self, key, value):
            self.pushed[key] = value

    class _FakeInterval:
        def __init__(self, d):
            self._d = d

        def in_timezone(self, _tz):
            return self

        def date(self):
            return self._d

    def _run_load_prices(self, dag_module, monkeypatch, db_url, score_date, tickers):
        import pandas as pd

        frame = pd.DataFrame(
            [
                {"ticker": t, "date": score_date, "close": 100.0}
                for t in tickers
            ]
        )
        # BUG-009 section 2.3: _load_prices now also queries corporate_actions
        # for the cutoff-aware adjustment. A single query-agnostic fake would
        # answer that second query with the price frame (missing ex_date/
        # known_at) and crash — distinguish by inspecting the SQL text.
        empty_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )

        def _fake_read_sql(query, *a, **k):
            if "corporate_actions" in str(query):
                return empty_actions.copy()
            return frame.copy()

        monkeypatch.setenv("DATABASE_URL", db_url)
        monkeypatch.setattr(pd, "read_sql", _fake_read_sql)
        ti = self._FakeTI()
        dag_module._load_prices(
            ti=ti, data_interval_end=self._FakeInterval(score_date)
        )
        pushed = pd.read_json(
            ti.pushed["prices_json"], orient="records", convert_dates=False
        )
        return set(pushed["ticker"]) if not pushed.empty else set()

    def test_ineligible_tickers_removed_before_scoring(
        self, dag_module, universe_db, monkeypatch
    ) -> None:
        from sqlalchemy import create_engine, text

        eng = create_engine(universe_db, future=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE universe_import_batches SET universe_id='sp500'"))
            conn.execute(text("UPDATE universe_membership SET universe_id='sp500'"))
        try:
            tickers = self._run_load_prices(
                dag_module, monkeypatch, universe_db, date(2022, 6, 1), ["AAA", "NOPE"]
            )
            assert "AAA" in tickers
            assert "NOPE" not in tickers
        finally:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE universe_import_batches SET universe_id='sp500_fixture'")
                )
                conn.execute(
                    text("UPDATE universe_membership SET universe_id='sp500_fixture'")
                )
            eng.dispose()

    def test_degrades_to_unfiltered_panel_without_universe(
        self, dag_module, monkeypatch
    ) -> None:
        tickers = self._run_load_prices(
            dag_module, monkeypatch, "sqlite:///:memory:", date(2022, 6, 1), ["AAA", "NOPE"]
        )
        assert tickers == {"AAA", "NOPE"}

    def test_pit_universe_applied_xcom_true_when_universe_available(
        self, dag_module, universe_db, monkeypatch
    ) -> None:
        """Adversarial-review round 11 (BUG-008/BUG-009 section 4):
        _write_scores needs to know whether PIT filtering actually
        succeeded for this score_date."""
        from sqlalchemy import create_engine, text

        eng = create_engine(universe_db, future=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE universe_import_batches SET universe_id='sp500'"))
            conn.execute(text("UPDATE universe_membership SET universe_id='sp500'"))
        try:
            import pandas as pd

            frame = pd.DataFrame([{"ticker": "AAA", "date": date(2022, 6, 1), "close": 100.0}])
            empty_actions = pd.DataFrame(
                columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
            )

            def _fake_read_sql(query, *a, **k):
                return empty_actions.copy() if "corporate_actions" in str(query) else frame.copy()

            monkeypatch.setenv("DATABASE_URL", universe_db)
            monkeypatch.setattr(pd, "read_sql", _fake_read_sql)
            ti = self._FakeTI()
            dag_module._load_prices(ti=ti, data_interval_end=self._FakeInterval(date(2022, 6, 1)))

            assert ti.pushed["pit_universe_applied"] is True
        finally:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE universe_import_batches SET universe_id='sp500_fixture'")
                )
                conn.execute(
                    text("UPDATE universe_membership SET universe_id='sp500_fixture'")
                )
            eng.dispose()

    def test_pit_universe_applied_xcom_false_when_universe_unavailable(
        self, dag_module, monkeypatch
    ) -> None:
        import pandas as pd

        frame = pd.DataFrame([{"ticker": "AAA", "date": date(2022, 6, 1), "close": 100.0}])
        empty_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )

        def _fake_read_sql(query, *a, **k):
            return empty_actions.copy() if "corporate_actions" in str(query) else frame.copy()

        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(pd, "read_sql", _fake_read_sql)
        ti = self._FakeTI()
        dag_module._load_prices(ti=ti, data_interval_end=self._FakeInterval(date(2022, 6, 1)))

        assert ti.pushed["pit_universe_applied"] is False


class TestWriteScoresMethodologyHonesty:
    """Adversarial-review round 11 (BUG-008/BUG-009 section 4): _write_scores
    must not tag a degraded (non-PIT-filtered) write with a research_run_id
    whose methodology claims PIT-universe safety -- routed through the
    single shared data.research.sql_compat.assert_methodology_write_is_honest
    gate, called BEFORE any row is written."""

    class _FakeTI:
        def __init__(self, xcom: dict):
            self._xcom = xcom

        def xcom_pull(self, key, task_ids=None):
            return self._xcom.get((task_ids, key))

    def _research_db(self, tmp_path, monkeypatch, *, universe_import_policy, name):
        from sqlalchemy import create_engine as _create_engine
        from sqlalchemy.orm import Session

        from data.research.identity import (
            MethodologySpec,
            activate_run,
            register_methodology,
            register_run,
        )
        from data.research.models import Base

        db_path = tmp_path / f"{name}.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("DATABASE_URL", db_url)
        engine = _create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            methodology = register_methodology(
                session,
                MethodologySpec(
                    name=name,
                    universe_import_policy=universe_import_policy,
                    timing_policy_id="t_plus_1_close_v1",
                    score_action_availability_policy="score_cutoff_known_at_v1",
                    realized_return_action_availability_policy="exit_cutoff_known_at_v1",
                    action_source_version="unknown",
                    return_adjustment_policy="total_return_adjusted_v1",
                    missing_data_policy="pct_change_fill_none_v1",
                    code_config_hash="test-hash",
                ),
            )
            session.commit()
            run = register_run(session, methodology.id, data_version="2026-01-01")
            session.commit()
            activate_run(session, run.id, activated_by="test")
            session.commit()
        return db_url

    def test_raises_before_any_write_when_pit_degraded_under_pit_claiming_run(
        self, dag_module, tmp_path, monkeypatch
    ) -> None:
        import pandas as pd

        # dag_module._OPERATIONAL_METHODOLOGY_NAME is the fixed methodology
        # name _write_scores always resolves its active run against.
        self._research_db(
            tmp_path,
            monkeypatch,
            universe_import_policy="pit_universe_effective_dated_v1",
            name=dag_module._OPERATIONAL_METHODOLOGY_NAME,
        )
        # Deliberately no factor_scores/alpha_scores tables in this DB --
        # proves the honesty gate raises BEFORE any INSERT is even attempted
        # (an INSERT against a missing table would raise a different,
        # SQLAlchemy-generic error, not MethodologyHonestyError).
        factor_json = pd.DataFrame(
            [{"ticker": "AAA", "score_date": date(2026, 6, 1), "factor_name": "momentum",
              "strategy_id": "v1", "z_score": 1.0, "raw_value": 0.1}]
        ).to_json(orient="records", date_format="iso")

        xcom = {
            ("combine_scores", "factor_scores_json"): factor_json,
            ("combine_scores", "alpha_scores_json"): "[]",
            ("load_prices", "pit_universe_applied"): False,
            ("compute_quality", "quality_pit_universe_applied"): True,
        }
        ti = self._FakeTI(xcom)

        from data.research.sql_compat import MethodologyHonestyError

        with pytest.raises(MethodologyHonestyError, match="misrepresent"):
            dag_module._write_scores(ti=ti)

    def test_does_not_raise_honesty_error_when_pit_applied(
        self, dag_module, tmp_path, monkeypatch
    ) -> None:
        """Sanity: a fully-PIT-applied write must clear the honesty gate
        (any later failure must come from the actual INSERT, e.g. a missing
        table -- proving the gate itself did not block an honest write)."""
        import pandas as pd
        from sqlalchemy.exc import SQLAlchemyError

        self._research_db(
            tmp_path,
            monkeypatch,
            universe_import_policy="pit_universe_effective_dated_v1",
            name=dag_module._OPERATIONAL_METHODOLOGY_NAME,
        )
        factor_json = pd.DataFrame(
            [{"ticker": "AAA", "score_date": date(2026, 6, 1), "factor_name": "momentum",
              "strategy_id": "v1", "z_score": 1.0, "raw_value": 0.1}]
        ).to_json(orient="records", date_format="iso")

        xcom = {
            ("combine_scores", "factor_scores_json"): factor_json,
            ("combine_scores", "alpha_scores_json"): "[]",
            ("load_prices", "pit_universe_applied"): True,
            ("compute_quality", "quality_pit_universe_applied"): True,
        }
        ti = self._FakeTI(xcom)

        from data.research.sql_compat import MethodologyHonestyError

        # No factor_scores table exists in this DB, so the INSERT itself
        # must fail -- but with a generic SQLAlchemy error, never
        # MethodologyHonestyError, proving the gate cleared this write.
        with pytest.raises(SQLAlchemyError):
            dag_module._write_scores(ti=ti)

    def test_missing_pit_flag_xcom_treated_as_not_applied(
        self, dag_module, tmp_path, monkeypatch
    ) -> None:
        """Missing XCom (e.g. an older DAG run without this key) must fail
        safe -- treated as PIT NOT applied, never assumed successful."""
        import pandas as pd

        self._research_db(
            tmp_path,
            monkeypatch,
            universe_import_policy="pit_universe_effective_dated_v1",
            name=dag_module._OPERATIONAL_METHODOLOGY_NAME,
        )
        factor_json = pd.DataFrame(
            [{"ticker": "AAA", "score_date": date(2026, 6, 1), "factor_name": "momentum",
              "strategy_id": "v1", "z_score": 1.0, "raw_value": 0.1}]
        ).to_json(orient="records", date_format="iso")

        xcom = {
            ("combine_scores", "factor_scores_json"): factor_json,
            ("combine_scores", "alpha_scores_json"): "[]",
            # pit_universe_applied deliberately absent from XCom.
        }
        ti = self._FakeTI(xcom)

        from data.research.sql_compat import MethodologyHonestyError

        with pytest.raises(MethodologyHonestyError, match="misrepresent"):
            dag_module._write_scores(ti=ti)


class TestCorporateActionWiring:
    """BUG-009 section 2.3 adversarial-review follow-up: momentum/lowvol
    must receive split/dividend-adjusted prices, not the raw daily_prices
    values value/quality use."""

    def test_momentum_and_lowvol_read_adjusted_prices_key(self, dag_module) -> None:
        """Source-level guard: the price-ratio factor tasks must pull
        adjusted_prices_json, not prices_json, from load_prices' XCom."""
        import inspect

        momentum_source = inspect.getsource(dag_module._compute_momentum)
        lowvol_source = inspect.getsource(dag_module._compute_lowvol)
        value_source = inspect.getsource(dag_module._compute_value)
        quality_source = inspect.getsource(dag_module._compute_quality)

        assert 'xcom_pull(key="adjusted_prices_json"' in momentum_source
        assert 'xcom_pull(key="adjusted_prices_json"' in lowvol_source
        # Value/quality deliberately stay on raw prices (valuation ratios
        # need the actual traded price, not a total-return-adjusted one).
        assert 'xcom_pull(key="prices_json"' in value_source
        assert 'xcom_pull(key="prices_json"' in quality_source
        assert 'xcom_pull(key="adjusted_prices_json"' not in value_source
        assert 'xcom_pull(key="adjusted_prices_json"' not in quality_source

    def test_load_prices_pushes_split_adjusted_series(self, dag_module, monkeypatch) -> None:
        import pandas as pd
        from datetime import datetime, timezone

        score_date = date(2022, 6, 3)
        pre_split_date = date(2022, 6, 1)
        raw_frame = pd.DataFrame(
            [
                {"ticker": "AAA", "date": pre_split_date, "close": 200.0},
                {"ticker": "AAA", "date": date(2022, 6, 2), "close": 100.0},
                {"ticker": "AAA", "date": score_date, "close": 101.0},
            ]
        )
        actions_frame = pd.DataFrame(
            [
                {
                    "ticker": "AAA",
                    "ex_date": date(2022, 6, 2),
                    "action_type": "split",
                    "value": 2.0,
                    "known_at": datetime(2022, 6, 1, 21, 0, tzinfo=timezone.utc),
                    "source_version": "test",
                }
            ]
        )

        def _fake_read_sql(query, *a, **k):
            return actions_frame.copy() if "corporate_actions" in str(query) else raw_frame.copy()

        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(pd, "read_sql", _fake_read_sql)

        ti = TestLoadPricesPreFilter._FakeTI()
        dag_module._load_prices(
            ti=ti, data_interval_end=TestLoadPricesPreFilter._FakeInterval(score_date)
        )

        raw_pushed = pd.read_json(ti.pushed["prices_json"], orient="records", convert_dates=False)
        adj_pushed = pd.read_json(ti.pushed["adjusted_prices_json"], orient="records", convert_dates=False)
        raw_pushed["date"] = pd.to_datetime(raw_pushed["date"]).dt.date
        adj_pushed["date"] = pd.to_datetime(adj_pushed["date"]).dt.date

        raw_close = float(raw_pushed[raw_pushed["date"] == pre_split_date]["close"].iloc[0])
        adj_close = float(adj_pushed[adj_pushed["date"] == pre_split_date]["close"].iloc[0])

        assert abs(raw_close - 200.0) < 1e-6  # prices_json stays raw
        assert abs(adj_close - 100.0) < 1e-6  # adjusted_prices_json is split-adjusted


class TestSimulationCorporateActionAdjustment:
    """BUG-009 section 2.3 (adversarial-review round 10 self-audit sweep):
    _write_simulation's close-to-close return, computed by
    _adjusted_closes_for_simulation, must be corporate-action-adjusted, not
    raw -- a split/dividend landing on sim_date or prev_date would
    otherwise inject a fabricated return into the compounding
    simulated_nav chain, permanently distorting every later NAV row."""

    class _FakeEngine:
        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_split_on_sim_date_is_adjusted(self, dag_module, monkeypatch) -> None:
        import pandas as pd
        from datetime import datetime, timezone

        prev_date = date(2022, 6, 1)
        sim_date = date(2022, 6, 2)
        prices_df = pd.DataFrame(
            [
                {"ticker": "AAA", "date": prev_date, "close": 100.0},
                {"ticker": "AAA", "date": sim_date, "close": 51.0},  # ~2:1 split + genuine gain
            ]
        )
        actions_frame = pd.DataFrame(
            [
                {
                    "ticker": "AAA",
                    "ex_date": sim_date,
                    "action_type": "split",
                    "value": 2.0,
                    "known_at": datetime(2022, 6, 1, 21, 0, tzinfo=timezone.utc),
                    "source_version": "test",
                }
            ]
        )

        def _fake_read_sql(query, *a, **k):
            return actions_frame.copy()

        monkeypatch.setattr(pd, "read_sql", _fake_read_sql)

        raw_today = prices_df[prices_df["date"] == sim_date].set_index("ticker")["close"]
        raw_prev = prices_df[prices_df["date"] == prev_date].set_index("ticker")["close"]

        adj_today, adj_prev = dag_module._adjusted_closes_for_simulation(
            prices_df, self._FakeEngine(), sim_date, prev_date, raw_today, raw_prev
        )

        # prev_date is BEFORE the split's ex_date, so it gets back-adjusted
        # by the split factor (1/2): 100.0 -> 50.0. sim_date is ON the
        # ex_date, so it stays at its raw close, 51.0 (adj_factor=1 for
        # dates on/after the most recent action).
        assert abs(float(adj_prev["AAA"]) - 50.0) < 1e-6
        assert abs(float(adj_today["AAA"]) - 51.0) < 1e-6

        # The RAW (unadjusted) closes would have implied a fake ~49% LOSS;
        # the adjusted return is the genuine +2% gain.
        raw_return = float(raw_today["AAA"]) / float(raw_prev["AAA"]) - 1.0
        adj_return = float(adj_today["AAA"]) / float(adj_prev["AAA"]) - 1.0
        assert raw_return < -0.4
        assert abs(adj_return - 0.02) < 1e-6

    def test_no_corporate_actions_table_degrades_to_raw(self, dag_module, monkeypatch) -> None:
        import pandas as pd

        prev_date = date(2022, 6, 1)
        sim_date = date(2022, 6, 2)
        prices_df = pd.DataFrame(
            [
                {"ticker": "AAA", "date": prev_date, "close": 100.0},
                {"ticker": "AAA", "date": sim_date, "close": 102.0},
            ]
        )

        def _raising_read_sql(query, *a, **k):
            raise Exception("no such table: corporate_actions")

        monkeypatch.setattr(pd, "read_sql", _raising_read_sql)

        raw_today = prices_df[prices_df["date"] == sim_date].set_index("ticker")["close"]
        raw_prev = prices_df[prices_df["date"] == prev_date].set_index("ticker")["close"]

        adj_today, adj_prev = dag_module._adjusted_closes_for_simulation(
            prices_df, self._FakeEngine(), sim_date, prev_date, raw_today, raw_prev
        )

        # Must not raise; degrades to the RAW closes passed in (non-blocking
        # "log and skip" philosophy, matching _write_simulation elsewhere).
        assert adj_today.equals(raw_today)
        assert adj_prev.equals(raw_prev)

    def test_no_actions_returns_raw_unchanged(self, dag_module, monkeypatch) -> None:
        import pandas as pd

        prev_date = date(2022, 6, 1)
        sim_date = date(2022, 6, 2)
        prices_df = pd.DataFrame(
            [
                {"ticker": "AAA", "date": prev_date, "close": 100.0},
                {"ticker": "AAA", "date": sim_date, "close": 102.0},
            ]
        )
        empty_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )

        def _fake_read_sql(query, *a, **k):
            return empty_actions.copy()

        monkeypatch.setattr(pd, "read_sql", _fake_read_sql)

        raw_today = prices_df[prices_df["date"] == sim_date].set_index("ticker")["close"]
        raw_prev = prices_df[prices_df["date"] == prev_date].set_index("ticker")["close"]

        adj_today, adj_prev = dag_module._adjusted_closes_for_simulation(
            prices_df, self._FakeEngine(), sim_date, prev_date, raw_today, raw_prev
        )

        assert adj_today.equals(raw_today)
        assert adj_prev.equals(raw_prev)


class TestComputeQualityPITCrossSection:
    """Codex PR #34 final P2: quality ratios use only fundamentals, so a
    non-member with valid financial_statements rows bypassed the filtered
    price panel and contaminated per-date quality z-scores. The DAG task
    must pass the PIT eligibility frame into compute_quality_scores."""

    class _FakeTI:
        def __init__(self, pulls):
            self._pulls = pulls
            self.pushed = {}

        def xcom_pull(self, key, task_ids=None):
            return self._pulls[key]

        def xcom_push(self, key, value):
            self.pushed[key] = value

    def _fundamentals(self, tickers, whale_extreme=False):
        import numpy as np

        rng = np.random.default_rng(2)
        rows = []
        for t in tickers:
            for item in [
                "net_income", "total_equity", "total_assets",
                "gross_profit", "operating_cash_flow",
            ]:
                value = float(rng.uniform(1e9, 5e9))
                if whale_extreme and t == "WHALE":
                    value *= 1000
                rows.append(
                    {
                        "ticker": t,
                        "period_end_date": "2021-12-31",
                        "release_date": "2022-02-01",
                        "period_type": "annual",
                        "item_name": item,
                        "value": value,
                    }
                )
        return pd.DataFrame(rows)

    def _run_compute_quality(self, dag_module, monkeypatch, db_url, score_date, fund):
        from sqlalchemy import text

        eng = create_engine(db_url, future=True)
        with eng.begin() as conn:
            conn.execute(
                text("CREATE TABLE IF NOT EXISTS financial_statements (id INTEGER)")
            )
            conn.execute(text("DELETE FROM financial_statements"))
            conn.execute(text("INSERT INTO financial_statements (id) VALUES (1)"))
        eng.dispose()

        monkeypatch.setenv("DATABASE_URL", db_url)
        monkeypatch.setattr(pd, "read_sql", lambda *a, **k: fund.copy())

        members = ["AAA", "CCC", "DDD", "FFF", "GGG", "HHH", "III", "JJJ"]
        prices = pd.DataFrame(
            [{"ticker": t, "date": str(score_date), "close": 100.0} for t in members]
        )
        ti = self._FakeTI(
            {
                "score_date": str(score_date),
                "prices_json": prices.to_json(orient="records", date_format="iso"),
            }
        )
        dag_module._compute_quality(ti=ti)
        raw = ti.pushed["quality_scores_json"]
        if raw == "[]":
            return pd.DataFrame(columns=["ticker", "quality_score"])
        return pd.read_json(raw, orient="records", convert_dates=False)

    def test_non_member_fundamentals_cannot_shift_member_quality_scores(
        self, dag_module, universe_db, monkeypatch, tmp_path
    ) -> None:
        from sqlalchemy import text

        score_date = date(2022, 6, 1)
        members = ["AAA", "CCC", "DDD", "FFF", "GGG", "HHH", "III", "JJJ"]

        eng = create_engine(universe_db, future=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE universe_import_batches SET universe_id='sp500'"))
            conn.execute(text("UPDATE universe_membership SET universe_id='sp500'"))
        try:
            with_whale = self._run_compute_quality(
                dag_module, monkeypatch, universe_db, score_date,
                self._fundamentals(members + ["WHALE"], whale_extreme=True),
            )
            without_whale = self._run_compute_quality(
                dag_module, monkeypatch, universe_db, score_date,
                self._fundamentals(members),
            )
        finally:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE universe_import_batches SET universe_id='sp500_fixture'")
                )
                conn.execute(
                    text("UPDATE universe_membership SET universe_id='sp500_fixture'")
                )
            eng.dispose()

        assert not (with_whale["ticker"] == "WHALE").any()
        w = with_whale.sort_values("ticker").reset_index(drop=True)
        wo = without_whale.sort_values("ticker").reset_index(drop=True)
        pd.testing.assert_frame_equal(w, wo)

    def test_quality_pit_universe_applied_xcom_reflects_its_own_lookup(
        self, dag_module, universe_db, monkeypatch
    ) -> None:
        """Adversarial-review round 11 (BUG-008/BUG-009 section 4): quality
        does its own independent PIT lookup (unlike momentum/lowvol/value,
        which rely solely on _load_prices' panel filter), so _write_scores
        needs its degrade state surfaced separately."""
        from sqlalchemy import text

        score_date = date(2022, 6, 1)
        members = ["AAA", "CCC"]

        eng = create_engine(universe_db, future=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE universe_import_batches SET universe_id='sp500'"))
            conn.execute(text("UPDATE universe_membership SET universe_id='sp500'"))
        try:
            ti = self._run_compute_quality_ti(
                dag_module, monkeypatch, universe_db, score_date, self._fundamentals(members)
            )
            assert ti.pushed["quality_pit_universe_applied"] is True
        finally:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE universe_import_batches SET universe_id='sp500_fixture'")
                )
                conn.execute(
                    text("UPDATE universe_membership SET universe_id='sp500_fixture'")
                )
            eng.dispose()

    def test_quality_pit_universe_applied_xcom_false_when_lookup_unavailable(
        self, dag_module, monkeypatch, tmp_path
    ) -> None:
        # A real file-backed DB (not :memory:) so financial_statements
        # persists across the setup connection and _compute_quality's own
        # internal create_engine() call -- :memory: creates a fresh, empty
        # DB per connection. No research_methodologies/universe tables
        # exist in this DB at all, so the PIT lookup itself degrades.
        score_date = date(2022, 6, 1)
        members = ["AAA", "CCC"]
        db_url = f"sqlite:///{tmp_path / 'no_universe.db'}"
        ti = self._run_compute_quality_ti(
            dag_module, monkeypatch, db_url, score_date, self._fundamentals(members)
        )
        assert ti.pushed["quality_pit_universe_applied"] is False

    def _run_compute_quality_ti(self, dag_module, monkeypatch, db_url, score_date, fund):
        from sqlalchemy import text

        eng = create_engine(db_url, future=True)
        with eng.begin() as conn:
            conn.execute(
                text("CREATE TABLE IF NOT EXISTS financial_statements (id INTEGER)")
            )
            conn.execute(text("DELETE FROM financial_statements"))
            conn.execute(text("INSERT INTO financial_statements (id) VALUES (1)"))
        eng.dispose()

        monkeypatch.setenv("DATABASE_URL", db_url)
        monkeypatch.setattr(pd, "read_sql", lambda *a, **k: fund.copy())

        members = ["AAA", "CCC"]
        prices = pd.DataFrame(
            [{"ticker": t, "date": str(score_date), "close": 100.0} for t in members]
        )
        ti = self._FakeTI(
            {
                "score_date": str(score_date),
                "prices_json": prices.to_json(orient="records", date_format="iso"),
            }
        )
        dag_module._compute_quality(ti=ti)
        return ti
