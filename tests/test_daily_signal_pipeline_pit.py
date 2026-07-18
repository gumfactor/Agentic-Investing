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
        banned = ("data.universe.runtime", "data.universe.models", "data.universe.import_pipeline")
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.startswith(banned)]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(banned) or node.module == "data.universe":
                    offenders.append(node.module)
        assert offenders == [], f"DAG imports SQLAlchemy-2-only universe modules: {offenders}"

    def test_dag_module_imports_cleanly(self, dag_module) -> None:
        assert hasattr(dag_module, "_pit_eligible_tickers_sql")
        assert hasattr(dag_module, "_pit_membership_filter")


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
        monkeypatch.setenv("DATABASE_URL", db_url)
        monkeypatch.setattr(pd, "read_sql", lambda *a, **k: frame)
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
