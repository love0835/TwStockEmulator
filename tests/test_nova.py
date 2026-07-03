from datetime import datetime, timezone

from tw_watchdesk.nova import _marketdata_rest_stock_client, _marketdata_stock_client, parse_aggregates_message


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
