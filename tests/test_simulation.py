from datetime import datetime, timedelta, timezone

from tw_watchdesk.simulation import (
    DAYTRADE_SELL_TAX_RATE,
    calculate_costs,
    risk_check_for_buy,
    session_window,
    should_fill,
)
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore


def test_daytrade_sell_cost_uses_discount_tax() -> None:
    result = calculate_costs(side="sell", strategy="daytrade", price=100, qty=1000, avg_cost=99, at=datetime(2026, 7, 2).date())

    assert result.tax == 100_000 * DAYTRADE_SELL_TAX_RATE
    assert result.net_cash_delta < 100_000
    assert result.realized_pnl > 0


def test_daytrade_session_forces_review_at_market_close() -> None:
    at_1329 = session_window(datetime(2026, 7, 2, 5, 29, tzinfo=timezone.utc))
    at_1330 = session_window(datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc))
    at_1331 = session_window(datetime(2026, 7, 2, 5, 31, tzinfo=timezone.utc))

    assert at_1329.can_open_daytrade is False
    assert at_1329.daytrade_exit_only is True
    assert at_1329.should_review is False
    assert at_1330.can_open_daytrade is False
    assert at_1330.daytrade_exit_only is True
    assert at_1330.should_review is True
    assert at_1331.daytrade_exit_only is False
    assert at_1331.should_review is True


def test_conservative_fill_requires_later_bar(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    created_at = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="test",
        expires_at=created_at + timedelta(minutes=5),
        created_at=created_at,
    )
    same_start = created_at.replace(second=0, microsecond=0)
    store.upsert_bar(
        symbol="2330",
        timeframe_minutes=5,
        start_time=same_start,
        end_time=same_start + timedelta(minutes=5),
        price=99,
        volume=1000,
    )
    later_start = same_start + timedelta(minutes=5)
    store.upsert_bar(
        symbol="2330",
        timeframe_minutes=5,
        start_time=later_start,
        end_time=later_start + timedelta(minutes=5),
        price=99,
        volume=1000,
    )

    order = store.list_open_orders()[0]
    bars = store.get_bars_after("2330", 5, created_at - timedelta(minutes=1))

    assert should_fill(order, bars[0]) is False
    assert should_fill(order, bars[1]) is True


def test_risk_blocks_daytrade_daily_loss(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    account = store.get_account(DAYTRADE_ACCOUNT)
    now = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        qty=1000,
        price=120,
        fee=0,
        realized_pnl=0,
        stop_loss=None,
        take_profit=None,
        at=now - timedelta(minutes=10),
    )
    order_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="sell",
        price=100,
        qty=1000,
        reason="loss",
        expires_at=now,
        created_at=now,
    )
    order = store.list_open_orders()[0]
    store.record_fill(order=order, price=100, qty=1000, fee=0, tax=0, net_cash_delta=100_000, realized_pnl=-account.capital * 0.02, filled_at=now)
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="sell",
        qty=1000,
        price=100,
        fee=0,
        realized_pnl=-account.capital * 0.02,
        stop_loss=None,
        take_profit=None,
        at=now,
    )

    check = risk_check_for_buy(
        store=store,
        account=account,
        strategy="daytrade",
        symbol="2317",
        price=50,
        qty=1000,
        stop_loss=49,
        now=now,
    )

    assert order_id > 0
    assert check.allowed is False
    assert "2%" in check.reason


def test_risk_uses_available_cash_after_reserved_orders(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=900,
        qty=1000,
        reason="large reserve",
        expires_at=now + timedelta(minutes=5),
        created_at=now,
    )
    account = store.get_account(DAYTRADE_ACCOUNT)

    check = risk_check_for_buy(
        store=store,
        account=account,
        strategy="daytrade",
        symbol="2317",
        price=100,
        qty=1000,
        stop_loss=99,
        now=now,
    )

    assert check.allowed is False
    assert check.reason == "可用現金不足"


def test_risk_blocks_swing_more_than_five_symbols(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    for symbol in ("1101", "1216", "1301", "2303", "2330"):
        store.upsert_position_after_fill(
            account_id=SWING_ACCOUNT,
            strategy="swing",
            symbol=symbol,
            side="buy",
            qty=1000,
            price=50,
            fee=71.25,
            realized_pnl=0,
            stop_loss=47,
            take_profit=55,
            at=now,
        )
    account = store.get_account(SWING_ACCOUNT)

    check = risk_check_for_buy(
        store=store,
        account=account,
        strategy="swing",
        symbol="2454",
        price=50,
        qty=1000,
        stop_loss=47,
        now=now,
    )

    assert check.allowed is False
    assert "5 檔" in check.reason
