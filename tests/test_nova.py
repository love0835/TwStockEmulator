import sys
import types
from datetime import datetime, timezone

import pytest

from tw_watchdesk.config import Settings
from tw_watchdesk.nova import (
    ProviderUnavailable,
    TaishinNovaProvider,
    _marketdata_rest_stock_client,
    _marketdata_stock_client,
    _nova_retry_interval_seconds,
    parse_aggregates_message,
    parse_realtime_market_event,
)


def test_parse_aggregates_message() -> None:
    exchange_time = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)
    quote = parse_aggregates_message(
        {
            "data": {
                "symbol": "2330",
                "name": "台積電",
                "lastPrice": 1000,
                "previousClose": 980,
                "lastUpdated": int(exchange_time.timestamp() * 1_000_000),
                "total": {"tradeVolume": 12345, "tradeValue": 12_345_000_000},
                "bids": [{"price": 999, "size": 10}, {"price": 998, "size": 8}],
                "asks": [{"price": 1000, "size": 7}, {"price": 1005, "size": 3}],
            }
        },
        received_at=datetime(2026, 7, 1, 1, 0, 2, tzinfo=timezone.utc),
    )

    assert quote.symbol == "2330"
    assert quote.price == 1000
    assert quote.previous_close == 980
    assert quote.bid_levels[0].price == 999
    assert quote.ask_levels[0].size == 7
    assert quote.exchange_time == exchange_time
    assert quote.is_realtime is True


def test_parse_missing_book_sets_flags() -> None:
    quote = parse_aggregates_message({"data": {"symbol": "2330", "lastPrice": 1000, "lastUpdated": "2026-07-01T01:00:00+00:00"}})

    assert quote.flags["missing_bids"] is True
    assert quote.flags["missing_asks"] is True
    assert quote.flags["provider_payload_missing_depth"] is True


def test_parse_book_ignores_zero_or_invalid_depth_prices() -> None:
    quote = parse_aggregates_message(
        {
            "data": {
                "symbol": "2330",
                "lastPrice": 1000,
                "lastUpdated": "2026-07-01T01:00:00+00:00",
                "bids": [{"price": 0, "size": 10}, {"price": "bad", "size": 10}],
                "asks": [{"price": 1000, "size": 7}],
            }
        }
    )

    assert quote.bid_levels == []
    assert quote.ask_levels[0].price == 1000
    assert quote.flags["missing_bids"] is True
    assert quote.flags["invalid_depth_price"] is True


def test_parse_realtime_market_event_trade() -> None:
    event = parse_realtime_market_event(
        {
            "event": "data",
            "channel": "trades",
            "data": {
                "symbol": "2330",
                "price": 568,
                "size": 4778,
                "volume": 54538,
                "time": 1685338200000000,
                "serial": 6652422,
            },
        }
    )

    assert event is not None
    assert event.channel == "trades"
    assert event.symbol == "2330"
    assert event.payload["serial"] == 6652422


def test_parse_realtime_market_event_ignores_control_message() -> None:
    event = parse_realtime_market_event({"event": "subscribed", "data": {"channel": "trades", "symbol": "2330"}})

    assert event is None


def test_marketdata_stock_client_prefers_realtime_stock() -> None:
    class TradingStock:
        pass

    class RealtimeStock:
        def subscribe(self, params):
            return params

    class WebsocketClient:
        stock = RealtimeStock()

    class MarketData:
        websocket_client = WebsocketClient()

    class Sdk:
        stock = TradingStock()
        marketdata = MarketData()

    assert _marketdata_stock_client(Sdk()) is MarketData.websocket_client.stock


def test_marketdata_rest_stock_client() -> None:
    class Snapshot:
        pass

    class RestStock:
        snapshot = Snapshot()

    class RestClient:
        stock = RestStock()

    class MarketData:
        rest_client = RestClient()

    class Sdk:
        marketdata = MarketData()

    assert _marketdata_rest_stock_client(Sdk()) is MarketData.rest_client.stock


def test_nova_retry_interval_is_five_minutes_before_open() -> None:
    now = datetime(2026, 7, 2, 0, 30, tzinfo=timezone.utc)

    assert _nova_retry_interval_seconds(now, "Asia/Taipei") == 5 * 60


def test_nova_retry_interval_is_hourly_off_hours() -> None:
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)

    assert _nova_retry_interval_seconds(now, "Asia/Taipei") == 60 * 60


def test_nova_provider_throttles_failed_sdk_login(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingSdk:
        calls = 0

        def __init__(self) -> None:
            FailingSdk.calls += 1

        def login(self, *args):
            raise RuntimeError("network down")

    module = types.ModuleType("taishin_sdk")
    module.TaishinSDK = FailingSdk
    monkeypatch.setitem(sys.modules, "taishin_sdk", module)
    settings = Settings(
        nova_user="user",
        nova_password="password",
        nova_cert_path="cert.pfx",
        nova_cert_password="cert-password",
    )
    provider = TaishinNovaProvider(settings)

    with pytest.raises(ProviderUnavailable, match="Nova SDK"):
        provider.get_stock_rest_client()
    with pytest.raises(ProviderUnavailable, match="等待下次 Nova 重試"):
        provider.get_stock_rest_client()

    assert FailingSdk.calls == 1
