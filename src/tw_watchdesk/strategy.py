from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tw_watchdesk.config import Settings
from tw_watchdesk.models import Advice, DataQuality, Quote, WatchState
from tw_watchdesk.quote_diagnostics import diagnose_quote_quality, quality_reasons
from tw_watchdesk.strategy_versions import DaytradeStrategyParams, SwingStrategyParams, default_daytrade_params, default_swing_params


def build_watch_state(
    settings: Settings,
    symbol: str,
    capital: float,
    strategy: str,
    holding_months: int | None,
    quote: Quote | None,
    error: str | None = None,
    now: datetime | None = None,
    swing_params: SwingStrategyParams | None = None,
    daytrade_params: DaytradeStrategyParams | None = None,
    strategy_version: str = "",
) -> WatchState:
    now = now or datetime.now(timezone.utc)
    institutional_note = "外資 / 投信：第一版僅作官方日報背景，不作為盤中即時條件。"
    if quote is None:
        quality = DataQuality(status="blocked", age_seconds=None, reasons=[error or "尚未取得即時資料"])
        return WatchState(
            status="blocked",
            quote=None,
            quality=quality,
            advice=_blocked_advice("；".join(quality.reasons)),
            updated_at=now,
            institutional_note=institutional_note,
        )
    quality_settings = settings
    if strategy.lower().strip() == "daytrade" and daytrade_params is not None:
        quality_settings = replace(settings, stale_seconds=daytrade_params.max_quote_age_seconds)
    quality = assess_quality(quality_settings, quote, now)
    if quality.status != "ok":
        return WatchState(
            status="blocked",
            quote=quote,
            quality=quality,
            advice=_blocked_advice("；".join(quality.reasons)),
            updated_at=now,
            institutional_note=institutional_note,
        )
    advice = build_advice(settings, symbol, capital, strategy, holding_months, quote, now, swing_params=swing_params, daytrade_params=daytrade_params, strategy_version=strategy_version)
    status = "ok" if advice.action != "blocked" else "blocked"
    return WatchState(
        status=status,
        quote=quote,
        quality=quality,
        advice=advice,
        updated_at=now,
        institutional_note=institutional_note,
    )


def assess_quality(settings: Settings, quote: Quote, now: datetime | None = None) -> DataQuality:
    now = now or datetime.now(timezone.utc)
    diagnostic = diagnose_quote_quality(settings, quote, now)
    reasons = quality_reasons(diagnostic, settings)
    if not quote.is_realtime:
        reasons.append("資料源未標記為即時")
    for key, active in quote.flags.items():
        if active and key in {"missing_price", "missing_exchange_time"}:
            reasons.append(key)
    return DataQuality(
        status="blocked" if reasons else "ok",
        age_seconds=diagnostic.exchange_age_seconds,
        receive_age_seconds=diagnostic.receive_age_seconds,
        reasons=list(dict.fromkeys(reasons)),
        flags={key: value for key, value in diagnostic.flags.items() if value},
        diagnosis=diagnostic.diagnosis,
    )


def build_advice(
    settings: Settings,
    symbol: str,
    capital: float,
    strategy: str,
    holding_months: int | None,
    quote: Quote,
    now: datetime | None = None,
    *,
    swing_params: SwingStrategyParams | None = None,
    daytrade_params: DaytradeStrategyParams | None = None,
    strategy_version: str = "",
) -> Advice:
    now = now or datetime.now(timezone.utc)
    strategy = strategy.lower().strip()
    if capital <= 0:
        return _blocked_advice("起始金額必須大於 0")
    eligibility_note = ""
    if strategy == "daytrade":
        params = daytrade_params or default_daytrade_params()
        eligible, reason, eligibility_note = _is_daytrade_eligible(settings.daytrade_eligible_symbols_file, symbol)
        if not eligible:
            return _blocked_advice(reason)
        local_now = now.astimezone(ZoneInfo(settings.timezone))
        if not _is_tw_regular_session(local_now):
            return _blocked_advice("非台股一般交易時段，不建立當沖建議")
        entry_start = _parse_hhmm(params.entry_start_time)
        entry_end = _parse_hhmm(params.entry_end_time)
        force_exit = _parse_hhmm(params.force_exit_time)
        if local_now.time() >= force_exit or local_now.time() >= entry_end:
            return Advice(
                action="exit_only",
                buy_price=None,
                sell_price=quote.bid_levels[0].price,
                qty=0,
                max_notional=0.0,
                stop_loss=None,
                take_profit=None,
                reason="已進入當沖收盤風控時段，只檢查出場，不新建倉。",
                risk_flags=["daytrade_exit_window"],
            )
        if local_now.time() < entry_start:
            return _blocked_advice("尚未到當沖策略進場時間")
        bid = quote.bid_levels[0].price
        ask = quote.ask_levels[0].price
        spread_pct = (ask - bid) / bid if bid > 0 else 0.0
        if spread_pct > params.max_spread_pct:
            return _blocked_advice(f"當沖策略版本 {strategy_version or 'daytrade-v1'}：買賣價差 {spread_pct:.2%} 超過上限 {params.max_spread_pct:.2%}")
        stop_pct = params.stop_loss_pct
        take_pct = params.take_profit_pct
        risk_pct = params.risk_pct
        max_position_pct = params.max_position_pct
        label = f"當沖 {strategy_version or 'daytrade-v1'}"
    elif strategy == "swing":
        if holding_months is None or not 1 <= holding_months <= 12:
            return _blocked_advice("短線波段需輸入 1 到 12 個月持有月份")
        params = swing_params or default_swing_params()
        bid = quote.bid_levels[0].price
        ask = quote.ask_levels[0].price
        if quote.turnover < params.min_turnover:
            return _blocked_advice(f"短線策略版本 {strategy_version or 'swing-v1'}：成交值低於流動性門檻")
        spread_pct = (ask - bid) / bid if bid > 0 else 0.0
        if spread_pct > params.max_spread_pct:
            return _blocked_advice(f"短線策略版本 {strategy_version or 'swing-v1'}：買賣價差 {spread_pct:.2%} 超過上限 {params.max_spread_pct:.2%}")
        stop_pct = params.stop_loss_pct
        take_pct = params.take_profit_pct_long if holding_months >= params.long_holding_months else params.take_profit_pct_short
        risk_pct = params.risk_pct
        max_position_pct = params.max_position_pct
        label = f"短線 {holding_months} 個月 {strategy_version or 'swing-v1'}"
    else:
        return _blocked_advice("策略只支援 daytrade 或 swing")

    buy_price = quote.bid_levels[0].price
    sell_price = quote.ask_levels[0].price
    max_notional = capital * max_position_pct
    stop_loss = round(buy_price * (1 - stop_pct), 2)
    take_profit = round(buy_price * (1 + take_pct), 2)
    risk_per_share = max(0.01, buy_price - stop_loss)
    qty_by_cash = int(max_notional // buy_price)
    qty_by_risk = int((capital * risk_pct) // risk_per_share)
    qty = min(qty_by_cash, qty_by_risk)
    qty = int(qty // 1000) * 1000
    if qty <= 0:
        return _blocked_advice("資金或單筆風險不足以建立一張整股部位")
    return Advice(
        action="buy",
        buy_price=buy_price,
        sell_price=sell_price,
        qty=qty,
        max_notional=round(max_notional, 2),
        stop_loss=stop_loss,
        take_profit=take_profit,
        reason=f"{label} 規則：以最佳買一作掛買基準，最佳賣一作賣出參考；資料不即時則不給價。{eligibility_note}",
        risk_flags=[eligibility_note] if eligibility_note else [],
        strategy_version=strategy_version,
    )


def _blocked_advice(reason: str) -> Advice:
    return Advice(
        action="blocked",
        buy_price=None,
        sell_price=None,
        qty=0,
        max_notional=0.0,
        stop_loss=None,
        take_profit=None,
        reason=reason or "資料不可用，不產生攻略。",
        risk_flags=[reason] if reason else [],
    )


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _is_daytrade_eligible(path: Path | None, symbol: str) -> tuple[bool, str, str]:
    if path is None:
        return True, "", "未使用本機當沖資格清單。"
    if not path.exists():
        return True, "", "未使用本機當沖資格清單。"
    symbols: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        symbols.update(part.strip().upper() for part in cleaned.split(",") if part.strip())
    if not symbols:
        return True, "", "本機當沖資格清單空白，未套用清單限制。"
    if symbol.upper() not in symbols:
        return False, f"{symbol} 不在當沖資格清單內", ""
    return True, "", ""


def _is_tw_regular_session(local_now: datetime) -> bool:
    if local_now.weekday() >= 5:
        return False
    return time(9, 0) <= local_now.time() <= time(13, 30)
