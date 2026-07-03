from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCOUT_STRATEGY = "scout"
DAYTRADE_STRATEGY = "daytrade"
SWING_STRATEGY = "swing"
FOLLOW_LATEST = "follow_latest"
MANUAL_LOCK = "manual_lock"


DEFAULT_SCOUT_PARAMS: dict[str, float | int | str] = {
    "min_turnover": 0.0,
    "max_spread_pct": 0.01,
    "daytrade_change_min": -0.02,
    "daytrade_change_max": 0.085,
    "swing_change_min": -0.035,
    "swing_change_max": 0.085,
    "liquidity_weight": 0.45,
    "momentum_weight": 0.35,
    "spread_weight": 0.20,
    "limit_up_policy": "avoid_new_entries",
    "limit_down_policy": "avoid_new_entries",
    "missing_depth_policy": "block_unless_limit_state_explained",
    "max_candidates_daytrade": 5,
    "max_candidates_swing": 5,
    "eligible_list_policy": "warn_and_commonstock_fallback",
}


DEFAULT_DAYTRADE_PARAMS: dict[str, float | int | str | bool] = {
    "stop_loss_pct": 0.012,
    "take_profit_pct": 0.018,
    "risk_pct": 0.0035,
    "max_position_pct": 0.25,
    "max_daily_loss_pct": 0.02,
    "entry_start_time": "09:10",
    "entry_end_time": "13:20",
    "force_exit_time": "13:25",
    "order_ttl_minutes": 5,
    "max_spread_pct": 0.006,
    "max_quote_age_seconds": 70,
    "missing_depth_policy": "block",
    "reentry_cooldown_minutes": 20,
    "consecutive_loss_stop": 2,
    "allow_limit_up_entry": False,
    "allow_limit_down_entry": False,
}


DEFAULT_SWING_PARAMS: dict[str, float | int] = {
    "stop_loss_pct": 0.06,
    "take_profit_pct_short": 0.08,
    "take_profit_pct_long": 0.10,
    "long_holding_months": 3,
    "risk_pct": 0.01,
    "max_position_pct": 0.25,
    "min_turnover": 0.0,
    "max_spread_pct": 0.02,
    "max_total_exposure_pct": 0.80,
    "max_position_symbols": 5,
}


SWING_PARAMETER_RANGES: dict[str, tuple[float, float]] = {
    "stop_loss_pct": (0.02, 0.15),
    "take_profit_pct_short": (0.03, 0.20),
    "take_profit_pct_long": (0.05, 0.30),
    "long_holding_months": (2, 6),
    "risk_pct": (0.002, 0.02),
    "max_position_pct": (0.05, 0.25),
    "min_turnover": (0.0, 5_000_000_000.0),
    "max_spread_pct": (0.001, 0.05),
    "max_total_exposure_pct": (0.10, 1.0),
    "max_position_symbols": (1, 20),
}

PARAMETER_RANGES = SWING_PARAMETER_RANGES


SCOUT_PARAMETER_RANGES: dict[str, tuple[float, float]] = {
    "min_turnover": (0.0, 5_000_000_000.0),
    "max_spread_pct": (0.001, 0.05),
    "daytrade_change_min": (-0.10, 0.05),
    "daytrade_change_max": (0.01, 0.10),
    "swing_change_min": (-0.10, 0.05),
    "swing_change_max": (0.01, 0.10),
    "liquidity_weight": (0.0, 1.0),
    "momentum_weight": (0.0, 1.0),
    "spread_weight": (0.0, 1.0),
    "max_candidates_daytrade": (1, 20),
    "max_candidates_swing": (1, 20),
}


DAYTRADE_PARAMETER_RANGES: dict[str, tuple[float, float]] = {
    "stop_loss_pct": (0.003, 0.05),
    "take_profit_pct": (0.003, 0.08),
    "risk_pct": (0.001, 0.01),
    "max_position_pct": (0.02, 0.40),
    "max_daily_loss_pct": (0.005, 0.05),
    "order_ttl_minutes": (1, 30),
    "max_spread_pct": (0.001, 0.03),
    "max_quote_age_seconds": (10, 180),
    "reentry_cooldown_minutes": (0, 120),
    "consecutive_loss_stop": (1, 10),
}

MISSING_DEPTH_POLICIES = {"block", "block_unless_limit_state_explained", "allow_review_only"}
LIMIT_UP_POLICIES = {"avoid_new_entries", "allow_only_if_depth_valid", "review_only"}
LIMIT_DOWN_POLICIES = {"avoid_new_entries", "allow_exit_only", "review_only"}
ELIGIBLE_LIST_POLICIES = {"strict", "warn_and_commonstock_fallback", "disable_daytrade_candidates"}


@dataclass(frozen=True)
class ScoutStrategyParams:
    min_turnover: float
    max_spread_pct: float
    daytrade_change_min: float
    daytrade_change_max: float
    swing_change_min: float
    swing_change_max: float
    liquidity_weight: float
    momentum_weight: float
    spread_weight: float
    limit_up_policy: str
    limit_down_policy: str
    missing_depth_policy: str
    max_candidates_daytrade: int
    max_candidates_swing: int
    eligible_list_policy: str

    def to_json(self) -> dict[str, float | int | str]:
        return {
            "min_turnover": self.min_turnover,
            "max_spread_pct": self.max_spread_pct,
            "daytrade_change_min": self.daytrade_change_min,
            "daytrade_change_max": self.daytrade_change_max,
            "swing_change_min": self.swing_change_min,
            "swing_change_max": self.swing_change_max,
            "liquidity_weight": self.liquidity_weight,
            "momentum_weight": self.momentum_weight,
            "spread_weight": self.spread_weight,
            "limit_up_policy": self.limit_up_policy,
            "limit_down_policy": self.limit_down_policy,
            "missing_depth_policy": self.missing_depth_policy,
            "max_candidates_daytrade": self.max_candidates_daytrade,
            "max_candidates_swing": self.max_candidates_swing,
            "eligible_list_policy": self.eligible_list_policy,
        }


@dataclass(frozen=True)
class DaytradeStrategyParams:
    stop_loss_pct: float
    take_profit_pct: float
    risk_pct: float
    max_position_pct: float
    max_daily_loss_pct: float
    entry_start_time: str
    entry_end_time: str
    force_exit_time: str
    order_ttl_minutes: int
    max_spread_pct: float
    max_quote_age_seconds: int
    missing_depth_policy: str
    reentry_cooldown_minutes: int
    consecutive_loss_stop: int
    allow_limit_up_entry: bool
    allow_limit_down_entry: bool

    def to_json(self) -> dict[str, float | int | str | bool]:
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "risk_pct": self.risk_pct,
            "max_position_pct": self.max_position_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "entry_start_time": self.entry_start_time,
            "entry_end_time": self.entry_end_time,
            "force_exit_time": self.force_exit_time,
            "order_ttl_minutes": self.order_ttl_minutes,
            "max_spread_pct": self.max_spread_pct,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "missing_depth_policy": self.missing_depth_policy,
            "reentry_cooldown_minutes": self.reentry_cooldown_minutes,
            "consecutive_loss_stop": self.consecutive_loss_stop,
            "allow_limit_up_entry": self.allow_limit_up_entry,
            "allow_limit_down_entry": self.allow_limit_down_entry,
        }


@dataclass(frozen=True)
class SwingStrategyParams:
    stop_loss_pct: float
    take_profit_pct_short: float
    take_profit_pct_long: float
    long_holding_months: int
    risk_pct: float
    max_position_pct: float
    min_turnover: float
    max_spread_pct: float
    max_total_exposure_pct: float = 0.80
    max_position_symbols: int = 5

    def to_json(self) -> dict[str, float | int]:
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct_short": self.take_profit_pct_short,
            "take_profit_pct_long": self.take_profit_pct_long,
            "long_holding_months": self.long_holding_months,
            "risk_pct": self.risk_pct,
            "max_position_pct": self.max_position_pct,
            "min_turnover": self.min_turnover,
            "max_spread_pct": self.max_spread_pct,
            "max_total_exposure_pct": self.max_total_exposure_pct,
            "max_position_symbols": self.max_position_symbols,
        }


@dataclass(frozen=True)
class SwingReviewDecision:
    should_create_version: bool
    summary: str
    discussion: str
    rules_text: str
    expected_effect: str
    risk_note: str
    no_change_reason: str
    params: SwingStrategyParams
    validation_error: str = ""


def default_swing_params() -> SwingStrategyParams:
    return swing_params_from_json(DEFAULT_SWING_PARAMS)


def default_scout_params() -> ScoutStrategyParams:
    return scout_params_from_json(DEFAULT_SCOUT_PARAMS)


def default_daytrade_params() -> DaytradeStrategyParams:
    return daytrade_params_from_json(DEFAULT_DAYTRADE_PARAMS)


def default_swing_rules_text() -> str:
    return "短線 v1：以最佳買一掛買，停損 6%，持有 3 個月以上停利 10%，較短持有停利 8%，單筆風險 1%。"


def default_scout_rules_text() -> str:
    return "抓盤 v1：依流動性、動能、價差加權排序，避開漲跌停與五檔品質不佳標的。"


def default_daytrade_rules_text() -> str:
    return "當沖 v1：一般交易時段內依 active params 控制停損、停利、單筆風險、價差與報價新鮮度。"


def scout_params_from_json(value: dict[str, Any] | None) -> ScoutStrategyParams:
    data = dict(DEFAULT_SCOUT_PARAMS)
    if value:
        for key in DEFAULT_SCOUT_PARAMS:
            if key in value:
                data[key] = value[key]
    return ScoutStrategyParams(
        min_turnover=float(data["min_turnover"]),
        max_spread_pct=float(data["max_spread_pct"]),
        daytrade_change_min=float(data["daytrade_change_min"]),
        daytrade_change_max=float(data["daytrade_change_max"]),
        swing_change_min=float(data["swing_change_min"]),
        swing_change_max=float(data["swing_change_max"]),
        liquidity_weight=float(data["liquidity_weight"]),
        momentum_weight=float(data["momentum_weight"]),
        spread_weight=float(data["spread_weight"]),
        limit_up_policy=str(data["limit_up_policy"]),
        limit_down_policy=str(data["limit_down_policy"]),
        missing_depth_policy=str(data["missing_depth_policy"]),
        max_candidates_daytrade=int(data["max_candidates_daytrade"]),
        max_candidates_swing=int(data["max_candidates_swing"]),
        eligible_list_policy=str(data["eligible_list_policy"]),
    )


def daytrade_params_from_json(value: dict[str, Any] | None) -> DaytradeStrategyParams:
    data = dict(DEFAULT_DAYTRADE_PARAMS)
    if value:
        for key in DEFAULT_DAYTRADE_PARAMS:
            if key in value:
                data[key] = value[key]
    return DaytradeStrategyParams(
        stop_loss_pct=float(data["stop_loss_pct"]),
        take_profit_pct=float(data["take_profit_pct"]),
        risk_pct=float(data["risk_pct"]),
        max_position_pct=float(data["max_position_pct"]),
        max_daily_loss_pct=float(data["max_daily_loss_pct"]),
        entry_start_time=str(data["entry_start_time"]),
        entry_end_time=str(data["entry_end_time"]),
        force_exit_time=str(data["force_exit_time"]),
        order_ttl_minutes=int(data["order_ttl_minutes"]),
        max_spread_pct=float(data["max_spread_pct"]),
        max_quote_age_seconds=int(data["max_quote_age_seconds"]),
        missing_depth_policy=str(data["missing_depth_policy"]),
        reentry_cooldown_minutes=int(data["reentry_cooldown_minutes"]),
        consecutive_loss_stop=int(data["consecutive_loss_stop"]),
        allow_limit_up_entry=bool(data["allow_limit_up_entry"]),
        allow_limit_down_entry=bool(data["allow_limit_down_entry"]),
    )


def swing_params_from_json(value: dict[str, Any] | None) -> SwingStrategyParams:
    data = dict(DEFAULT_SWING_PARAMS)
    if value:
        for key in DEFAULT_SWING_PARAMS:
            if key in value:
                data[key] = value[key]
    return SwingStrategyParams(
        stop_loss_pct=float(data["stop_loss_pct"]),
        take_profit_pct_short=float(data["take_profit_pct_short"]),
        take_profit_pct_long=float(data["take_profit_pct_long"]),
        long_holding_months=int(data["long_holding_months"]),
        risk_pct=float(data["risk_pct"]),
        max_position_pct=float(data["max_position_pct"]),
        min_turnover=float(data["min_turnover"]),
        max_spread_pct=float(data["max_spread_pct"]),
        max_total_exposure_pct=float(data["max_total_exposure_pct"]),
        max_position_symbols=int(data["max_position_symbols"]),
    )


def validate_swing_params(params: SwingStrategyParams) -> str:
    values = params.to_json()
    for key, (minimum, maximum) in PARAMETER_RANGES.items():
        value = float(values[key])
        if value < minimum or value > maximum:
            return f"{key} 超出允許範圍 {minimum:g}-{maximum:g}"
    if params.take_profit_pct_long < params.take_profit_pct_short:
        return "take_profit_pct_long 不可低於 take_profit_pct_short"
    return ""


def validate_scout_params(params: ScoutStrategyParams) -> str:
    values = params.to_json()
    for key, (minimum, maximum) in SCOUT_PARAMETER_RANGES.items():
        value = float(values[key])
        if value < minimum or value > maximum:
            return f"{key} 超出允許範圍 {minimum:g}-{maximum:g}"
    if params.daytrade_change_min >= params.daytrade_change_max:
        return "daytrade_change_min 必須小於 daytrade_change_max"
    if params.swing_change_min >= params.swing_change_max:
        return "swing_change_min 必須小於 swing_change_max"
    total_weight = params.liquidity_weight + params.momentum_weight + params.spread_weight
    if abs(total_weight - 1.0) > 0.001:
        return "scout 權重總和必須等於 1"
    if params.limit_up_policy not in LIMIT_UP_POLICIES:
        return "limit_up_policy 不在允許清單"
    if params.limit_down_policy not in LIMIT_DOWN_POLICIES:
        return "limit_down_policy 不在允許清單"
    if params.missing_depth_policy not in MISSING_DEPTH_POLICIES:
        return "missing_depth_policy 不在允許清單"
    if params.eligible_list_policy not in ELIGIBLE_LIST_POLICIES:
        return "eligible_list_policy 不在允許清單"
    return ""


def validate_daytrade_params(params: DaytradeStrategyParams) -> str:
    values = params.to_json()
    for key, (minimum, maximum) in DAYTRADE_PARAMETER_RANGES.items():
        value = float(values[key])
        if value < minimum or value > maximum:
            return f"{key} 超出允許範圍 {minimum:g}-{maximum:g}"
    if params.missing_depth_policy not in MISSING_DEPTH_POLICIES:
        return "missing_depth_policy 不在允許清單"
    time_error = _validate_ordered_times(params.entry_start_time, params.entry_end_time, params.force_exit_time)
    if time_error:
        return time_error
    return ""


def default_params_for_strategy(strategy: str) -> dict[str, Any]:
    if strategy == SCOUT_STRATEGY:
        return default_scout_params().to_json()
    if strategy == DAYTRADE_STRATEGY:
        return default_daytrade_params().to_json()
    if strategy == SWING_STRATEGY:
        return default_swing_params().to_json()
    raise KeyError(f"unknown strategy: {strategy}")


def default_rules_for_strategy(strategy: str) -> str:
    if strategy == SCOUT_STRATEGY:
        return default_scout_rules_text()
    if strategy == DAYTRADE_STRATEGY:
        return default_daytrade_rules_text()
    if strategy == SWING_STRATEGY:
        return default_swing_rules_text()
    raise KeyError(f"unknown strategy: {strategy}")


def validate_strategy_params(strategy: str, params: dict[str, Any]) -> str:
    try:
        if strategy == SCOUT_STRATEGY:
            return validate_scout_params(scout_params_from_json(params))
        if strategy == DAYTRADE_STRATEGY:
            return validate_daytrade_params(daytrade_params_from_json(params))
        if strategy == SWING_STRATEGY:
            return validate_swing_params(swing_params_from_json(params))
    except (TypeError, ValueError) as exc:
        return f"參數型別錯誤：{exc}"
    return f"未知策略：{strategy}"


def build_swing_review_decision(payload: dict[str, Any], current_params: SwingStrategyParams) -> SwingReviewDecision:
    should_create = bool(payload.get("should_create_version", False))
    summary = str(payload.get("summary", "")).strip()
    discussion = str(payload.get("discussion", "")).strip()
    rules_text = str(payload.get("rules_text", "")).strip()
    expected_effect = str(payload.get("expected_effect", "")).strip()
    risk_note = str(payload.get("risk_note", "")).strip()
    no_change_reason = str(payload.get("no_change_reason", "")).strip()
    changes = payload.get("parameter_changes") or {}
    if not isinstance(changes, dict):
        return SwingReviewDecision(
            should_create,
            summary,
            discussion,
            rules_text,
            expected_effect,
            risk_note,
            no_change_reason,
            current_params,
            "parameter_changes 必須是物件",
        )
    unknown = sorted(set(changes) - set(DEFAULT_SWING_PARAMS))
    if unknown:
        return SwingReviewDecision(
            should_create,
            summary,
            discussion,
            rules_text,
            expected_effect,
            risk_note,
            no_change_reason,
            current_params,
            "包含不允許的參數：" + ", ".join(unknown),
        )
    changes = {key: value for key, value in changes.items() if value is not None}
    updated = current_params.to_json()
    updated.update(changes)
    try:
        params = swing_params_from_json(updated)
    except (TypeError, ValueError) as exc:
        return SwingReviewDecision(
            should_create,
            summary,
            discussion,
            rules_text,
            expected_effect,
            risk_note,
            no_change_reason,
            current_params,
            f"參數型別錯誤：{exc}",
        )
    error = validate_swing_params(params)
    return SwingReviewDecision(
        should_create,
        summary,
        discussion,
        rules_text,
        expected_effect,
        risk_note,
        no_change_reason,
        params,
        error,
    )


def _validate_ordered_times(entry_start: str, entry_end: str, force_exit: str) -> str:
    try:
        start_minutes = _parse_hhmm(entry_start)
        end_minutes = _parse_hhmm(entry_end)
        exit_minutes = _parse_hhmm(force_exit)
    except ValueError as exc:
        return str(exc)
    if not start_minutes < end_minutes < exit_minutes:
        return "entry_start_time < entry_end_time < force_exit_time 必須成立"
    return ""


def _parse_hhmm(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"時間格式必須為 HH:MM：{value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"時間格式必須為 HH:MM：{value}")
    return hour * 60 + minute
