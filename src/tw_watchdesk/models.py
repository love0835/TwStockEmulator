from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    previous_close: float
    volume: float
    turnover: float
    bid_levels: list[OrderBookLevel]
    ask_levels: list[OrderBookLevel]
    exchange_time: datetime
    received_at: datetime
    source: str
    is_realtime: bool
    flags: dict[str, bool]

    @property
    def change(self) -> float:
        return self.price - self.previous_close

    @property
    def change_pct(self) -> float:
        return self.change / self.previous_close if self.previous_close > 0 else 0.0


@dataclass(frozen=True)
class RealtimeMarketEvent:
    channel: str
    symbol: str
    exchange_time: datetime
    received_at: datetime
    payload: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class DataQuality:
    status: str
    age_seconds: float | None
    reasons: list[str]
    receive_age_seconds: float | None = None
    flags: dict[str, bool] = field(default_factory=dict)
    diagnosis: str = ""


@dataclass(frozen=True)
class Advice:
    action: str
    buy_price: float | None
    sell_price: float | None
    qty: int
    max_notional: float
    stop_loss: float | None
    take_profit: float | None
    reason: str
    risk_flags: list[str]
    strategy_version: str = ""


@dataclass(frozen=True)
class WatchState:
    status: str
    quote: Quote | None
    quality: DataQuality
    advice: Advice
    updated_at: datetime
    institutional_note: str
