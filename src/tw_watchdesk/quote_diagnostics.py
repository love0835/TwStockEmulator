from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, floor
from typing import Any

from tw_watchdesk.config import Settings
from tw_watchdesk.models import Quote


@dataclass(frozen=True)
class QuoteDiagnostic:
    symbol: str
    price: float
    previous_close: float
    limit_up: float
    limit_down: float
    best_bid: float | None
    best_ask: float | None
    bid_count: int
    ask_count: int
    exchange_time: datetime
    received_at: datetime
    exchange_age_seconds: float
    receive_age_seconds: float
    flags: dict[str, bool]
    diagnosis: str
    event_type: str
    title: str
    payload_shape: dict[str, Any]

    @property
    def status(self) -> str:
        return "blocked" if any(self.flags.values()) else "ok"

    def metrics(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "prev_close": self.previous_close,
            "limit_up": self.limit_up,
            "limit_down": self.limit_down,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_count": self.bid_count,
            "ask_count": self.ask_count,
            "exchange_time": self.exchange_time.isoformat(),
            "received_at": self.received_at.isoformat(),
            "quote_age_seconds": self.exchange_age_seconds,
            "receive_age_seconds": self.receive_age_seconds,
            "quality_flags": {key: value for key, value in self.flags.items() if value},
            "diagnosis_reason": self.diagnosis,
            "payload_shape": self.payload_shape,
        }


def diagnose_quote_quality(settings: Settings, quote: Quote, now: datetime | None = None) -> QuoteDiagnostic:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    exchange_time = _aware_utc(quote.exchange_time)
    received_at = _aware_utc(quote.received_at)
    exchange_age = max(0.0, (now - exchange_time).total_seconds())
    receive_age = max(0.0, (now - received_at).total_seconds())
    best_bid = quote.bid_levels[0].price if quote.bid_levels else None
    best_ask = quote.ask_levels[0].price if quote.ask_levels else None
    limit_up = calculate_limit_up(quote.previous_close)
    limit_down = calculate_limit_down(quote.previous_close)

    flags: dict[str, bool] = {
        key: bool(value)
        for key, value in quote.flags.items()
        if key
    }
    flags["missing_bids"] = not quote.bid_levels
    flags["missing_asks"] = not quote.ask_levels
    flags["invalid_price"] = quote.price <= 0
    flags["missing_volume"] = quote.volume <= 0
    flags["stale_exchange_time"] = exchange_age > settings.stale_seconds
    flags["stale_received_at"] = receive_age > settings.stale_seconds
    flags["likely_limit_up_no_asks"] = bool(
        quote.previous_close > 0 and not quote.ask_levels and _near_or_above(quote.price, limit_up)
    )
    flags["likely_limit_down_no_bids"] = bool(
        quote.previous_close > 0 and not quote.bid_levels and _near_or_below(quote.price, limit_down)
    )
    flags["empty_asks_at_limit_up"] = flags["likely_limit_up_no_asks"]
    flags["empty_bids_at_limit_down"] = flags["likely_limit_down_no_bids"]

    diagnosis, event_type, title = _diagnosis(flags, settings.stale_seconds)
    payload_shape = {
        "bid_levels": len(quote.bid_levels),
        "ask_levels": len(quote.ask_levels),
        "flag_keys": sorted(key for key, value in quote.flags.items() if value),
        "source": quote.source,
    }
    return QuoteDiagnostic(
        symbol=quote.symbol,
        price=quote.price,
        previous_close=quote.previous_close,
        limit_up=limit_up,
        limit_down=limit_down,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_count=len(quote.bid_levels),
        ask_count=len(quote.ask_levels),
        exchange_time=exchange_time,
        received_at=received_at,
        exchange_age_seconds=exchange_age,
        receive_age_seconds=receive_age,
        flags=flags,
        diagnosis=diagnosis,
        event_type=event_type,
        title=title,
        payload_shape=payload_shape,
    )


def quality_reasons(diagnostic: QuoteDiagnostic, settings: Settings) -> list[str]:
    reasons: list[str] = []
    flags = diagnostic.flags
    if flags.get("invalid_price"):
        reasons.append("缺少有效成交價")
    if flags.get("missing_volume"):
        reasons.append("缺少有效成交量")
    if flags.get("provider_payload_missing_depth"):
        reasons.append("資料源未提供五檔欄位")
    if flags.get("provider_payload_depth_empty"):
        reasons.append("資料源五檔欄位為空")
    if flags.get("invalid_depth_price"):
        reasons.append("五檔價格無效")
    if flags.get("likely_limit_up_no_asks"):
        reasons.append("疑似漲停無賣盤")
    elif flags.get("missing_asks"):
        reasons.append("缺少賣方五檔")
    if flags.get("likely_limit_down_no_bids"):
        reasons.append("疑似跌停無買盤")
    elif flags.get("missing_bids"):
        reasons.append("缺少買方五檔")
    if flags.get("stale_exchange_time"):
        reasons.append(f"交易所時間超過 {settings.stale_seconds} 秒")
    if flags.get("stale_received_at"):
        reasons.append(f"本機超過 {settings.stale_seconds} 秒未收到報價")
    if not reasons and not diagnostic.status == "ok":
        reasons.append(diagnostic.diagnosis)
    return reasons


def calculate_limit_up(previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    tick = twse_tick_size(previous_close * 1.1)
    return round(floor((previous_close * 1.1) / tick + 1e-9) * tick, 2)


def calculate_limit_down(previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    tick = twse_tick_size(previous_close * 0.9)
    return round(ceil((previous_close * 0.9) / tick - 1e-9) * tick, 2)


def twse_tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _diagnosis(flags: dict[str, bool], stale_seconds: int) -> tuple[str, str, str]:
    if flags.get("likely_limit_up_no_asks"):
        return "疑似漲停無賣盤", "quote_limit_state_detected", "疑似漲停無賣盤"
    if flags.get("likely_limit_down_no_bids"):
        return "疑似跌停無買盤", "quote_limit_state_detected", "疑似跌停無買盤"
    if flags.get("stale_received_at"):
        return f"本機超過 {stale_seconds} 秒未收到報價", "quote_stale_received_at", "本機接收時間過舊"
    if flags.get("stale_exchange_time"):
        return f"交易所時間超過 {stale_seconds} 秒", "quote_stale_exchange_time", "交易所時間過舊"
    if flags.get("provider_payload_missing_depth"):
        return "資料源未提供五檔欄位", "quote_provider_payload_shape", "五檔欄位缺失"
    if flags.get("provider_payload_depth_empty"):
        return "資料源五檔欄位為空", "quote_provider_payload_shape", "五檔欄位為空"
    if flags.get("invalid_depth_price"):
        return "五檔價格無效", "quote_depth_missing", "五檔價格無效"
    if flags.get("missing_asks") or flags.get("missing_bids"):
        return "缺少五檔資料", "quote_depth_missing", "缺少五檔資料"
    if flags.get("invalid_price"):
        return "缺少有效成交價", "quote_quality_blocked", "成交價無效"
    if flags.get("missing_volume"):
        return "缺少有效成交量", "quote_quality_blocked", "成交量無效"
    return "報價品質正常", "quote_quality_ok", "報價品質正常"


def _near_or_above(price: float, limit_price: float) -> bool:
    if limit_price <= 0:
        return False
    return price >= limit_price - max(0.01, twse_tick_size(limit_price) / 2)


def _near_or_below(price: float, limit_price: float) -> bool:
    if limit_price <= 0:
        return False
    return price <= limit_price + max(0.01, twse_tick_size(limit_price) / 2)


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
