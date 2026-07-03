from __future__ import annotations

from tw_watchdesk.strategy_versions import (
    default_daytrade_params,
    default_scout_params,
    default_swing_params,
    validate_daytrade_params,
    validate_scout_params,
    validate_swing_params,
)


def test_default_strategy_params_are_valid() -> None:
    assert validate_scout_params(default_scout_params()) == ""
    assert validate_daytrade_params(default_daytrade_params()) == ""
    assert validate_swing_params(default_swing_params()) == ""


def test_scout_weight_sum_is_validated() -> None:
    params = default_scout_params()
    bad = type(params)(**{**params.to_json(), "spread_weight": 0.5})

    assert "權重總和" in validate_scout_params(bad)


def test_daytrade_time_order_is_validated() -> None:
    params = default_daytrade_params()
    bad = type(params)(**{**params.to_json(), "entry_end_time": "09:00"})

    assert "entry_start_time" in validate_daytrade_params(bad)
