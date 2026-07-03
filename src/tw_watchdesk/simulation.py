from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from sqlite3 import Row
from zoneinfo import ZoneInfo

from tw_watchdesk.storage import Account, OrderRecord, Position, SWING_ACCOUNT, TradingStore


COMMISSION_RATE = 0.001425
SELL_TAX_RATE = 0.003
DAYTRADE_SELL_TAX_RATE = 0.0015
DAYTRADE_TAX_DISCOUNT_END = date(2027, 12, 31)

DAYTRADE_RISK_PCT = 0.0035
SWING_RISK_PCT = 0.01
MAX_SINGLE_SYMBOL_EXPOSURE_PCT = 0.25
MAX_SWING_TOTAL_EXPOSURE_PCT = 0.80
MAX_SWING_POSITION_SYMBOLS = 5
DAYTRADE_MAX_DAILY_LOSS_PCT = 0.02


@dataclass(frozen=True)
class CostResult:
    gross_amount: float
    fee: float
    tax: float
    net_cash_delta: float
    realized_pnl: float


@dataclass(frozen=True)
class RiskCheck:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class SessionWindow:
    can_open_daytrade: bool
    daytrade_exit_only: bool
    should_review: bool


def session_window(now: datetime, timezone_name: str = "Asia/Taipei") -> SessionWindow:
    local = now.astimezone(ZoneInfo(timezone_name))
    if local.weekday() >= 5:
        return SessionWindow(can_open_daytrade=False, daytrade_exit_only=False, should_review=False)
    current = local.time()
    return SessionWindow(
        can_open_daytrade=time(9, 10) <= current < time(13, 20),
        daytrade_exit_only=time(13, 25) <= current <= time(13, 30),
        should_review=current >= time(13, 30),
    )


def order_expiry(created_at: datetime, strategy: str) -> datetime:
    minutes = 5 if strategy == "daytrade" else 30
    return created_at + timedelta(minutes=minutes)


def bucket_bounds(value: datetime, timeframe_minutes: int) -> tuple[datetime, datetime]:
    minute = (value.minute // timeframe_minutes) * timeframe_minutes
    start = value.replace(minute=minute, second=0, microsecond=0)
    return start, start + timedelta(minutes=timeframe_minutes)


def should_fill(order: OrderRecord, bar: Row) -> bool:
    if str(bar["start_time"]) <= order.created_at.isoformat():
        return False
    if float(bar["volume"]) <= 0:
        return False
    low = float(bar["low"])
    high = float(bar["high"])
    if order.side == "buy":
        return low <= order.price
    return high >= order.price


def calculate_costs(
    *,
    side: str,
    strategy: str,
    price: float,
    qty: int,
    avg_cost: float | None = None,
    at: date | None = None,
) -> CostResult:
    at = at or date.today()
    gross = price * qty
    fee = round(gross * COMMISSION_RATE, 2)
    tax = 0.0
    realized = 0.0
    if side == "sell":
        tax_rate = DAYTRADE_SELL_TAX_RATE if strategy == "daytrade" and at <= DAYTRADE_TAX_DISCOUNT_END else SELL_TAX_RATE
        tax = round(gross * tax_rate, 2)
        if avg_cost is not None:
            realized = round((price - avg_cost) * qty - fee - tax, 2)
        net = gross - fee - tax
    else:
        net = -(gross + fee)
    return CostResult(
        gross_amount=round(gross, 2),
        fee=fee,
        tax=tax,
        net_cash_delta=round(net, 2),
        realized_pnl=realized,
    )


def risk_check_for_buy(
    *,
    store: TradingStore,
    account: Account,
    strategy: str,
    symbol: str,
    price: float,
    qty: int,
    stop_loss: float | None,
    now: datetime,
    risk_pct: float | None = None,
    max_position_pct: float | None = None,
    max_daily_loss_pct: float | None = None,
    max_total_exposure_pct: float | None = None,
    max_position_symbols: int | None = None,
) -> RiskCheck:
    if qty <= 0:
        return RiskCheck(False, "委託股數必須大於 0")
    if price <= 0:
        return RiskCheck(False, "委託價格必須大於 0")
    notional = price * qty
    estimated_cash_required = round(notional * (1 + COMMISSION_RATE), 2)
    available_cash = account.cash - account.reserved_cash
    if estimated_cash_required > available_cash:
        return RiskCheck(False, "可用現金不足")
    single_symbol_cap = max_position_pct if max_position_pct is not None else MAX_SINGLE_SYMBOL_EXPOSURE_PCT
    if notional > account.capital * single_symbol_cap:
        return RiskCheck(False, f"超過單檔最大曝險 {single_symbol_cap:.0%}")
    if strategy == "daytrade":
        daily_loss = store.daily_realized_pnl(account.id, now.date())
        daily_loss_cap = max_daily_loss_pct if max_daily_loss_pct is not None else DAYTRADE_MAX_DAILY_LOSS_PCT
        if daily_loss <= -(account.capital * daily_loss_cap):
            return RiskCheck(False, f"當沖每日虧損達 {daily_loss_cap:.0%}，停止新倉")
        effective_risk_pct = risk_pct if risk_pct is not None else DAYTRADE_RISK_PCT
    else:
        if account.id == SWING_ACCOUNT:
            symbols_after = store.portfolio_symbols_with_open_buys(account.id)
            symbol_cap = max_position_symbols if max_position_symbols is not None else MAX_SWING_POSITION_SYMBOLS
            if symbol.upper() not in symbols_after and len(symbols_after) >= symbol_cap:
                return RiskCheck(False, f"短線最多同時持有 {symbol_cap} 檔")
            total_after = store.open_notional(account.id) + store.open_buy_notional(account.id) + notional
            total_cap = max_total_exposure_pct if max_total_exposure_pct is not None else MAX_SWING_TOTAL_EXPOSURE_PCT
            if total_after > account.capital * total_cap:
                return RiskCheck(False, f"短線總曝險超過 {total_cap:.0%}")
        effective_risk_pct = risk_pct if risk_pct is not None else SWING_RISK_PCT
    if stop_loss is not None:
        risk_amount = max(0.0, (price - stop_loss) * qty)
        if risk_amount > account.capital * effective_risk_pct:
            return RiskCheck(False, f"超過單筆風險上限 {effective_risk_pct:.2%}")
    return RiskCheck(True)


def mark_filled(store: TradingStore, order: OrderRecord, fill_price: float, filled_at: datetime) -> None:
    position: Position | None = store.get_position(order.account_id, order.symbol)
    avg_cost = position.avg_cost if position else None
    costs = calculate_costs(
        side=order.side,
        strategy=order.strategy,
        price=fill_price,
        qty=order.qty,
        avg_cost=avg_cost,
        at=filled_at.date(),
    )
    store.record_fill(
        order=order,
        price=fill_price,
        qty=order.qty,
        fee=costs.fee,
        tax=costs.tax,
        net_cash_delta=costs.net_cash_delta,
        realized_pnl=costs.realized_pnl,
        filled_at=filled_at,
    )
    store.upsert_position_after_fill(
        account_id=order.account_id,
        strategy=order.strategy,
        symbol=order.symbol,
        side=order.side,
        qty=order.qty,
        price=fill_price,
        fee=costs.fee,
        realized_pnl=costs.realized_pnl,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        strategy_version=order.strategy_version,
        at=filled_at,
    )
