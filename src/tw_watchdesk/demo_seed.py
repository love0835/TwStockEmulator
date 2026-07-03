from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tw_watchdesk.simulation import calculate_costs
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore


STOCKS = [
    ("2330", "台積電", 99.0),
    ("2317", "鴻海", 218.0),
    ("3707", "漢磊", 87.7),
    ("2303", "聯電", 52.4),
    ("2454", "聯發科", 138.0),
    ("1303", "南亞", 47.5),
    ("2882", "國泰金", 66.2),
    ("3034", "聯詠", 538.0),
    ("1216", "統一", 86.4),
    ("2603", "長榮", 188.5),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="建立交易實驗室 demo DB")
    parser.add_argument("--scenario", default="all", choices=["all"])
    parser.add_argument("--db", type=Path, default=Path("data/trading_lab_demo.sqlite3"))
    parser.add_argument("--reset", action="store_true", help="若 DB 已存在則先刪除")
    args = parser.parse_args()
    if args.db.exists():
        if not args.reset:
            raise SystemExit(f"{args.db} 已存在；請加 --reset 重建 demo DB")
        args.db.unlink()
    store = TradingStore(args.db)
    store.initialize()
    seed_all(store)
    store.close()
    print(f"demo DB created: {args.db}")


def seed_all(store: TradingStore) -> None:
    start = datetime(2026, 6, 15, 1, 30, tzinfo=timezone.utc)
    for idx in range(10):
        day = start + timedelta(days=idx)
        trade_date = day.date().isoformat()
        symbol, name, base_price = STOCKS[idx % len(STOCKS)]
        candidate_id = store.upsert_candidate(
            trade_date=trade_date,
            strategy="swing",
            symbol=symbol,
            name=name,
            score=72 + idx,
            reason=f"demo：流動性與短線動能符合第 {idx + 1} 日測試情境",
            source="demo",
            created_at=day,
        )
        if idx in {2, 7}:
            order_id = store.create_order(
                account_id=SWING_ACCOUNT,
                strategy="swing",
                symbol=symbol,
                side="buy",
                price=base_price,
                qty=1000,
                reason="demo 掛買未成交，用於測掛單過期與流動性檢討",
                expires_at=day + timedelta(minutes=30),
                candidate_id=candidate_id,
                strategy_version="swing-v1",
                created_at=day,
            )
            store.expire_orders(day + timedelta(minutes=31))
            store.add_monitor_event(
                actor="swing_trader",
                phase="orders",
                event_type="demo_order_expired",
                title="demo 掛單過期",
                detail=f"order {order_id} 未成交，供會後討論流動性門檻。",
                strategy="swing",
                symbol=symbol,
                trade_date=trade_date,
                created_at=day + timedelta(minutes=31),
            )
            continue
        buy_price = round(base_price * (1 + (idx % 3) * 0.004), 2)
        buy_time = day
        buy_id = store.create_order(
            account_id=SWING_ACCOUNT,
            strategy="swing",
            symbol=symbol,
            side="buy",
            price=buy_price,
            qty=1000,
            reason="demo 短線買進",
            expires_at=buy_time + timedelta(minutes=30),
            candidate_id=candidate_id,
            strategy_version="swing-v1" if idx < 5 else "swing-v2",
            created_at=buy_time,
        )
        buy_order = store.get_order(buy_id)
        buy_costs = calculate_costs(side="buy", strategy="swing", price=buy_price, qty=1000)
        store.record_fill(order=buy_order, price=buy_price, qty=1000, fee=buy_costs.fee, tax=buy_costs.tax, net_cash_delta=buy_costs.net_cash_delta, realized_pnl=0, filled_at=buy_time + timedelta(seconds=3))
        store.upsert_position_after_fill(
            account_id=SWING_ACCOUNT,
            strategy="swing",
            symbol=symbol,
            side="buy",
            qty=1000,
            price=buy_price,
            fee=buy_costs.fee,
            realized_pnl=0,
            stop_loss=round(buy_price * 0.94, 2),
            take_profit=round(buy_price * 1.10, 2),
            strategy_version=buy_order.strategy_version,
            candidate_id=buy_order.candidate_id,
            entry_order_id=buy_order.id,
            scout_version=buy_order.scout_version,
            attribution_status=buy_order.attribution_status,
            at=buy_time + timedelta(seconds=3),
        )
        if idx % 2 == 0:
            sell_price = round(buy_price * (1.07 if idx in {0, 4, 8} else 0.95), 2)
            sell_time = buy_time + timedelta(days=1, minutes=30)
            sell_id = store.create_order(
                account_id=SWING_ACCOUNT,
                strategy="swing",
                symbol=symbol,
                side="sell",
                price=sell_price,
                qty=1000,
                reason="demo 停利/停損出場",
                expires_at=sell_time + timedelta(minutes=30),
                strategy_version=buy_order.strategy_version,
                created_at=sell_time,
            )
            sell_order = store.get_order(sell_id)
            sell_costs = calculate_costs(side="sell", strategy="swing", price=sell_price, qty=1000, avg_cost=buy_price)
            store.record_fill(
                order=sell_order,
                price=sell_price,
                qty=1000,
                fee=sell_costs.fee,
                tax=sell_costs.tax,
                net_cash_delta=sell_costs.net_cash_delta,
                realized_pnl=sell_costs.realized_pnl,
                filled_at=sell_time + timedelta(seconds=5),
            )
            store.upsert_position_after_fill(
                account_id=SWING_ACCOUNT,
                strategy="swing",
                symbol=symbol,
                side="sell",
                qty=1000,
                price=sell_price,
                fee=sell_costs.fee,
                realized_pnl=sell_costs.realized_pnl,
                stop_loss=None,
                take_profit=None,
                strategy_version=sell_order.strategy_version,
                at=sell_time + timedelta(seconds=5),
            )
        if idx in {3, 6}:
            store.add_monitor_event(
                actor="risk_manager",
                phase="risk_check",
                event_type="risk_blocked",
                title="demo 風控阻擋買單",
                detail="短線總曝險或流動性條件不佳，供會後討論降低追價。",
                severity="warning",
                strategy="swing",
                symbol=symbol,
                trade_date=trade_date,
                created_at=day + timedelta(minutes=10),
            )
        store.upsert_daily_review(
            trade_date,
            "swing",
            f"{trade_date} demo 短線日結：含成交、掛單與風控測試資料。",
            {"scenario": "demo", "day_index": idx},
            proposal_status="no_change" if idx < 5 else "version_created_applied",
            strategy_version="swing-v1" if idx < 5 else "swing-v2",
            llm_summary="demo 會後討論摘要",
            llm_discussion="demo：檢討追價、停損與流動性門檻，供策略版本頁展示。",
            llm_result={"demo": True},
        )
    v2 = store.create_strategy_version(
        strategy="swing",
        params={
            "stop_loss_pct": 0.055,
            "take_profit_pct_short": 0.085,
            "take_profit_pct_long": 0.11,
            "long_holding_months": 3,
            "risk_pct": 0.009,
            "max_position_pct": 0.23,
            "min_turnover": 150_000_000,
            "max_spread_pct": 0.015,
        },
        rules_text="demo swing-v2：降低單筆曝險並加入成交值與價差門檻，避免流動性差的短線標的。",
        discussion="demo 會後討論：掛單過期與風控阻擋集中在價差較大的標的，因此提高流動性要求。",
        summary="demo：流動性防守版",
        data_start="2026-06-15",
        data_end="2026-06-22",
        metrics={"demo": True, "reason": "liquidity"},
    )
    store.create_strategy_version(
        strategy="swing",
        parent_version=v2.version,
        params={
            "stop_loss_pct": 0.055,
            "take_profit_pct_short": 0.09,
            "take_profit_pct_long": 0.12,
            "long_holding_months": 3,
            "risk_pct": 0.009,
            "max_position_pct": 0.23,
            "min_turnover": 200_000_000,
            "max_spread_pct": 0.012,
        },
        rules_text="demo swing-v3：延續流動性門檻，略提高停利目標，只保留價差更窄的標的。",
        discussion="demo 會後討論：v2 後勝率改善，但停利過早，嘗試提高停利並收緊價差上限。",
        summary="demo：提高停利並收緊價差",
        data_start="2026-06-23",
        data_end="2026-06-26",
        metrics={"demo": True, "reason": "take_profit"},
    )
    _seed_daytrade(store, start)


def _seed_daytrade(store: TradingStore, start: datetime) -> None:
    for idx in range(6):
        day = start + timedelta(days=idx)
        trade_date = day.date().isoformat()
        symbol, name, base_price = STOCKS[(idx + 2) % len(STOCKS)]
        candidate_id = store.upsert_candidate(
            trade_date=trade_date,
            strategy="daytrade",
            symbol=symbol,
            name=name,
            score=82 - idx,
            reason=f"demo：當沖量能與波動符合第 {idx + 1} 日測試情境",
            source="demo",
            created_at=day - timedelta(minutes=20),
        )
        buy_price = round(base_price * (1 + idx * 0.002), 2)
        buy_qty = 100 if buy_price >= 300 else 1000
        buy_time = day.replace(hour=1, minute=15)
        buy_id = store.create_order(
            account_id=DAYTRADE_ACCOUNT,
            strategy="daytrade",
            symbol=symbol,
            side="buy",
            price=buy_price,
            qty=buy_qty,
            reason="demo 當沖開倉",
            expires_at=buy_time + timedelta(minutes=5),
            candidate_id=candidate_id,
            created_at=buy_time,
        )
        buy_order = store.get_order(buy_id)
        buy_costs = calculate_costs(side="buy", strategy="daytrade", price=buy_price, qty=buy_qty)
        store.record_fill(order=buy_order, price=buy_price, qty=buy_qty, fee=buy_costs.fee, tax=buy_costs.tax, net_cash_delta=buy_costs.net_cash_delta, realized_pnl=0, filled_at=buy_time + timedelta(seconds=2))
        store.upsert_position_after_fill(account_id=DAYTRADE_ACCOUNT, strategy="daytrade", symbol=symbol, side="buy", qty=buy_qty, price=buy_price, fee=buy_costs.fee, realized_pnl=0, stop_loss=round(buy_price * 0.985, 2), take_profit=round(buy_price * 1.025, 2), strategy_version=buy_order.strategy_version, candidate_id=buy_order.candidate_id, entry_order_id=buy_order.id, scout_version=buy_order.scout_version, attribution_status=buy_order.attribution_status, at=buy_time + timedelta(seconds=2))
        sell_price = round(buy_price * (1.018 if idx in {0, 1, 4} else 0.992), 2)
        sell_time = day.replace(hour=4, minute=20)
        sell_id = store.create_order(account_id=DAYTRADE_ACCOUNT, strategy="daytrade", symbol=symbol, side="sell", price=sell_price, qty=buy_qty, reason="demo 當沖出場", expires_at=sell_time + timedelta(minutes=5), candidate_id=candidate_id, created_at=sell_time)
        sell_order = store.get_order(sell_id)
        sell_costs = calculate_costs(side="sell", strategy="daytrade", price=sell_price, qty=buy_qty, avg_cost=buy_price, at=day.date())
        store.record_fill(order=sell_order, price=sell_price, qty=buy_qty, fee=sell_costs.fee, tax=sell_costs.tax, net_cash_delta=sell_costs.net_cash_delta, realized_pnl=sell_costs.realized_pnl, filled_at=sell_time + timedelta(seconds=2))
        store.upsert_position_after_fill(account_id=DAYTRADE_ACCOUNT, strategy="daytrade", symbol=symbol, side="sell", qty=buy_qty, price=sell_price, fee=sell_costs.fee, realized_pnl=sell_costs.realized_pnl, stop_loss=None, take_profit=None, at=sell_time + timedelta(seconds=2))
        if idx in {2, 5}:
            store.add_monitor_event(actor="risk_manager", phase="risk_check", event_type="risk_blocked", title="demo 當沖風控阻擋", detail="當沖連續虧損或單檔曝險過高，供會後討論降低追價與縮小部位。", severity="warning", strategy="daytrade", symbol=symbol, trade_date=trade_date, created_at=day.replace(hour=2, minute=30))
        if idx == 4:
            store.add_monitor_event(actor="risk_manager", phase="force_flatten", event_type="daytrade_forced_flatten", title="demo 當沖強制平倉", detail="13:25 後出場-only，供檢討尾盤流動性與持倉時間。", severity="warning", strategy="daytrade", symbol=symbol, trade_date=trade_date, created_at=day.replace(hour=5, minute=25))
        store.upsert_daily_review(
            trade_date,
            "daytrade",
            f"{trade_date} demo 當沖日結：含開倉、出場、風控與強制平倉測試資料。",
            {"scenario": "demo", "day_index": idx, "realized_pnl": sell_costs.realized_pnl},
            proposal_status="reviewed",
            llm_summary="demo 當沖會後討論摘要",
            llm_discussion="demo：檢討進場追價、停損速度、尾盤出場與單筆風險，供每日檢討頁展示。",
            llm_result={
                "summary": "demo 當沖會後討論摘要",
                "mistakes": ["追價後容易遇到尾盤流動性下降", "虧損日需要更早降部位"],
                "next_rules_to_test": ["連續兩筆虧損後停止新倉", "13:10 後只允許減倉"],
                "capital_suggestion": "維持資金，不加碼。",
            },
        )


if __name__ == "__main__":
    main()
