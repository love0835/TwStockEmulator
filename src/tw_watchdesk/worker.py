from __future__ import annotations

import json
import hashlib
import threading
import queue
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from sqlite3 import Row
from zoneinfo import ZoneInfo

from tw_watchdesk.config import Settings
from tw_watchdesk.llm import (
    AnthropicMessagesAdapter,
    CodexExecAdapter,
    LlmJsonBackend,
    OpenAIResponsesAdapter,
    daily_review_schema,
    swing_strategy_review_schema,
)
from tw_watchdesk.models import Quote, RealtimeMarketEvent
from tw_watchdesk.nova import ProviderUnavailable, QuoteProvider, parse_aggregates_message
from tw_watchdesk.quote_diagnostics import diagnose_quote_quality
from tw_watchdesk.review import MultiAgentReviewOrchestrator
from tw_watchdesk.scout import NovaRestScoutDataProvider, ScoutDataError, ScoutPick, select_candidates
from tw_watchdesk.simulation import (
    bucket_bounds,
    mark_filled,
    order_expiry,
    risk_check_for_buy,
    session_window,
    should_fill,
)
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore
from tw_watchdesk.strategy import build_watch_state
from tw_watchdesk.strategy_versions import (
    FOLLOW_LATEST,
    MANUAL_LOCK,
    build_swing_review_decision,
    daytrade_params_from_json,
    scout_params_from_json,
    swing_params_from_json,
)


STRATEGY_LABELS = {"daytrade": "當沖", "swing": "短線"}
REVIEW_LOOKBACK_DAYS = 14
TRADITIONAL_CHINESE_OUTPUT_RULE = "All user-visible text fields must be written in Traditional Chinese (Taiwan); do not answer summaries, mistakes, rule suggestions, discussion, or capital suggestions in English."


@dataclass(frozen=True)
class WorkerStatus:
    running: bool
    actor: str
    phase: str
    strategy: str
    symbol: str
    message: str
    last_heartbeat: datetime | None
    last_event: str
    next_daytrade_tick: datetime | None
    next_swing_tick: datetime | None


class TradingLabWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        store: TradingStore,
        provider: QuoteProvider,
        llm_adapter: LlmJsonBackend | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self.llm_adapter = llm_adapter
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_scout_date: str | None = None
        self.last_daytrade_tick: str | None = None
        self.last_swing_tick: str | None = None
        self.last_review_date: str | None = None
        self.last_daytrade_flatten_date: str | None = None
        self.last_candidate_expiry_date: str | None = None
        self.last_closed_date: str | None = None
        self._realtime_capture_listener_registered = False
        self._realtime_capture_symbols: set[str] = set()
        self._realtime_capture_unavailable_logged = False
        self._realtime_capture_error_keys: set[str] = set()
        self._realtime_capture_queue: queue.Queue[RealtimeMarketEvent] = queue.Queue(maxsize=50_000)
        self._realtime_capture_thread: threading.Thread | None = None
        self._realtime_capture_dropped = 0
        self._provider_unavailable_last_log_at: datetime | None = None
        self._status_lock = threading.RLock()
        self._status = WorkerStatus(
            running=False,
            actor="system",
            phase="stopped",
            strategy="",
            symbol="",
            message="交易實驗室尚未啟動",
            last_heartbeat=None,
            last_event="",
            next_daytrade_tick=None,
            next_swing_tick=None,
        )

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self._set_status("system", "starting", "交易實驗室啟動中")
        self._log_event("system", "starting", "worker_started", "交易實驗室啟動", created_at=datetime.now(timezone.utc))
        self._start_realtime_capture_drain()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._realtime_capture_thread and self._realtime_capture_thread.is_alive():
            self._realtime_capture_thread.join(timeout=2.0)
        self._set_status("system", "stopped", "交易實驗室已停止", running=False)
        self._log_event("system", "stopped", "worker_stopped", "交易實驗室停止", created_at=datetime.now(timezone.utc))

    def status_snapshot(self) -> WorkerStatus:
        with self._status_lock:
            return replace(self._status)

    def run_tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo(self.settings.timezone))
        trade_date = local.date().isoformat()
        self._set_status("system", "tick", "檢查排程與掛單", now=now)
        self._process_open_orders("daytrade", 5, now)
        self._process_open_orders("swing", 30, now)
        expired = self.store.expire_orders(now)
        if expired:
            self._log_event(
                "system",
                "orders",
                "orders_expired",
                "掛單過期",
                detail=f"{expired} 筆未成交掛單已過期",
                created_at=now,
                trade_date=trade_date,
                metrics={"expired": expired},
            )
        if local.weekday() >= 5:
            self._set_status("system", "market_closed", "休市日，交易實驗室只檢查既有掛單", now=now)
            if self.last_closed_date != trade_date:
                self._log_event("system", "market_closed", "market_closed", "休市日跳過盤中流程", created_at=now, trade_date=trade_date)
                self.last_closed_date = trade_date
            return
        if self.last_candidate_expiry_date != trade_date:
            expired_candidates = self.store.expire_candidates_before(trade_date)
            if expired_candidates:
                self._log_event(
                    "scout",
                    "candidate_expiry",
                    "old_candidates_expired",
                    "舊候選跨日停用",
                    detail=f"{expired_candidates} 筆舊日期候選已停用，不再作為新倉候選。",
                    created_at=now,
                    trade_date=trade_date,
                    metrics={"expired_candidates": expired_candidates},
                )
            self.last_candidate_expiry_date = trade_date
        if local.time() >= _auto_scout_time(self.settings.auto_scout_time) and self.last_scout_date != trade_date:
            self._run_scout(trade_date, now, manual=False)
            self.last_scout_date = trade_date
        self._refresh_realtime_capture_subscriptions(now)
        window = session_window(now, self.settings.timezone)
        if (window.can_open_daytrade or window.daytrade_exit_only) and local.minute % 5 == 0:
            key = local.strftime("%Y-%m-%d %H:%M")
            if key != self.last_daytrade_tick:
                self._process_strategy("daytrade", now, exit_only=window.daytrade_exit_only)
                self.last_daytrade_tick = key
        if time(9, 10) <= local.time() <= time(13, 30) and local.minute % 30 == 0:
            key = local.strftime("%Y-%m-%d %H:%M")
            if key != self.last_swing_tick:
                self._process_strategy("swing", now, exit_only=False)
                self.last_swing_tick = key
        if window.should_review and self.last_review_date != trade_date:
            if self.last_daytrade_flatten_date != trade_date:
                self._set_status("risk_manager", "force_flatten", "當沖日內強制平倉檢查", strategy="daytrade", now=now)
                self._force_daytrade_flatten(now)
                self.last_daytrade_flatten_date = trade_date
            self._set_status("system", "daily_review", "產生日結檢討", now=now)
            self._write_daily_reviews(trade_date)
            self.last_review_date = trade_date
        self._set_status("system", "idle", "等待下一次排程", now=now)

    def add_manual_candidate(self, strategy: str, symbol: str, trade_date: str | None = None, reason: str = "手動加入候選") -> int:
        now = datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo(self.settings.timezone))
        trade_date = trade_date or local.date().isoformat()
        candidate_id = self.store.upsert_candidate(
            trade_date=trade_date or local.date().isoformat(),
            strategy=strategy,
            symbol=symbol,
            score=50.0,
            reason=reason,
            source="manual",
            created_at=now,
        )
        self._set_status("scout", "manual_candidate", f"手動加入候選 {symbol.upper()}", strategy=strategy, symbol=symbol, now=now)
        self._log_event(
            "scout",
            "manual_candidate",
            "candidate_added",
            "手動加入候選",
            detail=reason,
            strategy=strategy,
            symbol=symbol,
            ref_table="candidates",
            ref_id=candidate_id,
            created_at=now,
            trade_date=trade_date,
        )
        return candidate_id

    def run_auto_scout_now(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        trade_date = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        self._run_scout(trade_date, now, manual=True)

    def run_full_review_now(self, trade_date: str | None = None) -> str:
        now = datetime.now(timezone.utc)
        if trade_date is None:
            trade_date = self._latest_review_trade_date(now)
        self._set_status("system", "daily_review", f"立即執行完整會後討論 {trade_date}", now=now)
        self._log_event(
            "system",
            "daily_review",
            "full_review_requested",
            "立即完整會後討論",
            detail=f"{trade_date} 立即執行完整會後討論流程。",
            created_at=now,
            trade_date=trade_date,
        )
        self._write_daily_reviews(trade_date)
        reviews = [
            row
            for row in self.store.list_daily_reviews(limit=500)
            if str(row["review_date"]) == trade_date and str(row["strategy"]) in {"daytrade", "swing"}
        ]
        status_text = "、".join(f"{_strategy_label(str(row['strategy']))}:{_proposal_status_label(row['proposal_status'])}" for row in reviews) or "已送出"
        return f"{trade_date} 完整會後討論完成：{status_text}"

    def run_swing_review_now(self, trade_date: str | None = None) -> str:
        now = datetime.now(timezone.utc)
        if trade_date is None:
            swing_reviews = [row for row in self.store.list_daily_reviews(limit=500) if str(row["strategy"]) == "swing"]
            trade_date = str(swing_reviews[0]["review_date"]) if swing_reviews else now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        self._set_status("system", "daily_review", f"立即執行短線會後討論 {trade_date}", strategy="swing", now=now)
        account = self.store.get_account(SWING_ACCOUNT)
        evidence = self._strategy_review_evidence("swing", trade_date)
        fills = evidence["fills"]
        pnl = sum(float(row["realized_pnl"]) for row in fills)
        active = self.store.get_active_strategy_version("swing")
        summary = f"{evidence['data_start']}~{evidence['data_end']} 短線立即會後討論：成交 {len(fills)} 筆，已實現損益 {pnl:,.2f}，帳戶現金 {account.cash:,.2f}。"
        metrics = {
            "fills": len(fills),
            "orders": len(evidence["orders"]),
            "risk_events": len(evidence["risk_events"]),
            "realized_pnl": pnl,
            "cash": account.cash,
            "capital": account.capital,
            "strategy_version": active.version,
            "manual_review": True,
            "data_start": evidence["data_start"],
            "data_end": evidence["data_end"],
        }
        self.store.upsert_daily_review(
            trade_date,
            "swing",
            summary,
            metrics,
            strategy_version=active.version,
            proposal_status="reviewing",
            llm_summary="短線會後討論執行中",
            llm_discussion="正在呼叫 Codex LLM 檢討近 14 天短線模擬資料。",
        )
        self._log_event(
            "system",
            "daily_review",
            "swing_review_requested",
            "立即短線會後討論",
            detail=summary,
            strategy="swing",
            created_at=now,
            trade_date=trade_date,
            metrics=metrics,
        )
        self._maybe_run_swing_self_correction(trade_date, summary, metrics, force=True)
        self._run_multi_agent_review(trade_date)
        rows = [row for row in self.store.list_daily_reviews(limit=500) if str(row["strategy"]) == "swing" and str(row["review_date"]) == trade_date]
        if not rows:
            return f"{trade_date} 短線會後討論已執行"
        row = rows[0]
        return f"{trade_date} 短線會後討論完成：{_proposal_status_label(row['proposal_status'])}；版本 {row['strategy_version'] or active.version}"

    def run_daytrade_review_now(self, trade_date: str | None = None) -> str:
        now = datetime.now(timezone.utc)
        if trade_date is None:
            reviews = [row for row in self.store.list_daily_reviews(limit=500) if str(row["strategy"]) == "daytrade"]
            if reviews:
                trade_date = str(reviews[0]["review_date"])
            else:
                dates = _dates_from_rows(self.store.list_fills(500), "filled_at", "daytrade") or _dates_from_rows(self.store.list_orders(limit=500), "created_at", "daytrade")
                trade_date = max(dates) if dates else now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        self._set_status("system", "daily_review", f"立即執行當沖會後討論 {trade_date}", strategy="daytrade", now=now)
        account = self.store.get_account(DAYTRADE_ACCOUNT)
        evidence = self._strategy_review_evidence("daytrade", trade_date)
        fills = evidence["fills"]
        pnl = sum(float(row["realized_pnl"]) for row in fills)
        summary = f"{evidence['data_start']}~{evidence['data_end']} 當沖立即會後討論：成交 {len(fills)} 筆，已實現損益 {pnl:,.2f}，帳戶現金 {account.cash:,.2f}。"
        metrics = {
            "fills": len(fills),
            "orders": len(evidence["orders"]),
            "risk_events": len(evidence["risk_events"]),
            "realized_pnl": pnl,
            "cash": account.cash,
            "capital": account.capital,
            "manual_review": True,
            "data_start": evidence["data_start"],
            "data_end": evidence["data_end"],
        }
        self.store.upsert_daily_review(
            trade_date,
            "daytrade",
            summary,
            metrics,
            proposal_status="reviewing",
            llm_summary="當沖會後討論執行中",
            llm_discussion="正在呼叫 Codex LLM 檢討近 14 天當沖模擬資料。",
        )
        self._log_event(
            "system",
            "daily_review",
            "daytrade_review_requested",
            "立即當沖會後討論",
            detail=summary,
            strategy="daytrade",
            created_at=now,
            trade_date=trade_date,
            metrics=metrics,
        )
        self._maybe_run_daytrade_review(trade_date, summary, metrics, force=True)
        self._run_multi_agent_review(trade_date)
        rows = [row for row in self.store.list_daily_reviews(limit=500) if str(row["strategy"]) == "daytrade" and str(row["review_date"]) == trade_date]
        if not rows:
            return f"{trade_date} 當沖會後討論已執行"
        row = rows[0]
        return f"{trade_date} 當沖會後討論完成：{_proposal_status_label(row['proposal_status'])}"

    def run_multi_agent_review_now(self, trade_date: str | None = None) -> str:
        now = datetime.now(timezone.utc)
        if trade_date is None:
            trade_date = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        self._set_status("system", "daily_review", f"立即執行多 Agent 策略檢討 {trade_date}", now=now)
        orchestrator = MultiAgentReviewOrchestrator(store=self.store, settings=self.settings, backend=self._llm_backend())
        result = orchestrator.run(trade_date, include_news_context=self.settings.enable_news_context, created_at=now)
        self._log_event(
            "system",
            "daily_review",
            "multi_agent_review_completed",
            "多 Agent 策略檢討完成",
            detail=result.summary,
            created_at=now,
            trade_date=trade_date,
            metrics={"review_run_id": result.review_run_id, "pending_versions": result.pending_versions, "rejected": result.rejected},
        )
        return f"{trade_date} 多 Agent 策略檢討完成：{result.summary}"

    def _latest_review_trade_date(self, now: datetime) -> str:
        dates: list[str] = []
        for row in self.store.list_daily_reviews(limit=500):
            value = str(row["review_date"])
            if value:
                dates.append(value)
        fills = self.store.list_fills(500)
        orders = self.store.list_orders(limit=500)
        for strategy in ("daytrade", "swing"):
            dates.extend(_dates_from_rows(fills, "filled_at", strategy))
            dates.extend(_dates_from_rows(orders, "created_at", strategy))
        candidates = self.store.list_candidates()
        dates.extend(str(row.trade_date) for row in candidates if str(row.trade_date))
        return max(dates) if dates else now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()

    def _refresh_realtime_capture_subscriptions(self, now: datetime) -> None:
        if not self.settings.enable_realtime_capture or self.settings.market_data_mode != "live":
            return
        add_listener = getattr(self.provider, "add_market_data_listener", None)
        subscribe = getattr(self.provider, "subscribe_market_data", None)
        if not callable(add_listener) or not callable(subscribe):
            if not self._realtime_capture_unavailable_logged:
                self._log_event(
                    "market_data",
                    "realtime_capture",
                    "realtime_capture_unavailable",
                    "盤中行情落地不可用",
                    detail="目前 provider 不支援 realtime capture listener/subscribe_market_data。",
                    severity="warning",
                    created_at=now,
                    trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
                )
                self._realtime_capture_unavailable_logged = True
            return
        if not self._realtime_capture_listener_registered:
            try:
                add_listener(self._handle_realtime_market_event)
            except ProviderUnavailable as exc:
                self._handle_provider_unavailable(exc, now, source="盤中行情 listener 註冊")
                return
            self._realtime_capture_listener_registered = True
        symbols = self._realtime_capture_target_symbols(now)
        new_symbols = [symbol for symbol in symbols if symbol not in self._realtime_capture_symbols]
        if not new_symbols:
            return
        channels = tuple(channel for channel in self.settings.realtime_capture_channels if channel)
        if not channels:
            return
        try:
            subscribe(new_symbols, channels)
        except ProviderUnavailable as exc:
            self._handle_provider_unavailable(
                exc,
                now,
                source="盤中行情訂閱",
                metrics={"symbols": new_symbols, "channels": channels},
            )
            return
        self._realtime_capture_symbols.update(new_symbols)
        self._log_event(
            "market_data",
            "realtime_capture",
            "realtime_capture_subscribed",
            "盤中行情落地訂閱",
            detail=f"新增訂閱 {len(new_symbols)} 檔：{', '.join(new_symbols)}；頻道 {', '.join(channels)}",
            created_at=now,
            trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={"symbols": new_symbols, "channels": channels},
        )

    def _realtime_capture_target_symbols(self, now: datetime) -> list[str]:
        trade_date = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        symbols: set[str] = set()
        for strategy in ("daytrade", "swing"):
            symbols.update(candidate.symbol for candidate in self.store.list_candidates(trade_date, strategy) if candidate.status == "active")
        symbols.update(position.symbol for position in self.store.list_positions())
        symbols.update(order.symbol for order in self.store.list_open_orders())
        return sorted(symbols)[: max(0, self.settings.realtime_capture_max_symbols)]

    def _handle_realtime_market_event(self, event: RealtimeMarketEvent) -> None:
        if self.thread and self.thread.is_alive():
            try:
                self._realtime_capture_queue.put_nowait(event)
            except queue.Full:
                self._realtime_capture_dropped += 1
            return
        self._record_realtime_market_event(event)

    def _start_realtime_capture_drain(self) -> None:
        if self._realtime_capture_thread and self._realtime_capture_thread.is_alive():
            return
        self._realtime_capture_thread = threading.Thread(target=self._realtime_capture_drain_loop, daemon=True)
        self._realtime_capture_thread.start()

    def _realtime_capture_drain_loop(self) -> None:
        while not self.stop_event.is_set() or not self._realtime_capture_queue.empty():
            try:
                event = self._realtime_capture_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._record_realtime_market_event(event)
            finally:
                self._realtime_capture_queue.task_done()

    def _record_realtime_market_event(self, event: RealtimeMarketEvent) -> None:
        try:
            event_key = _market_event_key(event)
            self.store.insert_market_data_event(
                channel=event.channel,
                symbol=event.symbol,
                exchange_time=event.exchange_time,
                received_at=event.received_at,
                payload=event.payload,
                event_key=event_key,
            )
            if event.channel == "trades":
                self._record_realtime_trade(event, event_key)
            elif event.channel == "books":
                self._record_realtime_book(event, event_key)
            elif event.channel == "aggregates":
                self._record_quote(parse_aggregates_message(event.raw, received_at=event.received_at), 1)
            elif event.channel == "candles":
                self._record_realtime_candle(event)
        except Exception as exc:
            key = f"{event.channel}:{exc.__class__.__name__}"
            if key in self._realtime_capture_error_keys:
                return
            self._realtime_capture_error_keys.add(key)
            self._log_event(
                "market_data",
                "realtime_capture",
                "realtime_capture_event_error",
                "盤中行情寫入失敗",
                detail=f"{event.channel} {event.symbol}: {exc.__class__.__name__}: {exc}",
                severity="warning",
                symbol=event.symbol,
                created_at=event.received_at,
                trade_date=event.received_at.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            )

    def _record_realtime_trade(self, event: RealtimeMarketEvent, event_key: str) -> None:
        price = _payload_float(event.payload, "price")
        size = _payload_float(event.payload, "size")
        if price is None or size is None or price <= 0 or size <= 0:
            return
        bid = _payload_float(event.payload, "bid")
        ask = _payload_float(event.payload, "ask")
        volume = _payload_float(event.payload, "volume")
        serial = _payload_text(event.payload, "serial")
        self.store.insert_market_tick(
            symbol=event.symbol,
            trade_time=event.exchange_time,
            received_at=event.received_at,
            price=price,
            size=size,
            bid=bid,
            ask=ask,
            volume=volume,
            serial=serial,
            side=_trade_side(price, bid, ask),
            raw=event.raw,
            event_key=event_key,
        )

    def _record_realtime_book(self, event: RealtimeMarketEvent, event_key: str) -> None:
        self.store.insert_order_book(
            symbol=event.symbol,
            exchange_time=event.exchange_time,
            received_at=event.received_at,
            bids=_levels_from_payload(event.payload.get("bids")),
            asks=_levels_from_payload(event.payload.get("asks")),
            raw=event.raw,
            event_key=event_key,
        )

    def _record_realtime_candle(self, event: RealtimeMarketEvent) -> None:
        open_price = _payload_float(event.payload, "open")
        high_price = _payload_float(event.payload, "high")
        low_price = _payload_float(event.payload, "low")
        close_price = _payload_float(event.payload, "close")
        volume = _payload_float(event.payload, "volume")
        if None in (open_price, high_price, low_price, close_price, volume):
            return
        timeframe = int(_payload_float(event.payload, "timeframe") or 1)
        start, end = bucket_bounds(event.exchange_time, timeframe)
        self.store.upsert_ohlc_bar(
            symbol=event.symbol,
            timeframe_minutes=timeframe,
            start_time=start,
            end_time=end,
            open_price=float(open_price),
            high_price=float(high_price),
            low_price=float(low_price),
            close_price=float(close_price),
            volume=float(volume),
            source="taishin_nova_candles",
        )

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_tick()
            except ProviderUnavailable as exc:
                self._handle_provider_unavailable(exc, datetime.now(timezone.utc), source="交易實驗室排程")
            except Exception as exc:
                message = f"{exc.__class__.__name__}: {exc}"
                self._set_status("system", "error", message, running=True)
                self.store.add_risk_event(None, "system", "", "error", message)
                self._log_event("system", "error", "unhandled_error", "交易實驗室例外", detail=message, severity="error")
            self.stop_event.wait(15)
        self._set_status("system", "stopped", "交易實驗室已停止", running=False)

    def _handle_provider_unavailable(
        self,
        exc: ProviderUnavailable,
        now: datetime,
        *,
        source: str,
        metrics: dict[str, object] | None = None,
    ) -> None:
        local = now.astimezone(ZoneInfo(self.settings.timezone))
        retry_interval_seconds = _provider_unavailable_log_interval_seconds(local)
        if (
            self._provider_unavailable_last_log_at is not None
            and (now - self._provider_unavailable_last_log_at).total_seconds() < retry_interval_seconds
        ):
            return
        self._provider_unavailable_last_log_at = now
        active_retry_window = _is_provider_active_retry_window(local)
        severity = "warning" if active_retry_window else "info"
        detail = (
            f"{source}：{exc}；"
            "台股開盤前一小時到盤中每 5 分鐘重試，離峰時段只低頻記錄，不中斷主程式。"
        )
        merged_metrics = {
            "retry_interval_seconds": retry_interval_seconds,
            "active_retry_window": active_retry_window,
            **(metrics or {}),
        }
        self._set_status("market_data", "api_unavailable", "Nova API 連線不可用，等待下一次重試", now=now)
        self._log_event(
            "market_data",
            "api_unavailable",
            "provider_unavailable",
            "Nova API 連線不可用",
            detail=detail,
            severity=severity,
            created_at=now,
            trade_date=local.date().isoformat(),
            metrics=merged_metrics,
        )

    def _run_scout(self, trade_date: str, now: datetime, *, manual: bool) -> None:
        active_scout = self.store.get_active_strategy_version("scout")
        scout_params = scout_params_from_json(active_scout.params)
        self._set_status("scout", "scouting", "抓盤手開始篩選候選", now=now)
        self._log_event(
            "scout",
            "scouting",
            "scout_started",
            "抓盤手開始篩選",
            detail="手動立即選股" if manual else "排程自動選股",
            created_at=now,
            trade_date=trade_date,
            metrics={"manual": manual, "scout_version": active_scout.version},
        )
        if not self.settings.enable_auto_scout and not manual:
            self._log_event(
                "scout",
                "scouting",
                "scout_disabled",
                "自動選股未啟用",
                detail="TW_WATCH_ENABLE_AUTO_SCOUT=false，排程不會新增候選；可在 UI 勾選啟用或按立即選股。",
                created_at=now,
                trade_date=trade_date,
            )
            return
        try:
            result = select_candidates(self.settings, NovaRestScoutDataProvider(self.provider), scout_params)
        except ScoutDataError as exc:
            message = f"自動選股資料來源失敗：{exc}"
            self._set_status("scout", "scouting", message, now=now)
            self._log_event(
                "scout",
                "scouting",
                "scout_data_unavailable",
                "自動選股資料來源失敗",
                detail=str(exc),
                severity="warning",
                created_at=now,
                trade_date=trade_date,
            )
            return
        except Exception as exc:
            message = f"自動選股例外：{exc.__class__.__name__}: {exc}"
            self._set_status("scout", "scouting", message, now=now)
            self._log_event(
                "scout",
                "scouting",
                "scout_error",
                "自動選股例外",
                detail=f"{exc.__class__.__name__}: {exc}",
                severity="error",
                created_at=now,
                trade_date=trade_date,
            )
            return

        if result.excluded_counts:
            detail = "；".join(f"{reason} {count}" for reason, count in sorted(result.excluded_counts.items()))
            self._log_event(
                "scout",
                "scouting",
                "scout_exclusions",
                "自動選股排除統計",
                detail=detail,
                created_at=now,
                trade_date=trade_date,
                metrics=result.excluded_counts,
            )
        for note in result.notes:
            self._log_event(
                "scout",
                "scouting",
                "scout_note",
                "自動選股注意事項",
                detail=note,
                severity="warning",
                created_at=now,
                trade_date=trade_date,
            )
        daytrade_stats = self._write_auto_scout_picks("daytrade", result.daytrade, trade_date, now, active_scout.version)
        swing_stats = self._write_auto_scout_picks("swing", result.swing, trade_date, now, active_scout.version)
        if not result.daytrade:
            self._log_event("scout", "scouting", "candidate_shortage", "當沖候選不足", detail="本輪未找到符合條件的當沖候選", strategy="daytrade", severity="warning", created_at=now, trade_date=trade_date)
        if not result.swing:
            self._log_event("scout", "scouting", "candidate_shortage", "短線候選不足", detail="本輪未找到符合條件的短線候選", strategy="swing", severity="warning", created_at=now, trade_date=trade_date)
        summary = (
            f"掃描 {result.scanned} 檔；"
            f"當沖新增 {daytrade_stats['added']}、複核 {daytrade_stats['revalidated']}、剔除 {daytrade_stats['deactivated']}；"
            f"短線新增 {swing_stats['added']}、複核 {swing_stats['revalidated']}、剔除 {swing_stats['deactivated']}"
        )
        self._set_status("scout", "scouting", summary, now=now)
        self._log_event(
            "scout",
            "scouting",
            "scout_completed",
            "自動選股完成",
            detail=summary,
            created_at=now,
            trade_date=trade_date,
            metrics={
                "scanned": result.scanned,
                "daytrade_added": daytrade_stats["added"],
                "daytrade_revalidated": daytrade_stats["revalidated"],
                "daytrade_deactivated": daytrade_stats["deactivated"],
                "daytrade_kept_manual": daytrade_stats["kept_manual"],
                "swing_added": swing_stats["added"],
                "swing_revalidated": swing_stats["revalidated"],
                "swing_deactivated": swing_stats["deactivated"],
                "swing_kept_manual": swing_stats["kept_manual"],
                "scout_version": active_scout.version,
            },
        )

    def _write_auto_scout_picks(self, strategy: str, picks: list[ScoutPick], trade_date: str, now: datetime, scout_version: str) -> dict[str, int]:
        existing_by_symbol = {candidate.symbol: candidate for candidate in self.store.list_candidates(trade_date, strategy)}
        selected_symbols = {pick.symbol for pick in picks}
        stats = {"added": 0, "revalidated": 0, "deactivated": 0, "kept_manual": 0}
        for existing in existing_by_symbol.values():
            if existing.source == "auto_scout" and existing.status == "active" and existing.symbol not in selected_symbols:
                self.store.update_candidate_status(existing.id, "inactive")
                stats["deactivated"] += 1
                self._log_event(
                    "scout",
                    "scouting",
                    "candidate_deactivated",
                    "自動選股剔除候選",
                    detail="本輪未通過自動選股複核，停止作為新倉候選；若已有持倉仍會由交易員持續監控出場。",
                    strategy=strategy,
                    symbol=existing.symbol,
                    ref_table="candidates",
                    ref_id=existing.id,
                    created_at=now,
                    trade_date=trade_date,
                )
        for pick in picks:
            existing = existing_by_symbol.get(pick.symbol)
            if existing is not None and existing.source == "manual":
                stats["kept_manual"] += 1
                self._log_event(
                    "scout",
                    "scouting",
                    "candidate_kept_manual",
                    "保留手動候選",
                    detail=f"{pick.symbol} 已由手動加入，略過自動覆蓋",
                    strategy=strategy,
                    symbol=pick.symbol,
                    ref_table="candidates",
                    ref_id=existing.id,
                    created_at=now,
                    trade_date=trade_date,
                )
                continue
            is_new = existing is None
            candidate_id = self.store.upsert_candidate(
                trade_date=trade_date,
                strategy=strategy,
                symbol=pick.symbol,
                name=pick.name,
                score=pick.score,
                reason=f"{pick.reason}；scout_version={scout_version}",
                source="auto_scout",
                scout_version=scout_version,
                status="active",
                created_at=now,
            )
            if is_new:
                stats["added"] += 1
            else:
                stats["revalidated"] += 1
            self._log_event(
                "scout",
                "scouting",
                "candidate_added" if is_new else "candidate_revalidated",
                "自動選股加入候選" if is_new else "自動選股複核保留候選",
                detail=f"{pick.reason}；scout_version={scout_version}",
                strategy=strategy,
                symbol=pick.symbol,
                ref_table="candidates",
                ref_id=candidate_id,
                created_at=now,
                trade_date=trade_date,
                metrics={**pick.metrics, "scout_version": scout_version},
            )
        return stats

    def _process_strategy(self, strategy: str, now: datetime, *, exit_only: bool) -> None:
        local_date = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        candidates = [candidate for candidate in self.store.list_candidates(local_date, strategy) if candidate.status == "active"][:5]
        account_id = DAYTRADE_ACCOUNT if strategy == "daytrade" else SWING_ACCOUNT
        timeframe = 5 if strategy == "daytrade" else 30
        actor = _actor_for_strategy(strategy)
        phase = "exit_only" if exit_only else "strategy_check"
        strategy_version = ""
        swing_params = None
        daytrade_params = None
        if strategy == "daytrade":
            active_version = self.store.get_active_strategy_version("daytrade")
            strategy_version = active_version.version
            daytrade_params = daytrade_params_from_json(active_version.params)
        if strategy == "swing":
            active_version = self.store.get_active_strategy_version("swing")
            strategy_version = active_version.version
            swing_params = swing_params_from_json(active_version.params)
        candidate_symbols = {candidate.symbol for candidate in candidates}
        held_positions = [
            position
            for position in self.store.list_positions()
            if position.account_id == account_id and position.symbol not in candidate_symbols
        ]
        self._set_status(actor, phase, f"檢查 {len(candidates)} 檔候選、追蹤 {len(held_positions)} 檔持倉", strategy=strategy, now=now)
        self._log_event(
            actor,
            phase,
            "strategy_check_started",
            "交易員開始檢查候選",
            detail=f"本輪候選 {len(candidates)} 檔，額外追蹤既有持倉 {len(held_positions)} 檔",
            strategy=strategy,
            created_at=now,
            trade_date=local_date,
            metrics={"candidates": len(candidates), "positions": len(held_positions), "timeframe": timeframe, "exit_only": exit_only},
        )
        targets = [(candidate.symbol, candidate, False) for candidate in candidates]
        targets.extend((position.symbol, None, True) for position in held_positions)
        for symbol, candidate, position_only in targets:
            self._set_status(actor, phase, f"檢查 {symbol}", strategy=strategy, symbol=symbol, now=now)
            try:
                quote = self.provider.get_quote(symbol)
            except ProviderUnavailable as exc:
                self.store.add_risk_event(account_id, strategy, symbol, "warning", str(exc))
                self._log_event(
                    actor,
                    phase,
                    "quote_unavailable",
                    "取價失敗",
                    detail=str(exc),
                    severity="warning",
                    strategy=strategy,
                    symbol=symbol,
                    created_at=now,
                    trade_date=local_date,
                )
                continue
            except Exception as exc:
                message = f"{exc.__class__.__name__}: {exc}"
                self.store.add_risk_event(account_id, strategy, symbol, "error", message)
                self._log_event(
                    actor,
                    phase,
                    "quote_error",
                    "取價例外",
                    detail=message,
                    severity="error",
                    strategy=strategy,
                    symbol=symbol,
                    created_at=now,
                    trade_date=local_date,
                )
                continue
            self._record_quote(quote, timeframe)
            self._process_open_orders(strategy, timeframe, now)
            exit_order_created = self._maybe_create_exit_order(strategy, account_id, symbol, quote, now, force_exit=exit_only)
            if position_only:
                if not exit_order_created:
                    self._log_event(
                        actor,
                        phase,
                        "position_checked",
                        "持倉追蹤",
                        detail="此股票不在今日候選內，但已有持倉，已檢查停損/停利條件。",
                        strategy=strategy,
                        symbol=symbol,
                        created_at=now,
                        trade_date=local_date,
                    )
                continue
            if candidate is None:
                continue
            if exit_only or self.store.has_open_order_or_position(account_id, symbol):
                reason = "出場-only 時段，不建立新倉" if exit_only else "已有未完成委託或持倉"
                self._log_event(
                    actor,
                    phase,
                    "entry_skipped",
                    "跳過新倉",
                    detail=reason,
                    strategy=strategy,
                    symbol=symbol,
                    created_at=now,
                    trade_date=local_date,
                )
                continue
            account = self.store.get_account(account_id)
            months = None if strategy == "daytrade" else 3
            state = build_watch_state(
                self.settings,
                symbol,
                account.capital,
                strategy,
                months,
                quote,
                now=now,
                swing_params=swing_params,
                daytrade_params=daytrade_params,
                strategy_version=strategy_version,
            )
            advice = state.advice
            if state.status != "ok" or advice.action != "buy" or advice.buy_price is None or advice.qty <= 0:
                if state.status != "ok":
                    self._record_quote_diagnostic(
                        strategy=strategy,
                        quote=quote,
                        now=now,
                        trade_date=local_date,
                        actor=actor,
                        phase=phase,
                    )
                self._log_event(
                    actor,
                    phase,
                    "strategy_skipped",
                    "策略未建立買單",
                    detail=advice.reason,
                    strategy=strategy,
                    symbol=symbol,
                    created_at=now,
                    trade_date=local_date,
                metrics={"status": state.status, "action": advice.action, "qty": advice.qty, "strategy_version": advice.strategy_version},
                )
                continue
            self._log_event(
                actor,
                phase,
                "strategy_buy_signal",
                "策略建議買進",
                detail=advice.reason,
                strategy=strategy,
                symbol=symbol,
                created_at=now,
                trade_date=local_date,
                metrics={
                    "buy_price": advice.buy_price,
                    "qty": advice.qty,
                    "stop_loss": advice.stop_loss,
                    "take_profit": advice.take_profit,
                    "strategy_version": advice.strategy_version,
                },
            )
            risk = risk_check_for_buy(
                store=self.store,
                account=account,
                strategy=strategy,
                symbol=symbol,
                price=advice.buy_price,
                qty=advice.qty,
                stop_loss=advice.stop_loss,
                now=now,
                risk_pct=daytrade_params.risk_pct if daytrade_params is not None else (swing_params.risk_pct if swing_params is not None else None),
                max_position_pct=daytrade_params.max_position_pct if daytrade_params is not None else (swing_params.max_position_pct if swing_params is not None else None),
                max_daily_loss_pct=daytrade_params.max_daily_loss_pct if daytrade_params is not None else None,
                max_total_exposure_pct=swing_params.max_total_exposure_pct if swing_params is not None else None,
                max_position_symbols=swing_params.max_position_symbols if swing_params is not None else None,
            )
            if not risk.allowed:
                self.store.add_risk_event(account_id, strategy, symbol, "info", risk.reason)
                self._log_event(
                    "risk_manager",
                    "risk_check",
                    "risk_blocked",
                    "風控阻擋買單",
                    detail=risk.reason,
                    strategy=strategy,
                    symbol=symbol,
                    severity="warning",
                    created_at=now,
                    trade_date=local_date,
                )
                continue
            self._log_event(
                "risk_manager",
                "risk_check",
                "risk_allowed",
                "風控允許買單",
                detail="通過單筆風險與曝險檢查",
                strategy=strategy,
                symbol=symbol,
                created_at=now,
                trade_date=local_date,
            )
            try:
                order_id = self.store.create_order(
                    account_id=account_id,
                    strategy=strategy,
                    symbol=symbol,
                    side="buy",
                    price=advice.buy_price,
                    qty=advice.qty,
                    reason=advice.reason,
                    expires_at=order_expiry(now, strategy),
                    stop_loss=advice.stop_loss,
                    take_profit=advice.take_profit,
                    strategy_version=advice.strategy_version,
                    candidate_id=candidate.id,
                    created_at=now,
                )
            except (KeyError, ValueError) as exc:
                self.store.add_risk_event(account_id, strategy, symbol, "warning", str(exc))
                self._log_event(
                    "risk_manager",
                    "risk_check",
                    "order_rejected",
                    "委託建立失敗",
                    detail=str(exc),
                    strategy=strategy,
                    symbol=symbol,
                    severity="warning",
                    created_at=now,
                    trade_date=local_date,
                )
                continue
            self._log_event(
                actor,
                "order_created",
                "buy_order_created",
                "建立買進模擬單",
                detail=advice.reason,
                strategy=strategy,
                symbol=symbol,
                ref_table="orders",
                ref_id=order_id,
                created_at=now,
                trade_date=local_date,
                metrics={"price": advice.buy_price, "qty": advice.qty, "strategy_version": advice.strategy_version},
            )

    def _process_open_orders(self, strategy: str, timeframe: int, now: datetime) -> None:
        for order in self.store.list_open_orders():
            if order.strategy != strategy:
                continue
            filled = False
            ticks = self.store.get_ticks_after(order.symbol, order.created_at, order.expires_at)
            for tick in ticks:
                if _tick_should_fill(order, tick):
                    fill_time = datetime.fromisoformat(str(tick["trade_time"]))
                    mark_filled(self.store, order, order.price, fill_time)
                    trade_date = fill_time.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                    self._log_event(
                        _actor_for_strategy(strategy),
                        "fill_check",
                        "order_filled",
                        "模擬成交",
                        detail=f"{order.side} {order.qty:,} 股 @ {order.price:,.2f}",
                        strategy=strategy,
                        symbol=order.symbol,
                        ref_table="orders",
                        ref_id=order.id,
                        created_at=fill_time,
                        trade_date=trade_date,
                        metrics={
                            "side": order.side,
                            "price": order.price,
                            "qty": order.qty,
                            "source": "tick",
                            "tick_price": float(tick["price"]),
                            "tick_size": float(tick["size"]),
                        },
                    )
                    filled = True
                    break
            if filled:
                continue
            snapshots = self.store.get_snapshots_after(order.symbol, order.created_at, order.expires_at)
            for snapshot in snapshots:
                if _snapshot_should_fill(order, snapshot):
                    fill_time = datetime.fromisoformat(str(snapshot["snapshot_time"]))
                    mark_filled(self.store, order, order.price, fill_time)
                    trade_date = fill_time.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                    self._log_event(
                        _actor_for_strategy(strategy),
                        "fill_check",
                        "order_filled",
                        "模擬成交",
                        detail=f"{order.side} {order.qty:,} 股 @ {order.price:,.2f}",
                        strategy=strategy,
                        symbol=order.symbol,
                        ref_table="orders",
                        ref_id=order.id,
                        created_at=fill_time,
                        trade_date=trade_date,
                        metrics={
                            "side": order.side,
                            "price": order.price,
                            "qty": order.qty,
                            "source": "snapshot",
                            "snapshot_price": float(snapshot["price"]),
                        },
                    )
                    filled = True
                    break
            if filled:
                continue
            bars = self.store.get_bars_after(order.symbol, timeframe, order.created_at, order.expires_at)
            for bar in bars:
                if should_fill(order, bar):
                    fill_time = datetime.fromisoformat(str(bar["end_time"]))
                    mark_filled(self.store, order, order.price, fill_time)
                    trade_date = fill_time.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                    self._log_event(
                        _actor_for_strategy(strategy),
                        "fill_check",
                        "order_filled",
                        "模擬成交",
                        detail=f"{order.side} {order.qty:,} 股 @ {order.price:,.2f}",
                        strategy=strategy,
                        symbol=order.symbol,
                        ref_table="orders",
                        ref_id=order.id,
                        created_at=fill_time,
                        trade_date=trade_date,
                        metrics={"side": order.side, "price": order.price, "qty": order.qty},
                    )
                    break

    def _maybe_create_exit_order(
        self,
        strategy: str,
        account_id: str,
        symbol: str,
        quote: Quote,
        now: datetime,
        *,
        force_exit: bool,
    ) -> bool:
        position = self.store.get_position(account_id, symbol)
        if position is None:
            return False
        if any(order.symbol == symbol and order.account_id == account_id and order.side == "sell" for order in self.store.list_open_orders()):
            return False
        book = self.store.latest_order_book(symbol, now)
        bid = float(book["best_bid"]) if book is not None and book["best_bid"] is not None else (quote.bid_levels[0].price if quote.bid_levels else quote.price)
        ask = float(book["best_ask"]) if book is not None and book["best_ask"] is not None else (quote.ask_levels[0].price if quote.ask_levels else quote.price)
        reason = ""
        price = bid
        if force_exit:
            reason = "當沖出場-only 時段，建立收盤前出場模擬單"
        elif position.stop_loss is not None and quote.price <= position.stop_loss:
            reason = "觸發停損，建立出場模擬單"
        elif position.take_profit is not None and quote.price >= position.take_profit:
            reason = "觸發停利，建立出場模擬單"
            price = ask
        if not reason:
            return False
        try:
            order_id = self.store.create_order(
                account_id=account_id,
                strategy=strategy,
                symbol=symbol,
                side="sell",
                price=price,
                qty=position.qty,
                reason=reason,
                expires_at=order_expiry(now, strategy),
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                strategy_version=position.strategy_version,
                candidate_id=position.candidate_id,
                entry_order_id=position.entry_order_id,
                scout_version=position.scout_version,
                created_at=now,
            )
        except (KeyError, ValueError) as exc:
            self.store.add_risk_event(account_id, strategy, symbol, "warning", str(exc))
            self._log_event(
                "risk_manager",
                "exit_order",
                "order_rejected",
                "出場委託建立失敗",
                detail=str(exc),
                strategy=strategy,
                symbol=symbol,
                severity="warning",
                created_at=now,
                trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            )
            return False
        self._log_event(
            _actor_for_strategy(strategy),
            "exit_order",
            "sell_order_created",
            "建立賣出模擬單",
            detail=reason,
            strategy=strategy,
            symbol=symbol,
            ref_table="orders",
            ref_id=order_id,
            created_at=now,
            trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={
                "price": price,
                "qty": position.qty,
                "force_exit": force_exit,
                "strategy_version": position.strategy_version,
                "candidate_id": position.candidate_id,
                "entry_order_id": position.entry_order_id,
                "scout_version": position.scout_version,
            },
        )
        return True

    def _force_daytrade_flatten(self, now: datetime) -> None:
        positions = [position for position in self.store.list_positions() if position.account_id == DAYTRADE_ACCOUNT]
        if not positions:
            return
        trade_date = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        open_orders = self.store.list_open_orders()
        for position in positions:
            price, source, severity = self._forced_flatten_price(position.symbol, position.avg_cost, now)
            order = next(
                (
                    item
                    for item in open_orders
                    if item.account_id == DAYTRADE_ACCOUNT and item.symbol == position.symbol and item.side == "sell"
                ),
                None,
            )
            if order is None:
                try:
                    order_id = self.store.create_order(
                        account_id=DAYTRADE_ACCOUNT,
                        strategy="daytrade",
                        symbol=position.symbol,
                        side="sell",
                        price=price,
                        qty=position.qty,
                        reason="當沖日內強制平倉",
                        expires_at=now,
                        stop_loss=position.stop_loss,
                        take_profit=position.take_profit,
                        strategy_version=position.strategy_version,
                        candidate_id=position.candidate_id,
                        entry_order_id=position.entry_order_id,
                        scout_version=position.scout_version,
                        created_at=now,
                    )
                    order = self.store.get_order(order_id)
                except (KeyError, ValueError) as exc:
                    self.store.add_risk_event(DAYTRADE_ACCOUNT, "daytrade", position.symbol, "error", str(exc))
                    self._log_event(
                        "risk_manager",
                        "force_flatten",
                        "force_flatten_failed",
                        "當沖強制平倉失敗",
                        detail=str(exc),
                        severity="error",
                        strategy="daytrade",
                        symbol=position.symbol,
                        created_at=now,
                        trade_date=trade_date,
                    )
                    continue
            mark_filled(self.store, order, price, now)
            self.store.add_risk_event(DAYTRADE_ACCOUNT, "daytrade", position.symbol, severity, "當沖日內強制平倉")
            self._log_event(
                "risk_manager",
                "force_flatten",
                "daytrade_forced_flatten",
                "當沖日內強制平倉",
                detail=f"sell {position.qty:,} 股 @ {price:,.2f}；價格來源 {source}",
                severity=severity,
                strategy="daytrade",
                symbol=position.symbol,
                ref_table="orders",
                ref_id=order.id,
                created_at=now,
                trade_date=trade_date,
                metrics={
                    "price": price,
                    "qty": position.qty,
                    "source": source,
                    "strategy_version": position.strategy_version,
                    "candidate_id": position.candidate_id,
                    "entry_order_id": position.entry_order_id,
                    "scout_version": position.scout_version,
                },
            )

    def _forced_flatten_price(self, symbol: str, fallback_price: float, now: datetime) -> tuple[float, str, str]:
        book = self.store.latest_order_book(symbol, now)
        if book is not None and book["best_bid"] is not None and float(book["best_bid"]) > 0:
            return float(book["best_bid"]), "latest_order_book_bid", "warning"
        snapshot = self.store.latest_snapshot(symbol, now)
        if snapshot is not None:
            bid = snapshot["bid_price"]
            if bid is not None and float(bid) > 0:
                return float(bid), "latest_bid", "warning"
            price = float(snapshot["price"])
            if price > 0:
                return price, "latest_price", "warning"
        return max(0.01, float(fallback_price)), "position_avg_cost_fallback", "error"

    def _record_quote(self, quote: Quote, timeframe: int) -> None:
        bid = quote.bid_levels[0].price if quote.bid_levels else None
        ask = quote.ask_levels[0].price if quote.ask_levels else None
        self.store.insert_snapshot(
            symbol=quote.symbol,
            snapshot_time=quote.exchange_time,
            price=quote.price,
            previous_close=quote.previous_close,
            volume=quote.volume,
            turnover=quote.turnover,
            bid_price=bid,
            ask_price=ask,
            is_realtime=quote.is_realtime,
            raw={"source": quote.source, "flags": quote.flags},
        )
        start, end = bucket_bounds(quote.exchange_time, timeframe)
        self.store.upsert_bar(
            symbol=quote.symbol,
            timeframe_minutes=timeframe,
            start_time=start,
            end_time=end,
            price=quote.price,
            volume=quote.volume,
            source=quote.source,
        )

    def _record_quote_diagnostic(
        self,
        *,
        strategy: str,
        quote: Quote,
        now: datetime,
        trade_date: str,
        actor: str,
        phase: str,
    ) -> None:
        diagnostic = diagnose_quote_quality(self.settings, quote, now)
        if diagnostic.status == "ok":
            return
        diagnostic_id = self.store.insert_quote_diagnostic(
            diagnostic=diagnostic,
            strategy=strategy,
            trade_date=trade_date,
            created_at=now,
        )
        self._log_event(
            actor,
            phase,
            diagnostic.event_type,
            diagnostic.title,
            detail=diagnostic.diagnosis,
            severity="warning",
            strategy=strategy,
            symbol=quote.symbol,
            ref_table="quote_diagnostics",
            ref_id=diagnostic_id,
            metrics=diagnostic.metrics(),
            created_at=now,
            trade_date=trade_date,
        )

    def _llm_backend(self) -> LlmJsonBackend:
        if self.llm_adapter is not None:
            return self.llm_adapter
        backend = self.settings.llm_backend
        if backend in {"", "codex", "codex_cli"}:
            return CodexExecAdapter(
                cwd=Path.cwd(),
                model=self.settings.codex_model,
                timeout_seconds=self.settings.codex_timeout_seconds,
            )
        if backend in {"openai", "openai_api", "openai_responses_api"}:
            if not self.settings.openai_api_key:
                raise RuntimeError("TW_WATCH_LLM_BACKEND=openai_api 需要 OPENAI_API_KEY")
            return OpenAIResponsesAdapter(
                api_key=self.settings.openai_api_key,
                model=self.settings.codex_model,
                timeout_seconds=self.settings.codex_timeout_seconds,
            )
        if backend in {"anthropic", "anthropic_api", "anthropic_messages_api"}:
            if not self.settings.anthropic_api_key:
                raise RuntimeError("TW_WATCH_LLM_BACKEND=anthropic_api 需要 ANTHROPIC_API_KEY")
            return AnthropicMessagesAdapter(
                api_key=self.settings.anthropic_api_key,
                model=self.settings.codex_model,
                timeout_seconds=self.settings.codex_timeout_seconds,
            )
        raise RuntimeError(f"未知 LLM backend：{backend}")

    def _write_daily_reviews(self, trade_date: str) -> None:
        review_inputs: dict[str, tuple[str, dict[str, object]]] = {}
        for account_id, strategy in ((DAYTRADE_ACCOUNT, "daytrade"), (SWING_ACCOUNT, "swing")):
            account = self.store.get_account(account_id)
            fills = [row for row in self.store.list_fills(500) if str(row["strategy"]) == strategy and str(row["filled_at"]).startswith(trade_date)]
            pnl = sum(float(row["realized_pnl"]) for row in fills)
            strategy_version = ""
            if strategy == "swing":
                strategy_version = self.store.get_active_strategy_version("swing").version
            summary = f"{trade_date} {_strategy_label(strategy)} 日結：成交 {len(fills)} 筆，已實現損益 {pnl:,.2f}，帳戶現金 {account.cash:,.2f}。"
            metrics = {"fills": len(fills), "realized_pnl": pnl, "cash": account.cash, "capital": account.capital, "strategy_version": strategy_version}
            self.store.upsert_daily_review(
                trade_date,
                strategy,
                summary,
                metrics,
                strategy_version=strategy_version,
            )
            self._log_event(
                "system",
                "daily_review",
                "daily_review_written",
                "日結完成",
                detail=summary,
                strategy=strategy,
                created_at=datetime.now(timezone.utc),
                trade_date=trade_date,
                metrics=metrics,
            )
            review_inputs[strategy] = (summary, metrics)
        daytrade_input = review_inputs.get("daytrade")
        if daytrade_input is not None and self.settings.enable_codex_llm:
            self._maybe_run_daytrade_review(trade_date, daytrade_input[0], daytrade_input[1], force=True)
        swing_input = review_inputs.get("swing")
        if swing_input is not None and (self.llm_adapter is not None or self.settings.enable_codex_llm):
            self._maybe_run_swing_self_correction(trade_date, swing_input[0], swing_input[1], force=True)
        self._run_multi_agent_review(trade_date)

    def _run_multi_agent_review(self, trade_date: str) -> None:
        try:
            backend = self._llm_backend() if self.settings.enable_multi_agent_review else None
            if backend is None:
                self._log_event(
                    "system",
                    "daily_review",
                    "multi_agent_review_unavailable",
                    "多 Agent 策略檢討未執行",
                    detail="No LLM backend is enabled for multi-agent review.",
                    severity="warning",
                    trade_date=trade_date,
                )
                return
            orchestrator = MultiAgentReviewOrchestrator(store=self.store, settings=self.settings, backend=backend)
            result = orchestrator.run(trade_date, include_news_context=self.settings.enable_news_context)
            self._log_event(
                "system",
                "daily_review",
                "multi_agent_review_completed",
                "多 Agent 策略檢討完成",
                detail=result.summary,
                trade_date=trade_date,
                metrics={"review_run_id": result.review_run_id, "pending_versions": result.pending_versions, "rejected": result.rejected},
            )
        except Exception as exc:
            self._log_event(
                "system",
                "daily_review",
                "multi_agent_review_error",
                "多 Agent 策略檢討失敗",
                detail=str(exc),
                severity="warning",
                trade_date=trade_date,
            )

    def _maybe_run_daytrade_review(self, trade_date: str, base_summary: str, base_metrics: dict[str, object], *, force: bool = False) -> None:
        if not force and not self.settings.enable_codex_llm:
            self.store.upsert_daily_review(
                trade_date,
                "daytrade",
                base_summary,
                base_metrics,
                proposal_status="disabled",
                llm_summary="當沖 LLM 會後討論未啟用",
                llm_discussion="自動日結未啟用 TW_WATCH_ENABLE_CODEX_LLM；可按「立即完整討論」手動執行。",
            )
            return
        evidence = self._strategy_review_evidence("daytrade", trade_date)
        if not evidence["fills"] and not evidence["orders"] and not evidence["risk_events"]:
            detail = "當沖近 14 天沒有成交、委託或風控事件，略過 LLM 檢討。"
            self.store.upsert_daily_review(
                trade_date,
                "daytrade",
                base_summary,
                base_metrics,
                proposal_status="insufficient_data",
                llm_summary="資料不足，未完成當沖檢討",
                llm_discussion=detail,
                llm_result={"no_change_reason": detail},
            )
            self._log_event("system", "daily_review", "daytrade_review_skipped", "當沖會後討論略過", detail=detail, strategy="daytrade", trade_date=trade_date)
            return
        adapter = self.llm_adapter or CodexExecAdapter(
            cwd=Path.cwd(),
            model=self.settings.codex_model,
            timeout_seconds=self.settings.codex_timeout_seconds,
        )
        result = adapter.run_json(self._build_daytrade_review_prompt(trade_date, evidence), daily_review_schema())
        if not result.ok:
            self.store.add_llm_decision(strategy="daytrade", decision_type="daily_review", response={}, status="error", error=result.error)
            self.store.upsert_daily_review(
                trade_date,
                "daytrade",
                base_summary,
                base_metrics,
                proposal_status="llm_error",
                llm_summary="LLM 當沖會後討論失敗",
                llm_discussion=result.error,
                llm_result={"error": result.error},
            )
            self._log_event("system", "daily_review", "daytrade_review_error", "當沖會後討論失敗", detail=result.error, severity="warning", strategy="daytrade", trade_date=trade_date)
            return
        self.store.add_llm_decision(strategy="daytrade", decision_type="daily_review", response=result.data, status="ok")
        discussion = _format_daytrade_discussion(result.data)
        self.store.upsert_daily_review(
            trade_date,
            "daytrade",
            base_summary,
            base_metrics,
            proposal_status="reviewed",
            llm_summary=str(result.data.get("summary", "")).strip() or "當沖會後討論完成",
            llm_discussion=discussion,
            llm_result=result.data,
        )
        self._log_event(
            "system",
            "daily_review",
            "daytrade_review_completed",
            "當沖會後討論完成",
            detail=str(result.data.get("summary", "")).strip(),
            strategy="daytrade",
            trade_date=trade_date,
            metrics={"fills": len(evidence["fills"]), "orders": len(evidence["orders"]), "risk_events": len(evidence["risk_events"])},
        )

    def _maybe_run_swing_self_correction(self, trade_date: str, base_summary: str, base_metrics: dict[str, object], *, force: bool = False) -> None:
        active = self.store.get_active_strategy_version("swing")
        evidence = self._strategy_review_evidence("swing", trade_date)
        if not evidence["fills"] and not evidence["orders"] and not evidence["risk_events"]:
            detail = "短線近 14 天沒有成交、委託或風控事件，略過 LLM 改版。"
            self.store.upsert_daily_review(
                trade_date,
                "swing",
                base_summary,
                base_metrics,
                proposal_status="insufficient_data",
                strategy_version=active.version,
                llm_summary="資料不足，未產生新版",
                llm_discussion=detail,
                llm_result={"no_change_reason": detail},
            )
            self._log_event("system", "daily_review", "swing_review_skipped", "短線自我修正略過", detail=detail, strategy="swing", trade_date=trade_date)
            return
        prompt = self._build_swing_review_prompt(trade_date, active, evidence)
        adapter = self.llm_adapter or CodexExecAdapter(
            cwd=Path.cwd(),
            model=self.settings.codex_model,
            timeout_seconds=self.settings.codex_timeout_seconds,
        )
        result = adapter.run_json(prompt, swing_strategy_review_schema())
        if not result.ok:
            self.store.add_llm_decision(strategy="swing", decision_type="strategy_review", response={}, status="error", error=result.error)
            self.store.upsert_daily_review(
                trade_date,
                "swing",
                base_summary,
                base_metrics,
                proposal_status="llm_error",
                strategy_version=active.version,
                llm_summary="LLM 會後討論失敗",
                llm_discussion=result.error,
                llm_result={"error": result.error},
            )
            self._log_event("system", "daily_review", "swing_review_error", "短線自我修正失敗", detail=result.error, severity="warning", strategy="swing", trade_date=trade_date)
            return
        self.store.add_llm_decision(strategy="swing", decision_type="strategy_review", response=result.data, status="ok")
        current_params = swing_params_from_json(active.params)
        decision = build_swing_review_decision(result.data, current_params)
        llm_summary = decision.summary or ("建議建立新版" if decision.should_create_version else "不建議改版")
        if decision.validation_error:
            self.store.upsert_daily_review(
                trade_date,
                "swing",
                base_summary,
                base_metrics,
                proposal_status="validation_failed",
                strategy_version=active.version,
                llm_summary=llm_summary,
                llm_discussion=decision.discussion,
                llm_result={**result.data, "validation_error": decision.validation_error},
            )
            self._log_event(
                "system",
                "daily_review",
                "swing_review_validation_failed",
                "短線新版驗證失敗",
                detail=decision.validation_error,
                severity="warning",
                strategy="swing",
                trade_date=trade_date,
            )
            return
        if not decision.should_create_version:
            reason = decision.no_change_reason or "LLM 不建議建立新版。"
            self.store.upsert_daily_review(
                trade_date,
                "swing",
                base_summary,
                base_metrics,
                proposal_status="no_change",
                strategy_version=active.version,
                llm_summary=llm_summary,
                llm_discussion=decision.discussion or reason,
                llm_result=result.data,
            )
            self._log_event("system", "daily_review", "swing_review_no_change", "短線自我修正未改版", detail=reason, strategy="swing", trade_date=trade_date)
            return
        state = self.store.get_strategy_version_state("swing")
        new_version = self.store.create_strategy_version(
            strategy="swing",
            params=decision.params.to_json(),
            rules_text=decision.rules_text,
            discussion=decision.discussion,
            summary=llm_summary,
            data_start=str(evidence["data_start"]),
            data_end=str(evidence["data_end"]),
            metrics={
                "fills": len(evidence["fills"]),
                "orders": len(evidence["orders"]),
                "risk_events": len(evidence["risk_events"]),
                "expected_effect": decision.expected_effect,
                "risk_note": decision.risk_note,
            },
            parent_version=active.version,
            auto_activate=True,
        )
        applied = state.mode == FOLLOW_LATEST
        status = "version_created_applied" if applied else "version_created_locked"
        self.store.upsert_daily_review(
            trade_date,
                "swing",
                base_summary,
            {**base_metrics, "new_strategy_version": new_version.version, "applied": applied},
            proposal_status=status,
            strategy_version=new_version.version if applied else active.version,
            llm_summary=llm_summary,
            llm_discussion=decision.discussion,
            llm_result=result.data,
        )
        self._log_event(
            "system",
            "daily_review",
            "swing_strategy_version_created",
            "短線策略新版已建立",
            detail=f"{new_version.version}；{'已自動套用' if applied else '手動鎖定中，未覆蓋目前版本'}",
            strategy="swing",
            ref_table="strategy_versions",
            ref_id=new_version.id,
            trade_date=trade_date,
            metrics={"version": new_version.version, "applied": applied, "mode": state.mode},
        )

    def _strategy_review_evidence(self, strategy: str, trade_date: str) -> dict[str, object]:
        start_date, end_date = _review_window(trade_date)
        fills = [
            dict(row)
            for row in self.store.list_fills(500)
            if str(row["strategy"]) == strategy and _date_in_window(str(row["filled_at"]), start_date, end_date)
        ]
        orders = [
            dict(row)
            for row in self.store.list_orders(limit=500)
            if str(row["strategy"]) == strategy and _date_in_window(str(row["created_at"]), start_date, end_date)
        ]
        risk_events = [
            dict(row)
            for row in self.store.list_monitor_events(strategy=strategy, min_severity=None, limit=500)
            if _date_in_window(str(row["trade_date"]), start_date, end_date)
            and str(row["actor"]) in {"risk_manager", _actor_for_strategy(strategy), "system"}
        ]
        return {
            "data_start": start_date.isoformat(),
            "data_end": end_date.isoformat(),
            "fills": fills,
            "orders": orders,
            "risk_events": risk_events,
        }

    def _build_swing_review_prompt(self, trade_date: str, active_version, evidence: dict[str, object]) -> str:
        payload = {
            "task": "Review the Taiwan stock swing simulation result and decide whether to create a new swing strategy parameter version.",
            "output_language": "zh-Hant-TW",
            "hard_rules": [
                "Return JSON only.",
                TRADITIONAL_CHINESE_OUTPUT_RULE,
                "Do not suggest real-money trading or real order placement.",
                "Only change whitelisted parameters in parameter_changes.",
                "Return every parameter_changes key; use null for unchanged parameters.",
                "Set should_create_version=false when evidence is weak or no clear improvement is justified.",
            ],
            "trade_date": trade_date,
            "active_version": active_version.version,
            "active_params": active_version.params,
            "active_rules_text": active_version.rules_text,
            "evidence": evidence,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _build_daytrade_review_prompt(self, trade_date: str, evidence: dict[str, object]) -> str:
        payload = {
            "task": "Review the Taiwan stock daytrade simulation result and produce a post-session strategy review.",
            "output_language": "zh-Hant-TW",
            "hard_rules": [
                "Return JSON only.",
                TRADITIONAL_CHINESE_OUTPUT_RULE,
                "Do not suggest real-money trading or real order placement.",
                "Focus on simulated daytrade behavior, order quality, risk events, skipped trades, and next rules to test.",
                "Do not modify Python code or bypass hard risk limits.",
            ],
            "trade_date": trade_date,
            "strategy": "daytrade",
            "evidence": evidence,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _set_status(
        self,
        actor: str,
        phase: str,
        message: str,
        *,
        strategy: str = "",
        symbol: str = "",
        now: datetime | None = None,
        running: bool = True,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        next_daytrade, next_swing = _next_ticks(now, self.settings.timezone)
        with self._status_lock:
            self._status = WorkerStatus(
                running=running,
                actor=actor,
                phase=phase,
                strategy=strategy,
                symbol=symbol.upper(),
                message=message,
                last_heartbeat=now,
                last_event=self._status.last_event,
                next_daytrade_tick=next_daytrade,
                next_swing_tick=next_swing,
            )

    def _log_event(
        self,
        actor: str,
        phase: str,
        event_type: str,
        title: str,
        *,
        detail: str = "",
        severity: str = "info",
        trade_date: str | None = None,
        strategy: str = "",
        symbol: str = "",
        ref_table: str = "",
        ref_id: int | None = None,
        metrics: dict[str, object] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or datetime.now(timezone.utc)
        if trade_date is None:
            trade_date = created_at.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        event_id = self.store.add_monitor_event(
            actor=actor,
            phase=phase,
            event_type=event_type,
            title=title,
            detail=detail,
            severity=severity,
            trade_date=trade_date,
            strategy=strategy,
            symbol=symbol,
            ref_table=ref_table,
            ref_id=ref_id,
            metrics=metrics,
            created_at=created_at,
        )
        with self._status_lock:
            self._status = replace(self._status, last_event=title, last_heartbeat=created_at)
        return event_id


def _actor_for_strategy(strategy: str) -> str:
    return "daytrade_trader" if strategy == "daytrade" else "swing_trader"


def _strategy_label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, strategy)


def _proposal_status_label(value: object) -> str:
    return {
        "none": "無",
        "reviewing": "討論中",
        "reviewed": "已檢討",
        "disabled": "未啟用",
        "insufficient_data": "資料不足",
        "llm_error": "LLM 失敗",
        "validation_failed": "驗證失敗",
        "no_change": "已討論，不需改版",
        "review_only": "已討論，不需改版",
        "risk_rejected": "風控拒絕",
        "pending_version_created": "已建立新版",
        "pending_version_reused": "沿用既有新版",
        "version_created_applied": "已建立並套用",
        "version_created_locked": "已建立未套用",
        "version_reused_applied": "沿用既有新版並套用",
        "version_reused_locked": "沿用既有新版但目前鎖定",
    }.get(str(value), str(value))


def _review_window(trade_date: str, days: int = REVIEW_LOOKBACK_DAYS) -> tuple[date, date]:
    end_date = date.fromisoformat(str(trade_date)[:10])
    return end_date - timedelta(days=days - 1), end_date


def _date_in_window(value: str, start_date: date, end_date: date) -> bool:
    if not value:
        return False
    try:
        item_date = date.fromisoformat(value[:10])
    except ValueError:
        return False
    return start_date <= item_date <= end_date


def _dates_from_rows(rows: list[Row], timestamp_field: str, strategy: str) -> list[str]:
    dates: list[str] = []
    for row in rows:
        if str(row["strategy"]) != strategy:
            continue
        value = str(row[timestamp_field] or "")
        if len(value) >= 10:
            dates.append(value[:10])
    return dates


def _format_daytrade_discussion(data: dict[str, object]) -> str:
    mistakes = data.get("mistakes")
    rules = data.get("next_rules_to_test")
    mistake_lines = "\n".join(f"- {item}" for item in mistakes) if isinstance(mistakes, list) else "-"
    rule_lines = "\n".join(f"- {item}" for item in rules) if isinstance(rules, list) else "-"
    return (
        f"檢討摘要\n{str(data.get('summary', '')).strip() or '-'}\n\n"
        f"錯誤 / 弱點\n{mistake_lines}\n\n"
        f"下一步要測的規則\n{rule_lines}\n\n"
        f"資金建議\n{str(data.get('capital_suggestion', '')).strip() or '-'}"
    )


def _snapshot_should_fill(order, snapshot: Row) -> bool:
    price = float(snapshot["price"])
    bid = snapshot["bid_price"]
    ask = snapshot["ask_price"]
    bid_price = float(bid) if bid is not None else None
    ask_price = float(ask) if ask is not None else None
    volume = float(snapshot["volume"])
    if volume <= 0:
        return False
    if order.side == "buy":
        return price <= order.price or (ask_price is not None and ask_price <= order.price)
    return price >= order.price or (bid_price is not None and bid_price >= order.price)


def _tick_should_fill(order, tick: Row) -> bool:
    price = float(tick["price"])
    size = float(tick["size"])
    if size <= 0:
        return False
    if order.side == "buy":
        return price <= order.price
    return price >= order.price


def _market_event_key(event: RealtimeMarketEvent) -> str:
    serial = _payload_text(event.payload, "serial")
    if serial:
        return f"{event.channel}:{event.symbol}:{event.exchange_time.isoformat()}:{serial}"
    source = json.dumps(event.raw, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"{event.channel}:{event.symbol}:{event.exchange_time.isoformat()}:{digest}"


def _payload_float(payload: dict[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return "" if value is None else str(value)


def _levels_from_payload(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    levels: list[dict[str, object]] = []
    for row in value[:5]:
        if not isinstance(row, dict):
            continue
        price = _payload_float(row, "price")
        size = _payload_float(row, "size")
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        levels.append({"price": price, "size": size})
    return levels


def _trade_side(price: float, bid: float | None, ask: float | None) -> str:
    if ask is not None and price >= ask:
        return "ask"
    if bid is not None and price <= bid:
        return "bid"
    return ""


def _auto_scout_time(value: str) -> time:
    try:
        hour, minute = [int(part) for part in value.strip().split(":", 1)]
        return time(hour, minute)
    except (AttributeError, TypeError, ValueError):
        return time(9, 5)


def _provider_unavailable_log_interval_seconds(local: datetime) -> int:
    return 5 * 60 if _is_provider_active_retry_window(local) else 60 * 60


def _is_provider_active_retry_window(local: datetime) -> bool:
    return local.weekday() < 5 and time(8, 0) <= local.time() <= time(13, 35)


def _next_ticks(now: datetime, timezone_name: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(timezone_name))
    return (
        _next_tick(local, start=time(9, 10), end=time(13, 20), interval_minutes=5).astimezone(timezone.utc),
        _next_tick(local, start=time(9, 10), end=time(13, 30), interval_minutes=30).astimezone(timezone.utc),
    )


def _next_tick(local: datetime, *, start: time, end: time, interval_minutes: int) -> datetime:
    start_dt = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    end_dt = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if local <= start_dt:
        return start_dt
    if local > end_dt:
        return start_dt + timedelta(days=1)
    elapsed_minutes = int((local - start_dt).total_seconds() // 60)
    next_offset = ((elapsed_minutes // interval_minutes) + 1) * interval_minutes
    next_dt = start_dt + timedelta(minutes=next_offset)
    return next_dt if next_dt <= end_dt else start_dt + timedelta(days=1)
