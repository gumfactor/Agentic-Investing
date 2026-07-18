"""Tests for universe_loader.

Mocks external calls (Wikipedia, filesystem) so no network or disk access
is required.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import yaml

from config.universe_loader import (
    UniverseFetchError,
    _fetch_sp500_from_wikipedia,
    _load_from_csv,
    load_universe,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _write_config(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.dump(content))
    return p


def _write_csv(tmp_path: Path, tickers: list[str]) -> Path:
    p = tmp_path / "tickers.csv"
    p.write_text("ticker\n" + "\n".join(tickers))
    return p


# ─── _fetch_sp500_from_wikipedia ──────────────────────────────────────────────

def _make_wikipedia_html(tickers: list[str]) -> str:
    """Minimal HTML table that pd.read_html will parse as the S&P 500 list."""
    rows = "".join(f"<tr><td>{t}</td></tr>" for t in tickers)
    return f"<table><thead><tr><th>Symbol</th></tr></thead><tbody>{rows}</tbody></table>"


class TestFetchSp500FromWikipedia:
    def _mock_response(self, tickers: list[str]) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        # production code wraps response.text in io.StringIO before passing
        # to pd.read_html, so .text just needs to be a valid HTML string.
        mock_resp.text = _make_wikipedia_html(tickers)
        return mock_resp

    @patch("config.universe_loader.requests.get")
    def test_returns_ticker_list(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._mock_response(["AAPL", "MSFT", "GOOGL"])
        result = _fetch_sp500_from_wikipedia()
        assert result == ["AAPL", "MSFT", "GOOGL"]

    @patch("config.universe_loader.requests.get")
    def test_sends_browser_user_agent(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._mock_response(["AAPL"])
        _fetch_sp500_from_wikipedia()
        headers = mock_get.call_args[1]["headers"]
        assert "Mozilla" in headers["User-Agent"]

    @patch("config.universe_loader.requests.get")
    def test_replaces_dot_with_dash_in_ticker(self, mock_get: MagicMock) -> None:
        # BRK.B appears in S&P 500; yfinance expects BRK-B
        mock_get.return_value = self._mock_response(["BRK.B", "BF.B"])
        result = _fetch_sp500_from_wikipedia()
        assert "BRK-B" in result
        assert "BF-B" in result
        assert "BRK.B" not in result

    @patch(
        "config.universe_loader.requests.get",
        side_effect=__import__("requests").RequestException("network error"),
    )
    def test_raises_universe_fetch_error_on_failure(self, mock_get: MagicMock) -> None:
        # Deliberately changed for 01B-2 (BUG-008): the pre-01B-2 behavior of
        # returning [] on failure silently emptied downstream pipelines
        # (fail-open). The loader now fails closed.
        with pytest.raises(UniverseFetchError, match="Failing closed"):
            _fetch_sp500_from_wikipedia()

    @patch("config.universe_loader.requests.get")
    def test_returns_list_not_series(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._mock_response(["AAPL"])
        result = _fetch_sp500_from_wikipedia()
        assert isinstance(result, list)


# ─── _load_from_csv ───────────────────────────────────────────────────────────

class TestLoadFromCsv:
    def test_loads_tickers_from_csv(self, tmp_path: Path) -> None:
        csv = _write_csv(tmp_path, ["aapl", "msft", "googl"])
        result = _load_from_csv(csv)
        assert result == ["AAPL", "MSFT", "GOOGL"]

    def test_uppercases_tickers(self, tmp_path: Path) -> None:
        csv = _write_csv(tmp_path, ["aapl"])
        assert _load_from_csv(csv) == ["AAPL"]

    def test_raises_on_missing_ticker_column(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.csv"
        p.write_text("symbol\nAAPL\n")
        with pytest.raises(ValueError, match="ticker"):
            _load_from_csv(p)

    def test_drops_na_values(self, tmp_path: Path) -> None:
        p = tmp_path / "tickers.csv"
        p.write_text("ticker\nAAPL\n\nMSFT\n")
        result = _load_from_csv(p)
        assert len(result) == 2


# ─── load_universe ────────────────────────────────────────────────────────────

class TestLoadUniverse:
    @patch("config.universe_loader._fetch_sp500_from_wikipedia")
    def test_sp500_source_calls_wikipedia(
        self, mock_fetch: MagicMock, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = ["AAPL", "MSFT"]
        cfg = _write_config(tmp_path, {"source": "sp500_wikipedia"})
        result = load_universe(cfg)
        assert mock_fetch.called
        assert "AAPL" in result

    def test_csv_source_loads_from_file(self, tmp_path: Path) -> None:
        csv = _write_csv(tmp_path, ["AAPL", "TSLA"])
        cfg = _write_config(tmp_path, {"source": "csv", "csv_path": str(csv)})
        result = load_universe(cfg)
        assert set(result) == {"AAPL", "TSLA"}

    def test_csv_source_without_csv_path_raises(self, tmp_path: Path) -> None:
        cfg = _write_config(tmp_path, {"source": "csv"})
        with pytest.raises(ValueError, match="csv_path"):
            load_universe(cfg)

    def test_unknown_source_raises(self, tmp_path: Path) -> None:
        cfg = _write_config(tmp_path, {"source": "polygon"})
        with pytest.raises(ValueError, match="Unsupported"):
            load_universe(cfg)

    @patch("config.universe_loader._fetch_sp500_from_wikipedia")
    def test_force_exclude_removes_tickers(
        self, mock_fetch: MagicMock, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = ["AAPL", "MSFT", "GOOGL"]
        cfg = _write_config(
            tmp_path,
            {"source": "sp500_wikipedia", "force_exclude": ["MSFT"], "force_include": []},
        )
        result = load_universe(cfg)
        assert "MSFT" not in result
        assert "AAPL" in result

    @patch("config.universe_loader._fetch_sp500_from_wikipedia")
    def test_force_include_adds_ticker_not_in_universe(
        self, mock_fetch: MagicMock, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = ["AAPL", "MSFT"]
        cfg = _write_config(
            tmp_path,
            {"source": "sp500_wikipedia", "force_include": ["CUSTOM"], "force_exclude": []},
        )
        result = load_universe(cfg)
        assert "CUSTOM" in result

    @patch("config.universe_loader._fetch_sp500_from_wikipedia")
    def test_result_is_sorted_and_deduplicated(
        self, mock_fetch: MagicMock, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = ["MSFT", "AAPL", "AAPL"]
        cfg = _write_config(tmp_path, {"source": "sp500_wikipedia"})
        result = load_universe(cfg)
        assert result == sorted(set(result))

    @patch("config.universe_loader._fetch_sp500_from_wikipedia")
    def test_force_include_does_not_duplicate(
        self, mock_fetch: MagicMock, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = ["AAPL", "MSFT"]
        cfg = _write_config(
            tmp_path,
            {"source": "sp500_wikipedia", "force_include": ["AAPL"], "force_exclude": []},
        )
        result = load_universe(cfg)
        assert result.count("AAPL") == 1
