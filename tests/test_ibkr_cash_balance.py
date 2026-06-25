"""Tests for IBKRBroker.get_cash_balance_usd() and _account_values_for_tag()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import os
os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("IBKR_PORT", "7497")


def _make_av(tag: str, currency: str, value: str) -> MagicMock:
    av = MagicMock()
    av.tag = tag
    av.currency = currency
    av.value = value
    return av


@pytest.fixture()
def broker():
    """IBKRBroker instance with ib_insync mocked out."""
    import execution.brokers.ibkr as _ibkr_mod

    mock_ib_cls = MagicMock()
    mock_ib = MagicMock()
    mock_ib_cls.return_value = mock_ib

    orig_available = _ibkr_mod._IB_AVAILABLE
    orig_ib = getattr(_ibkr_mod, "IB", None)
    _ibkr_mod._IB_AVAILABLE = True
    _ibkr_mod.IB = mock_ib_cls
    try:
        b = _ibkr_mod.IBKRBroker(host="127.0.0.1", port=7497)
        b._ib = mock_ib
        yield b, mock_ib
    finally:
        _ibkr_mod._IB_AVAILABLE = orig_available
        if orig_ib is None:
            del _ibkr_mod.IB
        else:
            _ibkr_mod.IB = orig_ib


class TestAccountValuesForTag:
    def test_returns_simple_values_when_no_ledger(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("TotalCashValue", "USD", "50000.0"),
        ]
        result = b._account_values_for_tag("TotalCashValue", "$LEDGER-CashBalance")
        assert result == {"USD": 50000.0}

    def test_prefers_ledger_when_multiple_currencies(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("TotalCashValue", "USD", "50000.0"),
            _make_av("$LEDGER-CashBalance", "USD", "40000.0"),
            _make_av("$LEDGER-CashBalance", "CAD", "12000.0"),
        ]
        result = b._account_values_for_tag("TotalCashValue", "$LEDGER-CashBalance")
        assert result == {"USD": 40000.0, "CAD": 12000.0}

    def test_falls_back_to_simple_when_single_ledger_entry(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("TotalCashValue", "USD", "50000.0"),
            _make_av("$LEDGER-CashBalance", "USD", "50000.0"),
        ]
        result = b._account_values_for_tag("TotalCashValue", "$LEDGER-CashBalance")
        # single ledger entry — simple wins (matches existing NetLiquidation logic)
        assert result == {"USD": 50000.0}

    def test_skips_base_currency(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("TotalCashValue", "BASE", "50000.0"),
            _make_av("TotalCashValue", "USD", "50000.0"),
        ]
        result = b._account_values_for_tag("TotalCashValue", "$LEDGER-CashBalance")
        assert "BASE" not in result
        assert result == {"USD": 50000.0}

    def test_returns_empty_dict_when_tag_absent(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("NetLiquidation", "USD", "100000.0"),
        ]
        result = b._account_values_for_tag("TotalCashValue", "$LEDGER-CashBalance")
        assert result == {}


class TestGetCashBalanceUsd:
    def test_single_usd_account(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("TotalCashValue", "USD", "75000.00"),
        ]
        result = b.get_cash_balance_usd()
        assert result == pytest.approx(75000.00)

    def test_raises_when_no_cash_values(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = []
        with pytest.raises(RuntimeError, match="TotalCashValue not found"):
            b.get_cash_balance_usd()

    def test_multi_currency_converts_via_fx(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("$LEDGER-CashBalance", "USD", "50000.0"),
            _make_av("$LEDGER-CashBalance", "CAD", "10000.0"),
        ]
        # Patch _get_fx_rate to return 0.75 CAD→USD
        with patch.object(b, "_get_fx_rate", return_value=0.75):
            result = b.get_cash_balance_usd()
        assert result == pytest.approx(50000.0 + 10000.0 * 0.75)

    def test_usd_is_not_converted(self, broker):
        b, mock_ib = broker
        mock_ib.accountValues.return_value = [
            _make_av("TotalCashValue", "USD", "12345.67"),
        ]
        # _get_fx_rate should NOT be called for USD→USD conversion
        with patch.object(b, "_get_fx_rate", side_effect=AssertionError("should not call")) as p:
            result = b.get_cash_balance_usd()
        assert result == pytest.approx(12345.67)
