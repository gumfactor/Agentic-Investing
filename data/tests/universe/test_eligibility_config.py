"""Conformance tests for the fail-closed eligibility-filter config contract
(data/universe/eligibility_config.py, Roadmap 03A-4a §1.3/§1.5).

Mirrors backtesting/tests/test_config_contract.py's structure: every
eligibility-shaped key that appears in any config/strategy/*.yaml file must
resolve to an explicit PIT_SUPPORTED-or-FAIL_CLOSED_UNSUPPORTED
classification -- never "unclassified" (which would mean a filter neither
implemented nor named as unsupported, i.e. a silent-pass gap).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from data.universe.eligibility_config import (
    FAIL_CLOSED_UNSUPPORTED,
    PIT_SUPPORTED,
    UNCLASSIFIED,
    UnsupportedEligibilityFilterError,
    eligibility_filter_status,
    iter_universe_filter_keys,
    parse_universe_eligibility_filters,
)
from data.universe.runtime import EligibilityFilterOp, FilterSpec

_STRATEGY_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "strategy"


def _load_all_strategy_configs() -> dict[str, dict]:
    configs = {}
    for path in sorted(_STRATEGY_CONFIG_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            configs[path.name] = yaml.safe_load(fh)
    return configs


_ALL_CONFIGS = _load_all_strategy_configs()


def test_strategy_config_dir_is_nonempty() -> None:
    # Guards the whole conformance sweep against a silently-empty glob
    # (e.g. a wrong parents[] depth) making every parametrized case vacuous.
    assert _ALL_CONFIGS, f"No strategy YAMLs found under {_STRATEGY_CONFIG_DIR}"


@pytest.mark.parametrize("filename", sorted(_ALL_CONFIGS))
def test_every_universe_filter_key_is_explicitly_classified(filename: str) -> None:
    config = _ALL_CONFIGS[filename]
    for key in sorted(iter_universe_filter_keys(config)):
        status = eligibility_filter_status(key)
        assert status != UNCLASSIFIED, (
            f"{filename}: universe filter '{key}' is not classified by "
            "data/universe/eligibility_config.py (eligibility_filter_status "
            "returned 'unclassified'). Add it as PIT_SUPPORTED or "
            "FAIL_CLOSED_UNSUPPORTED -- never let a new eligibility filter "
            "pass through unclassified (Roadmap 03A-4a, §1.3)."
        )


def test_v1_base_momentum_declares_market_cap_and_adv() -> None:
    """Bottom-up sanity check: confirms the enumeration actually found the
    real keys from the shipped YAML, not an empty set that would make the
    conformance test above vacuously pass."""
    keys = iter_universe_filter_keys(_ALL_CONFIGS["v1_base_momentum.yaml"])
    assert keys == {"min_market_cap_usd", "min_adv_usd"}


def test_v2_mvo_momentum_declares_all_four_filters() -> None:
    keys = iter_universe_filter_keys(_ALL_CONFIGS["v2_mvo_momentum.yaml"])
    assert keys == {
        "min_market_cap_usd",
        "min_adv_usd",
        "min_price_usd",
        "allowed_security_types",
    }


@pytest.mark.parametrize(
    "key,expected_status",
    [
        ("min_adv_usd", PIT_SUPPORTED),
        ("min_price_usd", PIT_SUPPORTED),
        ("allowed_security_types", PIT_SUPPORTED),
        ("min_market_cap_usd", FAIL_CLOSED_UNSUPPORTED),
        ("max_market_cap_usd", FAIL_CLOSED_UNSUPPORTED),
        ("halted_flag", FAIL_CLOSED_UNSUPPORTED),
        ("bankruptcy_flag", FAIL_CLOSED_UNSUPPORTED),
        ("some_brand_new_never_reviewed_filter", UNCLASSIFIED),
    ],
)
def test_filter_status_classification(key: str, expected_status: str) -> None:
    assert eligibility_filter_status(key) == expected_status


def test_v1_base_momentum_fails_closed_on_market_cap() -> None:
    """Operator decision: min_market_cap_usd has no PIT source and must
    fail config load, not silently pass every ticker."""
    config = _ALL_CONFIGS["v1_base_momentum.yaml"]
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "min_market_cap_usd" in str(exc_info.value)


def test_v2_mvo_momentum_fails_closed_on_market_cap() -> None:
    config = _ALL_CONFIGS["v2_mvo_momentum.yaml"]
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "min_market_cap_usd" in str(exc_info.value)


def test_pit_supported_filters_parse_without_market_cap() -> None:
    """A config declaring only PIT-supported filters parses cleanly into
    FilterSpec objects the runtime can evaluate."""
    config = {
        "universe": {
            "source": "sp500",
            "filters": {
                "min_adv_usd": 1_000_000,
                "min_price_usd": 5.0,
                "allowed_security_types": ["CS", "ADR"],
            },
        }
    }
    specs = parse_universe_eligibility_filters(config)
    assert set(specs) == {"min_adv_usd", "min_price_usd", "allowed_security_types"}
    assert specs["min_adv_usd"] == FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0)
    assert specs["min_price_usd"] == FilterSpec("price_usd", EligibilityFilterOp.GTE, 5.0)
    assert specs["allowed_security_types"] == FilterSpec(
        "security_type", EligibilityFilterOp.IN, ("CS", "ADR")
    )


def test_flat_legacy_style_also_parses() -> None:
    config = {"universe": {"source": "sp500", "min_adv_usd": 2_000_000}}
    specs = parse_universe_eligibility_filters(config)
    assert specs["min_adv_usd"].threshold == 2_000_000.0


def test_no_universe_section_returns_empty() -> None:
    assert parse_universe_eligibility_filters({"name": "no_universe_here"}) == {}


def test_unclassified_filter_fails_closed_not_silently_dropped() -> None:
    config = {"universe": {"filters": {"some_future_unreviewed_key": 42}}}
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "some_future_unreviewed_key" in str(exc_info.value)


def test_new_explicit_eligibility_block_parses() -> None:
    config = {
        "universe": {
            "source": "sp500",
            "eligibility": {
                "adv_usd_20d": {"op": "gte", "threshold": 1_000_000},
                "security_type": {"op": "in", "threshold": ["common_stock", "adr"]},
            },
        }
    }
    specs = parse_universe_eligibility_filters(config)
    assert specs["eligibility.adv_usd_20d"] == FilterSpec(
        "adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0
    )
    assert specs["eligibility.security_type"] == FilterSpec(
        "security_type", EligibilityFilterOp.IN, ("common_stock", "adr")
    )


def test_eligibility_block_unsupported_attribute_rejected() -> None:
    config = {
        "universe": {
            "eligibility": {"market_cap_usd": {"op": "gte", "threshold": 500_000_000}}
        }
    }
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "market_cap_usd" in str(exc_info.value)


def test_non_numeric_threshold_on_recognized_key_fails_closed() -> None:
    """P2-1: a non-numeric threshold on a PIT-supported numeric key raises
    UnsupportedEligibilityFilterError (through the collect-violations
    contract), never a raw ValueError."""
    config = {"universe": {"filters": {"min_adv_usd": "not_a_number"}}}
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "min_adv_usd" in str(exc_info.value)


def test_malformed_security_types_value_fails_closed_not_silent_exclude() -> None:
    """P2-2: a non-list/tuple/str allowed_security_types value (e.g. a dict)
    must raise a named violation, not silently parse into a filter that
    excludes every ticker."""
    config = {"universe": {"filters": {"allowed_security_types": {"nested": "oops"}}}}
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "allowed_security_types" in str(exc_info.value)


def test_eligibility_block_non_numeric_threshold_fails_closed() -> None:
    config = {
        "universe": {
            "eligibility": {"adv_usd_20d": {"op": "gte", "threshold": "nope"}}
        }
    }
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    assert "adv_usd_20d" in str(exc_info.value)


def test_single_string_security_type_still_accepted() -> None:
    """A bare string (not a list) for an IN filter is a legitimate
    shorthand and must still parse -- the P2-2 guard rejects only
    non-collection, non-string values."""
    config = {"universe": {"filters": {"allowed_security_types": "common_stock"}}}
    specs = parse_universe_eligibility_filters(config)
    assert specs["allowed_security_types"].threshold == ("common_stock",)


def test_all_violations_reported_together() -> None:
    """Mirrors config_contract.py's 'collect every violation before raising'
    behavior -- a caller sees the full offending set in one error, not just
    the first."""
    config = {
        "universe": {
            "filters": {
                "min_market_cap_usd": 500_000_000,
                "min_adv_usd": 1_000_000,  # valid, should not appear in message
            },
            "eligibility": {"halted_flag": {"op": "eq", "threshold": 0}},
        }
    }
    with pytest.raises(UnsupportedEligibilityFilterError) as exc_info:
        parse_universe_eligibility_filters(config)
    message = str(exc_info.value)
    assert "min_market_cap_usd" in message
