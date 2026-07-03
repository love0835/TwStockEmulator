from __future__ import annotations

import json
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, BooleanVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk
from zoneinfo import ZoneInfo

from tw_watchdesk.config import app_base_dir, load_settings, nova_settings_file, save_app_settings, save_nova_settings, settings_search_dirs
from tw_watchdesk.models import WatchState
from tw_watchdesk.nova import create_provider
from tw_watchdesk.redaction import redact_text
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, SWING_ACCOUNT, TradingStore, default_db_path
from tw_watchdesk.strategy import build_watch_state
from tw_watchdesk.strategy_versions import FOLLOW_LATEST, MANUAL_LOCK, daytrade_params_from_json, swing_params_from_json
from tw_watchdesk.worker import TradingLabWorker


STRATEGY_LABELS = {
    "scout": "抓盤",
    "daytrade": "當沖",
    "swing": "短線",
}
STRATEGY_VALUES = {value: key for key, value in STRATEGY_LABELS.items()}
ACTOR_LABELS = {
    "system": "系統",
    "scout": "抓盤手",
    "daytrade_trader": "當沖模擬交易員",
    "swing_trader": "短線模擬交易員",
    "risk_manager": "風控員",
    "CoachAgent": "總教練 Agent",
    "RiskAgent": "風控 Agent",
    "NewsContextAgent": "新聞背景 Agent",
}
ACTOR_VALUES = {value: key for key, value in ACTOR_LABELS.items()}
SEVERITY_LABELS = {
    "info": "資訊",
    "warning": "警告",
    "error": "錯誤",
}
PHASE_LABELS = {
    "stopped": "已停止",
    "starting": "啟動中",
    "tick": "排程檢查",
    "idle": "等待排程",
    "market_closed": "休市",
    "scouting": "篩選候選",
    "manual_candidate": "手動候選",
    "strategy_check": "策略檢查",
    "exit_only": "出場-only",
    "risk_check": "風控檢查",
    "order_created": "建立委託",
    "fill_check": "成交檢查",
    "exit_order": "出場委託",
    "daily_review": "日結",
    "orders": "掛單管理",
    "error": "錯誤",
}


def _strategy_value(label_or_value: str) -> str:
    value = label_or_value.strip()
    return STRATEGY_VALUES.get(value, value.lower())


def _strategy_label(value: object) -> str:
    return STRATEGY_LABELS.get(str(value), str(value))


def _actor_value(label_or_value: str) -> str | None:
    value = label_or_value.strip()
    if value == "全部":
        return None
    return ACTOR_VALUES.get(value, value)


def _actor_label(value: object) -> str:
    return ACTOR_LABELS.get(str(value), str(value))


def _severity_label(value: object) -> str:
    return SEVERITY_LABELS.get(str(value), str(value))


def _phase_label(value: object) -> str:
    return PHASE_LABELS.get(str(value), str(value))


class WatchDeskApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.settings = load_settings()
        self.provider = create_provider(self.settings)
        self.store = TradingStore(self.settings.db_path or default_db_path(app_base_dir()))
        self.store.initialize()
        self.events: queue.Queue[WatchState] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.lab_worker = TradingLabWorker(
            settings=self.settings,
            store=self.store,
            provider=self.provider,
        )
        self._last_lab_refresh = 0.0
        env_label = (
            "設定檔：" + "、".join(str(path) for path in self.settings.loaded_env_files)
            if self.settings.loaded_env_files
            else "未讀到 .env.local；搜尋：" + "、".join(str(path) for path in settings_search_dirs())
        )

        self.symbol_var = StringVar(value="2330")
        self.capital_var = StringVar(value="1000000")
        self.strategy_var = StringVar(value="短線")
        self.months_var = StringVar(value="3")
        self.status_var = StringVar(value=env_label)
        self.quality_var = StringVar(value="-")
        self.quote_var = StringVar(value="-")
        self.volume_var = StringVar(value="-")
        self.time_var = StringVar(value="-")
        self.inst_var = StringVar(value="外資 / 投信：官方日報背景，不作為盤中即時條件")
        self.action_var = StringVar(value="資料不可用")
        self.buy_var = StringVar(value="-")
        self.sell_var = StringVar(value="-")
        self.qty_var = StringVar(value="-")
        self.stop_var = StringVar(value="-")
        self.take_var = StringVar(value="-")
        self.reason_var = StringVar(value="沒有即時資料時不產生攻略。")
        self.lab_symbol_var = StringVar(value="2330")
        self.lab_strategy_var = StringVar(value="當沖")
        self.lab_status_var = StringVar(value=f"交易實驗室就緒：{self.store.path}")
        self.auto_scout_enabled_var = BooleanVar(value=self.settings.enable_auto_scout)
        self.auto_scout_status_var = StringVar(value="自動選股已啟用" if self.settings.enable_auto_scout else "自動選股未啟用")
        self.swing_self_correction_enabled_var = BooleanVar(value=self.settings.enable_swing_self_correction)
        self.swing_self_correction_status_var = StringVar(value="短線自我修正已啟用" if self.settings.enable_swing_self_correction else "短線自我修正未啟用")
        self.daytrade_delta_var = StringVar(value="100000")
        self.swing_delta_var = StringVar(value="100000")
        self.monitor_running_var = StringVar(value="未啟動")
        self.monitor_actor_var = StringVar(value="-")
        self.monitor_phase_var = StringVar(value="-")
        self.monitor_message_var = StringVar(value="-")
        self.monitor_heartbeat_var = StringVar(value="-")
        self.monitor_last_event_var = StringVar(value="-")
        self.monitor_next_daytrade_var = StringVar(value="-")
        self.monitor_next_swing_var = StringVar(value="-")
        self.monitor_mode_var = StringVar(value=self.settings.market_data_mode)
        self.monitor_db_var = StringVar(value=str(self.store.path))
        self.monitor_candidates_var = StringVar(value="0")
        self.monitor_orders_var = StringVar(value="0")
        self.monitor_fills_var = StringVar(value="0")
        self.monitor_pnl_var = StringVar(value="0.00")
        self.monitor_positions_var = StringVar(value="0")
        self.monitor_warnings_var = StringVar(value="0")
        self.monitor_errors_var = StringVar(value="0")
        self.monitor_severity_filter_var = StringVar(value="全部")
        self.monitor_actor_filter_var = StringVar(value="全部")
        self.monitor_strategy_filter_var = StringVar(value="全部")
        self.monitor_symbol_filter_var = StringVar(value="")
        self.monitor_today_only_var = BooleanVar(value=True)
        self.strategy_versions_show_all_var = BooleanVar(value=False)
        self.strategy_version_strategy_var = StringVar(value="短線")
        self.strategy_active_version_var = StringVar(value="-")
        self.strategy_mode_var = StringVar(value="-")
        self.strategy_last_review_var = StringVar(value="-")
        self.strategy_last_result_var = StringVar(value="-")
        self.daily_review_rows_by_id: dict[str, object] = {}

        self._build_ui()
        self._pump_events()

    def _build_ui(self) -> None:
        self.root.title("台股即時看盤桌面版")
        self.root.geometry("1180x800")
        self.root.minsize(960, 680)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main_tabs = ttk.Notebook(self.root)
        main_tabs.grid(row=0, column=0, sticky="nsew")

        watch_tab = ttk.Frame(main_tabs, padding=12)
        lab_tab = ttk.Frame(main_tabs, padding=12)
        main_tabs.add(watch_tab, text="單檔看盤")
        main_tabs.add(lab_tab, text="模擬交易實驗室")

        self._make_watch_tab(watch_tab)
        self._make_lab_workspace(lab_tab)

    def _make_watch_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for idx in range(11):
            controls.columnconfigure(idx, weight=0)
        controls.columnconfigure(8, weight=1)

        ttk.Label(controls, text="股票").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.symbol_var, width=10).grid(row=0, column=1, padx=(0, 12))
        ttk.Label(controls, text="起始金額").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.capital_var, width=14).grid(row=0, column=3, padx=(0, 12))
        ttk.Label(controls, text="策略").grid(row=0, column=4, padx=(0, 6))
        ttk.Combobox(controls, textvariable=self.strategy_var, values=("短線", "當沖"), width=10, state="readonly").grid(row=0, column=5, padx=(0, 12))
        ttk.Label(controls, text="月份").grid(row=0, column=6, padx=(0, 6))
        ttk.Spinbox(controls, textvariable=self.months_var, from_=1, to=12, width=6).grid(row=0, column=7, padx=(0, 12))
        ttk.Button(controls, text="台新 API 設定", command=self.open_api_settings).grid(row=0, column=8, sticky="e", padx=(0, 6))
        ttk.Button(controls, text="開始看盤", command=self.start).grid(row=0, column=9, sticky="e", padx=(0, 6))
        ttk.Button(controls, text="停止看盤", command=self.stop).grid(row=0, column=10, sticky="e")

        body = ttk.Frame(parent)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        self._make_status_panel(body)
        self._make_book_panel(body)
        self._make_advice_panel(body)

    def _make_lab_workspace(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for idx in range(16):
            controls.columnconfigure(idx, weight=0)
        controls.columnconfigure(15, weight=1)

        ttk.Label(controls, text="候選股票").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.lab_symbol_var, width=10).grid(row=0, column=1, padx=(0, 12))
        ttk.Label(controls, text="策略").grid(row=0, column=2, padx=(0, 6))
        ttk.Combobox(controls, textvariable=self.lab_strategy_var, values=("當沖", "短線"), width=10, state="readonly").grid(row=0, column=3, padx=(0, 12))
        ttk.Button(controls, text="加入候選", command=self.add_lab_candidate).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(controls, text="啟動交易實驗室", command=self.start_lab).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(controls, text="停止交易實驗室", command=self.stop_lab).grid(row=0, column=6, padx=(0, 12))
        ttk.Checkbutton(controls, text="啟用自動選股", variable=self.auto_scout_enabled_var, command=self.toggle_auto_scout).grid(row=0, column=7, padx=(0, 8))
        ttk.Button(controls, text="立即選股", command=self.run_auto_scout_now).grid(row=0, column=8, padx=(0, 8))
        ttk.Label(controls, textvariable=self.auto_scout_status_var, wraplength=180).grid(row=0, column=9, sticky="w", padx=(0, 8))
        ttk.Checkbutton(controls, text="啟用短線自我修正", variable=self.swing_self_correction_enabled_var, command=self.toggle_swing_self_correction).grid(row=0, column=10, padx=(0, 8))
        ttk.Button(controls, text="立即當沖討論", command=self.run_daytrade_review_now).grid(row=0, column=11, padx=(0, 8))
        ttk.Button(controls, text="立即短線討論", command=self.run_swing_review_now).grid(row=0, column=12, padx=(0, 8))
        ttk.Button(controls, text="多 Agent 檢討", command=self.run_multi_agent_review_now).grid(row=0, column=13, padx=(0, 8))
        ttk.Label(controls, textvariable=self.swing_self_correction_status_var, wraplength=160).grid(row=0, column=14, sticky="w", padx=(0, 8))
        ttk.Label(controls, textvariable=self.lab_status_var, wraplength=320).grid(row=0, column=15, sticky="w")

        body = ttk.Frame(parent)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self._make_lab_tabs(body)

    def _make_status_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="即時狀態", padding=12)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 10))
        rows = [
            ("狀態", self.status_var),
            ("現價", self.quote_var),
            ("成交量", self.volume_var),
            ("資料時間", self.time_var),
            ("資料品質", self.quality_var),
            ("法人資料", self.inst_var),
        ]
        for idx, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=idx, column=0, sticky="w", pady=3)
            ttk.Label(frame, textvariable=var, wraplength=310).grid(row=idx, column=1, sticky="w", pady=3)

    def _make_book_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="五檔", padding=12)
        frame.grid(row=0, column=1, sticky="nsew", padx=8, pady=(0, 10))
        self.bid_tree = ttk.Treeview(frame, columns=("price", "size"), show="headings", height=5)
        self.ask_tree = ttk.Treeview(frame, columns=("price", "size"), show="headings", height=5)
        for tree, title in ((self.bid_tree, "買方"), (self.ask_tree, "賣方")):
            tree.heading("price", text=f"{title}價格")
            tree.heading("size", text="量")
            tree.column("price", width=90, anchor="e")
            tree.column("size", width=90, anchor="e")
        self.bid_tree.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.ask_tree.grid(row=0, column=1, sticky="nsew")

    def _make_advice_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="攻略", padding=12)
        frame.grid(row=0, column=2, sticky="nsew", padx=(8, 0), pady=(0, 10))
        rows = [
            ("動作", self.action_var),
            ("掛買", self.buy_var),
            ("掛賣", self.sell_var),
            ("股數", self.qty_var),
            ("停損", self.stop_var),
            ("停利", self.take_var),
            ("理由", self.reason_var),
        ]
        for idx, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=idx, column=0, sticky="w", pady=3)
            ttk.Label(frame, textvariable=var, wraplength=310).grid(row=idx, column=1, sticky="w", pady=3)

    def _make_lab_tabs(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.lab_notebook = notebook
        self.monitor_tree = self._make_monitor_tab(notebook)
        self.account_tree = self._make_account_tab(notebook)
        self.candidate_tree = self._make_tree_tab(
            notebook,
            "今日候選",
            ("date", "strategy", "symbol", "status", "score", "source", "reason"),
            ("日期", "策略", "股票", "狀態", "分數", "來源", "理由"),
        )
        self.position_tree = self._make_tree_tab(
            notebook,
            "持倉",
            ("account", "strategy", "symbol", "qty", "avg", "stop", "take"),
            ("帳戶", "策略", "股票", "股數", "均價", "停損", "停利"),
        )
        self.order_tree = self._make_tree_tab(
            notebook,
            "委託",
            ("strategy", "symbol", "side", "price", "qty", "status", "reason"),
            ("策略", "股票", "買賣", "價格", "股數", "狀態", "理由"),
        )
        self.fill_tree = self._make_tree_tab(
            notebook,
            "成交",
            ("time", "strategy", "symbol", "side", "price", "qty", "pnl"),
            ("時間", "策略", "股票", "買賣", "價格", "股數", "損益"),
        )
        self.review_tree = self._make_tree_tab(
            notebook,
            "每日檢討",
            ("date", "created", "strategy", "version", "status", "summary"),
            ("交易日", "檢討時間", "策略", "版本", "提案狀態", "摘要"),
        )
        self.review_tree.bind("<Double-1>", self.open_daily_review_detail)
        self.review_tree.column("date", width=100)
        self.review_tree.column("created", width=150)
        self.review_tree.column("summary", width=520)
        self.quote_diagnostic_tree = self._make_tree_tab(
            notebook,
            "五檔診斷",
            ("time", "strategy", "symbol", "diagnosis", "bid_ask", "age", "flags"),
            ("時間", "策略", "股票", "診斷", "買/賣檔", "資料年齡", "旗標"),
        )
        self.quote_diagnostic_tree.column("diagnosis", width=220)
        self.quote_diagnostic_tree.column("flags", width=420)
        self.strategy_version_tree = self._make_strategy_versions_tab(notebook)

    def _make_monitor_tab(self, notebook: ttk.Notebook) -> ttk.Treeview:
        frame = ttk.Frame(notebook, padding=10)
        notebook.add(frame, text="監控")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        status = ttk.LabelFrame(frame, text="目前狀態", padding=10)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for idx in range(8):
            status.columnconfigure(idx, weight=1)
        status_rows = [
            ("Worker", self.monitor_running_var),
            ("角色", self.monitor_actor_var),
            ("階段", self.monitor_phase_var),
            ("訊息", self.monitor_message_var),
            ("最後心跳", self.monitor_heartbeat_var),
            ("最後事件", self.monitor_last_event_var),
            ("下次當沖", self.monitor_next_daytrade_var),
            ("下次短線", self.monitor_next_swing_var),
            ("資料模式", self.monitor_mode_var),
            ("DB", self.monitor_db_var),
        ]
        for idx, (label, var) in enumerate(status_rows):
            row = idx // 4
            col = (idx % 4) * 2
            ttk.Label(status, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)
            ttk.Label(status, textvariable=var, wraplength=240).grid(row=row, column=col + 1, sticky="w", padx=(0, 14), pady=2)

        summary = ttk.LabelFrame(frame, text="今日摘要", padding=10)
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        summary_items = [
            ("候選", self.monitor_candidates_var),
            ("開放委託", self.monitor_orders_var),
            ("今日成交", self.monitor_fills_var),
            ("已實現損益", self.monitor_pnl_var),
            ("持倉", self.monitor_positions_var),
            ("警告", self.monitor_warnings_var),
            ("錯誤", self.monitor_errors_var),
        ]
        for idx, (label, var) in enumerate(summary_items):
            ttk.Label(summary, text=label).grid(row=0, column=idx * 2, sticky="w", padx=(0, 4))
            ttk.Label(summary, textvariable=var).grid(row=0, column=idx * 2 + 1, sticky="w", padx=(0, 18))

        log_frame = ttk.LabelFrame(frame, text="決策 Log", padding=10)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        filters = ttk.Frame(log_frame)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(filters, text="只看今日", variable=self.monitor_today_only_var, command=self._refresh_lab_views).grid(row=0, column=0, padx=(0, 10))
        ttk.Label(filters, text="等級").grid(row=0, column=1, padx=(0, 4))
        ttk.Combobox(filters, textvariable=self.monitor_severity_filter_var, values=("全部", "警告以上", "錯誤"), width=10, state="readonly").grid(row=0, column=2, padx=(0, 10))
        ttk.Label(filters, text="角色").grid(row=0, column=3, padx=(0, 4))
        ttk.Combobox(
            filters,
            textvariable=self.monitor_actor_filter_var,
            values=("全部", "系統", "抓盤手", "當沖模擬交易員", "短線模擬交易員", "風控員"),
            width=16,
            state="readonly",
        ).grid(row=0, column=4, padx=(0, 10))
        ttk.Label(filters, text="策略").grid(row=0, column=5, padx=(0, 4))
        ttk.Combobox(filters, textvariable=self.monitor_strategy_filter_var, values=("全部", "抓盤", "當沖", "短線"), width=10, state="readonly").grid(row=0, column=6, padx=(0, 10))
        ttk.Label(filters, text="股票").grid(row=0, column=7, padx=(0, 4))
        ttk.Entry(filters, textvariable=self.monitor_symbol_filter_var, width=10).grid(row=0, column=8, padx=(0, 10))
        ttk.Button(filters, text="手動刷新", command=self._refresh_lab_views).grid(row=0, column=9)
        for var in (self.monitor_severity_filter_var, self.monitor_actor_filter_var, self.monitor_strategy_filter_var):
            var.trace_add("write", lambda *_: self._refresh_lab_views())

        columns = ("time", "severity", "actor", "phase", "strategy", "symbol", "event", "detail")
        tree = ttk.Treeview(log_frame, columns=columns, show="headings", height=14)
        headings = ("時間", "等級", "角色", "階段", "策略", "股票", "事件", "說明")
        widths = (150, 70, 130, 100, 80, 130, 150, 370)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        return tree

    def _make_account_tab(self, notebook: ttk.Notebook) -> ttk.Treeview:
        frame = ttk.Frame(notebook, padding=10)
        notebook.add(frame, text="帳戶資金")
        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="當沖加減碼").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(controls, textvariable=self.daytrade_delta_var, width=12).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(controls, text="套用", command=lambda: self._apply_capital(DAYTRADE_ACCOUNT, self.daytrade_delta_var)).grid(row=0, column=2, padx=(0, 16))
        ttk.Label(controls, text="短線加減碼").grid(row=0, column=3, sticky="w", padx=(0, 6))
        ttk.Entry(controls, textvariable=self.swing_delta_var, width=12).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(controls, text="套用", command=lambda: self._apply_capital(SWING_ACCOUNT, self.swing_delta_var)).grid(row=0, column=5)
        tree = ttk.Treeview(frame, columns=("id", "capital", "cash", "reserved", "pnl"), show="headings", height=5)
        for column, heading in (
            ("id", "帳戶"),
            ("capital", "資本"),
            ("cash", "現金"),
            ("reserved", "凍結"),
            ("pnl", "已實現損益"),
        ):
            tree.heading(column, text=heading)
            tree.column(column, width=120, anchor="e" if column != "id" else "w")
        tree.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _make_strategy_versions_tab(self, notebook: ttk.Notebook) -> ttk.Treeview:
        frame = ttk.Frame(notebook, padding=10)
        notebook.add(frame, text="策略版本")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(3, weight=1)

        status = ttk.LabelFrame(frame, text="目前策略版本", padding=10)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        rows = [
            ("目前版本", self.strategy_active_version_var),
            ("模式", self.strategy_mode_var),
            ("最後討論", self.strategy_last_review_var),
            ("最後結果", self.strategy_last_result_var),
        ]
        for idx, (label, var) in enumerate(rows):
            ttk.Label(status, text=label).grid(row=0, column=idx * 2, sticky="w", padx=(0, 4))
            ttk.Label(status, textvariable=var, wraplength=220).grid(row=0, column=idx * 2 + 1, sticky="w", padx=(0, 18))

        controls = ttk.Frame(frame)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="策略").grid(row=0, column=0, padx=(0, 6))
        selector = ttk.Combobox(controls, textvariable=self.strategy_version_strategy_var, values=("抓盤", "當沖", "短線"), width=10, state="readonly")
        selector.grid(row=0, column=1, padx=(0, 10))
        selector.bind("<<ComboboxSelected>>", lambda *_: self._refresh_strategy_versions_view())
        ttk.Checkbutton(controls, text="顯示全部版本", variable=self.strategy_versions_show_all_var, command=self._refresh_strategy_versions_view).grid(row=0, column=2, padx=(0, 10))
        ttk.Button(controls, text="套用此版本", command=self.apply_selected_strategy_version).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(controls, text="回到跟隨最新版", command=self.follow_latest_strategy_version).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(controls, text="刷新", command=self._refresh_lab_views).grid(row=0, column=5)

        columns = ("version", "status", "created", "activated", "parent", "summary")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        headings = ("版本", "狀態", "建立時間", "啟用時間", "父版本", "摘要")
        widths = (90, 90, 150, 150, 90, 520)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w")
        tree.grid(row=2, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", lambda *_: self._refresh_strategy_version_detail())

        detail_frame = ttk.Frame(frame)
        detail_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.strategy_version_detail_text = Text(detail_frame, height=12, wrap="word")
        self.strategy_version_detail_text.grid(row=0, column=0, sticky="nsew")
        detail_scrollbar = ttk.Scrollbar(detail_frame, orient="vertical", command=self.strategy_version_detail_text.yview)
        detail_scrollbar.grid(row=0, column=1, sticky="ns")
        self.strategy_version_detail_text.configure(yscrollcommand=detail_scrollbar.set)
        self.strategy_version_detail_text.configure(state="disabled")
        return tree

    def _make_tree_tab(self, notebook: ttk.Notebook, title: str, columns: tuple[str, ...], headings: tuple[str, ...]) -> ttk.Treeview:
        frame = ttk.Frame(notebook, padding=10)
        notebook.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=9)
        for column, heading in zip(columns, headings):
            tree.heading(column, text=heading)
            tree.column(column, width=120, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.status_var.set("監控中")
        self.worker = threading.Thread(target=self._watch_loop, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("已停止")

    def start_lab(self) -> None:
        self.lab_worker.start()
        self.lab_status_var.set("交易實驗室執行中")

    def stop_lab(self) -> None:
        self.lab_worker.stop()
        self.lab_status_var.set("交易實驗室已停止")

    def add_lab_candidate(self) -> None:
        symbol = self.lab_symbol_var.get().strip().upper()
        strategy = _strategy_value(self.lab_strategy_var.get())
        if strategy not in {"daytrade", "swing"}:
            strategy = "daytrade"
        if not symbol:
            messagebox.showwarning("缺少股票", "請先輸入股票代號。")
            return
        self.lab_worker.add_manual_candidate(strategy, symbol)
        self.lab_status_var.set(f"已加入候選：{_strategy_label(strategy)} {symbol}")
        self._refresh_lab_views()

    def toggle_auto_scout(self) -> None:
        enabled = self.auto_scout_enabled_var.get()
        target_path = nova_settings_file(self.settings)
        save_app_settings(target_path, {"TW_WATCH_ENABLE_AUTO_SCOUT": "true" if enabled else "false"})
        self.settings = load_settings()
        self.lab_worker.settings = self.settings
        self.auto_scout_enabled_var.set(self.settings.enable_auto_scout)
        self.auto_scout_status_var.set("自動選股已啟用" if self.settings.enable_auto_scout else "自動選股未啟用")
        self.store.add_monitor_event(
            actor="scout",
            phase="scouting",
            event_type="auto_scout_toggle",
            title="自動選股設定更新",
            detail=self.auto_scout_status_var.get(),
            created_at=datetime.now(timezone.utc),
            trade_date=datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={"enabled": self.settings.enable_auto_scout},
        )
        self._refresh_lab_views()

    def toggle_swing_self_correction(self) -> None:
        enabled = self.swing_self_correction_enabled_var.get()
        target_path = nova_settings_file(self.settings)
        save_app_settings(target_path, {"TW_WATCH_ENABLE_SWING_SELF_CORRECTION": "true" if enabled else "false"})
        self.settings = load_settings()
        self.lab_worker.settings = self.settings
        self.swing_self_correction_enabled_var.set(self.settings.enable_swing_self_correction)
        self.swing_self_correction_status_var.set("短線自我修正已啟用" if self.settings.enable_swing_self_correction else "短線自我修正未啟用")
        now = datetime.now(timezone.utc)
        self.store.add_monitor_event(
            actor="system",
            phase="daily_review",
            event_type="swing_self_correction_toggle",
            title="短線自我修正設定更新",
            detail=self.swing_self_correction_status_var.get(),
            strategy="swing",
            created_at=now,
            trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={"enabled": self.settings.enable_swing_self_correction},
        )
        self._refresh_lab_views()

    def run_auto_scout_now(self) -> None:
        self.auto_scout_status_var.set("立即選股執行中")
        thread = threading.Thread(target=self._run_auto_scout_now_thread, daemon=True)
        thread.start()

    def _run_auto_scout_now_thread(self) -> None:
        try:
            self.lab_worker.run_auto_scout_now()
            message = self.lab_worker.status_snapshot().message or "立即選股完成"
        except Exception as exc:
            message = f"立即選股失敗：{exc.__class__.__name__}: {exc}"
            self.store.add_monitor_event(
                actor="scout",
                phase="scouting",
                event_type="manual_scout_error",
                title="立即選股失敗",
                detail=message,
                severity="error",
                created_at=datetime.now(timezone.utc),
                trade_date=datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat(),
            )
        self.root.after(0, lambda: self._finish_auto_scout_now(message))

    def _finish_auto_scout_now(self, message: str) -> None:
        self.auto_scout_status_var.set(message)
        self._refresh_lab_views()

    def run_swing_review_now(self) -> None:
        self.swing_self_correction_status_var.set("短線會後討論執行中")
        thread = threading.Thread(target=self._run_swing_review_now_thread, daemon=True)
        thread.start()

    def run_daytrade_review_now(self) -> None:
        self.lab_status_var.set("當沖會後討論執行中")
        thread = threading.Thread(target=self._run_daytrade_review_now_thread, daemon=True)
        thread.start()

    def run_multi_agent_review_now(self) -> None:
        self.lab_status_var.set("多 Agent 策略檢討執行中")
        thread = threading.Thread(target=self._run_multi_agent_review_now_thread, daemon=True)
        thread.start()

    def _run_daytrade_review_now_thread(self) -> None:
        try:
            message = self.lab_worker.run_daytrade_review_now()
        except Exception as exc:
            message = f"當沖會後討論失敗：{exc.__class__.__name__}: {exc}"
            self.store.add_monitor_event(
                actor="system",
                phase="daily_review",
                event_type="daytrade_review_manual_error",
                title="立即當沖討論失敗",
                detail=message,
                severity="error",
                strategy="daytrade",
                created_at=datetime.now(timezone.utc),
                trade_date=datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat(),
            )
        self.root.after(0, lambda: self._finish_daytrade_review_now(message))

    def _finish_daytrade_review_now(self, message: str) -> None:
        self.lab_status_var.set(message)
        self._refresh_lab_views()

    def _run_multi_agent_review_now_thread(self) -> None:
        try:
            message = self.lab_worker.run_multi_agent_review_now()
        except Exception as exc:
            message = f"多 Agent 策略檢討失敗：{exc.__class__.__name__}: {exc}"
            self.store.add_monitor_event(
                actor="system",
                phase="daily_review",
                event_type="multi_agent_review_manual_error",
                title="手動多 Agent 檢討失敗",
                detail=message,
                severity="error",
                created_at=datetime.now(timezone.utc),
                trade_date=datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat(),
            )
        self.root.after(0, lambda: self._finish_multi_agent_review_now(message))

    def _finish_multi_agent_review_now(self, message: str) -> None:
        self.lab_status_var.set(message)
        self._refresh_lab_views()

    def _run_swing_review_now_thread(self) -> None:
        try:
            message = self.lab_worker.run_swing_review_now()
        except Exception as exc:
            message = f"短線會後討論失敗：{exc.__class__.__name__}: {exc}"
            self.store.add_monitor_event(
                actor="system",
                phase="daily_review",
                event_type="swing_review_manual_error",
                title="立即短線討論失敗",
                detail=message,
                severity="error",
                strategy="swing",
                created_at=datetime.now(timezone.utc),
                trade_date=datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat(),
            )
        self.root.after(0, lambda: self._finish_swing_review_now(message))

    def _finish_swing_review_now(self, message: str) -> None:
        self.swing_self_correction_status_var.set(message)
        self.lab_status_var.set(message)
        self._refresh_lab_views()

    def open_api_settings(self) -> None:
        window = Toplevel(self.root)
        window.title("台新 Nova API 設定")
        window.transient(self.root)
        window.grab_set()
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")

        target_path = nova_settings_file(self.settings)
        fields = {
            "TAISHIN_NOVA_USER": StringVar(value=self.settings.nova_user),
            "TAISHIN_NOVA_PASSWORD": StringVar(value=self.settings.nova_password),
            "TAISHIN_NOVA_CERT_PATH": StringVar(value=self.settings.nova_cert_path),
            "TAISHIN_NOVA_CERT_PASSWORD": StringVar(value=self.settings.nova_cert_password),
            "TAISHIN_NOVA_QUOTE_WAIT_SECONDS": StringVar(value=str(self.settings.nova_quote_wait_seconds)),
        }
        labels = [
            ("身分證字號", "TAISHIN_NOVA_USER", False),
            ("登入密碼", "TAISHIN_NOVA_PASSWORD", True),
            ("憑證路徑", "TAISHIN_NOVA_CERT_PATH", False),
            ("憑證密碼", "TAISHIN_NOVA_CERT_PASSWORD", True),
            ("報價等待秒數", "TAISHIN_NOVA_QUOTE_WAIT_SECONDS", False),
        ]
        ttk.Label(frame, text=f"儲存位置：{target_path}", wraplength=560).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        for row, (label, key, masked) in enumerate(labels, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
            entry = ttk.Entry(frame, textvariable=fields[key], width=58, show="*" if masked else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if key == "TAISHIN_NOVA_CERT_PATH":
                ttk.Button(frame, text="瀏覽", command=lambda: self._browse_cert(fields["TAISHIN_NOVA_CERT_PATH"])).grid(row=row, column=2, padx=(8, 0), pady=4)

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(labels) + 1, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=window.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="儲存", command=lambda: self._save_api_settings(window, target_path, fields)).grid(row=0, column=1)

    def _browse_cert(self, cert_var: StringVar) -> None:
        path = filedialog.askopenfilename(
            title="選擇台新憑證檔",
            filetypes=[("憑證檔", "*.pfx *.p12 *.pem *.crt"), ("所有檔案", "*.*")],
        )
        if path:
            cert_var.set(path)

    def _save_api_settings(self, window: Toplevel, target_path: Path, fields: dict[str, StringVar]) -> None:
        values = {key: var.get().strip() for key, var in fields.items()}
        values["TW_WATCH_MARKET_DATA_MODE"] = "live"
        save_nova_settings(target_path, values)
        self.settings = load_settings()
        self.provider = create_provider(self.settings)
        self.lab_worker.settings = self.settings
        self.lab_worker.provider = self.provider
        self.auto_scout_enabled_var.set(self.settings.enable_auto_scout)
        self.auto_scout_status_var.set("自動選股已啟用" if self.settings.enable_auto_scout else "自動選股未啟用")
        self.swing_self_correction_enabled_var.set(self.settings.enable_swing_self_correction)
        self.swing_self_correction_status_var.set("短線自我修正已啟用" if self.settings.enable_swing_self_correction else "短線自我修正未啟用")
        self.monitor_mode_var.set(self.settings.market_data_mode)
        env_label = "設定檔：" + "、".join(str(path) for path in self.settings.loaded_env_files)
        self.status_var.set(env_label)
        window.destroy()
        messagebox.showinfo("已儲存", "台新 Nova API 設定已儲存，下一次開始監控會使用新設定。")

    def _watch_loop(self) -> None:
        while not self.stop_event.is_set():
            state = self._poll_once()
            self.events.put(state)
            self.stop_event.wait(self.settings.poll_seconds)

    def _poll_once(self) -> WatchState:
        symbol = self.symbol_var.get().strip().upper()
        try:
            capital = float(self.capital_var.get().replace(",", ""))
        except ValueError:
            capital = 0.0
        try:
            months = int(self.months_var.get())
        except ValueError:
            months = None
        strategy = _strategy_value(self.strategy_var.get())
        swing_params = None
        daytrade_params = None
        strategy_version = ""
        if strategy == "swing":
            active_version = self.store.get_active_strategy_version("swing")
            swing_params = swing_params_from_json(active_version.params)
            strategy_version = active_version.version
        elif strategy == "daytrade":
            active_version = self.store.get_active_strategy_version("daytrade")
            daytrade_params = daytrade_params_from_json(active_version.params)
            strategy_version = active_version.version
        try:
            quote = self.provider.get_quote(symbol)
            return build_watch_state(
                self.settings,
                symbol,
                capital,
                strategy,
                months,
                quote,
                swing_params=swing_params,
                daytrade_params=daytrade_params,
                strategy_version=strategy_version,
            )
        except Exception as exc:
            return build_watch_state(
                self.settings,
                symbol,
                capital,
                strategy,
                months,
                None,
                error=f"{exc.__class__.__name__}: {exc}",
                swing_params=swing_params,
                daytrade_params=daytrade_params,
                strategy_version=strategy_version,
            )

    def _pump_events(self) -> None:
        try:
            while True:
                self._render(self.events.get_nowait())
        except queue.Empty:
            pass
        now = time.monotonic()
        if now - self._last_lab_refresh >= 2:
            self._refresh_lab_views()
            self._last_lab_refresh = now
        self.root.after(250, self._pump_events)

    def _render(self, state: WatchState) -> None:
        self.status_var.set("資料可用" if state.status == "ok" else "資料不可用")
        self.quality_var.set("即時可用" if state.quality.status == "ok" else "；".join(state.quality.reasons))
        self.inst_var.set(state.institutional_note)
        self.action_var.set(state.advice.action)
        self.buy_var.set(_money(state.advice.buy_price))
        self.sell_var.set(_money(state.advice.sell_price))
        self.qty_var.set(f"{state.advice.qty:,}" if state.advice.qty else "-")
        self.stop_var.set(_money(state.advice.stop_loss))
        self.take_var.set(_money(state.advice.take_profit))
        self.reason_var.set(state.advice.reason)
        for tree in (self.bid_tree, self.ask_tree):
            for item in tree.get_children():
                tree.delete(item)
        if state.quote is None:
            self.quote_var.set("-")
            self.volume_var.set("-")
            self.time_var.set("-")
            return
        quote = state.quote
        self.quote_var.set(f"{_stock_label(quote.symbol, quote.name)}  {quote.price:,.2f} ({quote.change_pct:+.2%})")
        self.volume_var.set(f"{quote.volume:,.0f}")
        self.time_var.set(quote.exchange_time.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        for level in quote.bid_levels:
            self.bid_tree.insert("", "end", values=(f"{level.price:,.2f}", f"{level.size:,.0f}"))
        for level in quote.ask_levels:
            self.ask_tree.insert("", "end", values=(f"{level.price:,.2f}", f"{level.size:,.0f}"))

    def _apply_capital(self, account_id: str, value_var: StringVar) -> None:
        try:
            amount = float(value_var.get().replace(",", ""))
        except ValueError:
            messagebox.showwarning("金額錯誤", "請輸入可解析的加減碼金額，例如 100000 或 -50000。")
            return
        try:
            self.store.apply_capital_event(account_id, amount, "UI 核准加減碼")
        except ValueError as exc:
            messagebox.showwarning("資金錯誤", str(exc))
            return
        now = datetime.now(timezone.utc)
        self.store.add_monitor_event(
            actor="risk_manager",
            phase="capital",
            event_type="capital_approved",
            title="資金加減碼核准",
            detail=f"{_strategy_label(account_id)} 調整 {amount:,.0f}",
            strategy=account_id,
            severity="info",
            created_at=now,
            trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={"amount": amount},
        )
        self.lab_status_var.set(f"{_strategy_label(account_id)} 已調整 {amount:,.0f}")
        self._refresh_lab_views()

    def _refresh_lab_views(self) -> None:
        if not hasattr(self, "account_tree"):
            return
        self._refresh_monitor_view()
        today = datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
        self._replace_rows(
            self.account_tree,
            [
                (
                    _strategy_label(account.id),
                    f"{account.capital:,.0f}",
                    f"{account.cash:,.0f}",
                    f"{account.reserved_cash:,.0f}",
                    f"{account.realized_pnl:,.2f}",
                )
                for account in self.store.list_accounts()
            ],
        )
        self._replace_rows(
            self.candidate_tree,
            [
                (
                    row.trade_date,
                    _strategy_label(row.strategy),
                    _stock_label(row.symbol, row.name),
                    _candidate_status_label(row.status),
                    f"{row.score:.1f}",
                    row.source,
                    row.reason,
                )
                for row in self.store.list_candidates(today)[:200]
            ],
        )
        self._replace_rows(
            self.position_tree,
            [
                (
                    _strategy_label(row.account_id),
                    _strategy_label(row.strategy),
                    _stock_label(row.symbol, row.name),
                    f"{row.qty:,}",
                    f"{row.avg_cost:,.2f}",
                    _money(row.stop_loss),
                    _money(row.take_profit),
                )
                for row in self.store.list_positions()
            ],
        )
        self._replace_rows(
            self.order_tree,
            [
                (
                    _strategy_label(row["strategy"]),
                    _stock_label(row["symbol"], row["stock_name"]),
                    row["side"],
                    f"{float(row['price']):,.2f}",
                    f"{int(row['qty']):,}",
                    row["status"],
                    row["reason"],
                )
                for row in self.store.list_orders(limit=200)
            ],
        )
        self._replace_rows(
            self.fill_tree,
            [
                (
                    row["filled_at"],
                    _strategy_label(row["strategy"]),
                    _stock_label(row["symbol"], row["stock_name"]),
                    row["side"],
                    f"{float(row['price']):,.2f}",
                    f"{int(row['qty']):,}",
                    f"{float(row['realized_pnl']):,.2f}",
                )
                for row in self.store.list_fills(limit=200)
            ],
        )
        daily_reviews = self.store.list_daily_reviews()
        self.daily_review_rows_by_id = {str(row["id"]): row for row in daily_reviews}
        self._replace_items(
            self.review_tree,
            [
                (
                    str(row["id"]),
                    (
                        row["review_date"],
                        _format_db_time(row["created_at"], self.settings.timezone),
                        _strategy_label(row["strategy"]),
                        row["strategy_version"],
                        _proposal_status_label(row["proposal_status"]),
                        row["summary"],
                    ),
                )
                for row in daily_reviews
            ],
        )
        diagnostic_rows = self.store.list_quote_diagnostics(trade_date=today if self.monitor_today_only_var.get() else None, limit=300)
        self._replace_rows(
            self.quote_diagnostic_tree,
            [
                (
                    _format_db_time(row["created_at"], self.settings.timezone),
                    _strategy_label(row["strategy"]) if row["strategy"] else "",
                    _stock_label(row["symbol"], ""),
                    row["diagnosis"],
                    f"{row['bid_count']} / {row['ask_count']}",
                    _format_age_pair(row["exchange_age_seconds"], row["receive_age_seconds"]),
                    _format_json_flags(row["flags_json"]),
                )
                for row in diagnostic_rows
            ],
        )
        self._refresh_strategy_versions_view()

    def _refresh_monitor_view(self) -> None:
        status = self.lab_worker.status_snapshot()
        self.monitor_running_var.set("執行中" if status.running else "已停止")
        self.monitor_actor_var.set(_actor_label(status.actor))
        self.monitor_phase_var.set(_phase_label(status.phase))
        self.monitor_message_var.set(status.message or "-")
        self.monitor_heartbeat_var.set(_format_time(status.last_heartbeat, self.settings.timezone))
        self.monitor_last_event_var.set(status.last_event or "-")
        self.monitor_next_daytrade_var.set(_format_time(status.next_daytrade_tick, self.settings.timezone))
        self.monitor_next_swing_var.set(_format_time(status.next_swing_tick, self.settings.timezone))
        self.monitor_mode_var.set(self.settings.market_data_mode)
        self.monitor_db_var.set(str(self.store.path))

        today = datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
        summary = self.store.monitor_summary(today)
        self.monitor_candidates_var.set(f"{summary.candidates:,}")
        self.monitor_orders_var.set(f"{summary.open_orders:,}")
        self.monitor_fills_var.set(f"{summary.fills:,}")
        self.monitor_pnl_var.set(f"{summary.realized_pnl:,.2f}")
        self.monitor_positions_var.set(f"{summary.positions:,}")
        self.monitor_warnings_var.set(f"{summary.warnings:,}")
        self.monitor_errors_var.set(f"{summary.errors:,}")

        severity_filter = self.monitor_severity_filter_var.get()
        min_severity = "warning" if severity_filter == "警告以上" else "error" if severity_filter == "錯誤" else None
        actor = _actor_value(self.monitor_actor_filter_var.get())
        strategy_filter = self.monitor_strategy_filter_var.get()
        strategy = None if strategy_filter == "全部" else _strategy_value(strategy_filter)
        symbol = self.monitor_symbol_filter_var.get().strip().upper() or None
        stock_names = self.store.stock_name_map()
        rows = self.store.list_monitor_events(
            trade_date=today if self.monitor_today_only_var.get() else None,
            actor=actor,
            strategy=strategy,
            min_severity=min_severity,
            symbol=symbol,
            limit=300,
        )
        self._replace_rows(
            self.monitor_tree,
            [
                (
                    _format_time(datetime.fromisoformat(str(row["created_at"])), self.settings.timezone),
                    _severity_label(row["severity"]),
                    _actor_label(row["actor"]),
                    _phase_label(row["phase"]),
                    _strategy_label(row["strategy"]) if row["strategy"] else "",
                    _stock_label(row["symbol"], stock_names.get(str(row["symbol"]), "")),
                    row["title"],
                    row["detail"],
                )
                for row in rows
            ],
        )

    def _refresh_strategy_versions_view(self) -> None:
        if not hasattr(self, "strategy_version_tree"):
            return
        strategy = self._selected_strategy_version_strategy()
        state = self.store.get_strategy_version_state(strategy)
        active = self.store.get_strategy_version(strategy, state.active_version)
        self.strategy_active_version_var.set(active.version)
        self.strategy_mode_var.set(_strategy_mode_label(state.mode))
        strategy_reviews = [row for row in self.store.list_daily_reviews(limit=200) if str(row["strategy"]) == strategy]
        if strategy_reviews:
            latest = strategy_reviews[0]
            self.strategy_last_review_var.set(str(latest["review_date"]))
            self.strategy_last_result_var.set(_proposal_status_label(latest["proposal_status"]))
        else:
            self.strategy_last_review_var.set("-")
            self.strategy_last_result_var.set("-")

        selected = self.strategy_version_tree.selection()
        selected_version = selected[0] if selected else active.version
        for item in self.strategy_version_tree.get_children():
            self.strategy_version_tree.delete(item)
        versions = self.store.list_strategy_versions(strategy, limit=None if self.strategy_versions_show_all_var.get() else 50)
        for version in versions:
            status = "使用中" if version.version == state.active_version else _strategy_version_status_label(version.status)
            self.strategy_version_tree.insert(
                "",
                "end",
                iid=version.version,
                values=(
                    version.version,
                    status,
                    _format_time(version.created_at, self.settings.timezone),
                    _format_time(version.activated_at, self.settings.timezone),
                    version.parent_version or "-",
                    version.summary,
                ),
            )
        if selected_version in self.strategy_version_tree.get_children():
            self.strategy_version_tree.selection_set(selected_version)
        elif active.version in self.strategy_version_tree.get_children():
            self.strategy_version_tree.selection_set(active.version)
        self._refresh_strategy_version_detail()

    def _refresh_strategy_version_detail(self) -> None:
        if not hasattr(self, "strategy_version_detail_text"):
            return
        strategy = self._selected_strategy_version_strategy()
        latest_reviews = [row for row in self.store.list_daily_reviews(limit=200) if str(row["strategy"]) == strategy]
        latest_review_text = _format_latest_strategy_review(latest_reviews[0], self.settings.timezone) if latest_reviews else "最近會後討論\n-"
        selected = self.strategy_version_tree.selection()
        version_text = f"請選擇一個{_strategy_label(strategy)}策略版本。"
        if selected:
            version = self.store.get_strategy_version(strategy, selected[0])
            params = "\n".join(f"{key}: {value}" for key, value in version.params.items())
            metrics = "\n".join(f"{key}: {value}" for key, value in version.metrics.items()) or "-"
            param_diff = "-"
            if version.parent_version:
                try:
                    parent_version = self.store.get_strategy_version(strategy, version.parent_version)
                    param_diff = _format_param_diff(parent_version.params, version.params)
                except KeyError:
                    param_diff = "找不到父版本，無法比對。"
            version_text = (
                f"版本：{version.version}\n"
                f"父版本：{version.parent_version or '-'}\n"
                f"狀態：{version.status}\n"
                f"建立：{_format_time(version.created_at, self.settings.timezone)}\n"
                f"啟用：{_format_time(version.activated_at, self.settings.timezone)}\n"
                f"資料區間：{version.data_start or '-'} ~ {version.data_end or '-'}\n\n"
                f"參數\n{params}\n\n"
                f"與前版差異\n{param_diff}\n\n"
                f"規則文字\n{version.rules_text or '-'}\n\n"
                f"討論過程\n{version.discussion or '-'}\n\n"
                f"績效/產生資訊\n{metrics}"
            )
        text = f"{latest_review_text}\n\n選取版本\n{version_text}"
        self.strategy_version_detail_text.configure(state="normal")
        self.strategy_version_detail_text.delete("1.0", END)
        self.strategy_version_detail_text.insert("1.0", text)
        self.strategy_version_detail_text.configure(state="disabled")

    def apply_selected_strategy_version(self) -> None:
        selected = self.strategy_version_tree.selection()
        if not selected:
            messagebox.showwarning("未選版本", "請先選擇一個策略版本。")
            return
        strategy = self._selected_strategy_version_strategy()
        version = selected[0]
        selected_version = self.store.get_strategy_version(strategy, version)
        if selected_version.status == "pending":
            self.store.promote_strategy_version(strategy, version, activate=True)
        else:
            self.store.set_strategy_version_state(strategy, version, MANUAL_LOCK)
        now = datetime.now(timezone.utc)
        self.store.add_monitor_event(
            actor="system",
            phase="daily_review",
            event_type="strategy_version_manual_lock",
            title="手動套用策略版本",
            detail=f"已套用 {_strategy_label(strategy)} {version}，並進入手動鎖定。",
            strategy=strategy,
            created_at=now,
            trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={"version": version, "mode": MANUAL_LOCK},
        )
        self.lab_status_var.set(f"{_strategy_label(strategy)}策略已手動鎖定：{version}")
        self._refresh_lab_views()

    def follow_latest_strategy_version(self) -> None:
        strategy = self._selected_strategy_version_strategy()
        version = self.store.follow_latest_strategy_version(strategy)
        now = datetime.now(timezone.utc)
        self.store.add_monitor_event(
            actor="system",
            phase="daily_review",
            event_type="strategy_version_follow_latest",
            title="策略跟隨最新版",
            detail=f"{_strategy_label(strategy)}已切回跟隨最新版，目前使用 {version.version}。",
            strategy=strategy,
            created_at=now,
            trade_date=now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat(),
            metrics={"version": version.version, "mode": FOLLOW_LATEST},
        )
        self.lab_status_var.set(f"{_strategy_label(strategy)}策略跟隨最新版：{version.version}")
        self._refresh_lab_views()

    def _selected_strategy_version_strategy(self) -> str:
        strategy = _strategy_value(self.strategy_version_strategy_var.get())
        return strategy if strategy in {"scout", "daytrade", "swing"} else "swing"

    def open_daily_review_detail(self, event: object | None = None) -> None:
        item = ""
        if event is not None:
            y = getattr(event, "y", 0)
            item = self.review_tree.identify_row(y)
        if not item:
            selected = self.review_tree.selection()
            item = selected[0] if selected else ""
        row = self.daily_review_rows_by_id.get(str(item))
        if row is None:
            return
        strategy = str(row["strategy"])
        trade_date = str(row["review_date"])
        events = [
            event_row
            for event_row in self.store.list_monitor_events(trade_date=trade_date, strategy=strategy, min_severity=None, limit=300)
            if str(event_row["phase"]) == "daily_review" or "review" in str(event_row["event_type"])
        ]
        review_run_id = _review_run_id_from_metrics(row["metrics_json"])
        agent_reviews = self.store.list_agent_reviews(review_run_id) if review_run_id else []
        strategy_proposals = self.store.list_strategy_proposals(review_run_id) if review_run_id else []
        news_contexts = self.store.list_news_context_reviews(review_run_id) if review_run_id else []
        window = Toplevel(self.root)
        window.title(f"每日檢討 Log：{trade_date} {_strategy_label(strategy)}")
        window.geometry("900x650")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = Text(frame, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", _format_daily_review_detail(row, events, agent_reviews, strategy_proposals, news_contexts, self.settings.timezone))
        text.configure(state="disabled")
        ttk.Button(frame, text="關閉", command=window.destroy).grid(row=1, column=0, sticky="e", pady=(8, 0))
        window.focus_set()

    def _replace_rows(self, tree: ttk.Treeview, rows: list[tuple[object, ...]]) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for row in rows:
            tree.insert("", "end", values=row)

    def _replace_items(self, tree: ttk.Treeview, rows: list[tuple[str, tuple[object, ...]]]) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for item_id, values in rows:
            tree.insert("", "end", iid=item_id, values=values)


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


def _stock_label(symbol: object, name: object = "") -> str:
    clean_symbol = str(symbol or "").strip().upper()
    clean_name = str(name or "").strip()
    if clean_name and clean_name != clean_symbol:
        return f"{clean_symbol} {clean_name}"
    return clean_symbol


def _candidate_status_label(value: object) -> str:
    return {"active": "啟用", "inactive": "停用"}.get(str(value), str(value))


def _proposal_status_label(value: object) -> str:
    return {
        "none": "無",
        "reviewing": "討論中",
        "reviewed": "已檢討",
        "disabled": "未啟用",
        "insufficient_data": "資料不足",
        "llm_error": "LLM 失敗",
        "validation_failed": "驗證失敗",
        "no_change": "不改版",
        "review_only": "只記錄檢討",
        "risk_rejected": "風控拒絕",
        "pending_version_created": "已建立待套用版",
        "version_created_applied": "已建立並套用",
        "version_created_locked": "已建立未套用",
    }.get(str(value), str(value))


def _format_latest_strategy_review(row: object, timezone_name: str) -> str:
    review_date = str(row["review_date"] or "-")
    status = _proposal_status_label(row["proposal_status"])
    created_at = _format_db_time(row["created_at"], timezone_name)
    summary = str(row["summary"] or "-")
    llm_summary = str(row["llm_summary"] or "-")
    discussion = str(row["llm_discussion"] or "-")
    strategy_version = str(row["strategy_version"] or "-")
    return (
        "最近會後討論\n"
        f"日期：{review_date}\n"
        f"完成時間：{created_at}\n"
        f"結果：{status}\n"
        f"策略版本：{strategy_version}\n\n"
        f"摘要\n{summary}\n\n"
        f"LLM 摘要\n{llm_summary}\n\n"
        f"討論 / 失敗原因\n{discussion}"
    )


def _format_daily_review_detail(
    row: object,
    events: list[object],
    agent_reviews: list[object],
    strategy_proposals: list[object],
    news_contexts: list[object],
    timezone_name: str,
) -> str:
    event_lines = []
    for event in events:
        event_lines.append(
            "\n".join(
                [
                    f"- 時間：{_format_db_time(event['created_at'], timezone_name)}",
                    f"  等級：{_severity_label(event['severity'])}",
                    f"  角色：{_actor_label(event['actor'])}",
                    f"  階段：{_phase_label(event['phase'])}",
                    f"  事件：{event['title']}",
                    f"  說明：{event['detail'] or '-'}",
                ]
            )
        )
    event_text = "\n\n".join(event_lines) if event_lines else "-"
    agent_text = _format_agent_reviews(agent_reviews, timezone_name)
    proposal_text = _format_strategy_proposals(strategy_proposals)
    news_text = _format_news_contexts(news_contexts)
    return (
        f"每日檢討 Log\n"
        f"交易日：{row['review_date']}\n"
        f"檢討時間：{_format_db_time(row['created_at'], timezone_name)}\n"
        f"策略：{_strategy_label(row['strategy'])}\n"
        f"策略版本：{row['strategy_version'] or '-'}\n"
        f"提案狀態：{_proposal_status_label(row['proposal_status'])}\n\n"
        f"摘要\n{row['summary'] or '-'}\n\n"
        f"績效 / 指標\n{_format_json_text(row['metrics_json'])}\n\n"
        f"LLM 摘要\n{row['llm_summary'] or '-'}\n\n"
        f"檢討過程 / 失敗原因\n{row['llm_discussion'] or '-'}\n\n"
        f"LLM 原始結果\n{_format_json_text(row['llm_result_json'])}\n\n"
        f"Agent 檢討\n{agent_text}\n\n"
        f"策略提案 / 驗證\n{proposal_text}\n\n"
        f"新聞 / 公告背景（context-only）\n{news_text}\n\n"
        f"相關監控事件\n{event_text}"
    )


def _format_agent_reviews(rows: list[object], timezone_name: str) -> str:
    if not rows:
        return "-"
    lines = []
    for row in rows:
        output = _format_json_text(row["output_json"])
        lines.append(
            "\n".join(
                [
                    f"- {_format_db_time(row['created_at'], timezone_name)} {row['agent_name']}",
                    f"  狀態：{row['status']}；動作：{row['action'] or '-'}；信心：{row['confidence'] if row['confidence'] is not None else '-'}",
                    f"  evidence_quality：{row['evidence_quality'] or '-'}",
                    f"  output_hash：{row['output_hash'] or '-'}",
                    f"  摘要/JSON：{output}",
                ]
            )
        )
    return "\n\n".join(lines)


def _format_strategy_proposals(rows: list[object]) -> str:
    if not rows:
        return "-"
    lines = []
    for row in rows:
        lines.append(
            "\n".join(
                [
                    f"- {_strategy_label(row['strategy'])}：{_proposal_status_label(row['status'])}",
                    f"  摘要：{row['summary'] or '-'}",
                    f"  strategy_version_id：{row['strategy_version_id'] or '-'}",
                    f"  proposed_params：{_format_json_text(row['proposed_params_json'])}",
                    f"  validation：{_format_json_text(row['validation_json'])}",
                    f"  replay：{_format_json_text(row['replay_json'])}",
                    f"  risk_gate：{_format_json_text(row['risk_gate_json'])}",
                ]
            )
        )
    return "\n\n".join(lines)


def _format_news_contexts(rows: list[object]) -> str:
    if not rows:
        return "-"
    lines = []
    for row in rows:
        lines.append(
            "\n".join(
                [
                    f"- {row['symbol']}：{row['summary'] or '-'}",
                    f"  狀態：{row['status']}",
                    f"  sources：{_format_json_text(row['source_urls_json'])}",
                    f"  context：{_format_json_text(row['context_json'])}",
                ]
            )
        )
    return "\n\n".join(lines)


def _format_param_diff(parent: dict[str, object], current: dict[str, object]) -> str:
    lines = []
    for key in sorted(set(parent) | set(current)):
        old = parent.get(key)
        new = current.get(key)
        if old != new:
            lines.append(f"{key}: {old} -> {new}")
    return "\n".join(lines) if lines else "無參數差異"


def _format_json_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return text


def _review_run_id_from_metrics(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        review_run_id = int(payload.get("review_run_id", 0))
    except (AttributeError, TypeError, ValueError):
        return None
    return review_run_id or None


def _format_json_flags(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict):
        return text
    active = [key for key, flag in payload.items() if flag]
    return ", ".join(active) if active else "-"


def _format_age_pair(exchange_age: object, receive_age: object) -> str:
    def clean(value: object) -> str:
        if value is None or value == "":
            return "-"
        try:
            return f"{float(value):.0f}s"
        except (TypeError, ValueError):
            return str(value)

    return f"ex {clean(exchange_age)} / rx {clean(receive_age)}"


def _strategy_mode_label(value: object) -> str:
    return {
        FOLLOW_LATEST: "跟隨最新版",
        MANUAL_LOCK: "手動鎖定",
    }.get(str(value), str(value))


def _strategy_version_status_label(value: object) -> str:
    return {
        "validated": "可用",
        "pending": "待套用",
        "rejected": "已拒絕",
    }.get(str(value), str(value))


def _format_time(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")


def _format_db_time(value: object, timezone_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        return _format_time(datetime.fromisoformat(text), timezone_name)
    except ValueError:
        return text


def _write_error(exc: BaseException) -> None:
    base = app_base_dir()
    target = base / "data" / "desktop-error.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(redact_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))), encoding="utf-8")


def main() -> None:
    try:
        root = Tk()
        WatchDeskApp(root)
        root.mainloop()
    except BaseException as exc:
        _write_error(exc)
        messagebox.showerror("啟動失敗", str(exc))
        raise


if __name__ == "__main__":
    main()
