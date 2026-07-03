from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tw_watchdesk.config import Settings
from tw_watchdesk.models import OrderBookLevel, Quote
from tw_watchdesk.quote_diagnostics import calculate_limit_down, calculate_limit_up, diagnose_quote_quality


def _quote(
    now: datetime,
    *,
    symbol: str = "1718",
    price: float = 14.95,
    previous_close: float = 13.6,
    bids: list[OrderBookLevel] | None = None,
    asks: list[OrderBookLevel] | None = None,
    exchange_delta: timedelta = timedelta(seconds=2),
    receive_delta: timedelta = timedelta(seconds=1),
    flags: dict[str, bool] | None = None,
) -> Quote:
    return Quote(
        symbol=symbol,
        name="測試股",
        price=price,
        previous_close=previous_close,
        volume=100_000,
        turnover=100_000_000,
        bid_levels=bids if bids is not None else [OrderBookLevel(price=max(0.01, price - 0.05), size=10)],
        ask_levels=asks if asks is not None else [OrderBookLevel(price=price, size=10)],
        exchange_time=now - exchange_delta,
        received_at=now - receive_delta,
        source="taishin_nova",
        is_realtime=True,
        flags=flags or {},
    )


def test_limit_up_missing_asks_is_explained() -> None:
    now = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    diagnostic = diagnose_quote_quality(Settings(stale_seconds=70), _quote(now, asks=[]), now)

    assert calculate_limit_up(13.6) == 14.95
    assert diagnostic.flags["likely_limit_up_no_asks"] is True
    assert diagnostic.diagnosis == "疑似漲停無賣盤"
    assert diagnostic.event_type == "quote_limit_state_detected"


def test_limit_down_missing_bids_is_explained() -> None:
    now = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    previous_close = 13.6
    quote = _quote(now, price=calculate_limit_down(previous_close), previous_close=previous_close, bids=[], asks=[OrderBookLevel(price=12.3, size=10)])

    diagnostic = diagnose_quote_quality(Settings(stale_seconds=70), quote, now)

    assert diagnostic.flags["likely_limit_down_no_bids"] is True
    assert diagnostic.diagnosis == "疑似跌停無買盤"


def test_exchange_stale_and_received_stale_are_separate() -> None:
    now = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    exchange_only = diagnose_quote_quality(
        Settings(stale_seconds=70),
        _quote(now, exchange_delta=timedelta(seconds=90), receive_delta=timedelta(seconds=2)),
        now,
    )
    both = diagnose_quote_quality(
        Settings(stale_seconds=70),
        _quote(now, exchange_delta=timedelta(seconds=90), receive_delta=timedelta(seconds=91)),
        now,
    )

    assert exchange_only.flags["stale_exchange_time"] is True
    assert exchange_only.flags["stale_received_at"] is False
    assert exchange_only.diagnosis == "交易所時間超過 70 秒"
    assert both.flags["stale_received_at"] is True
    assert both.diagnosis == "本機超過 70 秒未收到報價"
