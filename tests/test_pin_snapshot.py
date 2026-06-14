from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine

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
        lambda _df, data_type, snapshot_date: (
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
