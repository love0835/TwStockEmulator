from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from tw_watchdesk.config import Settings
from tw_watchdesk.models import OrderBookLevel, Quote, RealtimeMarketEvent


class QuoteProvider(Protocol):
    def get_quote(self, symbol: str) -> Quote:
        ...


class StockRestClientProvider(Protocol):
    def get_stock_rest_client(self) -> Any:
        ...


class RealtimeMarketDataProvider(Protocol):
    def add_market_data_listener(self, listener: Callable[[RealtimeMarketEvent], None]) -> None:
        ...

    def subscribe_market_data(self, symbols: list[str], channels: tuple[str, ...]) -> None:
        ...


class ProviderUnavailable(RuntimeError):
    pass


MARKET_DATA_CHANNELS = {"trades", "books", "aggregates", "candles", "indices"}


def parse_aggregates_message(message: str | Mapping[str, Any], received_at: datetime | None = None) -> Quote:
    received_at = received_at or datetime.now(timezone.utc)
    payload = json.loads(message) if isinstance(message, str) else message
    payload = _mapping(payload)
    data = _mapping(payload.get("data") or payload.get("payload") or payload)
    flags: dict[str, bool] = {}

    symbol = str(_first(data, "symbol", "code", "stockNo") or "").upper()
    name = str(_first(data, "name", "symbolName", "stockName") or symbol)
    price = _number(_first(data, "lastPrice", "lastTrade.price", "closePrice", "price"))
    previous_close = _number(_first(data, "previousClose", "referencePrice", "prevClose", "yesterdayPrice"))
    volume = _number(_first(data, "total.tradeVolume", "tradeVolume", "volume"))
    turnover = _number(_first(data, "total.tradeValue", "tradeValue", "turnover"))
    exchange_time = _timestamp(_first(data, "lastUpdated", "lastUpdate", "lastTrade.time", "timestamp"))
    bid_payload = _first(data, "bids", "bestBids", "bidLevels")
    ask_payload = _first(data, "asks", "bestAsks", "askLevels")
    bids, bid_flags = _levels(bid_payload)
    asks, ask_flags = _levels(ask_payload)
    _merge_depth_flags(flags, "bid", bid_payload, bid_flags)
    _merge_depth_flags(flags, "ask", ask_payload, ask_flags)

    if not symbol:
        symbol = "UNKNOWN"
        flags["missing_symbol"] = True
    if price is None:
        price = 0.0
        flags["missing_price"] = True
    if previous_close is None:
        previous_close = price
        flags["missing_previous_close"] = True
    if volume is None:
        volume = 0.0
        flags["missing_volume"] = True
    if turnover is None:
        turnover = price * volume * 1000
        flags["missing_turnover"] = True
    if exchange_time is None:
        exchange_time = received_at
        flags["missing_exchange_time"] = True
    if not bids:
        flags["missing_bids"] = True
    if not asks:
        flags["missing_asks"] = True

    return Quote(
        symbol=symbol,
        name=name,
        price=price,
        previous_close=previous_close,
        volume=volume,
        turnover=turnover,
        bid_levels=bids,
        ask_levels=asks,
        exchange_time=exchange_time,
        received_at=received_at,
        source="taishin_nova",
        is_realtime=not any(flags.get(key) for key in ("missing_price", "missing_exchange_time")),
        flags=flags,
    )


def parse_realtime_market_event(message: str | Mapping[str, Any], received_at: datetime | None = None) -> RealtimeMarketEvent | None:
    received_at = received_at or datetime.now(timezone.utc)
    decoded = json.loads(message) if isinstance(message, str) else message
    payload = _mapping(decoded)
    if not payload:
        return None
    raw = dict(payload)
    event_type = str(payload.get("event") or "").lower()
    if event_type and event_type != "data":
        return None
    data = _mapping(payload.get("data") or payload.get("payload") or payload)
    channel = str(payload.get("channel") or data.get("channel") or "").strip().lower()
    if not channel:
        channel = _infer_channel(data)
    if channel not in MARKET_DATA_CHANNELS:
        return None
    symbol = str(_first(data, "symbol", "code", "stockNo") or "").upper()
    if not symbol:
        return None
    exchange_time = _timestamp(_first(data, "time", "date", "lastUpdated", "lastUpdate", "lastTrade.time", "timestamp")) or received_at
    return RealtimeMarketEvent(
        channel=channel,
        symbol=symbol,
        exchange_time=exchange_time,
        received_at=received_at,
        payload=dict(data),
        raw=dict(payload),
    )


class TaishinNovaProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        missing = [
            name
            for name, value in (
                ("TAISHIN_NOVA_USER", settings.nova_user),
                ("TAISHIN_NOVA_PASSWORD", settings.nova_password),
                ("TAISHIN_NOVA_CERT_PATH", settings.nova_cert_path),
                ("TAISHIN_NOVA_CERT_PASSWORD", settings.nova_cert_password),
            )
            if not value
        ]
        if missing:
            loaded = ", ".join(str(path) for path in settings.loaded_env_files) or "未讀到 .env.local / .env"
            raise ProviderUnavailable(f"缺少 Nova 設定：{', '.join(missing)}；設定檔：{loaded}")
        self._lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self._quotes: dict[str, Quote] = {}
        self._subscribed: set[str] = set()
        self._marketdata_ready = False
        self._connected = False
        self._sdk: Any = None
        self._stock: Any = None
        self._listeners: list[Callable[[RealtimeMarketEvent], None]] = []
        self._channel_subscriptions: set[tuple[str, str]] = set()

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper().strip()
        self._subscribe(symbol)
        deadline = time.monotonic() + self.settings.nova_quote_wait_seconds
        while time.monotonic() < deadline:
            with self._lock:
                quote = self._quotes.get(symbol)
            if quote is not None:
                return quote
            time.sleep(0.1)
        raise ProviderUnavailable(f"等待 Nova 即時報價逾時：{symbol}")

    def _ensure_connected(self) -> None:
        with self._connect_lock:
            if self._connected:
                return
            self._ensure_marketdata()
            self._stock = _marketdata_stock_client(self._sdk)
            try:
                self._stock.on("message", self._handle_message)
                self._stock.connect()
            except Exception as exc:
                raise ProviderUnavailable(f"Nova 即時行情連線失敗：{exc}") from exc
            self._connected = True

    def _ensure_marketdata(self) -> None:
        with self._connect_lock:
            if self._marketdata_ready:
                return
            try:
                from taishin_sdk import TaishinSDK  # type: ignore
            except ImportError as exc:
                raise ProviderUnavailable("缺少 taishin_sdk，無法使用 Nova live mode") from exc
            self._sdk = TaishinSDK()
            accounts = self._sdk.login(
                self.settings.nova_user,
                self.settings.nova_password,
                self.settings.nova_cert_path,
                self.settings.nova_cert_password,
            )
            account = accounts[0] if accounts else None
            if account is None:
                raise ProviderUnavailable("Nova 登入成功但沒有可用帳戶")
            self._sdk.init_realtime(account)
            self._marketdata_ready = True

    def get_stock_rest_client(self) -> Any:
        self._ensure_marketdata()
        return _marketdata_rest_stock_client(self._sdk)

    def add_market_data_listener(self, listener: Callable[[RealtimeMarketEvent], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def subscribe_market_data(self, symbols: list[str], channels: tuple[str, ...]) -> None:
        self._ensure_connected()
        for symbol in symbols:
            clean_symbol = symbol.upper().strip()
            if not clean_symbol:
                continue
            for channel in channels:
                clean_channel = channel.strip().lower()
                if clean_channel not in MARKET_DATA_CHANNELS:
                    continue
                key = (clean_channel, clean_symbol)
                if key in self._channel_subscriptions:
                    continue
                try:
                    self._stock.subscribe({"channel": clean_channel, "symbol": clean_symbol})
                except Exception as exc:
                    raise ProviderUnavailable(f"Nova 訂閱 {clean_channel} 失敗：{clean_symbol}；{exc}") from exc
                self._channel_subscriptions.add(key)

    def _subscribe(self, symbol: str) -> None:
        self._ensure_connected()
        if symbol in self._subscribed:
            return
        try:
            self._stock.subscribe({"channel": "aggregates", "symbol": symbol})
        except Exception as exc:
            raise ProviderUnavailable(f"Nova 訂閱 aggregates 失敗：{symbol}；{exc}") from exc
        self._subscribed.add(symbol)

    def _handle_message(self, message: Any) -> None:
        event = parse_realtime_market_event(message)
        listeners: list[Callable[[RealtimeMarketEvent], None]] = []
        if event is not None:
            with self._lock:
                listeners = list(self._listeners)
            for listener in listeners:
                try:
                    listener(event)
                except Exception:
                    continue
            if event.channel != "aggregates":
                return
        try:
            quote = parse_aggregates_message(message)
        except Exception:
            return
        with self._lock:
            self._quotes[quote.symbol] = quote


class UnavailableProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def get_quote(self, symbol: str) -> Quote:
        raise ProviderUnavailable(self.reason)

    def get_stock_rest_client(self) -> Any:
        raise ProviderUnavailable(self.reason)


def create_provider(settings: Settings) -> QuoteProvider:
    if settings.market_data_mode != "live":
        return UnavailableProvider("此獨立版不提供 mock 行情；請設定 TW_WATCH_MARKET_DATA_MODE=live 與 Nova 憑證")
    try:
        return TaishinNovaProvider(settings)
    except ProviderUnavailable as exc:
        return UnavailableProvider(str(exc))


def _marketdata_stock_client(sdk: Any) -> Any:
    marketdata = getattr(sdk, "marketdata", None)
    websocket_client = None
    if marketdata is not None:
        websocket_client = getattr(marketdata, "websocket_client", None) or getattr(marketdata, "webSocketClient", None)
    stock = getattr(websocket_client, "stock", None) if websocket_client is not None else None
    if stock is not None and hasattr(stock, "subscribe"):
        return stock

    legacy_stock = getattr(sdk, "stock", None)
    if legacy_stock is not None and hasattr(legacy_stock, "subscribe"):
        return legacy_stock
    raise ProviderUnavailable("Nova 即時行情 websocket client 不可用：找不到 sdk.marketdata.websocket_client.stock")


def _marketdata_rest_stock_client(sdk: Any) -> Any:
    marketdata = getattr(sdk, "marketdata", None)
    rest_client = getattr(marketdata, "rest_client", None) if marketdata is not None else None
    stock = getattr(rest_client, "stock", None) if rest_client is not None else None
    if stock is not None and hasattr(stock, "snapshot"):
        return stock
    raise ProviderUnavailable("Nova REST stock client 不可用：找不到 sdk.marketdata.rest_client.stock")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _path(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first(data: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _path(data, path)
        if value is not None:
            return value
    return None


def _infer_channel(data: Mapping[str, Any]) -> str:
    if "serial" in data or ("price" in data and "size" in data and "volume" in data):
        return "trades"
    if "open" in data and "high" in data and "low" in data and "close" in data:
        return "candles"
    if ("bids" in data or "asks" in data) and "total" not in data and "closePrice" not in data and "lastPrice" not in data:
        return "books"
    if "lastPrice" in data or "closePrice" in data or "total" in data:
        return "aggregates"
    return ""


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            if stripped.endswith("Z"):
                stripped = stripped[:-1] + "+00:00"
            parsed = datetime.fromisoformat(stripped)
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            value = stripped
    numeric = _number(value)
    if numeric is None:
        return None
    if numeric > 10_000_000_000_000:
        seconds = numeric / 1_000_000
    elif numeric > 10_000_000_000:
        seconds = numeric / 1_000
    else:
        seconds = numeric
    return datetime.fromtimestamp(seconds, timezone.utc)


def _levels(value: Any) -> tuple[list[OrderBookLevel], dict[str, bool]]:
    rows = value if isinstance(value, list) else []
    levels: list[OrderBookLevel] = []
    flags = {"invalid_depth_price": False}
    for row in rows[:5]:
        mapping = _mapping(row)
        price = _number(_first(mapping, "price", "bid", "ask"))
        size = _number(_first(mapping, "size", "volume", "qty", "quantity"))
        if price is None or size is None:
            flags["invalid_depth_price"] = True
            continue
        if price <= 0 or size <= 0:
            flags["invalid_depth_price"] = True
            continue
        levels.append(OrderBookLevel(price=price, size=size))
    return levels, flags


def _merge_depth_flags(flags: dict[str, bool], side: str, payload: Any, level_flags: dict[str, bool]) -> None:
    if payload is None:
        flags["provider_payload_missing_depth"] = True
        flags[f"provider_payload_missing_{side}s"] = True
    elif isinstance(payload, list) and not payload:
        flags["provider_payload_depth_empty"] = True
        flags[f"provider_payload_{side}s_empty"] = True
    elif not isinstance(payload, list):
        flags["provider_payload_missing_depth"] = True
        flags[f"provider_payload_invalid_{side}s"] = True
    if level_flags.get("invalid_depth_price"):
        flags["invalid_depth_price"] = True
