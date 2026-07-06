import json
from datetime import datetime, timedelta, timezone

from tw_watchdesk.config import Settings
from tw_watchdesk.llm import FakeJsonBackend
from tw_watchdesk.review import MultiAgentReviewOrchestrator
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore


def _agent_payload(action: str, changes: dict[str, object], *, supporting: list[str] | None = None) -> dict[str, object]:
    return {
        "summary": f"{action} summary",
        "action": action,
        "evidence_quality": "sufficient",
        "confidence": 0.8,
        "parameter_changes": changes,
        "rules_text": "unit pending rules",
        "expected_effect": "unit expected effect",
        "risk_note": "unit risk note",
        "reject_reasons": [],
        "supporting_event_ids": supporting or ["order:1"],
    }


def _none_changes(keys: list[str]) -> dict[str, object]:
    return {key: None for key in keys}


def _seed_review_evidence(store: TradingStore, at: datetime) -> None:
    trade_date = at.date().isoformat()
    daytrade_candidate_id = store.upsert_candidate(trade_date=trade_date, strategy="daytrade", symbol="2330", name="台積電", source="manual", created_at=at)
    swing_candidate_id = store.upsert_candidate(trade_date=trade_date, strategy="swing", symbol="3707", name="漢磊", source="manual", created_at=at)
    daytrade_order = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="unit",
        expires_at=at + timedelta(minutes=5),
        candidate_id=daytrade_candidate_id,
        created_at=at,
    )
    order = store.get_order(daytrade_order)
    store.record_fill(order=order, price=100, qty=1000, fee=142.5, tax=0, net_cash_delta=-100_142.5, realized_pnl=0, filled_at=at)
    store.create_order(
        account_id=SWING_ACCOUNT,
        strategy="swing",
        symbol="3707",
        side="buy",
        price=80,
        qty=1000,
        reason="unit swing",
        expires_at=at + timedelta(minutes=30),
        candidate_id=swing_candidate_id,
        created_at=at,
    )


def test_multi_agent_review_creates_pending_versions_without_activation(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    at = datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)
    _seed_review_evidence(store, at)
    scout_changes = _none_changes(
        [
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
    )
    scout_changes["max_spread_pct"] = 0.008
    daytrade_changes = _none_changes(
        [
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
    )
    daytrade_changes["max_spread_pct"] = 0.005
    swing_changes = _none_changes(
        [
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
    )
    backend = FakeJsonBackend(
        {
            "ScoutAgent": _agent_payload("propose_change", scout_changes),
            "DaytradeAgent": _agent_payload("propose_change", daytrade_changes),
            "SwingAgent": _agent_payload("record_review_only", swing_changes),
            "RiskAgent": {"summary": "pass", "verdict": "pass", "confidence": 0.9, "rejections": [], "warnings": []},
            "CoachAgent": {
                "summary": "route",
                "confidence": 0.9,
                "proposals": [
                    {"strategy": "scout", "action": "propose_change", "summary": "scout pending", "supporting_agent": "ScoutAgent"},
                    {"strategy": "daytrade", "action": "propose_change", "summary": "daytrade pending", "supporting_agent": "DaytradeAgent"},
                    {"strategy": "swing", "action": "record_review_only", "summary": "swing no change", "supporting_agent": "SwingAgent"},
                ],
                "rejected": [],
            },
        }
    )

    result = MultiAgentReviewOrchestrator(store=store, settings=Settings(market_data_mode="fake"), backend=backend).run("2026-07-03")

    assert result.status == "completed"
    assert backend.calls
    for call in backend.calls:
        payload = json.loads(call["prompt"])
        assert payload["output_language"] == "zh-Hant-TW"
        assert any("Traditional Chinese" in rule for rule in payload["hard_rules"])
    daytrade_prompt = next(json.loads(call["prompt"]) for call in backend.calls if json.loads(call["prompt"]).get("strategy") == "daytrade")
    assert any("single trading day" in rule for rule in daytrade_prompt["evaluation_policy"])
    assert daytrade_prompt["evidence"]["date_distribution"]["orders"]["counts"] == {"2026-07-03": 1}
    assert daytrade_prompt["evidence"]["date_distribution"]["fills"]["counts"] == {"2026-07-03": 1}
    assert sorted(result.pending_versions) == ["daytrade-v2", "scout-v2"]
    assert store.get_active_strategy_version("scout").version == "scout-v2"
    assert store.get_active_strategy_version("daytrade").version == "daytrade-v2"
    assert store.get_strategy_version("scout", "scout-v2").status == "validated"
    assert store.get_strategy_version("daytrade", "daytrade-v2").status == "validated"
    run = store.list_review_runs(review_date="2026-07-03")[0]
    assert run["status"] == "completed"
    evidence = json.loads(store.get_review_evidence(run["evidence_id"])["evidence_json"])
    assert evidence["attribution_summary"]["orders"]["status_counts"]["complete"] == 2
    assert evidence["attribution_summary"]["fills"]["status_counts"]["complete"] == 1
    assert len(store.list_agent_reviews(run["id"])) == 5
    statuses = {(row["strategy"], row["status"]) for row in store.list_strategy_proposals(run["id"])}
    assert ("scout", "version_created_applied") in statuses
    assert ("daytrade", "version_created_applied") in statuses
    swing_review = [row for row in store.list_daily_reviews(limit=10) if row["strategy"] == "swing"][0]
    assert "review_only" not in swing_review["summary"]
    assert "已討論，不需改版" in swing_review["summary"]
    assert backend.calls[0]["allow_web_search"] is False


def test_multi_agent_review_rejects_news_only_strategy_support(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    at = datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)
    _seed_review_evidence(store, at)
    scout_changes = _none_changes(
        [
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
    )
    scout_changes["max_spread_pct"] = 0.008
    backend = FakeJsonBackend(
        {
            "ScoutAgent": _agent_payload("propose_change", scout_changes, supporting=["news:2330"]),
            "DaytradeAgent": _agent_payload("record_review_only", {}),
            "SwingAgent": _agent_payload("record_review_only", {}),
            "RiskAgent": {"summary": "pass", "verdict": "pass", "confidence": 0.9, "rejections": [], "warnings": []},
            "CoachAgent": {
                "summary": "route",
                "confidence": 0.9,
                "proposals": [
                    {"strategy": "scout", "action": "propose_change", "summary": "scout pending", "supporting_agent": "ScoutAgent"}
                ],
                "rejected": [],
            },
        }
    )

    result = MultiAgentReviewOrchestrator(store=store, settings=Settings(), backend=backend).run("2026-07-03")

    assert result.pending_versions == []
    proposal = [row for row in store.list_strategy_proposals(result.review_run_id) if row["strategy"] == "scout"][0]
    assert proposal["status"] == "validation_failed"
    assert "NewsContextAgent" in proposal["validation_json"]


def test_multi_agent_review_respects_manual_lock(tmp_path) -> None:
    store = TradingStore(tmp_path / "lab.sqlite3")
    store.initialize()
    store.set_strategy_version_state("daytrade", "daytrade-v1", "manual_lock")
    at = datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)
    _seed_review_evidence(store, at)
    daytrade_changes = _none_changes(
        [
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
    )
    daytrade_changes["max_spread_pct"] = 0.005
    backend = FakeJsonBackend(
        {
            "ScoutAgent": _agent_payload("record_review_only", {}),
            "DaytradeAgent": _agent_payload("propose_change", daytrade_changes),
            "SwingAgent": _agent_payload("record_review_only", {}),
            "RiskAgent": {"summary": "pass", "verdict": "pass", "confidence": 0.9, "rejections": [], "warnings": []},
            "CoachAgent": {
                "summary": "route",
                "confidence": 0.9,
                "proposals": [
                    {"strategy": "daytrade", "action": "propose_change", "summary": "daytrade locked", "supporting_agent": "DaytradeAgent"}
                ],
                "rejected": [],
            },
        }
    )

    result = MultiAgentReviewOrchestrator(store=store, settings=Settings(market_data_mode="fake"), backend=backend).run("2026-07-03")

    assert result.pending_versions == ["daytrade-v2"]
    assert store.get_active_strategy_version("daytrade").version == "daytrade-v1"
    assert store.get_strategy_version("daytrade", "daytrade-v2").status == "validated"
    proposals = store.list_strategy_proposals(result.review_run_id)
    assert any(row["strategy"] == "daytrade" and row["status"] == "version_created_locked" for row in proposals)
