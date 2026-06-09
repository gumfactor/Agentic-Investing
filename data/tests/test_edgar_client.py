"""Tests for data/ingestion/fundamentals/edgar_client.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from data.ingestion.fundamentals.edgar_client import (
    EdgarClient,
    _classify_period,
    _compute_derived,
    _extract_concept,
    _parse_observations,
)
from data.ingestion.fundamentals.concept_map import CONCEPT_ALIASES


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_facts(concept_name: str, observations: list[dict]) -> dict:
    """Build a minimal companyfacts JSON blob for one concept."""
    return {
        "cik": "0000320193",
        "entityName": "Test Corp",
        "facts": {
            "us-gaap": {
                concept_name: {
                    "label": concept_name,
                    "units": {
                        "USD": observations,
                    },
                }
            }
        },
    }


def _obs(
    end: str,
    filed: str,
    val: int,
    start: str | None = None,
    fp: str = "Q1",
    form: str = "10-Q",
) -> dict:
    """Build a single EDGAR observation dict."""
    o = {"end": end, "filed": filed, "val": val, "fp": fp, "form": form, "accn": "test"}
    if start:
        o["start"] = start
    return o


# ─── _classify_period ─────────────────────────────────────────────────────────

class TestClassifyPeriod:
    def test_annual_by_duration(self):
        assert _classify_period("2023-01-01", "2023-12-31", "") == "annual"

    def test_quarterly_by_duration(self):
        assert _classify_period("2023-01-01", "2023-03-31", "") == "quarterly"

    def test_ttm_returns_none(self):
        # 270-day TTM window — not annual, not quarterly
        assert _classify_period("2022-07-01", "2023-03-31", "") == None

    def test_annual_by_fp_when_no_start(self):
        assert _classify_period(None, "2023-12-31", "FY") == "annual"

    def test_quarterly_by_fp_q2_no_start(self):
        assert _classify_period(None, "2023-06-30", "Q2") == "quarterly"

    def test_unknown_fp_no_start_returns_none(self):
        assert _classify_period(None, "2023-12-31", "H1") is None

    def test_invalid_date_returns_none(self):
        assert _classify_period("not-a-date", "2023-12-31", "") is None


# ─── _parse_observations ──────────────────────────────────────────────────────

class TestParseObservations:
    def test_basic_quarterly_row(self):
        obs = [_obs("2023-03-31", "2023-05-05", 1000000, start="2023-01-01")]
        rows = _parse_observations(obs, "AAPL", "net_income", {"quarterly"})
        assert len(rows) == 1
        assert rows[0]["period_end_date"] == date(2023, 3, 31)
        assert rows[0]["release_date"] == date(2023, 5, 5)
        assert rows[0]["period_type"] == "quarterly"
        assert rows[0]["value"] == Decimal("1000000")
        assert rows[0]["item_name"] == "net_income"
        assert rows[0]["source"] == "sec_edgar"

    def test_rejected_form_excluded(self):
        obs = [_obs("2023-03-31", "2023-05-05", 999, form="8-K")]
        rows = _parse_observations(obs, "AAPL", "net_income", {"quarterly", "annual"})
        assert len(rows) == 0

    def test_missing_filed_date_excluded(self):
        obs = [{"end": "2023-03-31", "val": 1000, "fp": "Q1", "form": "10-Q", "accn": "x"}]
        rows = _parse_observations(obs, "AAPL", "net_income", {"quarterly"})
        assert len(rows) == 0

    def test_period_type_filter_respected(self):
        annual_obs = _obs("2023-12-31", "2024-02-15", 5000000, start="2023-01-01", fp="FY", form="10-K")
        quarterly_obs = _obs("2023-03-31", "2023-05-05", 1000000, start="2023-01-01")
        rows = _parse_observations([annual_obs, quarterly_obs], "AAPL", "net_income", {"annual"})
        assert len(rows) == 1
        assert rows[0]["period_type"] == "annual"

    def test_multiple_observations_all_stored(self):
        """All qualifying observations stored — including restatements (different filed dates)."""
        obs = [
            _obs("2023-03-31", "2023-05-05", 1000000, start="2023-01-01"),
            _obs("2023-03-31", "2023-08-15", 1050000, start="2023-01-01"),  # restatement
        ]
        rows = _parse_observations(obs, "AAPL", "net_income", {"quarterly"})
        assert len(rows) == 2
        filed_dates = {r["release_date"] for r in rows}
        assert date(2023, 5, 5) in filed_dates
        assert date(2023, 8, 15) in filed_dates

    def test_annual_10k_amendment_accepted(self):
        obs = [_obs("2023-12-31", "2024-03-01", 9000000, start="2023-01-01", fp="FY", form="10-K/A")]
        rows = _parse_observations(obs, "MSFT", "net_income", {"annual"})
        assert len(rows) == 1

    def test_same_accession_deduped(self):
        """A 10-K re-reporting prior-year comparatives under the same accn/filed
        should produce only one row for that (end, filed, accn) combination."""
        obs = [
            {**_obs("2022-12-31", "2023-11-01", 5000000, start="2022-01-01", fp="FY", form="10-K"),
             "accn": "0000320193-23-000001"},
            # Same accn, same end, same filed — duplicate from comparative data
            {**_obs("2022-12-31", "2023-11-01", 5000000, start="2022-01-01", fp="FY", form="10-K"),
             "accn": "0000320193-23-000001"},
        ]
        rows = _parse_observations(obs, "AAPL", "net_income", {"annual"})
        assert len(rows) == 1


# ─── _extract_concept ─────────────────────────────────────────────────────────

class TestExtractConcept:
    def test_first_matching_alias_used(self):
        """Revenue: first alias 'RevenueFromContractWithCustomer...' should match."""
        us_gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "label": "Revenue",
                "units": {
                    "USD": [_obs("2023-03-31", "2023-05-05", 10000, start="2023-01-01")]
                },
            }
        }
        rows = _extract_concept(us_gaap, "revenue", CONCEPT_ALIASES["revenue"], "AAPL", {"quarterly"})
        assert len(rows) == 1

    def test_falls_back_to_second_alias(self):
        """If first alias missing, second is tried."""
        us_gaap = {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [_obs("2023-03-31", "2023-05-05", 10000, start="2023-01-01")]
                },
            }
        }
        rows = _extract_concept(us_gaap, "revenue", CONCEPT_ALIASES["revenue"], "AAPL", {"quarterly"})
        assert len(rows) == 1

    def test_no_match_returns_empty(self):
        rows = _extract_concept({}, "revenue", CONCEPT_ALIASES["revenue"], "AAPL", {"quarterly"})
        assert rows == []


# ─── _compute_derived ─────────────────────────────────────────────────────────

class TestComputeDerived:
    def _make_base_rows(
        self,
        ticker: str,
        period_end: date,
        release_dt: date,
        op_cf: int,
        capex: int,
        period_type: str = "quarterly",
    ) -> list[dict]:
        return [
            {
                "ticker": ticker,
                "period_end_date": period_end,
                "release_date": release_dt,
                "period_type": period_type,
                "item_name": "operating_cash_flow",
                "value": Decimal(str(op_cf)),
                "source": "sec_edgar",
                "source_version": "xbrl_companyfacts_v2",
            },
            {
                "ticker": ticker,
                "period_end_date": period_end,
                "release_date": release_dt,
                "period_type": period_type,
                "item_name": "capex",
                "value": Decimal(str(capex)),
                "source": "sec_edgar",
                "source_version": "xbrl_companyfacts_v2",
            },
        ]

    def test_free_cash_flow_computed(self):
        rows = self._make_base_rows("AAPL", date(2023, 3, 31), date(2023, 5, 5), 5000000, 1000000)
        derived = _compute_derived(rows, "AAPL")
        fcf_rows = [r for r in derived if r["item_name"] == "free_cash_flow"]
        assert len(fcf_rows) == 1
        assert fcf_rows[0]["value"] == Decimal("4000000")

    def test_missing_capex_no_fcf(self):
        rows = [{
            "ticker": "AAPL",
            "period_end_date": date(2023, 3, 31),
            "release_date": date(2023, 5, 5),
            "period_type": "quarterly",
            "item_name": "operating_cash_flow",
            "value": Decimal("5000000"),
            "source": "sec_edgar",
            "source_version": "xbrl_companyfacts_v2",
        }]
        derived = _compute_derived(rows, "AAPL")
        assert not any(r["item_name"] == "free_cash_flow" for r in derived)

    def test_multiple_periods_each_get_fcf(self):
        rows = (
            self._make_base_rows("AAPL", date(2023, 3, 31), date(2023, 5, 5), 4000000, 800000)
            + self._make_base_rows("AAPL", date(2023, 6, 30), date(2023, 8, 5), 5000000, 900000)
        )
        derived = _compute_derived(rows, "AAPL")
        fcf_rows = [r for r in derived if r["item_name"] == "free_cash_flow"]
        assert len(fcf_rows) == 2


# ─── EdgarClient (with mocked HTTP) ──────────────────────────────────────────

class TestEdgarClientGetCikMap:
    def test_maps_ticker_to_zero_padded_cik(self):
        mock_response = {
            "0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."},
            "1": {"ticker": "MSFT", "cik_str": 789019, "title": "Microsoft"},
        }
        client = EdgarClient()
        with patch.object(client, "_get_json", return_value=mock_response):
            result = client.get_cik_map()
        assert result["AAPL"] == "0000320193"
        assert result["MSFT"] == "0000789019"

    def test_cik_zero_padded_to_10_digits(self):
        mock_response = {"0": {"ticker": "TEST", "cik_str": 123, "title": "Test"}}
        client = EdgarClient()
        with patch.object(client, "_get_json", return_value=mock_response):
            result = client.get_cik_map()
        assert result["TEST"] == "0000000123"


class TestEdgarClientExtractFundamentals:
    def test_extracts_net_income_quarterly(self):
        facts = _make_facts(
            "NetIncomeLoss",
            [_obs("2023-03-31", "2023-05-05", 2400000000, start="2023-01-01")],
        )
        client = EdgarClient()
        rows = client.extract_fundamentals("AAPL", facts, period_types={"quarterly"})
        ni_rows = [r for r in rows if r["item_name"] == "net_income"]
        assert len(ni_rows) == 1
        assert ni_rows[0]["value"] == Decimal("2400000000")

    def test_schema_fields_present(self):
        facts = _make_facts(
            "NetIncomeLoss",
            [_obs("2023-03-31", "2023-05-05", 1000, start="2023-01-01")],
        )
        client = EdgarClient()
        rows = client.extract_fundamentals("AAPL", facts)
        assert rows, "Expected at least one row"
        required = {
            "ticker", "period_end_date", "release_date", "period_type",
            "item_name", "value", "source", "source_version",
        }
        for row in rows:
            assert required.issubset(set(row.keys()))

    def test_empty_facts_returns_empty_list(self):
        facts = {"cik": "0000320193", "entityName": "Test", "facts": {}}
        client = EdgarClient()
        rows = client.extract_fundamentals("AAPL", facts)
        assert rows == []

    def test_ticker_propagated_to_all_rows(self):
        facts = _make_facts(
            "NetIncomeLoss",
            [_obs("2023-03-31", "2023-05-05", 1000, start="2023-01-01")],
        )
        client = EdgarClient()
        rows = client.extract_fundamentals("MSFT", facts)
        assert all(r["ticker"] == "MSFT" for r in rows)


class TestEdgarClientBackfill:
    def test_missing_tickers_skipped(self):
        client = EdgarClient()
        cik_map = {"AAPL": "0000320193"}
        with patch.object(client, "fetch_company_facts", return_value={"facts": {}}):
            results = client.backfill(["AAPL", "ZZZZ"], cik_map=cik_map)
        assert "ZZZZ" not in results

    def test_http_error_skips_ticker(self):
        import requests as req
        client = EdgarClient()
        cik_map = {"AAPL": "0000320193"}
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(
            client, "fetch_company_facts",
            side_effect=req.HTTPError(response=mock_resp)
        ):
            results = client.backfill(["AAPL"], cik_map=cik_map)
        assert "AAPL" not in results

    def test_successful_ticker_returns_rows(self):
        facts = _make_facts(
            "NetIncomeLoss",
            [_obs("2023-03-31", "2023-05-05", 1000, start="2023-01-01")],
        )
        client = EdgarClient()
        cik_map = {"AAPL": "0000320193"}
        with patch.object(client, "fetch_company_facts", return_value=facts):
            results = client.backfill(["AAPL"], cik_map=cik_map)
        assert "AAPL" in results
        assert len(results["AAPL"]) > 0
