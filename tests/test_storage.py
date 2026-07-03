from datetime import datetime, timedelta, timezone

import pytest

from tw_watchdesk.quote_diagnostics import QuoteDiagnostic
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, TradingStore
from tw_watchdesk.strategy_versions import FOLLOW_LATEST, MANUAL_LOCK


def test_store_initializes_accounts_and_candidates(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()

    accounts = store.list_accounts()
    assert {account.id for account in accounts} == {"daytrade", "swing"}

    candidate_id = store.upsert_candidate(
        trade_date="2026-07-02",
        strategy="daytrade",
        symbol="2330",
        score=88,
        reason="test",
        source="unit",
    )
    assert candidate_id > 0
    assert store.list_candidates("2026-07-02", "daytrade")[0].symbol == "2330"


def test_capital_event_updates_cash_and_capital(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()

    before = store.get_account(DAYTRADE_ACCOUNT)
    store.apply_capital_event(DAYTRADE_ACCOUNT, 50_000, "test add")
    after = store.get_account(DAYTRADE_ACCOUNT)

    assert after.cash == before.cash + 50_000
    assert after.capital == before.capital + 50_000


def test_buy_order_reserves_cash_and_expiry_releases_it(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)

    store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="reserve",
        expires_at=now,
        created_at=now,
    )

    reserved = store.get_account(DAYTRADE_ACCOUNT).reserved_cash
    assert reserved == 100_142.5
    assert store.expire_orders(now.replace(minute=11)) == 1
    assert store.get_account(DAYTRADE_ACCOUNT).reserved_cash == 0


def test_buy_order_fill_releases_reserved_cash_and_duplicate_open_is_blocked(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="reserve",
        expires_at=now,
        created_at=now,
    )
    with pytest.raises(ValueError, match="未完成委託"):
        store.create_order(
            account_id=DAYTRADE_ACCOUNT,
            strategy="daytrade",
            symbol="2330",
            side="buy",
            price=100,
            qty=1000,
            reason="duplicate",
            expires_at=now,
            created_at=now,
        )

    order = store.list_open_orders()[0]
    store.record_fill(order=order, price=100, qty=1000, fee=142.5, tax=0, net_cash_delta=-100_142.5, realized_pnl=0, filled_at=now)
    account = store.get_account(DAYTRADE_ACCOUNT)
    assert account.reserved_cash == 0
    assert account.cash == 899_857.5


def test_capital_withdraw_cannot_drop_below_reserved_cash(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="reserve",
        expires_at=now,
        created_at=now,
    )

    with pytest.raises(ValueError, match="凍結金額"):
        store.apply_capital_event(DAYTRADE_ACCOUNT, -950_000, "too much")


def test_order_fill_persists_position(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    order_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="test",
        expires_at=now,
        stop_loss=98,
        take_profit=103,
        created_at=now,
    )
    order = store.list_open_orders()[0]
    store.record_fill(order=order, price=100, qty=1000, fee=142.5, tax=0, net_cash_delta=-100_142.5, realized_pnl=0, filled_at=now)
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        qty=1000,
        price=100,
        fee=142.5,
        realized_pnl=0,
        stop_loss=98,
        take_profit=103,
        at=now,
    )

    assert order_id > 0
    assert store.get_position(DAYTRADE_ACCOUNT, "2330").qty == 1000


def test_stock_names_are_available_for_orders_fills_and_positions(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    candidate_id = store.upsert_candidate(
        trade_date="2026-07-02",
        strategy="daytrade",
        symbol="2330",
        name="台積電",
        score=88,
        reason="test",
        source="unit",
        created_at=now,
    )
    store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="test",
        expires_at=now,
        candidate_id=candidate_id,
        created_at=now,
    )

    order = store.list_open_orders()[0]
    assert store.list_orders()[0]["stock_name"] == "台積電"
    store.record_fill(order=order, price=100, qty=1000, fee=142.5, tax=0, net_cash_delta=-100_142.5, realized_pnl=0, filled_at=now)
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        qty=1000,
        price=100,
        fee=142.5,
        realized_pnl=0,
        stop_loss=None,
        take_profit=None,
        at=now,
    )

    assert store.list_fills()[0]["stock_name"] == "台積電"
    assert store.list_positions()[0].name == "台積電"
    assert store.stock_name_map()["2330"] == "台積電"


def test_monitor_events_filter_and_persist(tmp_path) -> None:
    db_path = tmp_path / "lab.sqlite3"
    store = TradingStore(db_path)
    store.initialize()
    created_at = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)

    event_id = store.add_monitor_event(
        actor="scout",
        phase="scouting",
        event_type="candidate_added",
        title="抓盤手加入候選",
        detail="unit test",
        trade_date="2026-07-02",
        strategy="daytrade",
        symbol="2330",
        created_at=created_at,
    )
    store.add_monitor_event(
        actor="risk_manager",
        phase="risk_check",
        event_type="risk_blocked",
        title="風控阻擋買單",
        severity="warning",
        trade_date="2026-07-02",
        strategy="swing",
        symbol="2317",
        created_at=created_at,
    )

    rows = store.list_monitor_events(trade_date="2026-07-02", actor="risk_manager", min_severity="warning", symbol="2317")
    assert len(rows) == 1
    assert rows[0]["title"] == "風控阻擋買單"
    store.close()

    reopened = TradingStore(db_path)
    reopened.initialize()
    persisted = reopened.list_monitor_events(strategy="daytrade")
    assert persisted[0]["id"] == event_id
    assert persisted[0]["symbol"] == "2330"


def test_strategy_versions_initialize_and_manual_lock_persists(tmp_path) -> None:
    db_path = tmp_path / "lab.sqlite3"
    store = TradingStore(db_path)
    store.initialize()

    assert store.get_active_strategy_version("scout").version == "scout-v1"
    assert store.get_active_strategy_version("daytrade").version == "daytrade-v1"
    active = store.get_active_strategy_version("swing")
    assert active.version == "swing-v1"
    assert active.params["stop_loss_pct"] == 0.06
    assert store.get_strategy_version_state("swing").mode == FOLLOW_LATEST

    new_version = store.create_strategy_version(
        strategy="swing",
        params={**active.params, "stop_loss_pct": 0.055},
        rules_text="unit v2",
        discussion="unit discussion",
        summary="unit summary",
    )
    assert new_version.version == "swing-v2"
    assert store.get_active_strategy_version("swing").version == "swing-v2"

    store.set_strategy_version_state("swing", "swing-v1", MANUAL_LOCK)
    assert store.get_strategy_version_state("swing").active_version == "swing-v1"
    assert store.get_strategy_version_state("swing").mode == MANUAL_LOCK
    store.close()

    reopened = TradingStore(db_path)
    reopened.initialize()
    assert [version.version for version in reopened.list_strategy_versions("swing", limit=None)] == ["swing-v2", "swing-v1"]
    assert reopened.get_strategy_version_state("swing").active_version == "swing-v1"


def test_review_run_lease_blocks_duplicate_until_release_or_expiry(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)

    assert store.acquire_review_run_lease(run_key="unit", owner="a", ttl_seconds=60, now=now) is True
    assert store.acquire_review_run_lease(run_key="unit", owner="b", ttl_seconds=60, now=now + timedelta(seconds=10)) is False
    assert store.acquire_review_run_lease(run_key="unit", owner="b", ttl_seconds=60, now=now + timedelta(seconds=61)) is True
    store.release_review_run_lease(run_key="unit", owner="b")
    assert store.acquire_review_run_lease(run_key="unit", owner="c", ttl_seconds=60, now=now + timedelta(seconds=62)) is True


def test_quote_diagnostic_persists_structured_reason(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    at = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    diagnostic = QuoteDiagnostic(
        symbol="1718",
        price=14.95,
        previous_close=13.6,
        limit_up=14.95,
        limit_down=12.25,
        best_bid=14.95,
        best_ask=None,
        bid_count=1,
        ask_count=0,
        exchange_time=at,
        received_at=at,
        exchange_age_seconds=1,
        receive_age_seconds=1,
        flags={"likely_limit_up_no_asks": True, "missing_asks": True},
        diagnosis="疑似漲停無賣盤",
        event_type="quote_limit_state_detected",
        title="疑似漲停無賣盤",
        payload_shape={"ask_levels": 0, "bid_levels": 1},
    )

    diagnostic_id = store.insert_quote_diagnostic(diagnostic=diagnostic, strategy="daytrade", trade_date="2026-07-03", created_at=at)
    row = store.list_quote_diagnostics(trade_date="2026-07-03", symbol="1718")[0]

    assert diagnostic_id > 0
    assert row["diagnosis"] == "疑似漲停無賣盤"
    assert row["ask_count"] == 0
    assert "likely_limit_up_no_asks" in row["flags_json"]


def test_monitor_and_llm_storage_redacts_sensitive_values(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()

    store.add_monitor_event(
        actor="system",
        phase="test",
        event_type="sensitive",
        title="sensitive",
        detail="Authorization=Bearer abcdefghijklmnopqrstuvwxyz123456 nationalId=A123456789 pin=1234",
        metrics={"authToken": "secret-token-value", "safe": "ok"},
    )
    store.add_llm_decision(
        strategy="daytrade",
        decision_type="test",
        response={"token": "secret-token-value", "summary": "ok"},
        status="error",
        error="realtimeToken=abcdefghijklmnopqrstuvwxyz123456",
    )
    store.upsert_daily_review(
        "2026-07-03",
        "daytrade",
        "summary token=abcdefghijklmnopqrstuvwxyz123456",
        {"account": "1234567"},
        llm_summary="ok",
        llm_discussion="nationalId=A123456789",
        llm_result={"pin": "1234"},
    )

    event = store.list_monitor_events(limit=1)[0]
    decision = store._conn.execute("SELECT * FROM llm_decisions ORDER BY id DESC LIMIT 1").fetchone()
    review = store.list_daily_reviews(limit=1)[0]

    combined = "\n".join(
        [
            event["detail"],
            event["metrics_json"],
            decision["response_json"],
            decision["error"],
            review["summary"],
            review["metrics_json"],
            review["llm_discussion"],
            review["llm_result_json"],
        ]
    )
    assert "A123456789" not in combined
    assert "abcdefghijklmnopqrstuvwxyz123456" not in combined
    assert "secret-token-value" not in combined
    assert "[REDACTED" in combined
