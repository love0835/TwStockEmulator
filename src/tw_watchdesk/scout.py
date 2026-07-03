from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tw_watchdesk.config import Settings
from tw_watchdesk.strategy_versions import ScoutStrategyParams, default_scout_params


MARKETS = ("TSE", "OTC")


class ScoutDataError(RuntimeError):
    pass


class ScoutDataProvider(Protocol):
    def fetch_universe(self) -> list["ScoutStock"]:
        ...


@dataclass(frozen=True)
class ScoutStock:
    symbol: str
    name: str
    market: str
    price: float
    previous_close: float
    change_pct: float
    volume: float
    turnover: float
    bid_price: float | None
    ask_price: float | None
    source_tags: tuple[str, ...]

    @property
    def spread_pct(self) -> float:
        if self.bid_price is None or self.ask_price is None or self.price <= 0 or self.ask_price < self.bid_price:
            return 0.01
        return (self.ask_price - self.bid_price) / self.price


@dataclass(frozen=True)
class ScoutPick:
    strategy: str
    symbol: str
    name: str
    score: float
    reason: str
    metrics: dict[str, float | str]


@dataclass(frozen=True)
class ScoutResult:
    daytrade: list[ScoutPick]
    swing: list[ScoutPick]
    excluded_counts: dict[str, int]
    scanned: int
    notes: tuple[str, ...] = ()


class NovaRestScoutDataProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def fetch_universe(self) -> list[ScoutStock]:
        getter = getattr(self.provider, "get_stock_rest_client", None)
        if getter is None:
            raise ScoutDataError("目前行情 provider 不支援 Nova REST stock client")
        try:
            client = getter()
            snapshot = getattr(client, "snapshot")
        except Exception as exc:
            raise ScoutDataError(f"Nova REST stock client 不可用：{exc}") from exc

        stocks: dict[str, ScoutStock] = {}
        for market in MARKETS:
            for endpoint, params in _snapshot_calls(market):
                try:
                    payload = getattr(snapshot, endpoint)(**params)
                except Exception as exc:
                    raise ScoutDataError(f"Nova REST snapshot.{endpoint}({market}) 失敗：{exc}") from exc
                for row in _rows(payload):
                    stock = _parse_stock(row, market, endpoint)
                    if stock is None:
                        continue
                    existing = stocks.get(stock.symbol)
                    if existing is None or _data_quality_key(stock) > _data_quality_key(existing):
                        tags = set(existing.source_tags if existing else ())
                        tags.update(stock.source_tags)
                        stocks[stock.symbol] = ScoutStock(
                            symbol=stock.symbol,
                            name=stock.name,
                            market=stock.market,
                            price=stock.price,
                            previous_close=stock.previous_close,
                            change_pct=stock.change_pct,
                            volume=stock.volume,
                            turnover=stock.turnover,
                            bid_price=stock.bid_price,
                            ask_price=stock.ask_price,
                            source_tags=tuple(sorted(tags)),
                        )
                    else:
                        tags = set(existing.source_tags)
                        tags.update(stock.source_tags)
                        stocks[stock.symbol] = ScoutStock(
                            symbol=existing.symbol,
                            name=existing.name,
                            market=existing.market,
                            price=existing.price,
                            previous_close=existing.previous_close,
                            change_pct=existing.change_pct,
                            volume=existing.volume,
                            turnover=existing.turnover,
                            bid_price=existing.bid_price,
                            ask_price=existing.ask_price,
                            source_tags=tuple(sorted(tags)),
                        )
        return list(stocks.values())


def _snapshot_calls(market: str) -> tuple[tuple[str, dict[str, str]], ...]:
    return (
        ("actives", {"market": market, "trade": "value", "type": "COMMONSTOCK"}),
        ("actives", {"market": market, "trade": "volume", "type": "COMMONSTOCK"}),
        ("movers", {"market": market, "direction": "up", "change": "percent", "type": "COMMONSTOCK"}),
    )


def select_candidates(settings: Settings, provider: ScoutDataProvider, params: ScoutStrategyParams | None = None) -> ScoutResult:
    params = params or default_scout_params()
    universe = provider.fetch_universe()
    excluded = Counter()
    manual_excluded = _read_symbol_file(settings.scout_excluded_symbols_file)
    daytrade_eligible = _read_symbol_file(settings.daytrade_eligible_symbols_file)
    notes: list[str] = []
    if not daytrade_eligible:
        notes.append("當沖資格清單未設定，使用 Nova COMMONSTOCK 普通股排行選股")

    usable: list[ScoutStock] = []
    for stock in universe:
        reason = _common_exclusion(stock, manual_excluded, params)
        if reason:
            excluded[reason] += 1
            continue
        usable.append(stock)

    daytrade_limit = min(max(1, settings.scout_max_daytrade), params.max_candidates_daytrade)
    swing_limit = min(max(1, settings.scout_max_swing), params.max_candidates_swing)
    daytrade = _rank_daytrade(usable, daytrade_eligible, excluded, daytrade_limit, params)
    swing = _rank_swing(usable, swing_limit, params)
    return ScoutResult(daytrade=daytrade, swing=swing, excluded_counts=dict(excluded), scanned=len(universe), notes=tuple(notes))


def _rank_daytrade(stocks: list[ScoutStock], eligible: set[str], excluded: Counter[str], limit: int, params: ScoutStrategyParams) -> list[ScoutPick]:
    picks: list[ScoutPick] = []
    for stock in stocks:
        if eligible and stock.symbol not in eligible:
            excluded["不在當沖資格清單"] += 1
            continue
        if stock.change_pct <= params.daytrade_change_min:
            excluded["當沖動能不足"] += 1
            continue
        if stock.change_pct >= params.daytrade_change_max:
            excluded["當沖漲幅過熱"] += 1
            continue
        score = _weighted_score(stock, params, swing=False)
        picks.append(_pick("daytrade", stock, score))
    picks.sort(key=lambda item: (-item.score, item.symbol))
    return picks[:limit]


def _rank_swing(stocks: list[ScoutStock], limit: int, params: ScoutStrategyParams) -> list[ScoutPick]:
    picks: list[ScoutPick] = []
    for stock in stocks:
        if stock.change_pct <= params.swing_change_min:
            continue
        if stock.change_pct >= params.swing_change_max:
            continue
        score = _weighted_score(stock, params, swing=True)
        picks.append(_pick("swing", stock, score))
    picks.sort(key=lambda item: (-item.score, item.symbol))
    return picks[:limit]


def _pick(strategy: str, stock: ScoutStock, score: float) -> ScoutPick:
    reason = (
        f"auto_scout：成交量 {stock.volume:,.0f}，成交值 {stock.turnover:,.0f}，"
        f"漲跌幅 {stock.change_pct:+.2%}，價差 {stock.spread_pct:.2%}"
    )
    return ScoutPick(
        strategy=strategy,
        symbol=stock.symbol,
        name=stock.name,
        score=round(score, 2),
        reason=reason,
        metrics={
            "market": stock.market,
            "price": stock.price,
            "change_pct": round(stock.change_pct, 6),
            "volume": stock.volume,
            "turnover": stock.turnover,
            "spread_pct": round(stock.spread_pct, 6),
        },
    )


def _common_exclusion(stock: ScoutStock, manual_excluded: set[str], params: ScoutStrategyParams) -> str | None:
    if stock.symbol in manual_excluded:
        return "手動排除"
    if stock.market not in MARKETS:
        return "非 TSE/OTC"
    if not stock.symbol.isdigit() or len(stock.symbol) != 4:
        return "非普通股代號"
    if stock.symbol.startswith("00"):
        return "排除 ETF/ETN"
    if _looks_like_non_common_stock(stock.name):
        return "排除非普通股"
    if stock.price <= 0 or stock.previous_close <= 0:
        return "價格資料不足"
    if stock.volume <= 0:
        return "缺量"
    if stock.turnover <= 0:
        return "成交值資料不足"
    if stock.turnover < params.min_turnover:
        return "成交值低於抓盤門檻"
    if stock.spread_pct > params.max_spread_pct:
        return "價差超過抓盤門檻"
    return None


def _looks_like_non_common_stock(name: str) -> bool:
    upper = name.upper()
    keywords = ("ETF", "ETN", "權證", "購", "售", "牛", "熊", "債", "受益", "基金", "特別股")
    return any(keyword in upper for keyword in keywords)


def _liquidity_score(stock: ScoutStock) -> float:
    volume_score = min(35.0, math.log10(max(stock.volume, 1)) * 6.0)
    turnover_score = min(35.0, math.log10(max(stock.turnover, 1)) * 3.2)
    return volume_score + turnover_score


def _momentum_score(stock: ScoutStock) -> float:
    if stock.change_pct < 0:
        return max(-8.0, stock.change_pct * 150)
    return min(25.0, stock.change_pct * 260)


def _swing_momentum_score(stock: ScoutStock) -> float:
    if stock.change_pct < 0:
        return max(-10.0, stock.change_pct * 180)
    if stock.change_pct <= 0.045:
        return stock.change_pct * 260
    return 11.7 - ((stock.change_pct - 0.045) * 180)


def _spread_score(stock: ScoutStock) -> float:
    spread = stock.spread_pct
    if spread <= 0.0025:
        return 12
    if spread <= 0.006:
        return 6
    return -10


def _weighted_score(stock: ScoutStock, params: ScoutStrategyParams, *, swing: bool) -> float:
    liquidity = _liquidity_score(stock)
    momentum = _swing_momentum_score(stock) if swing else _momentum_score(stock)
    spread = _spread_score(stock)
    return (
        liquidity * params.liquidity_weight
        + momentum * params.momentum_weight
        + spread * params.spread_weight
    )


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("data", "items", "list", "quotes", "actives", "movers", "stocks"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            nested = _rows(value)
            if nested:
                return nested
    return []


def _parse_stock(row: Mapping[str, Any], market: str, source: str) -> ScoutStock | None:
    symbol = str(_first(row, "symbol", "code", "stockNo", "ticker", "securityCode") or "").strip().upper()
    if not symbol:
        return None
    price = _number(_first(row, "lastPrice", "lastTrade.price", "closePrice", "price", "close"))
    previous_close = _number(_first(row, "previousClose", "referencePrice", "prevClose", "yesterdayPrice"))
    change = _number(_first(row, "change", "priceChange", "changePrice"))
    raw_change_pct = _change_pct_from_row(row)
    if previous_close is None and price is not None:
        if change is not None:
            previous_close = price - change
        elif raw_change_pct is not None and raw_change_pct > -1:
            previous_close = price / (1 + raw_change_pct)
    volume = _number(_first(row, "total.tradeVolume", "tradeVolume", "volume", "totalVolume"))
    turnover = _number(_first(row, "total.tradeValue", "tradeValue", "turnover", "amount", "totalAmount"))
    bid = _number(_first(row, "bestBidPrice", "bidPrice", "bids.0.price"))
    ask = _number(_first(row, "bestAskPrice", "askPrice", "asks.0.price"))
    change_pct = _change_pct(row, price, previous_close, raw_change_pct)
    if price is None or previous_close is None or volume is None:
        return None
    if turnover is None:
        turnover = price * volume * 1000
    return ScoutStock(
        symbol=symbol,
        name=str(_first(row, "name", "symbolName", "stockName", "securityName") or symbol),
        market=str(_first(row, "market", "exchange", "marketType") or market).upper(),
        price=price,
        previous_close=previous_close,
        change_pct=change_pct,
        volume=volume,
        turnover=turnover,
        bid_price=bid,
        ask_price=ask,
        source_tags=(source,),
    )


def _change_pct(row: Mapping[str, Any], price: float | None, previous_close: float | None, raw_change_pct: float | None = None) -> float:
    if raw_change_pct is not None:
        return raw_change_pct
    if price is None or previous_close is None or previous_close <= 0:
        return 0.0
    return (price - previous_close) / previous_close


def _normalized_change_pct(value: Any) -> float | None:
    raw = _number(value)
    if raw is None:
        return None
    return raw / 100 if abs(raw) > 1 else raw


def _change_pct_from_row(row: Mapping[str, Any]) -> float | None:
    for key in ("changePercent", "changeRate", "percentChange"):
        raw = _number(_first(row, key))
        if raw is not None:
            return raw / 100
    return _normalized_change_pct(_first(row, "change_pct"))


def _data_quality_key(stock: ScoutStock) -> tuple[float, float, int]:
    return (stock.turnover, stock.volume, 0 if stock.bid_price is None or stock.ask_price is None else 1)


def _read_symbol_file(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    symbols: set[str] = set()
    for raw in path.read_text(encoding="utf-8").replace(",", "\n").splitlines():
        token = raw.strip().upper()
        if not token or token.startswith("#"):
            continue
        symbols.add(token)
    return symbols


def _first(data: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if isinstance(current, Mapping):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if index < len(current) else None
            else:
                current = None
            if current is None:
                break
        if current is not None:
            return current
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
