import json
from datetime import datetime, timedelta, timezone

from tw_watchdesk.llm import CodexResult
from tw_watchdesk.config import Settings
from tw_watchdesk.models import OrderBookLevel, Quote
from tw_watchdesk.nova import parse_realtime_market_event
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore
from tw_watchdesk.worker import TradingLabWorker


class FakeProvider:
    def __init__(self, quote: Quote) -> None:
        self.quote = quote
        self.snapshot_rows: list[dict[str, object]] = []
        self.listeners = []
        self.market_data_subscriptions: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def get_quote(self, symbol: str) -> Quote:
        return self.quote

    def add_market_data_listener(self, listener) -> None:
        self.listeners.append(listener)

    def subscribe_market_data(self, symbols: list[str], channels: tuple[str, ...]) -> None:
        self.market_data_subscriptions.append((tuple(symbols), tuple(channels)))

    def emit_market_data(self, message: dict[str, object], received_at: datetime) -> None:
        event = parse_realtime_market_event(message, received_at=received_at)
        assert event is not None
        for listener in list(self.listeners):
            listener(event)

    def get_stock_rest_client(self):
        provider = self

        class Snapshot:
            def actives(self, **params):
                return {"data": provider.snapshot_rows}

            def movers(self, **params):
                return {"data": provider.snapshot_rows}

        class StockClient:
            snapshot = Snapshot()

        return StockClient()


class FakeLlmAdapter:
    def __init__(self, result: CodexResult) -> None:
        self.result = result
        self.calls = 0
        self.prompts: list[str] = []

    def run_json(self, prompt: str, schema: dict):
        self.calls += 1
        self.prompts.append(prompt)
        return self.result


def _quote(now: datetime, price: float = 100.0, symbol: str = "2330", name: str = "台積電") -> Quote:
    return Quote(
        symbol=symbol,
        name=name,
        price=price,
        previous_close=98,
        volume=10_000,
        turnover=1_000_000,
        bid_levels=[OrderBookLevel(price=99.9, size=1000)],
        ask_levels=[OrderBookLevel(price=100.0, size=1000)],
        exchange_time=now,
        received_at=now,
        source="fake",
        is_realtime=True,
        flags={},
    )


def _seed_swing_buy_fill(store: TradingStore, at: datetime, *, strategy_version: str = "swing-v1", symbol: str = "3707") -> None:
    order_id = store.create_order(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol=symbol,
        side="buy",
        price=80,
        qty=1000,
        reason="unit swing fill",
        expires_at=at + timedelta(minutes=30),
        strategy_version=strategy_version,
        created_at=at,
    )
    order = store.get_order(order_id)
    store.record_fill(order=order, price=80, qty=1000, fee=114, tax=0, net_cash_delta=-80_114, realized_pnl=0, filled_at=at + timedelta(seconds=3))
    store.upsert_position_after_fill(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol=symbol,
        side="buy",
        qty=1000,
        price=80,
        fee=114,
        realized_pnl=0,
        stop_loss=75,
        take_profit=90,
        strategy_version=strategy_version,
        at=at + timedelta(seconds=3),
    )


def _seed_daytrade_roundtrip(store: TradingStore, at: datetime) -> None:
    buy_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="unit daytrade buy",
        expires_at=at + timedelta(minutes=5),
        created_at=at,
    )
    buy_order = store.get_order(buy_id)
    store.record_fill(order=buy_order, price=100, qty=1000, fee=143, tax=0, net_cash_delta=-100_143, realized_pnl=0, filled_at=at + timedelta(seconds=1))
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        qty=1000,
        price=100,
        fee=143,
        realized_pnl=0,
        stop_loss=98,
        take_profit=103,
        at=at + timedelta(seconds=1),
    )
    sell_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="sell",
        price=102,
        qty=1000,
        reason="unit daytrade sell",
        expires_at=at + timedelta(hours=3),
        created_at=at + timedelta(hours=3),
    )
    sell_order = store.get_order(sell_id)
    store.record_fill(order=sell_order, price=102, qty=1000, fee=145, tax=153, net_cash_delta=101_702, realized_pnl=1_702, filled_at=at + timedelta(hours=3, seconds=1))
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="sell",
        qty=1000,
        price=102,
        fee=145,
        realized_pnl=1_702,
        stop_loss=None,
        take_profit=None,
        at=at + timedelta(hours=3, seconds=1),
    )


def _seed_attributed_position(store: TradingStore, *, account_id: str, strategy: str, symbol: str, at: datetime) -> tuple[int, int]:
    candidate_id = store.upsert_candidate(
        trade_date=at.date().isoformat(),
        strategy=strategy,
        symbol=symbol,
        name=f"{symbol} 測試股",
        score=75,
        reason="unit attributed candidate",
        source="auto_scout",
        scout_version="scout-v1",
        created_at=at,
    )
    order_id = store.create_order(
        account_id=account_id,
        strategy=strategy,
        symbol=symbol,
        side="buy",
        price=100,
        qty=1000,
        reason="unit attributed buy",
        expires_at=at + timedelta(minutes=5 if strategy == "daytrade" else 30),
        stop_loss=98,
        take_profit=103,
        candidate_id=candidate_id,
        created_at=at,
    )
    order = store.get_order(order_id)
    store.record_fill(order=order, price=100, qty=1000, fee=142.5, tax=0, net_cash_delta=-100_142.5, realized_pnl=0, filled_at=at + timedelta(seconds=1))
    store.upsert_position_after_fill(
        account_id=account_id,
        strategy=strategy,
        symbol=symbol,
        side="buy",
        qty=1000,
        price=100,
        fee=142.5,
        realized_pnl=0,
        stop_loss=98,
        take_profit=103,
        strategy_version=order.strategy_version,
        candidate_id=order.candidate_id,
        entry_order_id=order.id,
        scout_version=order.scout_version,
        attribution_status=order.attribution_status,
        at=at + timedelta(seconds=1),
    )
    return order_id, candidate_id


def _valid_swing_review_payload() -> dict[str, object]:
    return {
        "summary": "降低追價並收緊流動性條件",
        "discussion": "成交樣本顯示價差較大時容易掛單品質不佳，因此建立新版。",
        "should_create_version": True,
        "parameter_changes": {"stop_loss_pct": 0.055, "max_spread_pct": 0.015},
        "rules_text": "只做成交值足夠且價差收斂的短線標的。",
        "expected_effect": "減少流動性不佳造成的滑價。",
        "risk_note": "仍受既有風控硬限制約束。",
        "no_change_reason": "",
    }


def _valid_daytrade_review_payload() -> dict[str, object]:
    return {
        "summary": "當沖檢討完成",
        "mistakes": ["追價太快", "尾盤出場太晚"],
        "next_rules_to_test": ["連續虧損後停止新倉", "13:10 後只減倉"],
        "capital_suggestion": "維持資金，不加碼。",
    }


def test_worker_scout_does_not_add_current_watch_symbol(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 5, tzinfo=timezone.utc)
    worker = TradingLabWorker(settings=Settings(), store=store, provider=FakeProvider(_quote(now)))

    worker.run_tick(now)

    assert store.list_candidates("2026-07-02", "daytrade") == []
    assert store.list_candidates("2026-07-02", "swing") == []
    events = store.list_monitor_events(trade_date="2026-07-02", actor="scout")
    assert any(row["event_type"] == "scout_disabled" for row in events)
    assert not any(row["event_type"] == "candidate_added" for row in events)


def test_worker_auto_scout_adds_candidates_when_enabled(tmp_path) -> None:
    eligible = tmp_path / "eligible.txt"
    eligible.write_text("2330\n", encoding="utf-8")
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 5, tzinfo=timezone.utc)
    settings = Settings(enable_auto_scout=True, daytrade_eligible_symbols_file=eligible)
    provider = FakeProvider(_quote(now))
    provider.snapshot_rows = [
        {"symbol": "2330", "name": "台積電", "price": 100, "previousClose": 98, "volume": 100_000, "turnover": 500_000_000, "changePercent": 2.04}
    ]
    worker = TradingLabWorker(settings=settings, store=store, provider=provider)

    worker.run_tick(now)

    daytrade = store.list_candidates("2026-07-02", "daytrade")
    swing = store.list_candidates("2026-07-02", "swing")
    assert daytrade[0].symbol == "2330"
    assert daytrade[0].source == "auto_scout"
    assert swing[0].symbol == "2330"
    assert any(row["event_type"] == "scout_completed" for row in store.list_monitor_events(trade_date="2026-07-02", actor="scout"))


def test_worker_auto_scout_adds_daytrade_without_eligible_file(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 5, tzinfo=timezone.utc)
    provider = FakeProvider(_quote(now))
    provider.snapshot_rows = [
        {"symbol": "2330", "name": "台積電", "price": 100, "previousClose": 98, "volume": 100_000, "turnover": 500_000_000, "changePercent": 2.04}
    ]
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=True), store=store, provider=provider)

    worker.run_tick(now)

    daytrade = store.list_candidates("2026-07-02", "daytrade")
    assert daytrade[0].symbol == "2330"
    event_types = {row["event_type"] for row in store.list_monitor_events(trade_date="2026-07-02", actor="scout")}
    assert "scout_note" in event_types


def test_worker_manual_scout_can_rerun_and_keeps_manual_candidate(tmp_path) -> None:
    eligible = tmp_path / "eligible.txt"
    eligible.write_text("2330\n", encoding="utf-8")
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 0, 30, tzinfo=timezone.utc)
    settings = Settings(daytrade_eligible_symbols_file=eligible)
    provider = FakeProvider(_quote(now))
    provider.snapshot_rows = [
        {"symbol": "2330", "name": "台積電", "price": 100, "previousClose": 98, "volume": 100_000, "turnover": 500_000_000, "changePercent": 2.04}
    ]
    worker = TradingLabWorker(settings=settings, store=store, provider=provider)
    store.upsert_candidate(trade_date="2026-07-02", strategy="daytrade", symbol="2330", reason="manual keep", source="manual")

    worker.run_auto_scout_now(now)
    worker.run_auto_scout_now(now)

    daytrade = store.list_candidates("2026-07-02", "daytrade")
    assert len(daytrade) == 1
    assert daytrade[0].source == "manual"
    assert daytrade[0].reason == "manual keep"
    events = store.list_monitor_events(trade_date="2026-07-02", actor="scout")
    assert any(row["event_type"] == "candidate_kept_manual" for row in events)


def test_worker_auto_scout_deactivates_auto_candidate_missing_from_rerun(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 5, tzinfo=timezone.utc)
    provider = FakeProvider(_quote(now))
    provider.snapshot_rows = [
        {"symbol": "2330", "name": "台積電", "price": 100, "previousClose": 98, "volume": 100_000, "turnover": 500_000_000, "changePercent": 2.04}
    ]
    worker = TradingLabWorker(settings=Settings(), store=store, provider=provider)
    old_id = store.upsert_candidate(
        trade_date="2026-07-02",
        strategy="daytrade",
        symbol="2317",
        name="鴻海",
        reason="old auto",
        source="auto_scout",
        status="active",
    )

    worker.run_auto_scout_now(now)

    by_symbol = {row.symbol: row for row in store.list_candidates("2026-07-02", "daytrade")}
    assert by_symbol["2317"].id == old_id
    assert by_symbol["2317"].status == "inactive"
    assert by_symbol["2330"].status == "active"
    events = store.list_monitor_events(trade_date="2026-07-02", actor="scout")
    assert any(row["event_type"] == "candidate_deactivated" and row["symbol"] == "2317" for row in events)
    completed = [row for row in events if row["event_type"] == "scout_completed"]
    assert completed
    assert "新增 1" in completed[0]["detail"]
    assert "剔除 1" in completed[0]["detail"]


def test_worker_expires_old_candidates_on_new_trade_date(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    store.upsert_candidate(trade_date="2026-07-02", strategy="daytrade", symbol="2330", source="manual", status="active")
    now = datetime(2026, 7, 3, 1, 0, tzinfo=timezone.utc)
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=FakeProvider(_quote(now)))

    worker.run_tick(now)

    old = store.list_candidates("2026-07-02", "daytrade")[0]
    assert old.status == "inactive"
    events = store.list_monitor_events(trade_date="2026-07-03", actor="scout")
    assert any(row["event_type"] == "old_candidates_expired" for row in events)


def test_worker_creates_order_and_fills_on_later_bar(tmp_path) -> None:
    eligible = tmp_path / "eligible.txt"
    eligible.write_text("2330\n", encoding="utf-8")
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    settings = Settings(daytrade_eligible_symbols_file=eligible)
    first = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    provider = FakeProvider(_quote(first))
    worker = TradingLabWorker(settings=settings, store=store, provider=provider)
    store.upsert_candidate(trade_date="2026-07-02", strategy="daytrade", symbol="2330")

    worker.run_tick(first)
    assert len(store.list_open_orders()) == 1

    later = first + timedelta(minutes=5)
    provider.quote = _quote(later, price=99.0)
    worker.run_tick(later)

    assert len(store.list_fills()) == 1
    assert store.get_position(DAYTRADE_ACCOUNT, "2330").qty > 0
    event_types = {row["event_type"] for row in store.list_monitor_events(trade_date="2026-07-02")}
    assert "buy_order_created" in event_types
    assert "order_filled" in event_types


def test_worker_realtime_capture_subscribes_candidates_and_persists_events(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    provider = FakeProvider(_quote(now))
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=provider)
    store.upsert_candidate(trade_date="2026-07-02", strategy="daytrade", symbol="2330", name="台積電")

    worker.run_tick(now)

    assert provider.market_data_subscriptions
    symbols, channels = provider.market_data_subscriptions[0]
    assert symbols == ("2330",)
    assert channels == ("trades", "books", "aggregates", "candles")
    provider.emit_market_data(
        {
            "event": "data",
            "channel": "trades",
            "data": {"symbol": "2330", "price": 99.5, "size": 10, "bid": 99.5, "ask": 99.6, "volume": 10000, "time": int((now + timedelta(seconds=5)).timestamp() * 1_000_000), "serial": 1},
        },
        received_at=now + timedelta(seconds=5),
    )
    provider.emit_market_data(
        {
            "event": "data",
            "channel": "books",
            "data": {"symbol": "2330", "bids": [{"price": 99.5, "size": 10}], "asks": [{"price": 99.6, "size": 12}], "time": int((now + timedelta(seconds=6)).timestamp() * 1_000_000)},
        },
        received_at=now + timedelta(seconds=6),
    )
    provider.emit_market_data(
        {
            "event": "data",
            "channel": "candles",
            "data": {"symbol": "2330", "date": (now + timedelta(minutes=1)).isoformat(), "timeframe": 1, "open": 99.5, "high": 100, "low": 99.5, "close": 100, "volume": 20},
        },
        received_at=now + timedelta(minutes=1),
    )

    assert store.count_market_data_events("trades", "2330") == 1
    assert store.get_ticks_after("2330", now)[0]["price"] == 99.5
    assert store.latest_order_book("2330")["best_bid"] == 99.5
    assert store.get_bars_after("2330", 1, now - timedelta(minutes=1))


def test_worker_realtime_capture_drain_flushes_queued_events_after_stop(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=FakeProvider(_quote(now)))
    event = parse_realtime_market_event(
        {
            "event": "data",
            "channel": "trades",
            "data": {
                "symbol": "2330",
                "price": 99.5,
                "size": 10,
                "volume": 10000,
                "time": int((now + timedelta(seconds=5)).timestamp() * 1_000_000),
                "serial": 1,
            },
        },
        received_at=now + timedelta(seconds=5),
    )
    assert event is not None
    worker.stop_event.set()
    worker._realtime_capture_queue.put_nowait(event)

    worker._realtime_capture_drain_loop()

    assert store.count_market_data_events("trades", "2330") == 1
    assert store.get_ticks_after("2330", now)[0]["price"] == 99.5


def test_worker_fills_open_order_from_realtime_tick(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    created_at = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    order_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="tick fill",
        expires_at=created_at + timedelta(minutes=5),
        created_at=created_at,
    )
    store.insert_market_tick(
        symbol="2330",
        trade_time=created_at + timedelta(seconds=2),
        received_at=created_at + timedelta(seconds=2),
        price=99.5,
        size=1000,
        raw={"channel": "trades"},
        event_key="tick-fill",
    )
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=FakeProvider(_quote(created_at)))

    worker.run_tick(created_at + timedelta(minutes=1))

    assert store.get_order(order_id).status == "filled"
    assert store.list_fills()[0]["symbol"] == "2330"
    fill_events = [row for row in store.list_monitor_events(symbol="2330") if row["event_type"] == "order_filled"]
    assert json.loads(fill_events[0]["metrics_json"])["source"] == "tick"


def test_worker_creates_daytrade_order_without_eligible_file(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    first = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    provider = FakeProvider(_quote(first))
    worker = TradingLabWorker(settings=Settings(), store=store, provider=provider)
    store.upsert_candidate(trade_date="2026-07-02", strategy="daytrade", symbol="2330", name="台積電")

    worker.run_tick(first)

    orders = store.list_orders()
    assert len(orders) == 1
    assert orders[0]["strategy"] == "daytrade"
    assert orders[0]["stock_name"] == "台積電"
    assert orders[0]["strategy_version"] == "daytrade-v1"


def test_worker_daytrade_uses_active_strategy_params(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    active = store.get_active_strategy_version("daytrade")
    store.create_strategy_version(
        strategy="daytrade",
        params={**active.params, "stop_loss_pct": 0.02, "take_profit_pct": 0.03, "max_position_pct": 0.10},
        rules_text="unit daytrade v2",
        discussion="unit",
        summary="unit",
        auto_activate=True,
    )
    now = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    provider = FakeProvider(_quote(now, price=100))
    worker = TradingLabWorker(settings=Settings(), store=store, provider=provider)
    store.upsert_candidate(trade_date="2026-07-02", strategy="daytrade", symbol="2330", name="台積電")

    worker.run_tick(now)

    order = store.list_orders()[0]
    assert order["strategy_version"] == "daytrade-v2"
    assert order["stop_loss"] == 97.9
    assert order["take_profit"] == 102.9
    assert order["qty"] <= 1000


def test_worker_records_quote_diagnostic_when_quality_blocks_order(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 3, 1, 10, tzinfo=timezone.utc)
    quote = Quote(
        symbol="1718",
        name="中纖",
        price=14.95,
        previous_close=13.6,
        volume=100_000,
        turnover=100_000_000,
        bid_levels=[OrderBookLevel(price=14.95, size=10)],
        ask_levels=[],
        exchange_time=now,
        received_at=now,
        source="fake",
        is_realtime=True,
        flags={"missing_asks": True},
    )
    worker = TradingLabWorker(settings=Settings(), store=store, provider=FakeProvider(quote))
    store.upsert_candidate(trade_date="2026-07-03", strategy="daytrade", symbol="1718", name="中纖")

    worker.run_tick(now)

    assert store.list_orders() == []
    diagnostic = store.list_quote_diagnostics(trade_date="2026-07-03", symbol="1718")[0]
    assert diagnostic["diagnosis"] == "疑似漲停無賣盤"
    events = store.list_monitor_events(trade_date="2026-07-03", symbol="1718")
    assert any(row["event_type"] == "quote_limit_state_detected" for row in events)


def test_worker_fills_open_order_from_later_snapshot_before_expiring(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    created_at = datetime(2026, 7, 2, 4, 30, 6, tzinfo=timezone.utc)
    order_id = store.create_order(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol="3707",
        side="buy",
        price=87.7,
        qty=1000,
        reason="test",
        expires_at=created_at + timedelta(minutes=30),
        created_at=created_at,
    )
    store.insert_snapshot(
        symbol="3707",
        snapshot_time=created_at + timedelta(seconds=2),
        price=87.7,
        previous_close=84.0,
        volume=26224,
        turnover=2_095_920_300,
        bid_price=87.7,
        ask_price=87.8,
        is_realtime=True,
        raw={"source": "unit"},
    )
    worker = TradingLabWorker(settings=Settings(), store=store, provider=FakeProvider(_quote(created_at)))

    worker.run_tick(created_at + timedelta(minutes=31))

    orders = store.list_orders()
    assert orders[0]["id"] == order_id
    assert orders[0]["status"] == "filled"
    assert store.list_fills()[0]["symbol"] == "3707"
    assert any(row["event_type"] == "order_filled" for row in store.list_monitor_events(symbol="3707"))


def test_worker_fills_sell_order_from_later_snapshot(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    created_at = datetime(2026, 7, 2, 4, 30, 6, tzinfo=timezone.utc)
    store.upsert_position_after_fill(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol="3707",
        side="buy",
        qty=1000,
        price=80,
        fee=100,
        realized_pnl=0,
        stop_loss=None,
        take_profit=None,
        at=created_at - timedelta(days=1),
    )
    store.create_order(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol="3707",
        side="sell",
        price=87.7,
        qty=1000,
        reason="test sell",
        expires_at=created_at + timedelta(minutes=30),
        created_at=created_at,
    )
    store.insert_snapshot(
        symbol="3707",
        snapshot_time=created_at + timedelta(seconds=2),
        price=87.7,
        previous_close=84.0,
        volume=26224,
        turnover=2_095_920_300,
        bid_price=87.7,
        ask_price=87.8,
        is_realtime=True,
        raw={"source": "unit"},
    )
    worker = TradingLabWorker(settings=Settings(), store=store, provider=FakeProvider(_quote(created_at)))

    worker.run_tick(created_at + timedelta(minutes=31))

    assert store.list_orders()[0]["status"] == "filled"
    assert store.get_position(SWING_ACCOUNT, "3707") is None


def test_worker_does_not_fill_from_bar_after_order_expiry(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    created_at = datetime(2026, 7, 2, 4, 30, 6, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(minutes=30)
    store.create_order(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol="3707",
        side="buy",
        price=87.7,
        qty=1000,
        reason="test",
        expires_at=expires_at,
        created_at=created_at,
    )
    store.upsert_bar(
        symbol="3707",
        timeframe_minutes=30,
        start_time=expires_at + timedelta(seconds=1),
        end_time=expires_at + timedelta(minutes=30),
        price=87.7,
        volume=1000,
        source="unit",
    )
    worker = TradingLabWorker(settings=Settings(), store=store, provider=FakeProvider(_quote(created_at)))

    worker.run_tick(expires_at + timedelta(minutes=31))

    assert store.list_orders()[0]["status"] == "expired"
    assert store.list_fills() == []


def test_worker_monitors_existing_swing_position_without_today_candidate(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    opened_at = datetime(2026, 7, 2, 4, 30, tzinfo=timezone.utc)
    now = datetime(2026, 7, 3, 1, 30, tzinfo=timezone.utc)
    store.upsert_position_after_fill(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol="3707",
        side="buy",
        qty=1000,
        price=87.7,
        fee=125,
        realized_pnl=0,
        stop_loss=82.4,
        take_profit=96.5,
        at=opened_at,
    )
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False),
        store=store,
        provider=FakeProvider(_quote(now, price=81.5, symbol="3707", name="漢磊")),
    )

    worker.run_tick(now)

    orders = store.list_orders()
    assert len(orders) == 1
    assert orders[0]["symbol"] == "3707"
    assert orders[0]["side"] == "sell"
    assert orders[0]["reason"] == "觸發停損，建立出場模擬單"


def test_worker_exit_order_keeps_position_attribution(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    opened_at = datetime(2026, 7, 2, 4, 30, tzinfo=timezone.utc)
    entry_order_id, candidate_id = _seed_attributed_position(store, account_id=SWING_ACCOUNT, strategy="swing", symbol="3707", at=opened_at)
    now = datetime(2026, 7, 3, 1, 30, tzinfo=timezone.utc)
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False),
        store=store,
        provider=FakeProvider(_quote(now, price=81.5, symbol="3707", name="漢磊")),
    )

    worker.run_tick(now)

    sell_order = [row for row in store.list_orders() if row["side"] == "sell"][0]
    assert sell_order["candidate_id"] == candidate_id
    assert sell_order["entry_order_id"] == entry_order_id
    assert sell_order["strategy_version"] == "swing-v1"
    assert sell_order["scout_version"] == "scout-v1"
    assert sell_order["attribution_status"] == "complete"


def test_worker_forces_daytrade_flatten_before_daily_review(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    opened_at = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    now = datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc)
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
        at=opened_at,
    )
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=FakeProvider(_quote(now, price=101)))

    worker.run_tick(now)

    assert store.get_position(DAYTRADE_ACCOUNT, "2330") is None
    orders = store.list_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "filled"
    assert orders[0]["side"] == "sell"
    events = store.list_monitor_events(trade_date="2026-07-02", actor="risk_manager")
    assert any(row["event_type"] == "daytrade_forced_flatten" for row in events)


def test_worker_force_flatten_order_keeps_position_attribution(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    opened_at = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    entry_order_id, candidate_id = _seed_attributed_position(store, account_id=DAYTRADE_ACCOUNT, strategy="daytrade", symbol="2330", at=opened_at)
    now = datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc)
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=FakeProvider(_quote(now, price=101)))

    worker.run_tick(now)

    sell_order = [row for row in store.list_orders() if row["side"] == "sell"][0]
    assert sell_order["status"] == "filled"
    assert sell_order["candidate_id"] == candidate_id
    assert sell_order["entry_order_id"] == entry_order_id
    assert sell_order["strategy_version"] == "daytrade-v1"
    assert sell_order["scout_version"] == "scout-v1"
    assert sell_order["attribution_status"] == "complete"
    sell_fill = [row for row in store.list_fills() if row["side"] == "sell"][0]
    assert sell_fill["candidate_id"] == candidate_id
    assert sell_fill["entry_order_id"] == entry_order_id
    assert sell_fill["strategy_version"] == "daytrade-v1"


def test_worker_force_flatten_uses_latest_order_book_bid(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    opened_at = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)
    now = datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc)
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
        at=opened_at,
    )
    store.insert_order_book(
        symbol="2330",
        exchange_time=now - timedelta(seconds=1),
        received_at=now - timedelta(seconds=1),
        bids=[{"price": 100.8, "size": 20}],
        asks=[{"price": 100.9, "size": 20}],
        raw={"channel": "books"},
        event_key="force-book",
    )
    worker = TradingLabWorker(settings=Settings(enable_auto_scout=False), store=store, provider=FakeProvider(_quote(now, price=101)))

    worker.run_tick(now)

    sell_order = [row for row in store.list_orders() if row["side"] == "sell"][0]
    assert sell_order["price"] == 100.8
    event = [row for row in store.list_monitor_events(strategy="daytrade") if row["event_type"] == "daytrade_forced_flatten"][0]
    assert json.loads(event["metrics_json"])["source"] == "latest_order_book_bid"


def test_worker_creates_and_applies_valid_swing_strategy_version(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    filled_at = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    now = datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc)
    _seed_swing_buy_fill(store, filled_at)
    adapter = FakeLlmAdapter(CodexResult(True, _valid_swing_review_payload()))
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False, enable_swing_self_correction=True),
        store=store,
        provider=FakeProvider(_quote(now, symbol="3707", name="漢磊")),
        llm_adapter=adapter,
    )

    worker.run_tick(now)

    assert adapter.calls == 1
    assert store.get_strategy_version_state("swing").active_version == "swing-v2"
    active = store.get_active_strategy_version("swing")
    assert active.params["stop_loss_pct"] == 0.055
    swing_review = [row for row in store.list_daily_reviews() if row["strategy"] == "swing"][0]
    assert swing_review["proposal_status"] == "version_created_applied"
    assert swing_review["strategy_version"] == "swing-v2"
    assert any(row["event_type"] == "swing_strategy_version_created" for row in store.list_monitor_events(trade_date="2026-07-02", strategy="swing"))


def test_worker_can_run_swing_review_immediately(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    filled_at = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    _seed_swing_buy_fill(store, filled_at)
    store.upsert_daily_review("2026-07-02", "swing", "base review", {"fills": 1}, strategy_version="swing-v1")
    adapter = FakeLlmAdapter(CodexResult(True, _valid_swing_review_payload()))
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False, enable_swing_self_correction=True),
        store=store,
        provider=FakeProvider(_quote(filled_at, symbol="3707", name="漢磊")),
        llm_adapter=adapter,
    )

    message = worker.run_swing_review_now()

    assert "短線會後討論完成" in message
    assert adapter.calls == 1
    assert store.get_active_strategy_version("swing").version == "swing-v2"


def test_worker_swing_review_immediate_uses_recent_window(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    first = datetime(2026, 6, 15, 1, 30, tzinfo=timezone.utc)
    latest = datetime(2026, 6, 24, 1, 30, tzinfo=timezone.utc)
    _seed_swing_buy_fill(store, first)
    _seed_swing_buy_fill(store, latest, symbol="5328")
    store.upsert_daily_review("2026-06-24", "swing", "base review", {"fills": 1}, strategy_version="swing-v1")
    adapter = FakeLlmAdapter(CodexResult(True, _valid_swing_review_payload()))
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False, enable_swing_self_correction=False),
        store=store,
        provider=FakeProvider(_quote(latest, symbol="3707", name="漢磊")),
        llm_adapter=adapter,
    )

    message = worker.run_swing_review_now()

    assert "短線會後討論完成" in message
    assert adapter.calls == 1
    payload = json.loads(adapter.prompts[0])
    assert payload["evidence"]["data_start"] == "2026-06-11"
    assert payload["evidence"]["data_end"] == "2026-06-24"
    assert len(payload["evidence"]["fills"]) == 2


def test_worker_can_run_daytrade_review_immediately(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    at = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    _seed_daytrade_roundtrip(store, at)
    store.upsert_daily_review("2026-07-02", "daytrade", "base review", {"fills": 2})
    adapter = FakeLlmAdapter(CodexResult(True, _valid_daytrade_review_payload()))
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False),
        store=store,
        provider=FakeProvider(_quote(at, symbol="2330", name="台積電")),
        llm_adapter=adapter,
    )

    message = worker.run_daytrade_review_now()

    assert "當沖會後討論完成" in message
    assert adapter.calls == 1
    review = [row for row in store.list_daily_reviews() if row["strategy"] == "daytrade"][0]
    assert review["proposal_status"] == "reviewed"
    assert "追價太快" in review["llm_discussion"]


def test_worker_manual_lock_keeps_selected_swing_version_when_new_version_is_created(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    store.set_strategy_version_state("swing", "swing-v1", "manual_lock")
    filled_at = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    now = datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc)
    _seed_swing_buy_fill(store, filled_at)
    adapter = FakeLlmAdapter(CodexResult(True, _valid_swing_review_payload()))
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False, enable_swing_self_correction=True),
        store=store,
        provider=FakeProvider(_quote(now, symbol="3707", name="漢磊")),
        llm_adapter=adapter,
    )

    worker.run_tick(now)

    assert store.get_strategy_version_state("swing").active_version == "swing-v1"
    assert [version.version for version in store.list_strategy_versions("swing", limit=None)] == ["swing-v2", "swing-v1"]
    swing_review = [row for row in store.list_daily_reviews() if row["strategy"] == "swing"][0]
    assert swing_review["proposal_status"] == "version_created_locked"
    assert swing_review["strategy_version"] == "swing-v1"


def test_worker_rejects_invalid_swing_strategy_version_payload(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    filled_at = datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)
    now = datetime(2026, 7, 2, 5, 30, tzinfo=timezone.utc)
    _seed_swing_buy_fill(store, filled_at)
    payload = {**_valid_swing_review_payload(), "parameter_changes": {"stop_loss_pct": 0.99}}
    adapter = FakeLlmAdapter(CodexResult(True, payload))
    worker = TradingLabWorker(
        settings=Settings(enable_auto_scout=False, enable_swing_self_correction=True),
        store=store,
        provider=FakeProvider(_quote(now, symbol="3707", name="漢磊")),
        llm_adapter=adapter,
    )

    worker.run_tick(now)

    assert store.get_strategy_version_state("swing").active_version == "swing-v1"
    assert [version.version for version in store.list_strategy_versions("swing", limit=None)] == ["swing-v1"]
    swing_review = [row for row in store.list_daily_reviews() if row["strategy"] == "swing"][0]
    assert swing_review["proposal_status"] == "validation_failed"
    assert "超出允許範圍" in swing_review["llm_result_json"]


def test_worker_status_updates_without_idle_log_noise(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
    worker = TradingLabWorker(settings=Settings(), store=store, provider=FakeProvider(_quote(now)))

    worker.run_tick(now)

    status = worker.status_snapshot()
    assert status.running is True
    assert status.phase == "idle"
    assert status.last_heartbeat == now
    assert store.list_monitor_events(trade_date="2026-07-02") == []
