import io
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from minio.error import S3Error
from sqlalchemy import create_engine

from data.storage.parquet_snapshots import ParquetSnapshots
from data.universe.models import Base as UniverseBase
from data.research.models import Base as ResearchBase
from scripts.pin_snapshot import pin_bundle


def _create_universe_and_research_tables(engine) -> None:
    """Empty universe_import_batches/universe_eligibility_batches/
    research_methodologies tables so pin_bundle's best-effort batch lookups
    (03A-5) can query them without an OperationalError -- real deployments
    always have these tables via Alembic migrations; these sqlite fixtures
    build the DB manually with plain to_sql, so the ORM tables must be
    created explicitly. No rows are inserted unless a specific test needs
    to exercise a real batch id."""
    UniverseBase.metadata.create_all(engine)
    ResearchBase.metadata.create_all(engine)


def _engine_with_bundle_data():
    engine = create_engine("sqlite://")
    _create_universe_and_research_tables(engine)
    prices = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "date": [date(2022, 1, 3), date(2022, 1, 4)],
            "close": [100.0, 101.0],
        }
    )
    alpha = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "score_date": [date(2022, 1, 4)],
            "strategy_id": ["v1"],
            "alpha_score": [1.0],
        }
    )
    actions = pd.DataFrame(
        columns=["ticker", "ex_date", "action_type", "value"]
    )
    prices.to_sql("daily_prices", engine, index=False)
    alpha.to_sql("alpha_scores", engine, index=False)
    actions.to_sql("corporate_actions", engine, index=False)
    return engine


def _benchmark_client(start=date(2022, 1, 3), end=date(2022, 1, 4)):
    client = MagicMock()
    client.fetch_market_data.return_value = (
        pd.DataFrame(
            {
                "ticker": ["SPY", "SPY"],
                "date": [start, end],
                "close": [470.0, 471.0],
            }
        ),
        pd.DataFrame(),
    )
    return client


def test_pin_bundle_saves_all_sources_and_manifest():
    snapshots = MagicMock()
    snapshots.save_snapshot.side_effect = (
        lambda _df, data_type, snapshot_date, bytes_sha256_out=None: (
            f"rqis-snapshots/snapshots/{data_type}/{snapshot_date}/data.parquet"
        )
    )
    snapshots.save_dataset_manifest.return_value = (
        "rqis-snapshots/manifests/2022-01-05/manifest.json"
    )

    path = pin_bundle(
        "v1",
        "SPY",
        date(2022, 1, 5),
        engine=_engine_with_bundle_data(),
        snapshots=snapshots,
        market_client=_benchmark_client(),
    )

    assert path == "rqis-snapshots/manifests/2022-01-05/manifest.json"
    assert {
        call.kwargs["data_type"] for call in snapshots.save_snapshot.call_args_list
    } == {"daily_prices", "alpha_scores", "corporate_actions", "benchmark"}
    manifest = snapshots.save_dataset_manifest.call_args.args[0]
    assert manifest.strategy_id == "v1"
    assert manifest.row_counts["alpha_scores"] == 1
    assert manifest.row_counts["benchmark"] == 2


def test_pin_bundle_rejects_missing_strategy_scores():
    with pytest.raises(ValueError, match="No alpha_scores"):
        pin_bundle(
            "missing",
            "SPY",
            date(2022, 1, 5),
            engine=_engine_with_bundle_data(),
            snapshots=MagicMock(),
            market_client=_benchmark_client(),
        )


def test_pin_bundle_rejects_incomplete_benchmark_coverage():
    with pytest.raises(ValueError, match="does not cover alpha scores"):
        pin_bundle(
            "v1",
            "SPY",
            date(2022, 1, 5),
            engine=_engine_with_bundle_data(),
            snapshots=MagicMock(),
            market_client=_benchmark_client(
                start=date(2022, 1, 3),
                end=date(2022, 1, 3),
            ),
        )


# ─── research_run_id collision handling (BUG-009 section 4, adversarial round 3) ──


def _engine_with_colliding_runs():
    """Two research runs both scored AAPL on the same score_date -- pinning
    both would silently duplicate that (ticker, score_date) cross-section."""
    engine = create_engine("sqlite://")
    _create_universe_and_research_tables(engine)
    prices = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "date": [date(2022, 1, 3), date(2022, 1, 4)],
            "close": [100.0, 101.0],
        }
    )
    alpha = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "score_date": [date(2022, 1, 4), date(2022, 1, 4)],
            "strategy_id": ["v1", "v1"],
            "alpha_score": [1.0, 1.2],
            "research_run_id": [7, 8],
        }
    )
    actions = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    prices.to_sql("daily_prices", engine, index=False)
    alpha.to_sql("alpha_scores", engine, index=False)
    actions.to_sql("corporate_actions", engine, index=False)
    return engine


def test_pin_bundle_rejects_colliding_research_runs_without_explicit_selection():
    with pytest.raises(ValueError, match="spanning more than one research_run_id"):
        pin_bundle(
            "v1",
            "SPY",
            date(2022, 1, 5),
            engine=_engine_with_colliding_runs(),
            snapshots=MagicMock(),
            market_client=_benchmark_client(),
        )


def test_pin_bundle_research_run_id_disambiguates_collision():
    snapshots = MagicMock()
    snapshots.save_snapshot.side_effect = (
        lambda _df, data_type, snapshot_date, bytes_sha256_out=None: (
            f"rqis-snapshots/snapshots/{data_type}/{snapshot_date}/data.parquet"
        )
    )
    snapshots.save_dataset_manifest.return_value = (
        "rqis-snapshots/manifests/2022-01-05/manifest.json"
    )

    path = pin_bundle(
        "v1",
        "SPY",
        date(2022, 1, 5),
        research_run_id=8,
        engine=_engine_with_colliding_runs(),
        snapshots=snapshots,
        market_client=_benchmark_client(),
    )

    assert path == "rqis-snapshots/manifests/2022-01-05/manifest.json"
    manifest = snapshots.save_dataset_manifest.call_args.args[0]
    assert manifest.row_counts["alpha_scores"] == 1


# ─── disjoint-date multi-run splice (BUG-009 section 4, adversarial round 11) ──


def _engine_with_disjoint_date_multi_run_history():
    """Two runs covering DISJOINT score_dates for the same strategy (a
    realistic incremental-backfill pattern, e.g. a legacy same-close run
    covering earlier dates and a newer t+1/PIT run covering later ones) --
    zero (ticker, score_date) collisions between them, but still two
    methodologically distinct series spliced into one bundle if pinned
    together unscoped."""
    engine = create_engine("sqlite://")
    _create_universe_and_research_tables(engine)
    prices = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "date": [date(2022, 1, 3), date(2022, 1, 4)],
            "close": [100.0, 101.0],
        }
    )
    alpha = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "score_date": [date(2022, 1, 3), date(2022, 1, 4)],
            "strategy_id": ["v1", "v1"],
            "alpha_score": [1.0, 1.2],
            "research_run_id": [7, 8],
        }
    )
    actions = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    prices.to_sql("daily_prices", engine, index=False)
    alpha.to_sql("alpha_scores", engine, index=False)
    actions.to_sql("corporate_actions", engine, index=False)
    return engine


def test_pin_bundle_rejects_disjoint_date_multi_run_history_without_explicit_selection():
    """Round 3 only rejected a same-(ticker, score_date) collision across
    runs; round 11 found that disjoint date ranges across DIFFERENT runs
    still splice methodologically distinct score series together and must
    also be rejected by default."""
    with pytest.raises(ValueError, match="distinct research_run_ids"):
        pin_bundle(
            "v1",
            "SPY",
            date(2022, 1, 5),
            engine=_engine_with_disjoint_date_multi_run_history(),
            snapshots=MagicMock(),
            market_client=_benchmark_client(),
        )


def test_pin_bundle_research_run_id_disambiguates_disjoint_date_splice():
    snapshots = MagicMock()
    snapshots.save_snapshot.side_effect = (
        lambda _df, data_type, snapshot_date, bytes_sha256_out=None: (
            f"rqis-snapshots/snapshots/{data_type}/{snapshot_date}/data.parquet"
        )
    )
    snapshots.save_dataset_manifest.return_value = (
        "rqis-snapshots/manifests/2022-01-05/manifest.json"
    )

    path = pin_bundle(
        "v1",
        "SPY",
        date(2022, 1, 5),
        research_run_id=8,
        engine=_engine_with_disjoint_date_multi_run_history(),
        snapshots=snapshots,
        market_client=_benchmark_client(),
    )
    assert path == "rqis-snapshots/manifests/2022-01-05/manifest.json"
    manifest = snapshots.save_dataset_manifest.call_args.args[0]
    assert manifest.row_counts["alpha_scores"] == 1


def test_pin_bundle_single_run_history_is_allowed_without_explicit_selection():
    """Sanity: the common case (a strategy scored under exactly one
    research_run_id for its whole history) must NOT require
    --research-run-id -- only genuinely multi-run history needs it."""
    engine = create_engine("sqlite://")
    _create_universe_and_research_tables(engine)
    prices = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "date": [date(2022, 1, 3), date(2022, 1, 4)],
            "close": [100.0, 101.0],
        }
    )
    alpha = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "score_date": [date(2022, 1, 3), date(2022, 1, 4)],
            "strategy_id": ["v1", "v1"],
            "alpha_score": [1.0, 1.2],
            "research_run_id": [7, 7],
        }
    )
    actions = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    prices.to_sql("daily_prices", engine, index=False)
    alpha.to_sql("alpha_scores", engine, index=False)
    actions.to_sql("corporate_actions", engine, index=False)

    snapshots = MagicMock()
    snapshots.save_snapshot.side_effect = (
        lambda _df, data_type, snapshot_date, bytes_sha256_out=None: (
            f"rqis-snapshots/snapshots/{data_type}/{snapshot_date}/data.parquet"
        )
    )
    snapshots.save_dataset_manifest.return_value = (
        "rqis-snapshots/manifests/2022-01-05/manifest.json"
    )

    path = pin_bundle(
        "v1",
        "SPY",
        date(2022, 1, 5),
        engine=engine,
        snapshots=snapshots,
        market_client=_benchmark_client(),
    )
    assert path == "rqis-snapshots/manifests/2022-01-05/manifest.json"


# ─── Idempotent re-pin against a real ParquetSnapshots (03A-1, section 2.5) ───


class _InMemoryMinio:
    """Fake MinIO client backed by a dict, exercising the real
    ParquetSnapshots.save_snapshot / DatasetManifest.save_manifest content-
    addressing logic end to end (not a mock of pin_bundle's dependencies)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_object_calls = 0

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def make_bucket(self, bucket: str) -> None:
        pass

    def stat_object(self, bucket: str, key: str):
        if key not in self.objects:
            raise S3Error(
                code="NoSuchKey", message="not found",
                resource="", request_id="", host_id="", response=MagicMock()
            )
        return MagicMock()

    def get_object(self, bucket: str, key: str):
        if key not in self.objects:
            raise S3Error(
                code="NoSuchKey", message="not found",
                resource="", request_id="", host_id="", response=MagicMock()
            )
        resp = MagicMock()
        resp.read.return_value = self.objects[key]
        return resp

    def put_object(self, bucket_name, object_name, data, length, content_type):
        self.objects[object_name] = data.read()
        self.put_object_calls += 1

    def list_objects(self, bucket, prefix="", recursive=False):
        return []


def _real_snapshots(client: _InMemoryMinio) -> ParquetSnapshots:
    with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
        mock_cls.return_value = client
        snapshots = ParquetSnapshots(
            endpoint="localhost:9000",
            access_key="k",
            secret_key="s",
            bucket="rqis-snapshots",
        )
    return snapshots


def test_repinning_unchanged_data_writes_zero_new_objects() -> None:
    """Section 2.5's core acceptance test: re-running pin_snapshot against an
    unchanged DB produces zero new MinIO writes, exercised through the real
    ParquetSnapshots + DatasetManifest content-addressing path (not mocks)."""
    client = _InMemoryMinio()
    snapshots = _real_snapshots(client)

    kwargs = dict(
        strategy_id="v1",
        benchmark_ticker="SPY",
        engine=_engine_with_bundle_data(),
        snapshots=snapshots,
        market_client=_benchmark_client(),
    )

    path1 = pin_bundle(snapshot_date=date(2022, 1, 5), **kwargs)
    writes_after_first = client.put_object_calls
    assert writes_after_first > 0

    path2 = pin_bundle(snapshot_date=date(2022, 1, 6), **kwargs)  # even a different label date

    assert path1 == path2  # same logical content -> same manifest key
    assert client.put_object_calls == writes_after_first  # zero new writes


def test_backtest_loader_reads_a_pinned_bundle_end_to_end() -> None:
    """P0-1 regression: backtesting.loader.load_from_snapshot must consume a
    03A-1 content-addressed bundle through the manifest-driven read path
    (load_manifest -> load_snapshot_by_manifest) without crashing. Pins a
    real bundle via the in-memory MinIO fake, then loads it back by the
    manifest's content hash (the data_version) and asserts a usable
    DataHandler comes out."""
    from backtesting.loader import load_from_snapshot

    client = _InMemoryMinio()
    snapshots = _real_snapshots(client)

    manifest_path = pin_bundle(
        "v1", "SPY", date(2022, 1, 5),
        engine=_engine_with_bundle_data(),
        snapshots=snapshots,
        market_client=_benchmark_client(),
    )
    # manifest_path == "rqis-snapshots/manifests/{hash}/manifest.json"
    data_version = manifest_path.split("/")[-2]
    assert len(data_version) == 64  # a content hash, not a date string

    config = {
        "name": "v1",
        "strategy_id": "v1",
        "data_version": data_version,
        "portfolio": {"method": "equal_weight", "n_long": 10},
        "backtest": {
            "start_date": "2022-01-01",
            "end_date": "2022-12-31",
            "initial_capital": 100_000.0,
        },
    }

    handler = load_from_snapshot(data_version, config, snapshots=snapshots)

    # The bundle's single AAPL score is present and the prices loaded.
    signals = handler.get_latest_signals(date(2022, 1, 5))
    assert "AAPL" in signals["ticker"].tolist()


def test_repinning_with_one_changed_row_writes_new_objects_and_keeps_old_ones() -> None:
    client = _InMemoryMinio()
    snapshots = _real_snapshots(client)

    engine1 = _engine_with_bundle_data()
    path1 = pin_bundle(
        "v1", "SPY", date(2022, 1, 5),
        engine=engine1, snapshots=snapshots, market_client=_benchmark_client(),
    )
    objects_after_first = dict(client.objects)

    engine2 = create_engine("sqlite://")
    _create_universe_and_research_tables(engine2)
    prices = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "date": [date(2022, 1, 3), date(2022, 1, 4)],
            "close": [100.0, 999.0],  # changed row
        }
    )
    alpha = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "score_date": [date(2022, 1, 4)],
            "strategy_id": ["v1"],
            "alpha_score": [1.0],
        }
    )
    actions = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    prices.to_sql("daily_prices", engine2, index=False)
    alpha.to_sql("alpha_scores", engine2, index=False)
    actions.to_sql("corporate_actions", engine2, index=False)

    path2 = pin_bundle(
        "v1", "SPY", date(2022, 1, 5),
        engine=engine2, snapshots=snapshots, market_client=_benchmark_client(),
    )

    assert path1 != path2
    # Old objects remain byte-identical and present (nothing overwritten).
    for key, value in objects_after_first.items():
        assert client.objects[key] == value


# ─── 03A-5: manifest/methodology linkage populated from real batch rows ──────


def _engine_with_bundle_data_and_universe_batches(
    *, import_status: str = "published", with_eligibility_batch: bool = True
):
    """Bundle-data engine (as `_engine_with_bundle_data`) plus a real
    UniverseImportBatch/UniverseEligibilityBatch row for universe_id="sp500"
    so pin_bundle's lookups (03A-5) resolve to actual ids instead of None."""
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session

    from data.universe.models import UniverseEligibilityBatch, UniverseImportBatch

    engine = _engine_with_bundle_data()
    with Session(engine) as session:
        import_batch = UniverseImportBatch(
            universe_id="sp500",
            provider="test",
            source_version="v1",
            raw_artifact_path="raw/x",
            raw_checksum_sha256="a" * 64,
            retrieved_at=datetime.now(timezone.utc),
            status=import_status,
            published_at=datetime.now(timezone.utc) if import_status == "published" else None,
            created_at=datetime.now(timezone.utc),
        )
        session.add(import_batch)
        if with_eligibility_batch:
            session.add(
                UniverseEligibilityBatch(
                    universe_id="sp500",
                    code_version="v1",
                    computed_at=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.commit()
        import_batch_id = import_batch.id
    return engine, import_batch_id


def test_pin_bundle_populates_membership_and_eligibility_batch_ids_from_real_rows():
    """03A-5, requirement 5c: pin_bundle looks up the real published
    UniverseImportBatch/UniverseEligibilityBatch rows for --universe-id and
    the resulting manifest carries both ids -- verified end to end through
    the real ParquetSnapshots + DatasetManifest content-addressing path (not
    a MagicMock stand-in for the manifest), so the ids are also proven to
    participate in manifest_content_sha256."""
    engine, import_batch_id = _engine_with_bundle_data_and_universe_batches()

    client = _InMemoryMinio()
    snapshots = _real_snapshots(client)

    manifest_path = pin_bundle(
        "v1", "SPY", date(2022, 1, 5),
        engine=engine, snapshots=snapshots, market_client=_benchmark_client(),
        universe_id="sp500",
    )

    from backtesting.dataset_manifest import load_manifest

    data_version = manifest_path.split("/")[-2]
    manifest = load_manifest(data_version, client, "rqis-snapshots")
    assert manifest.membership_import_batch_id == import_batch_id
    assert manifest.eligibility_batch_id is not None

    # The linked ids survive into the content hash: pinning the identical
    # bundle for a universe_id with no batches at all yields a different
    # manifest_content_sha256 (proves the ids are hashed, not decorative).
    bare_engine = _engine_with_bundle_data()
    bare_path = pin_bundle(
        "v1", "SPY", date(2022, 1, 5),
        engine=bare_engine, snapshots=snapshots, market_client=_benchmark_client(),
        universe_id="sp500",
    )
    assert bare_path != manifest_path


def test_pin_bundle_leaves_membership_batch_id_none_when_no_published_import_exists():
    """Best-effort lookup, not a hard block: a universe_id with only an
    unpublished (staged) import batch must not fail the pin -- it just
    leaves membership_import_batch_id unset."""
    engine, _ = _engine_with_bundle_data_and_universe_batches(
        import_status="staged", with_eligibility_batch=False
    )
    snapshots = MagicMock()
    snapshots.save_snapshot.side_effect = (
        lambda _df, data_type, snapshot_date, bytes_sha256_out=None: (
            f"rqis-snapshots/snapshots/{data_type}/{snapshot_date}/data.parquet"
        )
    )
    snapshots.save_dataset_manifest.return_value = (
        "rqis-snapshots/manifests/2022-01-05/manifest.json"
    )

    pin_bundle(
        "v1", "SPY", date(2022, 1, 5),
        engine=engine, snapshots=snapshots, market_client=_benchmark_client(),
        universe_id="sp500",
    )
    manifest = snapshots.save_dataset_manifest.call_args.args[0]
    assert manifest.membership_import_batch_id is None
    assert manifest.eligibility_batch_id is None


def test_latest_eligibility_batch_id_tiebreaks_on_id_when_computed_at_ties():
    """Adversarial review P1: _latest_eligibility_batch_id must break a
    computed_at tie the same deterministic way
    PITEligibilityLookup._resolve_attribute already does
    (data/universe/runtime.py) -- computed_at is application-supplied
    wall-clock time, not DB-guaranteed monotonic, so two batches can
    legitimately share an identical value (clock resolution, a rerun with a
    fixed computed_at override, concurrent workers). Without an explicit
    secondary sort key, SQL gives no ordering guarantee among ties."""
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session

    from data.universe.models import UniverseEligibilityBatch
    from scripts.pin_snapshot import _latest_eligibility_batch_id

    engine = _engine_with_bundle_data()
    shared_computed_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as session:
        older = UniverseEligibilityBatch(
            universe_id="sp500",
            code_version="v1",
            computed_at=shared_computed_at,
            created_at=shared_computed_at,
        )
        newer = UniverseEligibilityBatch(
            universe_id="sp500",
            code_version="v1",
            computed_at=shared_computed_at,  # identical computed_at -- the tie
            created_at=shared_computed_at,
        )
        session.add_all([older, newer])
        session.commit()
        newer_id = newer.id
        assert newer_id > older.id  # sanity: insertion order gives a higher id

    result = _latest_eligibility_batch_id(engine, "sp500")
    assert result == newer_id


def test_pin_bundle_rejects_nonexistent_research_methodology_id():
    """A caller-supplied --research-methodology-id that does not exist must
    fail the pin closed (build_manifest's ManifestBatchLinkageError), not
    silently link to garbage."""
    from backtesting.dataset_manifest import ManifestBatchLinkageError

    engine = _engine_with_bundle_data()
    with pytest.raises(ManifestBatchLinkageError):
        pin_bundle(
            "v1", "SPY", date(2022, 1, 5),
            engine=engine, snapshots=MagicMock(), market_client=_benchmark_client(),
            research_methodology_id=999,
        )
