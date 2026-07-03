from datetime import datetime, timedelta, timezone

from tw_watchdesk.config import Settings
from tw_watchdesk.models import OrderBookLevel, Quote
from tw_watchdesk.strategy import build_watch_state
from tw_watchdesk.strategy_versions import SwingStrategyParams, build_swing_review_decision


def _quote(now: datetime, *, delta: timedelta = timedelta(seconds=2)) -> Quote:
    exchange_time = now - delta
    return Quote(
        symbol="2330",
        name="台積電",
        price=100.0,
        previous_close=98.0,
        volume=20000,
        turnover=2_000_000_000,
        bid_levels=[OrderBookLevel(price=99.9, size=20000)],
        ask_levels=[OrderBookLevel(price=100.0, size=18000)],
        exchange_time=exchange_time,
        received_at=now,
        source="taishin_nova",
        is_realtime=True,
        flags={},
    )


def test_swing_creates_round_lot_advice() -> None:
    now = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    state = build_watch_state(Settings(stale_seconds=70), "2330", 1_000_000, "swing", 3, _quote(now), now=now)

    assert state.status == "ok"
    assert state.advice.action == "buy"
    assert state.advice.qty % 1000 == 0
    assert state.advice.buy_price == 99.9


def test_swing_uses_strategy_version_parameters() -> None:
    now = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    params = SwingStrategyParams(
        stop_loss_pct=0.04,
        take_profit_pct_short=0.07,
        take_profit_pct_long=0.15,
        long_holding_months=3,
        risk_pct=0.005,
        max_position_pct=0.10,
        min_turnover=0,
        max_spread_pct=0.02,
    )

    state = build_watch_state(Settings(stale_seconds=70), "2330", 1_000_000, "swing", 3, _quote(now), now=now, swing_params=params, strategy_version="swing-v2")

    assert state.status == "ok"
    assert state.advice.strategy_version == "swing-v2"
    assert state.advice.max_notional == 100_000
    assert state.advice.stop_loss == round(99.9 * 0.96, 2)
    assert state.advice.take_profit == round(99.9 * 1.15, 2)
    assert "swing-v2" in state.advice.reason


def test_swing_version_liquidity_gate_blocks_advice() -> None:
    now = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    params = SwingStrategyParams(
        stop_loss_pct=0.06,
        take_profit_pct_short=0.08,
        take_profit_pct_long=0.10,
        long_holding_months=3,
        risk_pct=0.01,
        max_position_pct=0.25,
        min_turnover=3_000_000_000,
        max_spread_pct=0.02,
    )

    state = build_watch_state(Settings(stale_seconds=70), "2330", 1_000_000, "swing", 3, _quote(now), now=now, swing_params=params, strategy_version="swing-v2")

    assert state.status == "blocked"
    assert "成交值低於流動性門檻" in state.advice.reason


def test_swing_review_decision_ignores_null_parameter_changes() -> None:
    current = SwingStrategyParams(
        stop_loss_pct=0.06,
        take_profit_pct_short=0.08,
        take_profit_pct_long=0.10,
        long_holding_months=3,
        risk_pct=0.01,
        max_position_pct=0.25,
        min_turnover=0,
        max_spread_pct=0.02,
    )
    payload = {
        "summary": "收斂價差條件",
        "discussion": "只調整價差，其他參數維持不變。",
        "should_create_version": True,
        "parameter_changes": {
            "stop_loss_pct": None,
            "take_profit_pct_short": None,
            "take_profit_pct_long": None,
            "long_holding_months": None,
            "risk_pct": None,
            "max_position_pct": None,
            "min_turnover": None,
            "max_spread_pct": 0.015,
        },
        "rules_text": "價差收斂才進場。",
        "expected_effect": "減少掛單品質不佳。",
        "risk_note": "仍受硬風控限制。",
        "no_change_reason": "",
    }

    decision = build_swing_review_decision(payload, current)

    assert decision.validation_error == ""
    assert decision.params.stop_loss_pct == 0.06
    assert decision.params.max_spread_pct == 0.015


def test_stale_quote_blocks_advice() -> None:
    now = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    state = build_watch_state(
        Settings(stale_seconds=70),
        "2330",
        1_000_000,
        "swing",
        3,
        _quote(now, delta=timedelta(seconds=90)),
        now=now,
    )

    assert state.status == "blocked"
    assert state.advice.qty == 0
    assert any("交易所時間超過 70 秒" in reason for reason in state.quality.reasons)


def test_limit_up_missing_asks_quality_reason_is_explained() -> None:
    now = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    quote = Quote(
        symbol="1718",
        name="中纖",
        price=14.95,
        previous_close=13.6,
        volume=100_000,
        turnover=100_000_000,
        bid_levels=[OrderBookLevel(price=14.95, size=20)],
        ask_levels=[],
        exchange_time=now,
        received_at=now,
        source="taishin_nova",
        is_realtime=True,
        flags={"missing_asks": True},
    )

    state = build_watch_state(Settings(stale_seconds=70), "1718", 1_000_000, "swing", 3, quote, now=now)

    assert state.status == "blocked"
    assert "疑似漲停無賣盤" in state.advice.reason
    assert state.quality.flags["likely_limit_up_no_asks"] is True


def test_received_stale_quality_reason_is_separate_from_exchange_time() -> None:
    now = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    quote = _quote(now, delta=timedelta(seconds=2))
    quote = Quote(
        **{
            **quote.__dict__,
            "received_at": now - timedelta(seconds=90),
        }
    )

    state = build_watch_state(Settings(stale_seconds=70), "2330", 1_000_000, "swing", 3, quote, now=now)

    assert state.status == "blocked"
    assert "本機超過 70 秒未收到報價" in state.advice.reason


def test_daytrade_allows_missing_or_empty_eligible_symbol_file(tmp_path) -> None:
    now = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    state = build_watch_state(Settings(stale_seconds=70), "2330", 1_000_000, "daytrade", None, _quote(now), now=now)
    assert state.status == "ok"
    assert state.advice.action == "buy"
    assert "未使用本機當沖資格清單" in state.advice.reason

    empty = tmp_path / "empty.txt"
    empty.write_text("# empty\n", encoding="utf-8")
    empty_allowed = build_watch_state(Settings(stale_seconds=70, daytrade_eligible_symbols_file=empty), "2330", 1_000_000, "daytrade", None, _quote(now), now=now)
    assert empty_allowed.status == "ok"
    assert "本機當沖資格清單空白" in empty_allowed.advice.reason


def test_daytrade_eligible_symbol_file_restricts_when_present(tmp_path) -> None:
    now = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    blocked_path = tmp_path / "blocked.txt"
    blocked_path.write_text("2317\n", encoding="utf-8")
    blocked = build_watch_state(Settings(stale_seconds=70, daytrade_eligible_symbols_file=blocked_path), "2330", 1_000_000, "daytrade", None, _quote(now), now=now)
    assert blocked.status == "blocked"
    assert "不在當沖資格清單內" in blocked.advice.reason

    path = tmp_path / "eligible.txt"
    path.write_text("2330\n", encoding="utf-8")
    settings = Settings(stale_seconds=70, daytrade_eligible_symbols_file=path)
    allowed = build_watch_state(settings, "2330", 1_000_000, "daytrade", None, _quote(now), now=now)
    assert allowed.status == "ok"
