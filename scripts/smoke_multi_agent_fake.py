from __future__ import annotations

import argparse
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tw_watchdesk.config import Settings
from tw_watchdesk.llm import FakeJsonBackend
from tw_watchdesk.quote_diagnostics import QuoteDiagnostic
from tw_watchdesk.review import MultiAgentReviewOrchestrator
from tw_watchdesk.simulation import calculate_costs
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore


SCOUT_KEYS = [
    "min_turnover",
    "max_spread_pct",
    "daytrade_change_min",
    "daytrade_change_max",
    "swing_change_min",
    "swing_change_max",
    "liquidity_weight",
    "momentum_weight",
    "spread_weight",
    "limit_up_policy",
    "limit_down_policy",
    "missing_depth_policy",
    "max_candidates_daytrade",
    "max_candidates_swing",
    "eligible_list_policy",
]
DAYTRADE_KEYS = [
    "stop_loss_pct",
    "take_profit_pct",
    "risk_pct",
    "max_position_pct",
    "max_daily_loss_pct",
    "entry_start_time",
    "entry_end_time",
    "force_exit_time",
    "order_ttl_minutes",
    "max_spread_pct",
    "max_quote_age_seconds",
    "missing_depth_policy",
    "reentry_cooldown_minutes",
    "consecutive_loss_stop",
    "allow_limit_up_entry",
    "allow_limit_down_entry",
]
SWING_KEYS = [
    "stop_loss_pct",
    "take_profit_pct_short",
    "take_profit_pct_long",
    "long_holding_months",
    "risk_pct",
    "max_position_pct",
    "min_turnover",
    "max_spread_pct",
    "max_total_exposure_pct",
    "max_position_symbols",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run fake-data multi-agent review smoke test.")
    parser.add_argument("--db", type=Path, default=None, help="Optional SQLite path. Defaults to a temp DB.")
    args = parser.parse_args()
    if args.db is None:
        with tempfile.TemporaryDirectory(prefix="tw-watchdesk-smoke-", ignore_cleanup_errors=True) as folder:
            _run(Path(folder) / "smoke.sqlite3")
    else:
        if args.db.exists():
            args.db.unlink()
        _run(args.db)


def _run(db_path: Path) -> None:
    store = TradingStore(db_path)
    try:
        store.initialize()
        at = datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)
        trade_date = at.date().isoformat()
        _seed_market_day(store, at)
        backend = FakeJsonBackend(_fake_responses())
        result = MultiAgentReviewOrchestrator(
            store=store,
            settings=Settings(market_data_mode="fake", enable_news_context=True),
            backend=backend,
        ).run(trade_date, include_news_context=True, created_at=at)

        assert result.status == "completed", result
        assert sorted(result.pending_versions) == ["daytrade-v2", "scout-v2"], result.pending_versions
        assert store.get_active_strategy_version("scout").version == "scout-v1"
        assert store.get_strategy_version("scout", "scout-v2").status == "pending"
        assert len(store.list_agent_reviews(result.review_run_id)) == 6
        assert store.list_news_context_reviews(result.review_run_id), "NewsContextAgent did not persist context-only rows"

        promoted = store.promote_strategy_version("daytrade", "daytrade-v2", activate=True)
        assert promoted.status == "validated"
        assert store.get_active_strategy_version("daytrade").version == "daytrade-v2"

        diagnostics = store.list_quote_diagnostics(trade_date=trade_date, symbol="1718")
        assert diagnostics and diagnostics[0]["diagnosis"] == "疑似漲停無賣盤"
        print(
            "FAKE_SMOKE_OK "
            f"db={db_path} review_run_id={result.review_run_id} "
            f"pending={','.join(result.pending_versions)} "
            f"promoted_daytrade={promoted.version} "
            "diagnostic=likely_limit_up_no_asks"
        )
    finally:
        store.close()


def _seed_market_day(store: TradingStore, at: datetime) -> None:
    trade_date = at.date().isoformat()
    store.upsert_candidate(trade_date=trade_date, strategy="daytrade", symbol="2330", name="台積電", source="fake", created_at=at)
    store.upsert_candidate(trade_date=trade_date, strategy="swing", symbol="3707", name="漢磊", source="fake", created_at=at)
    _seed_roundtrip(store, at, DAYTRADE_ACCOUNT, "daytrade", "2330", 100.0, 1000, at + timedelta(minutes=30), 101.6)
    _seed_roundtrip(store, at, SWING_ACCOUNT, "swing", "3707", 80.0, 1000, at + timedelta(days=1), 84.0)
    store.insert_quote_diagnostic(
        trade_date=trade_date,
        strategy="daytrade",
        diagnostic=QuoteDiagnostic(
            symbol="1718",
            price=14.95,
            previous_close=13.6,
            limit_up=14.95,
            limit_down=12.25,
            best_bid=14.95,
            best_ask=None,
            bid_count=1,
            ask_count=0,
            exchange_time=at,
            received_at=at,
            exchange_age_seconds=0,
            receive_age_seconds=0,
            flags={"missing_asks": True, "likely_limit_up_no_asks": True},
            diagnosis="疑似漲停無賣盤",
            event_type="quote_limit_state_detected",
            title="疑似漲停無賣盤",
            payload_shape={"source": "fake", "bid_levels": 1, "ask_levels": 0},
        ),
        created_at=at,
    )


def _seed_roundtrip(
    store: TradingStore,
    at: datetime,
    account_id: str,
    strategy: str,
    symbol: str,
    buy_price: float,
    qty: int,
    sell_time: datetime,
    sell_price: float,
) -> None:
    buy_id = store.create_order(
        account_id=account_id,
        strategy=strategy,
        symbol=symbol,
        side="buy",
        price=buy_price,
        qty=qty,
        reason="fake smoke buy",
        expires_at=at + timedelta(minutes=5),
        created_at=at,
    )
    buy_order = store.get_order(buy_id)
    buy_costs = calculate_costs(side="buy", strategy=strategy, price=buy_price, qty=qty)
    store.record_fill(
        order=buy_order,
        price=buy_price,
        qty=qty,
        fee=buy_costs.fee,
        tax=buy_costs.tax,
        net_cash_delta=buy_costs.net_cash_delta,
        realized_pnl=0,
        filled_at=at + timedelta(seconds=1),
    )
    store.upsert_position_after_fill(
        account_id=account_id,
        strategy=strategy,
        symbol=symbol,
        side="buy",
        qty=qty,
        price=buy_price,
        fee=buy_costs.fee,
        realized_pnl=0,
        stop_loss=round(buy_price * 0.98, 2),
        take_profit=round(buy_price * 1.03, 2),
        at=at + timedelta(seconds=1),
    )
    sell_id = store.create_order(
        account_id=account_id,
        strategy=strategy,
        symbol=symbol,
        side="sell",
        price=sell_price,
        qty=qty,
        reason="fake smoke sell",
        expires_at=sell_time + timedelta(minutes=5),
        created_at=sell_time,
    )
    sell_order = store.get_order(sell_id)
    sell_costs = calculate_costs(side="sell", strategy=strategy, price=sell_price, qty=qty, avg_cost=buy_price, at=sell_time.date())
    store.record_fill(
        order=sell_order,
        price=sell_price,
        qty=qty,
        fee=sell_costs.fee,
        tax=sell_costs.tax,
        net_cash_delta=sell_costs.net_cash_delta,
        realized_pnl=sell_costs.realized_pnl,
        filled_at=sell_time + timedelta(seconds=1),
    )
    store.upsert_position_after_fill(
        account_id=account_id,
        strategy=strategy,
        symbol=symbol,
        side="sell",
        qty=qty,
        price=sell_price,
        fee=sell_costs.fee,
        realized_pnl=sell_costs.realized_pnl,
        stop_loss=None,
        take_profit=None,
        at=sell_time + timedelta(seconds=1),
    )


def _fake_responses() -> dict[str, dict[str, object]]:
    scout_changes = _none_changes(SCOUT_KEYS)
    scout_changes["max_spread_pct"] = 0.008
    daytrade_changes = _none_changes(DAYTRADE_KEYS)
    daytrade_changes["max_spread_pct"] = 0.005
    swing_changes = _none_changes(SWING_KEYS)
    return {
        "ScoutAgent": _agent_payload("propose_change", scout_changes),
        "DaytradeAgent": _agent_payload("propose_change", daytrade_changes),
        "SwingAgent": _agent_payload("record_review_only", swing_changes),
        "RiskAgent": {"summary": "風險可接受", "verdict": "pass", "confidence": 0.9, "rejections": [], "warnings": []},
        "CoachAgent": {
            "summary": "建立抓盤與當沖 pending 版本，短線只記錄檢討。",
            "confidence": 0.9,
            "proposals": [
                {"strategy": "scout", "action": "propose_change", "summary": "收緊五檔價差", "supporting_agent": "ScoutAgent"},
                {"strategy": "daytrade", "action": "propose_change", "summary": "收緊進場價差", "supporting_agent": "DaytradeAgent"},
                {"strategy": "swing", "action": "record_review_only", "summary": "短線不改版", "supporting_agent": "SwingAgent"},
            ],
            "rejected": [],
        },
        "NewsContextAgent": {
            "contexts": [
                {
                    "symbol": "1718",
                    "summary": "fake context-only：1718 當日疑似漲停無賣盤，僅供 UI 背景。",
                    "source_urls": ["https://example.invalid/1718-context-only"],
                }
            ]
        },
    }


def _none_changes(keys: list[str]) -> dict[str, object]:
    return {key: None for key in keys}


def _agent_payload(action: str, changes: dict[str, object]) -> dict[str, object]:
    return {
        "summary": f"fake {action}",
        "action": action,
        "evidence_quality": "sufficient",
        "confidence": 0.8,
        "parameter_changes": changes,
        "rules_text": "fake smoke pending rules",
        "expected_effect": "fake smoke expected effect",
        "risk_note": "fake smoke risk note",
        "reject_reasons": [],
        "supporting_event_ids": ["order:1"],
    }


if __name__ == "__main__":
    main()
