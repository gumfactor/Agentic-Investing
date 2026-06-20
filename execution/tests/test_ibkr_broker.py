"""Tests for IBKR broker account-currency handling."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from execution.brokers.ibkr import IBKRBroker
from execution.oms.order import Order, OrderSide


class FakeIB:
    def __init__(self, values):
        self._values = values

    def isConnected(self):
        return True

    def sleep(self, _seconds):
        return None

    def accountValues(self):
        return self._values


class FakeTicker:
    bid = 1.2
    ask = 1.3

    def marketPrice(self):
        return None


class FakeIBForFx(FakeIB):
    def __init__(self, qualified_contract):
        super().__init__([])
        self.qualified_contract = qualified_contract
        self.requested_contract = None
        self.cancelled_contract = None

    def qualifyContracts(self, _contract):
        return [self.qualified_contract]

    def reqMktData(self, contract, *_args):
        self.requested_contract = contract
        return FakeTicker()

    def cancelMktData(self, contract):
        self.cancelled_contract = contract


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class FakeIBForWhatIf(FakeIB):
    def __init__(self, *, emit_error: bool = False, empty_state: bool = False) -> None:
        super().__init__([])
        self.errorEvent = FakeEvent()
        self.emit_error = emit_error
        self.empty_state = empty_state
        self.last_order = None

    def whatIfOrder(self, _contract, _order):
        self.last_order = _order
        if self.emit_error:
            self.errorEvent.emit(3, 10243, "Fractional-sized order cannot be placed via API.", None)
        if self.empty_state:
            return SimpleNamespace(
                status="",
                initMarginBefore="",
                maintMarginBefore="",
                equityWithLoanBefore="",
                initMarginChange="",
                maintMarginChange="",
                equityWithLoanChange="",
                initMarginAfter="",
                maintMarginAfter="",
                equityWithLoanAfter="",
                commission="",
                minCommission="",
                maxCommission="",
                commissionCurrency="",
                warningText="",
                completedStatus="",
            )
        return SimpleNamespace(
            status="PreSubmitted",
            initMarginBefore="1000",
            maintMarginBefore="1000",
            equityWithLoanBefore="1000",
            initMarginChange="10",
            maintMarginChange="5",
            equityWithLoanChange="0",
            initMarginAfter="1010",
            maintMarginAfter="1005",
            equityWithLoanAfter="1000",
            commission="1",
            minCommission="1",
            maxCommission="1",
            commissionCurrency="USD",
            warningText="",
            completedStatus="",
        )


def _account_value(tag: str, value: str, currency: str):
    return SimpleNamespace(tag=tag, value=value, currency=currency)


def _broker(values) -> IBKRBroker:
    broker = IBKRBroker.__new__(IBKRBroker)
    broker._ib = FakeIB(values)
    broker._port = 7497
    return broker


def test_account_values_by_currency_prefers_net_liquidation():
    broker = _broker(
        [
            _account_value("$LEDGER-NetLiquidationByCurrency", "999.00", "CAD"),
            _account_value("NetLiquidation", "1000.00", "CAD"),
            _account_value("NetLiquidation", "500.00", "USD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "1000.00", "BASE"),
        ]
    )

    assert broker.get_account_values_by_currency() == {"CAD": 1000.0, "USD": 500.0}


def test_account_values_by_currency_uses_multi_currency_ledger_components():
    broker = _broker(
        [
            _account_value("NetLiquidation", "1100.00", "CAD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "1000.00", "CAD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "100.00", "USD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "1100.00", "BASE"),
        ]
    )

    assert broker.get_account_values_by_currency() == {"CAD": 1000.0, "USD": 100.0}


def test_account_values_by_currency_does_not_use_partial_ledger_components():
    broker = _broker(
        [
            _account_value("NetLiquidation", "1100.00", "CAD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "100.00", "USD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "1100.00", "BASE"),
        ]
    )

    assert broker.get_account_values_by_currency() == {"CAD": 1100.0}


def test_get_account_value_in_direct_currency():
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])

    assert broker.get_account_value_in_currency("cad") == 1_000_000.0


def test_get_account_value_converts_cad_to_usd(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setattr(broker, "_get_fx_rate", lambda source, target: 0.75)

    assert broker.get_account_value_in_currency("USD") == 750_000.0


def test_get_account_value_sums_mixed_currencies_in_target_currency(monkeypatch):
    broker = _broker(
        [
            _account_value("$LEDGER-NetLiquidationByCurrency", "1000000.00", "CAD"),
            _account_value("$LEDGER-NetLiquidationByCurrency", "25000.00", "USD"),
        ]
    )
    monkeypatch.setattr(broker, "_get_fx_rate", lambda source, target: 0.75)

    assert broker.get_account_value_in_currency("USD") == 775_000.0


def test_get_account_value_uses_configured_fx_rate(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", "0.74")
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD_AS_OF", date.today().isoformat())

    assert broker.get_account_value_in_currency("USD") == 740_000.0


def test_get_account_value_uses_configured_inverse_fx_rate(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setenv("IBKR_FX_RATE_USD_CAD", "1.25")
    monkeypatch.setenv("IBKR_FX_RATE_USD_CAD_AS_OF", date.today().isoformat())

    assert broker.get_account_value_in_currency("USD") == 800_000.0


def test_configured_fx_rate_must_be_positive(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", "0")
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD_AS_OF", date.today().isoformat())

    with pytest.raises(RuntimeError, match="finite and positive"):
        broker.get_account_value_in_currency("USD")


@pytest.mark.parametrize("raw_rate", ["nan", "inf", "-inf"])
def test_configured_fx_rate_must_be_finite(monkeypatch, raw_rate):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", raw_rate)
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD_AS_OF", date.today().isoformat())

    with pytest.raises(RuntimeError, match="finite and positive"):
        broker.get_account_value_in_currency("USD")


def test_configured_fx_rate_requires_as_of_date(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", "0.74")

    with pytest.raises(RuntimeError, match="AS_OF is required"):
        broker.get_account_value_in_currency("USD")


def test_configured_fx_rate_rejects_stale_as_of_date(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    stale_date = date.today() - timedelta(days=2)
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", "0.74")
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD_AS_OF", stale_date.isoformat())

    with pytest.raises(RuntimeError, match="stale"):
        broker.get_account_value_in_currency("USD")


def test_configured_fx_rate_rejects_future_as_of_date(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    future_date = date.today() + timedelta(days=1)
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", "0.74")
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD_AS_OF", future_date.isoformat())

    with pytest.raises(RuntimeError, match="future"):
        broker.get_account_value_in_currency("USD")


def test_get_account_value_returns_usd_equivalent_even_if_reporting_env_is_cad(monkeypatch):
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])
    monkeypatch.setenv("IBKR_REPORTING_CURRENCY", "CAD")
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD", "0.74")
    monkeypatch.setenv("IBKR_FX_RATE_CAD_USD_AS_OF", date.today().isoformat())

    assert broker.get_account_value() == 740_000.0


def test_get_account_value_in_currency_rejects_invalid_currency():
    broker = _broker([_account_value("NetLiquidation", "1000000.00", "CAD")])

    with pytest.raises(RuntimeError, match="3-letter currency code"):
        broker.get_account_value_in_currency("")


def test_get_fx_rate_uses_inverse_when_direct_request_fails(monkeypatch):
    broker = _broker([])
    requested = []

    def fake_request(pair):
        requested.append(pair)
        if pair == "USDCAD":
            return 1.25
        return None

    monkeypatch.setattr(broker, "_request_fx_rate", fake_request)

    assert broker._get_fx_rate("CAD", "USD") == 0.8
    assert requested == ["CADUSD", "USDCAD"]


def test_request_fx_rate_uses_qualified_contract():
    qualified_contract = object()
    broker = _broker([])
    broker._ib = FakeIBForFx(qualified_contract)

    assert broker._request_fx_rate("USDCAD") == 1.25
    assert broker._ib.requested_contract is qualified_contract
    assert broker._ib.cancelled_contract is qualified_contract


def test_account_value_raises_when_no_net_liquidation():
    broker = _broker([_account_value("TotalCashValue", "1000000.00", "CAD")])

    with pytest.raises(RuntimeError, match="NetLiquidation not found"):
        broker.get_account_values_by_currency()


def test_what_if_order_raises_on_ibkr_error_event():
    broker = _broker([])
    broker._ib = FakeIBForWhatIf(emit_error=True)

    order = Order(ticker="AAPL", side=OrderSide.BUY, quantity=2.5, limit_price=200.0)

    with pytest.raises(RuntimeError, match="Fractional-sized order cannot be placed via API"):
        broker.what_if_order(order)


def test_what_if_order_raises_on_empty_order_state():
    broker = _broker([])
    broker._ib = FakeIBForWhatIf(empty_state=True)

    order = Order(ticker="AAPL", side=OrderSide.BUY, quantity=2.5, limit_price=200.0)

    with pytest.raises(RuntimeError, match="empty order state"):
        broker.what_if_order(order)


def test_what_if_order_returns_margin_summary():
    broker = _broker([])
    fake_ib = FakeIBForWhatIf()
    broker._ib = fake_ib

    order = Order(ticker="AAPL", side=OrderSide.BUY, quantity=1.0, limit_price=200.0)

    assert broker.what_if_order(order)["status"] == "PreSubmitted"
    assert fake_ib.last_order.tif == "DAY"
