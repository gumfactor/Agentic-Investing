import io
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from minio.error import S3Error
from sqlalchemy import create_engine

from data.storage.parquet_snapshots import ParquetSnapshots
from scripts.pin_snapshot import pin_bundle


def _engine_with_bundle_data():
    engine = create_engine("sqlite://")
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
