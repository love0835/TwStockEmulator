from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tw_watchdesk.quote_diagnostics import QuoteDiagnostic
from tw_watchdesk.redaction import redact_json, redact_text
from tw_watchdesk.strategy_versions import (
    DAYTRADE_STRATEGY,
    FOLLOW_LATEST,
    MANUAL_LOCK,
    SCOUT_STRATEGY,
    SWING_STRATEGY,
    default_params_for_strategy,
    default_rules_for_strategy,
)


DAYTRADE_ACCOUNT = "daytrade"
SWING_ACCOUNT = "swing"
BUY_ORDER_RESERVE_RATE = 0.001425


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    strategy: str
    capital: float
    cash: float
    reserved_cash: float
    realized_pnl: float
    updated_at: datetime


@dataclass(frozen=True)
class Candidate:
    id: int
    trade_date: str
    strategy: str
    symbol: str
    name: str
    score: float
    reason: str
    source: str
    scout_version: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class OrderRecord:
    id: int
    account_id: str
    strategy: str
    symbol: str
    side: str
    price: float
    qty: int
    status: str
    reason: str
    reserved_cash: float
    candidate_id: int | None
    entry_order_id: int | None
    stop_loss: float | None
    take_profit: float | None
    strategy_version: str
    scout_version: str
    candidate_score: float | None
    candidate_source: str
    candidate_reason: str
    attribution_status: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Position:
    account_id: str
    strategy: str
    symbol: str
    name: str
    qty: int
    avg_cost: float
    stop_loss: float | None
    take_profit: float | None
    realized_pnl: float
    strategy_version: str
    candidate_id: int | None
    entry_order_id: int | None
    scout_version: str
    candidate_score: float | None
    candidate_source: str
    candidate_reason: str
    attribution_status: str
    updated_at: datetime


@dataclass(frozen=True)
class MonitorSummary:
    candidates: int
    open_orders: int
    fills: int
    realized_pnl: float
    positions: int
    warnings: int
    errors: int


@dataclass(frozen=True)
class StrategyVersion:
    id: int
    strategy: str
    version: str
    parent_version: str
    status: str
    params: dict[str, Any]
    rules_text: str
    discussion: str
    summary: str
    data_start: str
    data_end: str
    metrics: dict[str, Any]
    created_at: datetime
    activated_at: datetime | None


@dataclass(frozen=True)
class StrategyVersionState:
    strategy: str
    active_version: str
    mode: str
    updated_at: datetime


def default_db_path(base_dir: Path) -> Path:
    return base_dir / "data" / "trading_lab.sqlite3"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TradingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    capital REAL NOT NULL,
                    cash REAL NOT NULL,
                    reserved_cash REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capital_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    event_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'approved',
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    score REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    scout_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    UNIQUE(trade_date, strategy, symbol)
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    price REAL NOT NULL,
                    previous_close REAL NOT NULL,
                    volume REAL NOT NULL,
                    turnover REAL NOT NULL,
                    bid_price REAL,
                    ask_price REAL,
                    is_realtime INTEGER NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe_minutes INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'nova',
                    UNIQUE(symbol, timeframe_minutes, start_time)
                );

                CREATE TABLE IF NOT EXISTS market_data_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_time TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    trade_time TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    bid REAL,
                    ask REAL,
                    volume REAL,
                    serial TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    event_key TEXT NOT NULL UNIQUE,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS order_books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange_time TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    bid_count INTEGER NOT NULL DEFAULT 0,
                    ask_count INTEGER NOT NULL DEFAULT 0,
                    bids_json TEXT NOT NULL DEFAULT '[]',
                    asks_json TEXT NOT NULL DEFAULT '[]',
                    event_key TEXT NOT NULL UNIQUE,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'limit',
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    reason TEXT NOT NULL DEFAULT '',
                    reserved_cash REAL NOT NULL DEFAULT 0,
                    candidate_id INTEGER REFERENCES candidates(id),
                    entry_order_id INTEGER REFERENCES orders(id),
                    stop_loss REAL,
                    take_profit REAL,
                    strategy_version TEXT NOT NULL DEFAULT '',
                    scout_version TEXT NOT NULL DEFAULT '',
                    candidate_score REAL,
                    candidate_source TEXT NOT NULL DEFAULT '',
                    candidate_reason TEXT NOT NULL DEFAULT '',
                    attribution_status TEXT NOT NULL DEFAULT 'legacy_unverified',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    filled_at TEXT,
                    raw_decision TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    gross_amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    tax REAL NOT NULL,
                    net_cash_delta REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    strategy_version TEXT NOT NULL DEFAULT '',
                    candidate_id INTEGER REFERENCES candidates(id),
                    entry_order_id INTEGER REFERENCES orders(id),
                    scout_version TEXT NOT NULL DEFAULT '',
                    attribution_status TEXT NOT NULL DEFAULT 'legacy_unverified',
                    filled_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    strategy_version TEXT NOT NULL DEFAULT '',
                    candidate_id INTEGER REFERENCES candidates(id),
                    entry_order_id INTEGER REFERENCES orders(id),
                    scout_version TEXT NOT NULL DEFAULT '',
                    attribution_status TEXT NOT NULL DEFAULT 'legacy_unverified',
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT REFERENCES accounts(id),
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quote_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT '',
                    price REAL,
                    previous_close REAL,
                    limit_up REAL,
                    limit_down REAL,
                    best_bid REAL,
                    best_ask REAL,
                    bid_count INTEGER NOT NULL DEFAULT 0,
                    ask_count INTEGER NOT NULL DEFAULT 0,
                    exchange_time TEXT,
                    received_at TEXT,
                    exchange_age_seconds REAL,
                    receive_age_seconds REAL,
                    flags_json TEXT NOT NULL DEFAULT '{}',
                    diagnosis TEXT NOT NULL,
                    payload_shape_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS daily_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    proposal_status TEXT NOT NULL DEFAULT 'none',
                    strategy_version TEXT NOT NULL DEFAULT '',
                    llm_summary TEXT NOT NULL DEFAULT '',
                    llm_discussion TEXT NOT NULL DEFAULT '',
                    llm_result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(review_date, strategy)
                );

                CREATE TABLE IF NOT EXISTS llm_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    decision_type TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    ref_table TEXT NOT NULL DEFAULT '',
                    ref_id INTEGER,
                    metrics_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS strategy_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    version TEXT NOT NULL,
                    parent_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'validated',
                    params_json TEXT NOT NULL DEFAULT '{}',
                    rules_text TEXT NOT NULL DEFAULT '',
                    discussion TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    data_start TEXT NOT NULL DEFAULT '',
                    data_end TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    UNIQUE(strategy, version)
                );

                CREATE TABLE IF NOT EXISTS strategy_version_state (
                    strategy TEXT PRIMARY KEY,
                    active_version TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'follow_latest',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_date TEXT NOT NULL,
                    db_path TEXT NOT NULL DEFAULT '',
                    runtime_mode TEXT NOT NULL DEFAULT '',
                    evidence_hash TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    redaction_report_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    review_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    backend TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    db_path TEXT NOT NULL DEFAULT '',
                    runtime_mode TEXT NOT NULL DEFAULT '',
                    evidence_id INTEGER REFERENCES review_evidence(id),
                    input_hash TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(run_key, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS review_run_leases (
                    run_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    agent_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    evidence_quality TEXT NOT NULL DEFAULT '',
                    confidence REAL,
                    input_hash TEXT NOT NULL DEFAULT '',
                    output_hash TEXT NOT NULL DEFAULT '',
                    prompt_hash TEXT NOT NULL DEFAULT '',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposed_params_json TEXT NOT NULL DEFAULT '{}',
                    rules_text TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    replay_json TEXT NOT NULL DEFAULT '{}',
                    risk_gate_json TEXT NOT NULL DEFAULT '{}',
                    strategy_version_id INTEGER REFERENCES strategy_versions(id),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS news_context_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_run_id INTEGER NOT NULL REFERENCES review_runs(id),
                    symbol TEXT NOT NULL,
                    query_hash TEXT NOT NULL DEFAULT '',
                    source_urls_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'ok',
                    retrieved_at TEXT NOT NULL
                );
                """
            )
            self._ensure_order_schema()
            self._ensure_strategy_version_columns()
            self._ensure_market_data_schema()
            self._hydrate_open_buy_reserves()
            self._dedupe_open_orders()
            self._reconcile_reserved_cash()
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_one_open_account_symbol
                ON orders(account_id, symbol)
                WHERE status = 'open'
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_market_data_events_symbol_time ON market_data_events(symbol, exchange_time)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_time ON market_ticks(symbol, trade_time)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_order_books_symbol_time ON order_books(symbol, exchange_time)")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_market_data_events_event_key ON market_data_events(event_key) WHERE event_key <> ''")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_market_ticks_event_key ON market_ticks(event_key) WHERE event_key <> ''")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_order_books_event_key ON order_books(event_key) WHERE event_key <> ''")
            self._seed_accounts()
            self._seed_strategy_versions()
            self._backfill_legacy_attribution()
            self._conn.commit()

    def _ensure_order_schema(self) -> None:
        _ensure_columns(
            self._conn,
            "candidates",
            (
                ("scout_version", "TEXT NOT NULL DEFAULT ''"),
            ),
        )
        _ensure_columns(
            self._conn,
            "orders",
            (
                ("reserved_cash", "REAL NOT NULL DEFAULT 0"),
                ("strategy_version", "TEXT NOT NULL DEFAULT ''"),
                ("entry_order_id", "INTEGER REFERENCES orders(id)"),
                ("scout_version", "TEXT NOT NULL DEFAULT ''"),
                ("candidate_score", "REAL"),
                ("candidate_source", "TEXT NOT NULL DEFAULT ''"),
                ("candidate_reason", "TEXT NOT NULL DEFAULT ''"),
                ("attribution_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
            ),
        )

    def _ensure_strategy_version_columns(self) -> None:
        tables = {
            "fills": (
                ("strategy_version", "TEXT NOT NULL DEFAULT ''"),
                ("candidate_id", "INTEGER REFERENCES candidates(id)"),
                ("entry_order_id", "INTEGER REFERENCES orders(id)"),
                ("scout_version", "TEXT NOT NULL DEFAULT ''"),
                ("attribution_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
            ),
            "positions": (
                ("strategy_version", "TEXT NOT NULL DEFAULT ''"),
                ("candidate_id", "INTEGER REFERENCES candidates(id)"),
                ("entry_order_id", "INTEGER REFERENCES orders(id)"),
                ("scout_version", "TEXT NOT NULL DEFAULT ''"),
                ("attribution_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
            ),
            "daily_reviews": (
                ("strategy_version", "TEXT NOT NULL DEFAULT ''"),
                ("llm_summary", "TEXT NOT NULL DEFAULT ''"),
                ("llm_discussion", "TEXT NOT NULL DEFAULT ''"),
                ("llm_result_json", "TEXT NOT NULL DEFAULT '{}'"),
            ),
        }
        for table, columns in tables.items():
            _ensure_columns(self._conn, table, columns)

    def _ensure_market_data_schema(self) -> None:
        _ensure_columns(
            self._conn,
            "market_data_events",
            (
                ("event_key", "TEXT NOT NULL DEFAULT ''"),
                ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
            ),
        )
        _ensure_columns(
            self._conn,
            "market_ticks",
            (
                ("bid", "REAL"),
                ("ask", "REAL"),
                ("volume", "REAL"),
                ("serial", "TEXT NOT NULL DEFAULT ''"),
                ("side", "TEXT NOT NULL DEFAULT ''"),
                ("event_key", "TEXT NOT NULL DEFAULT ''"),
                ("raw_json", "TEXT NOT NULL DEFAULT '{}'"),
            ),
        )
        _ensure_columns(
            self._conn,
            "order_books",
            (
                ("best_bid", "REAL"),
                ("best_ask", "REAL"),
                ("bid_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ask_count", "INTEGER NOT NULL DEFAULT 0"),
                ("bids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("asks_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("event_key", "TEXT NOT NULL DEFAULT ''"),
                ("raw_json", "TEXT NOT NULL DEFAULT '{}'"),
            ),
        )

    def _hydrate_open_buy_reserves(self) -> None:
        self._conn.execute(
            """
            UPDATE orders
            SET reserved_cash = ROUND(price * qty * ?, 2)
            WHERE status = 'open' AND side = 'buy' AND reserved_cash <= 0
            """,
            (1 + BUY_ORDER_RESERVE_RATE,),
        )

    def _dedupe_open_orders(self) -> None:
        duplicate_groups = self._conn.execute(
            """
            SELECT account_id, symbol
            FROM orders
            WHERE status = 'open'
            GROUP BY account_id, symbol
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for group in duplicate_groups:
            rows = self._conn.execute(
                """
                SELECT id
                FROM orders
                WHERE status = 'open' AND account_id = ? AND symbol = ?
                ORDER BY created_at, id
                """,
                (group["account_id"], group["symbol"]),
            ).fetchall()
            for row in rows[1:]:
                self._conn.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (row["id"],))

    def _reconcile_reserved_cash(self) -> None:
        self._conn.execute("UPDATE accounts SET reserved_cash = 0")
        rows = self._conn.execute(
            """
            SELECT account_id, COALESCE(SUM(reserved_cash), 0) AS value
            FROM orders
            WHERE status = 'open' AND side = 'buy'
            GROUP BY account_id
            """
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "UPDATE accounts SET reserved_cash = ? WHERE id = ?",
                (float(row["value"] or 0.0), row["account_id"]),
            )

    def _seed_accounts(self) -> None:
        now = _to_text(utc_now())
        rows = (
            (DAYTRADE_ACCOUNT, "當沖模擬交易員", "daytrade", 1_000_000.0, 1_000_000.0, now),
            (SWING_ACCOUNT, "短線模擬交易員", "swing", 1_000_000.0, 1_000_000.0, now),
        )
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO accounts
                (id, name, strategy, capital, cash, reserved_cash, realized_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, ?)
            """,
            rows,
        )

    def _seed_strategy_versions(self) -> None:
        now = _to_text(utc_now())
        labels = {
            SCOUT_STRATEGY: "抓盤",
            DAYTRADE_STRATEGY: "當沖",
            SWING_STRATEGY: "短線",
        }
        for strategy in (SCOUT_STRATEGY, DAYTRADE_STRATEGY, SWING_STRATEGY):
            version = f"{strategy}-v1"
            exists = self._conn.execute(
                "SELECT 1 FROM strategy_versions WHERE strategy = ? AND version = ?",
                (strategy, version),
            ).fetchone()
            if exists is None:
                label = labels[strategy]
                self._conn.execute(
                    """
                    INSERT INTO strategy_versions
                        (strategy, version, parent_version, status, params_json, rules_text,
                         discussion, summary, data_start, data_end, metrics_json, created_at, activated_at)
                    VALUES (?, ?, '', 'validated', ?, ?, ?, ?, '', '', ?, ?, ?)
                    """,
                    (
                        strategy,
                        version,
                        json.dumps(default_params_for_strategy(strategy), ensure_ascii=False),
                        default_rules_for_strategy(strategy),
                        f"系統初始化{label}策略版本。",
                        f"初始{label}策略版本，行為等同既有規則。",
                        json.dumps({"source": "initial"}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO strategy_version_state(strategy, active_version, mode, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (strategy, version, FOLLOW_LATEST, now),
            )

    def _backfill_legacy_attribution(self) -> None:
        self._conn.execute(
            """
            UPDATE candidates
            SET scout_version = ?
            WHERE source = 'auto_scout' AND scout_version = ''
            """,
            (f"{SCOUT_STRATEGY}-v1",),
        )
        self._backfill_buy_orders()
        self._backfill_sell_orders()
        self._backfill_fills()
        self._backfill_positions()

    def _backfill_buy_orders(self) -> None:
        rows = self._conn.execute(
            """
            SELECT o.*, c.score AS linked_candidate_score, c.source AS linked_candidate_source,
                   c.reason AS linked_candidate_reason, c.scout_version AS linked_scout_version
            FROM orders o
            LEFT JOIN candidates c ON c.id = o.candidate_id
            WHERE o.side = 'buy'
              AND (
                  o.attribution_status = 'legacy_unverified'
                  OR o.strategy_version = ''
                  OR o.scout_version = ''
                  OR o.entry_order_id IS NULL
              )
            ORDER BY o.created_at, o.id
            """
        ).fetchall()
        for row in rows:
            strategy_version = _row_text(row, "strategy_version") or _default_version(_row_text(row, "strategy"))
            candidate_source = _row_text(row, "candidate_source") or _row_text(row, "linked_candidate_source")
            scout_version = _row_text(row, "scout_version") or _row_text(row, "linked_scout_version")
            if candidate_source == "auto_scout" and not scout_version:
                scout_version = f"{SCOUT_STRATEGY}-v1"
            entry_order_id = _row_int(row, "entry_order_id") or int(row["id"])
            candidate_id = _row_int(row, "candidate_id")
            status = _order_attribution_status(
                side="buy",
                strategy_version=strategy_version,
                candidate_id=candidate_id,
                entry_order_id=entry_order_id,
                candidate_source=candidate_source,
                scout_version=scout_version,
            )
            self._conn.execute(
                """
                UPDATE orders
                SET strategy_version = ?,
                    entry_order_id = ?,
                    scout_version = ?,
                    candidate_score = COALESCE(candidate_score, ?),
                    candidate_source = COALESCE(NULLIF(candidate_source, ''), ?),
                    candidate_reason = COALESCE(NULLIF(candidate_reason, ''), ?),
                    attribution_status = ?
                WHERE id = ?
                """,
                (
                    strategy_version,
                    entry_order_id,
                    scout_version,
                    _optional_float(row["linked_candidate_score"]),
                    candidate_source,
                    _row_text(row, "linked_candidate_reason"),
                    status,
                    int(row["id"]),
                ),
            )

    def _backfill_sell_orders(self) -> None:
        rows = self._conn.execute(
            """
            SELECT *
            FROM orders
            WHERE side = 'sell'
              AND (
                  attribution_status = 'legacy_unverified'
                  OR strategy_version = ''
                  OR candidate_id IS NULL
                  OR entry_order_id IS NULL
                  OR scout_version = ''
              )
            ORDER BY created_at, id
            """
        ).fetchall()
        for row in rows:
            entry = self._find_backfill_entry_order(row)
            strategy_version = _row_text(row, "strategy_version") or _row_text(entry, "strategy_version") or _default_version(_row_text(row, "strategy"))
            candidate_id = _row_int(row, "candidate_id") or _row_int(entry, "candidate_id")
            entry_order_id = _row_int(row, "entry_order_id") or _row_int(entry, "entry_order_id") or (_row_int(entry, "id") if entry is not None else None)
            scout_version = _row_text(row, "scout_version") or _row_text(entry, "scout_version")
            candidate_source = _row_text(row, "candidate_source") or _row_text(entry, "candidate_source")
            candidate_reason = _row_text(row, "candidate_reason") or _row_text(entry, "candidate_reason")
            candidate_score = _optional_float(row["candidate_score"]) if row["candidate_score"] is not None else _optional_float(entry["candidate_score"] if entry is not None else None)
            if candidate_source == "auto_scout" and not scout_version:
                scout_version = f"{SCOUT_STRATEGY}-v1"
            status = _order_attribution_status(
                side="sell",
                strategy_version=strategy_version,
                candidate_id=candidate_id,
                entry_order_id=entry_order_id,
                candidate_source=candidate_source,
                scout_version=scout_version,
            )
            self._conn.execute(
                """
                UPDATE orders
                SET strategy_version = ?,
                    candidate_id = ?,
                    entry_order_id = ?,
                    scout_version = ?,
                    candidate_score = COALESCE(candidate_score, ?),
                    candidate_source = COALESCE(NULLIF(candidate_source, ''), ?),
                    candidate_reason = COALESCE(NULLIF(candidate_reason, ''), ?),
                    attribution_status = ?
                WHERE id = ?
                """,
                (
                    strategy_version,
                    candidate_id,
                    entry_order_id,
                    scout_version,
                    candidate_score,
                    candidate_source,
                    candidate_reason,
                    status,
                    int(row["id"]),
                ),
            )

    def _backfill_fills(self) -> None:
        rows = self._conn.execute(
            """
            SELECT f.*, o.strategy_version AS order_strategy_version, o.candidate_id AS order_candidate_id,
                   o.entry_order_id AS order_entry_order_id, o.scout_version AS order_scout_version,
                   o.candidate_source AS order_candidate_source
            FROM fills f
            LEFT JOIN orders o ON o.id = f.order_id
            WHERE f.attribution_status = 'legacy_unverified'
               OR f.strategy_version = ''
               OR f.candidate_id IS NULL
               OR f.entry_order_id IS NULL
               OR f.scout_version = ''
            ORDER BY f.filled_at, f.id
            """
        ).fetchall()
        for row in rows:
            strategy = _row_text(row, "strategy")
            side = _row_text(row, "side")
            strategy_version = _row_text(row, "strategy_version") or _row_text(row, "order_strategy_version") or _default_version(strategy)
            candidate_id = _row_int(row, "candidate_id") or _row_int(row, "order_candidate_id")
            entry_order_id = _row_int(row, "entry_order_id") or _row_int(row, "order_entry_order_id")
            if side == "buy" and entry_order_id is None:
                entry_order_id = _row_int(row, "order_id")
            scout_version = _row_text(row, "scout_version") or _row_text(row, "order_scout_version")
            candidate_source = _row_text(row, "order_candidate_source")
            if candidate_source == "auto_scout" and not scout_version:
                scout_version = f"{SCOUT_STRATEGY}-v1"
            status = _order_attribution_status(
                side=side,
                strategy_version=strategy_version,
                candidate_id=candidate_id,
                entry_order_id=entry_order_id,
                candidate_source=candidate_source,
                scout_version=scout_version,
            )
            self._conn.execute(
                """
                UPDATE fills
                SET strategy_version = ?,
                    candidate_id = ?,
                    entry_order_id = ?,
                    scout_version = ?,
                    attribution_status = ?
                WHERE id = ?
                """,
                (strategy_version, candidate_id, entry_order_id, scout_version, status, int(row["id"])),
            )

    def _backfill_positions(self) -> None:
        rows = self._conn.execute(
            """
            SELECT *
            FROM positions
            WHERE attribution_status = 'legacy_unverified'
               OR strategy_version = ''
               OR candidate_id IS NULL
               OR entry_order_id IS NULL
               OR scout_version = ''
            ORDER BY updated_at, symbol
            """
        ).fetchall()
        for row in rows:
            entry = self._find_backfill_entry_order(row)
            strategy_version = _row_text(row, "strategy_version") or _row_text(entry, "strategy_version") or _default_version(_row_text(row, "strategy"))
            candidate_id = _row_int(row, "candidate_id") or _row_int(entry, "candidate_id")
            entry_order_id = _row_int(row, "entry_order_id") or _row_int(entry, "entry_order_id") or (_row_int(entry, "id") if entry is not None else None)
            scout_version = _row_text(row, "scout_version") or _row_text(entry, "scout_version")
            status = _position_attribution_status(
                strategy_version=strategy_version,
                candidate_id=candidate_id,
                entry_order_id=entry_order_id,
            )
            self._conn.execute(
                """
                UPDATE positions
                SET strategy_version = ?,
                    candidate_id = ?,
                    entry_order_id = ?,
                    scout_version = ?,
                    attribution_status = ?
                WHERE account_id = ? AND symbol = ?
                """,
                (
                    strategy_version,
                    candidate_id,
                    entry_order_id,
                    scout_version,
                    status,
                    row["account_id"],
                    row["symbol"],
                ),
            )

    def _find_backfill_entry_order(self, row: sqlite3.Row) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT *
            FROM orders
            WHERE account_id = ?
              AND strategy = ?
              AND symbol = ?
              AND side = 'buy'
              AND status = 'filled'
              AND created_at <= ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                row["account_id"],
                row["strategy"],
                row["symbol"],
                _row_text(row, "created_at") or _row_text(row, "updated_at"),
            ),
        ).fetchone()

    def get_account(self, account_id: str) -> Account:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown account: {account_id}")
        return _account(row)

    def list_accounts(self) -> list[Account]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [_account(row) for row in rows]

    def apply_capital_event(self, account_id: str, amount: float, note: str = "") -> None:
        now = _to_text(utc_now())
        event_type = "deposit" if amount >= 0 else "withdraw"
        with self._lock:
            account = self._conn.execute("SELECT cash, capital, reserved_cash FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if account is None:
                raise KeyError(f"unknown account: {account_id}")
            new_cash = float(account["cash"]) + amount
            new_capital = float(account["capital"]) + amount
            if new_cash < 0 or new_capital < 0:
                raise ValueError("資金不可調整為負數")
            if new_cash < float(account["reserved_cash"]):
                raise ValueError("減碼後現金不可低於凍結金額")
            self._conn.execute(
                "UPDATE accounts SET cash = ?, capital = ?, updated_at = ? WHERE id = ?",
                (new_cash, new_capital, now, account_id),
            )
            self._conn.execute(
                """
                INSERT INTO capital_events(account_id, event_type, amount, note, status, created_at, approved_at)
                VALUES (?, ?, ?, ?, 'approved', ?, ?)
                """,
                (account_id, event_type, amount, note, now, now),
            )
            self._conn.commit()

    def add_monitor_event(
        self,
        *,
        actor: str,
        phase: str,
        event_type: str,
        title: str,
        detail: str = "",
        severity: str = "info",
        trade_date: str | None = None,
        strategy: str = "",
        symbol: str = "",
        ref_table: str = "",
        ref_id: int | None = None,
        metrics: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        trade_date = trade_date or created_at.date().isoformat()
        safe_detail = redact_text(detail)
        safe_metrics = redact_json(metrics or {})
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO monitor_events
                    (created_at, trade_date, actor, phase, strategy, symbol, severity,
                     event_type, title, detail, ref_table, ref_id, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _to_text(created_at),
                    trade_date,
                    actor,
                    phase,
                    strategy,
                    symbol.upper(),
                    severity,
                    event_type,
                    title,
                    safe_detail,
                    ref_table,
                    ref_id,
                    json.dumps(safe_metrics, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_monitor_events(
        self,
        *,
        trade_date: str | None = None,
        actor: str | None = None,
        strategy: str | None = None,
        min_severity: str | None = None,
        symbol: str | None = None,
        limit: int = 300,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if min_severity:
            minimum = _severity_rank(min_severity)
            severities = [name for name in ("info", "warning", "error") if _severity_rank(name) >= minimum]
            clauses.append(f"severity IN ({','.join('?' for _ in severities)})")
            params.extend(severities)
        sql = "SELECT * FROM monitor_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def upsert_candidate(
        self,
        *,
        trade_date: str,
        strategy: str,
        symbol: str,
        name: str = "",
        score: float = 0.0,
        reason: str = "",
        source: str = "manual",
        scout_version: str = "",
        status: str = "active",
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO candidates(trade_date, strategy, symbol, name, score, reason, source, scout_version, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, strategy, symbol) DO UPDATE SET
                    name = excluded.name,
                    score = excluded.score,
                    reason = excluded.reason,
                    source = excluded.source,
                    scout_version = excluded.scout_version,
                    status = excluded.status
                """,
                (
                    trade_date,
                    strategy,
                    symbol.upper(),
                    name,
                    score,
                    reason,
                    source,
                    scout_version,
                    status,
                    _to_text(created_at),
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM candidates WHERE trade_date = ? AND strategy = ? AND symbol = ?",
                (trade_date, strategy, symbol.upper()),
            ).fetchone()
            self._conn.commit()
        return int(row["id"])

    def list_candidates(self, trade_date: str | None = None, strategy: str | None = None) -> list[Candidate]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        sql = "SELECT * FROM candidates"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY trade_date DESC, strategy, score DESC, symbol"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_candidate(row) for row in rows]

    def update_candidate_status(self, candidate_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE candidates SET status = ? WHERE id = ?", (status, candidate_id))
            self._conn.commit()

    def expire_candidates_before(self, trade_date: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE candidates SET status = 'inactive' WHERE status = 'active' AND trade_date < ?",
                (trade_date,),
            )
            self._conn.commit()
            return int(cursor.rowcount)

    def stock_name_map(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol, name
                FROM candidates
                WHERE name <> ''
                ORDER BY trade_date DESC, created_at DESC, id DESC
                """
            ).fetchall()
        names: dict[str, str] = {}
        for row in rows:
            symbol = str(row["symbol"])
            if symbol not in names:
                names[symbol] = str(row["name"])
        return names

    def insert_snapshot(
        self,
        *,
        symbol: str,
        snapshot_time: datetime,
        price: float,
        previous_close: float,
        volume: float,
        turnover: float,
        bid_price: float | None,
        ask_price: float | None,
        is_realtime: bool,
        raw: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO market_snapshots
                    (symbol, snapshot_time, price, previous_close, volume, turnover, bid_price, ask_price, is_realtime, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    _to_text(snapshot_time),
                    price,
                    previous_close,
                    volume,
                    turnover,
                    bid_price,
                    ask_price,
                    1 if is_realtime else 0,
                    json.dumps(raw or {}, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def insert_quote_diagnostic(
        self,
        *,
        diagnostic: QuoteDiagnostic,
        strategy: str = "",
        trade_date: str | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        trade_date = trade_date or created_at.date().isoformat()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO quote_diagnostics(
                    created_at, trade_date, symbol, strategy, price, previous_close,
                    limit_up, limit_down, best_bid, best_ask, bid_count, ask_count,
                    exchange_time, received_at, exchange_age_seconds, receive_age_seconds,
                    flags_json, diagnosis, payload_shape_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _to_text(created_at),
                    trade_date,
                    diagnostic.symbol.upper(),
                    strategy,
                    diagnostic.price,
                    diagnostic.previous_close,
                    diagnostic.limit_up,
                    diagnostic.limit_down,
                    diagnostic.best_bid,
                    diagnostic.best_ask,
                    diagnostic.bid_count,
                    diagnostic.ask_count,
                    _to_text(diagnostic.exchange_time),
                    _to_text(diagnostic.received_at),
                    diagnostic.exchange_age_seconds,
                    diagnostic.receive_age_seconds,
                    json.dumps(redact_json(diagnostic.flags), ensure_ascii=False),
                    redact_text(diagnostic.diagnosis),
                    json.dumps(redact_json(diagnostic.payload_shape), ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_quote_diagnostics(
        self,
        *,
        trade_date: str | None = None,
        strategy: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        sql = "SELECT * FROM quote_diagnostics"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def insert_market_data_event(
        self,
        *,
        channel: str,
        symbol: str,
        exchange_time: datetime,
        received_at: datetime,
        payload: dict[str, Any],
        event_key: str = "",
    ) -> None:
        event_key = event_key or _market_event_key(channel, symbol, exchange_time, payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO market_data_events
                    (channel, symbol, exchange_time, received_at, event_key, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    channel.lower(),
                    symbol.upper(),
                    _to_text(exchange_time),
                    _to_text(received_at),
                    event_key,
                    json.dumps(redact_json(payload), ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()

    def insert_market_tick(
        self,
        *,
        symbol: str,
        trade_time: datetime,
        received_at: datetime,
        price: float,
        size: float,
        bid: float | None = None,
        ask: float | None = None,
        volume: float | None = None,
        serial: str = "",
        side: str = "",
        raw: dict[str, Any] | None = None,
        event_key: str = "",
    ) -> None:
        raw = raw or {}
        event_key = event_key or _market_event_key("trades", symbol, trade_time, {"price": price, "size": size, "serial": serial, **raw})
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO market_ticks
                    (symbol, trade_time, received_at, price, size, bid, ask, volume, serial, side, event_key, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    _to_text(trade_time),
                    _to_text(received_at),
                    price,
                    size,
                    bid,
                    ask,
                    volume,
                    serial,
                    side,
                    event_key,
                    json.dumps(redact_json(raw), ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()

    def insert_order_book(
        self,
        *,
        symbol: str,
        exchange_time: datetime,
        received_at: datetime,
        bids: list[dict[str, Any]],
        asks: list[dict[str, Any]],
        raw: dict[str, Any] | None = None,
        event_key: str = "",
    ) -> None:
        raw = raw or {}
        best_bid = _level_price(bids, 0)
        best_ask = _level_price(asks, 0)
        event_key = event_key or _market_event_key("books", symbol, exchange_time, {"bids": bids, "asks": asks, **raw})
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO order_books
                    (symbol, exchange_time, received_at, best_bid, best_ask, bid_count, ask_count,
                     bids_json, asks_json, event_key, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    _to_text(exchange_time),
                    _to_text(received_at),
                    best_bid,
                    best_ask,
                    len(bids),
                    len(asks),
                    json.dumps(redact_json(bids), ensure_ascii=False, sort_keys=True),
                    json.dumps(redact_json(asks), ensure_ascii=False, sort_keys=True),
                    event_key,
                    json.dumps(redact_json(raw), ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()

    def get_ticks_after(self, symbol: str, after: datetime, before: datetime | None = None) -> list[sqlite3.Row]:
        params: list[Any] = [symbol.upper(), _to_text(after)]
        sql = """
            SELECT * FROM market_ticks
            WHERE symbol = ? AND trade_time > ?
        """
        if before is not None:
            sql += " AND trade_time <= ?"
            params.append(_to_text(before))
        sql += " ORDER BY trade_time, id"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def latest_order_book(self, symbol: str, before: datetime | None = None) -> sqlite3.Row | None:
        params: list[Any] = [symbol.upper()]
        sql = "SELECT * FROM order_books WHERE symbol = ?"
        if before is not None:
            sql += " AND exchange_time <= ?"
            params.append(_to_text(before))
        sql += " ORDER BY exchange_time DESC, id DESC LIMIT 1"
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def count_market_data_events(self, channel: str | None = None, symbol: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel.lower())
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        sql = "SELECT COUNT(*) AS count FROM market_data_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["count"] or 0)

    def market_data_coverage(self, start_date: date, end_date: date) -> dict[str, Any]:
        start_text = start_date.isoformat()
        end_text = end_date.isoformat()
        with self._lock:
            channel_rows = self._conn.execute(
                """
                SELECT channel, COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols
                FROM market_data_events
                WHERE substr(exchange_time, 1, 10) BETWEEN ? AND ?
                GROUP BY channel
                ORDER BY channel
                """,
                (start_text, end_text),
            ).fetchall()
            tick_row = self._conn.execute(
                "SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols FROM market_ticks WHERE substr(trade_time, 1, 10) BETWEEN ? AND ?",
                (start_text, end_text),
            ).fetchone()
            book_row = self._conn.execute(
                "SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols FROM order_books WHERE substr(exchange_time, 1, 10) BETWEEN ? AND ?",
                (start_text, end_text),
            ).fetchone()
            snapshot_row = self._conn.execute(
                "SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols FROM market_snapshots WHERE substr(snapshot_time, 1, 10) BETWEEN ? AND ?",
                (start_text, end_text),
            ).fetchone()
            bar_row = self._conn.execute(
                "SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols FROM bars WHERE timeframe_minutes = 1 AND substr(start_time, 1, 10) BETWEEN ? AND ?",
                (start_text, end_text),
            ).fetchone()
        return {
            "data_start": start_text,
            "data_end": end_text,
            "events_by_channel": {
                str(row["channel"]): {"events": int(row["count"] or 0), "symbols": int(row["symbols"] or 0)}
                for row in channel_rows
            },
            "ticks": {"events": int(tick_row["count"] or 0), "symbols": int(tick_row["symbols"] or 0)},
            "books": {"events": int(book_row["count"] or 0), "symbols": int(book_row["symbols"] or 0)},
            "snapshots": {"events": int(snapshot_row["count"] or 0), "symbols": int(snapshot_row["symbols"] or 0)},
            "one_minute_bars": {"events": int(bar_row["count"] or 0), "symbols": int(bar_row["symbols"] or 0)},
        }

    def upsert_bar(
        self,
        *,
        symbol: str,
        timeframe_minutes: int,
        start_time: datetime,
        end_time: datetime,
        price: float,
        volume: float,
        source: str = "nova",
    ) -> None:
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT open, high, low, close, volume FROM bars
                WHERE symbol = ? AND timeframe_minutes = ? AND start_time = ?
                """,
                (symbol.upper(), timeframe_minutes, _to_text(start_time)),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO bars(symbol, timeframe_minutes, start_time, end_time, open, high, low, close, volume, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol.upper(),
                        timeframe_minutes,
                        _to_text(start_time),
                        _to_text(end_time),
                        price,
                        price,
                        price,
                        price,
                        volume,
                        source,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE bars SET high = ?, low = ?, close = ?, volume = ?, end_time = ?
                    WHERE symbol = ? AND timeframe_minutes = ? AND start_time = ?
                    """,
                    (
                        max(float(existing["high"]), price),
                        min(float(existing["low"]), price),
                        price,
                        max(float(existing["volume"]), volume),
                        _to_text(end_time),
                        symbol.upper(),
                        timeframe_minutes,
                        _to_text(start_time),
                    ),
                )
            self._conn.commit()

    def upsert_ohlc_bar(
        self,
        *,
        symbol: str,
        timeframe_minutes: int,
        start_time: datetime,
        end_time: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: float,
        source: str = "nova",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bars(symbol, timeframe_minutes, start_time, end_time, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe_minutes, start_time) DO UPDATE SET
                    high = MAX(bars.high, excluded.high),
                    low = MIN(bars.low, excluded.low),
                    close = excluded.close,
                    volume = MAX(bars.volume, excluded.volume),
                    end_time = excluded.end_time,
                    source = excluded.source
                """,
                (
                    symbol.upper(),
                    timeframe_minutes,
                    _to_text(start_time),
                    _to_text(end_time),
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    volume,
                    source,
                ),
            )
            self._conn.commit()

    def get_bars_after(self, symbol: str, timeframe_minutes: int, after: datetime, before: datetime | None = None) -> list[sqlite3.Row]:
        params: list[Any] = [symbol.upper(), timeframe_minutes, _to_text(after)]
        sql = """
            SELECT * FROM bars
            WHERE symbol = ? AND timeframe_minutes = ? AND start_time > ?
        """
        if before is not None:
            sql += " AND end_time <= ?"
            params.append(_to_text(before))
        sql += " ORDER BY start_time"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def get_snapshots_after(self, symbol: str, after: datetime, before: datetime | None = None) -> list[sqlite3.Row]:
        params: list[Any] = [symbol.upper(), _to_text(after)]
        sql = """
            SELECT * FROM market_snapshots
            WHERE symbol = ? AND snapshot_time > ?
        """
        if before is not None:
            sql += " AND snapshot_time <= ?"
            params.append(_to_text(before))
        sql += " ORDER BY snapshot_time"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def latest_snapshot(self, symbol: str, before: datetime | None = None) -> sqlite3.Row | None:
        params: list[Any] = [symbol.upper()]
        sql = "SELECT * FROM market_snapshots WHERE symbol = ?"
        if before is not None:
            sql += " AND snapshot_time <= ?"
            params.append(_to_text(before))
        sql += " ORDER BY snapshot_time DESC LIMIT 1"
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def create_order(
        self,
        *,
        account_id: str,
        strategy: str,
        symbol: str,
        side: str,
        price: float,
        qty: int,
        reason: str,
        expires_at: datetime,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        strategy_version: str = "",
        candidate_id: int | None = None,
        entry_order_id: int | None = None,
        scout_version: str = "",
        candidate_score: float | None = None,
        candidate_source: str = "",
        candidate_reason: str = "",
        attribution_status: str = "",
        raw_decision: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        symbol = symbol.upper()
        side = side.lower()
        if price <= 0:
            raise ValueError("委託價格必須大於 0")
        if qty <= 0:
            raise ValueError("委託股數必須大於 0")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                open_order = self._conn.execute(
                    "SELECT 1 FROM orders WHERE account_id = ? AND symbol = ? AND status = 'open'",
                    (account_id, symbol),
                ).fetchone()
                if open_order is not None:
                    raise ValueError("同帳戶同股票已有未完成委託")

                reserved_cash = 0.0
                if side == "buy":
                    position = self._conn.execute(
                        "SELECT 1 FROM positions WHERE account_id = ? AND symbol = ?",
                        (account_id, symbol),
                    ).fetchone()
                    if position is not None:
                        raise ValueError("同帳戶同股票已有持倉")
                    if not strategy_version:
                        strategy_version = _active_strategy_version(self._conn, strategy)
                    account = self._conn.execute(
                        "SELECT cash, reserved_cash FROM accounts WHERE id = ?",
                        (account_id,),
                    ).fetchone()
                    if account is None:
                        raise KeyError(f"unknown account: {account_id}")
                    reserved_cash = _buy_order_reserve(price, qty)
                    available_cash = float(account["cash"]) - float(account["reserved_cash"])
                    if reserved_cash > available_cash:
                        raise ValueError("可用現金不足")
                    self._conn.execute(
                        "UPDATE accounts SET reserved_cash = reserved_cash + ?, updated_at = ? WHERE id = ?",
                        (reserved_cash, _to_text(created_at), account_id),
                    )
                elif side == "sell":
                    position = self._conn.execute(
                        "SELECT * FROM positions WHERE account_id = ? AND symbol = ?",
                        (account_id, symbol),
                    ).fetchone()
                    if position is None:
                        raise ValueError("沒有持倉不可建立賣單")
                    if qty > int(position["qty"]):
                        raise ValueError("賣出股數超過持倉")
                    if not strategy_version:
                        strategy_version = _row_text(position, "strategy_version")
                    if candidate_id is None:
                        candidate_id = _row_int(position, "candidate_id")
                    if entry_order_id is None:
                        entry_order_id = _row_int(position, "entry_order_id")
                    if not scout_version:
                        scout_version = _row_text(position, "scout_version")
                else:
                    raise ValueError("委託買賣別只支援 buy/sell")

                if candidate_id is not None:
                    candidate = self._conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
                    if candidate is not None:
                        if not scout_version:
                            scout_version = _row_text(candidate, "scout_version")
                        if candidate_score is None:
                            candidate_score = float(candidate["score"])
                        if not candidate_source:
                            candidate_source = str(candidate["source"])
                        if not candidate_reason:
                            candidate_reason = str(candidate["reason"])
                attribution_status = attribution_status or _order_attribution_status(
                    side=side,
                    strategy_version=strategy_version,
                    candidate_id=candidate_id,
                    entry_order_id=entry_order_id,
                    candidate_source=candidate_source,
                    scout_version=scout_version,
                )
                cursor = self._conn.execute(
                    """
                    INSERT INTO orders
                        (account_id, strategy, symbol, side, price, qty, status, reason, reserved_cash, candidate_id,
                         entry_order_id, stop_loss, take_profit, strategy_version, scout_version,
                         candidate_score, candidate_source, candidate_reason, attribution_status,
                         created_at, expires_at, raw_decision)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        strategy,
                        symbol,
                        side,
                        price,
                        qty,
                        reason,
                        reserved_cash,
                        candidate_id,
                        entry_order_id,
                        stop_loss,
                        take_profit,
                        strategy_version,
                        scout_version,
                        candidate_score,
                        candidate_source,
                        candidate_reason,
                        attribution_status,
                        _to_text(created_at),
                        _to_text(expires_at),
                        json.dumps(raw_decision or {}, ensure_ascii=False),
                    ),
                )
                order_id = int(cursor.lastrowid)
                if side == "buy" and entry_order_id is None:
                    self._conn.execute("UPDATE orders SET entry_order_id = ? WHERE id = ?", (order_id, order_id))
                self._conn.commit()
                return order_id
            except Exception:
                self._conn.rollback()
                raise

    def list_orders(self, status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        sql = """
            SELECT o.*,
                   COALESCE(
                       NULLIF(c.name, ''),
                       (
                           SELECT c2.name
                           FROM candidates c2
                           WHERE c2.symbol = o.symbol AND c2.name <> ''
                           ORDER BY c2.trade_date DESC, c2.created_at DESC, c2.id DESC
                           LIMIT 1
                       ),
                       ''
                   ) AS stock_name
            FROM orders o
            LEFT JOIN candidates c ON c.id = o.candidate_id
        """
        params: list[Any] = []
        if status:
            sql += " WHERE o.status = ?"
            params.append(status)
        sql += " ORDER BY o.created_at DESC, o.id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def list_open_orders(self) -> list[OrderRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM orders WHERE status = 'open' ORDER BY created_at").fetchall()
        return [_order(row) for row in rows]

    def get_order(self, order_id: int) -> OrderRecord:
        with self._lock:
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown order: {order_id}")
        return _order(row)

    def expire_orders(self, now: datetime) -> int:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, account_id, reserved_cash
                FROM orders
                WHERE status = 'open' AND expires_at < ?
                """,
                (_to_text(now),),
            ).fetchall()
            if not rows:
                return 0
            release_by_account: dict[str, float] = {}
            for row in rows:
                release_by_account[str(row["account_id"])] = release_by_account.get(str(row["account_id"]), 0.0) + float(row["reserved_cash"] or 0.0)
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(f"UPDATE orders SET status = 'expired' WHERE id IN ({placeholders})", ids)
            for account_id, release in release_by_account.items():
                if release <= 0:
                    continue
                self._conn.execute(
                    "UPDATE accounts SET reserved_cash = MAX(0, reserved_cash - ?), updated_at = ? WHERE id = ?",
                    (release, _to_text(now), account_id),
                )
            self._conn.commit()
            return len(rows)

    def record_fill(
        self,
        *,
        order: OrderRecord,
        price: float,
        qty: int,
        fee: float,
        tax: float,
        net_cash_delta: float,
        realized_pnl: float,
        filled_at: datetime,
    ) -> None:
        gross = price * qty
        entry_order_id = order.entry_order_id or (order.id if order.side == "buy" else None)
        attribution_status = _fill_attribution_status(order, entry_order_id)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fills
                    (order_id, account_id, strategy, symbol, side, price, qty, gross_amount,
                     fee, tax, net_cash_delta, realized_pnl, strategy_version, candidate_id,
                     entry_order_id, scout_version, attribution_status, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    order.account_id,
                    order.strategy,
                    order.symbol,
                    order.side,
                    price,
                    qty,
                    gross,
                    fee,
                    tax,
                    net_cash_delta,
                    realized_pnl,
                    order.strategy_version,
                    order.candidate_id,
                    entry_order_id,
                    order.scout_version,
                    attribution_status,
                    _to_text(filled_at),
                ),
            )
            self._conn.execute(
                "UPDATE orders SET status = 'filled', filled_at = ? WHERE id = ?",
                (_to_text(filled_at), order.id),
            )
            self._conn.execute(
                """
                UPDATE accounts
                SET cash = cash + ?,
                    reserved_cash = MAX(0, reserved_cash - ?),
                    realized_pnl = realized_pnl + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    net_cash_delta,
                    order.reserved_cash if order.side == "buy" else 0.0,
                    realized_pnl,
                    _to_text(filled_at),
                    order.account_id,
                ),
            )
            self._conn.commit()

    def get_position(self, account_id: str, symbol: str) -> Position | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT p.*,
                       COALESCE(eo.candidate_score, pc.score) AS candidate_score,
                       COALESCE(NULLIF(eo.candidate_source, ''), NULLIF(pc.source, ''), '') AS candidate_source,
                       COALESCE(NULLIF(eo.candidate_reason, ''), NULLIF(pc.reason, ''), '') AS candidate_reason
                FROM positions p
                LEFT JOIN orders eo ON eo.id = p.entry_order_id
                LEFT JOIN candidates pc ON pc.id = p.candidate_id
                WHERE p.account_id = ? AND p.symbol = ?
                """,
                (account_id, symbol.upper()),
            ).fetchone()
        return _position(row) if row else None

    def upsert_position_after_fill(
        self,
        *,
        account_id: str,
        strategy: str,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        fee: float,
        realized_pnl: float,
        stop_loss: float | None,
        take_profit: float | None,
        strategy_version: str = "",
        candidate_id: int | None = None,
        entry_order_id: int | None = None,
        scout_version: str = "",
        attribution_status: str = "",
        at: datetime,
    ) -> None:
        symbol = symbol.upper()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM positions WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            ).fetchone()
            if side == "buy":
                if existing is None:
                    avg_cost = (price * qty + fee) / qty
                    resolved_attribution_status = attribution_status or _position_attribution_status(
                        strategy_version=strategy_version,
                        candidate_id=candidate_id,
                        entry_order_id=entry_order_id,
                    )
                    self._conn.execute(
                        """
                        INSERT INTO positions
                            (account_id, strategy, symbol, qty, avg_cost, stop_loss, take_profit,
                             realized_pnl, strategy_version, candidate_id, entry_order_id,
                             scout_version, attribution_status, opened_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            strategy,
                            symbol,
                            qty,
                            avg_cost,
                            stop_loss,
                            take_profit,
                            strategy_version,
                            candidate_id,
                            entry_order_id,
                            scout_version,
                            resolved_attribution_status,
                            _to_text(at),
                            _to_text(at),
                        ),
                    )
                else:
                    old_qty = int(existing["qty"])
                    new_qty = old_qty + qty
                    avg_cost = ((float(existing["avg_cost"]) * old_qty) + price * qty + fee) / new_qty
                    resolved_strategy_version = strategy_version or _row_text(existing, "strategy_version")
                    resolved_candidate_id = candidate_id if candidate_id is not None else _row_int(existing, "candidate_id")
                    resolved_entry_order_id = entry_order_id if entry_order_id is not None else _row_int(existing, "entry_order_id")
                    resolved_scout_version = scout_version or _row_text(existing, "scout_version")
                    resolved_attribution_status = attribution_status or _position_attribution_status(
                        strategy_version=resolved_strategy_version,
                        candidate_id=resolved_candidate_id,
                        entry_order_id=resolved_entry_order_id,
                    )
                    self._conn.execute(
                        """
                        UPDATE positions
                        SET qty = ?, avg_cost = ?, stop_loss = ?, take_profit = ?,
                            strategy_version = ?, candidate_id = ?, entry_order_id = ?,
                            scout_version = ?, attribution_status = ?, updated_at = ?
                        WHERE account_id = ? AND symbol = ?
                        """,
                        (
                            new_qty,
                            avg_cost,
                            stop_loss,
                            take_profit,
                            resolved_strategy_version,
                            resolved_candidate_id,
                            resolved_entry_order_id,
                            resolved_scout_version,
                            resolved_attribution_status,
                            _to_text(at),
                            account_id,
                            symbol,
                        ),
                    )
            else:
                if existing is None:
                    raise ValueError("cannot sell without a position")
                old_qty = int(existing["qty"])
                new_qty = old_qty - qty
                if new_qty < 0:
                    raise ValueError("sell quantity exceeds position")
                if new_qty == 0:
                    self._conn.execute(
                        "DELETE FROM positions WHERE account_id = ? AND symbol = ?",
                        (account_id, symbol),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE positions
                        SET qty = ?, realized_pnl = realized_pnl + ?, updated_at = ?
                        WHERE account_id = ? AND symbol = ?
                        """,
                        (new_qty, realized_pnl, _to_text(at), account_id, symbol),
                    )
            self._conn.commit()

    def list_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.*,
                       COALESCE(eo.candidate_score, pc.score) AS candidate_score,
                       COALESCE(NULLIF(eo.candidate_source, ''), NULLIF(pc.source, ''), '') AS candidate_source,
                       COALESCE(NULLIF(eo.candidate_reason, ''), NULLIF(pc.reason, ''), '') AS candidate_reason,
                       COALESCE(
                           NULLIF(pc.name, ''),
                           (
                               SELECT c2.name
                               FROM candidates c2
                               WHERE c2.symbol = p.symbol AND c2.name <> ''
                               ORDER BY c2.trade_date DESC, c2.created_at DESC, c2.id DESC
                               LIMIT 1
                           ),
                           ''
                       ) AS stock_name
                FROM positions p
                LEFT JOIN orders eo ON eo.id = p.entry_order_id
                LEFT JOIN candidates pc ON pc.id = p.candidate_id
                ORDER BY p.strategy, p.symbol
                """
            ).fetchall()
        return [_position(row) for row in rows]

    def list_fills(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT f.*,
                       COALESCE(o.candidate_score, c.score) AS candidate_score,
                       COALESCE(NULLIF(o.candidate_source, ''), NULLIF(c.source, ''), '') AS candidate_source,
                       COALESCE(NULLIF(o.candidate_reason, ''), NULLIF(c.reason, ''), '') AS candidate_reason,
                       COALESCE(
                           NULLIF(c.name, ''),
                           (
                               SELECT c2.name
                               FROM candidates c2
                               WHERE c2.symbol = f.symbol AND c2.name <> ''
                               ORDER BY c2.trade_date DESC, c2.created_at DESC, c2.id DESC
                               LIMIT 1
                           ),
                           ''
                       ) AS stock_name
                FROM fills f
                LEFT JOIN orders o ON o.id = f.order_id
                LEFT JOIN candidates c ON c.id = COALESCE(f.candidate_id, o.candidate_id)
                ORDER BY f.filled_at DESC, f.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def daily_realized_pnl(self, account_id: str, review_date: date) -> float:
        start = datetime(review_date.year, review_date.month, review_date.day, tzinfo=timezone.utc)
        end = start.replace(day=start.day)  # placeholder for type checkers
        end = datetime.fromtimestamp(start.timestamp() + 24 * 60 * 60, timezone.utc)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
                FROM fills
                WHERE account_id = ? AND filled_at >= ? AND filled_at < ?
                """,
                (account_id, _to_text(start), _to_text(end)),
            ).fetchone()
        return float(row["pnl"] or 0.0)

    def open_notional(self, account_id: str, prices: dict[str, float] | None = None) -> float:
        prices = prices or {}
        with self._lock:
            rows = self._conn.execute("SELECT symbol, qty, avg_cost FROM positions WHERE account_id = ?", (account_id,)).fetchall()
        return sum(int(row["qty"]) * float(prices.get(row["symbol"], row["avg_cost"])) for row in rows)

    def open_buy_notional(self, account_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(price * qty), 0) AS value
                FROM orders
                WHERE account_id = ? AND status = 'open' AND side = 'buy'
                """,
                (account_id,),
            ).fetchone()
        return float(row["value"] or 0.0)

    def portfolio_symbols_with_open_buys(self, account_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol FROM positions WHERE account_id = ?
                UNION
                SELECT symbol FROM orders WHERE account_id = ? AND status = 'open' AND side = 'buy'
                """,
                (account_id, account_id),
            ).fetchall()
        return {str(row["symbol"]) for row in rows}

    def has_open_order_or_position(self, account_id: str, symbol: str) -> bool:
        symbol = symbol.upper()
        with self._lock:
            order = self._conn.execute(
                "SELECT 1 FROM orders WHERE account_id = ? AND symbol = ? AND status = 'open'",
                (account_id, symbol),
            ).fetchone()
            position = self._conn.execute(
                "SELECT 1 FROM positions WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            ).fetchone()
        return order is not None or position is not None

    def get_strategy_version(self, strategy: str, version: str) -> StrategyVersion:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_versions WHERE strategy = ? AND version = ?",
                (strategy, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown strategy version: {strategy} {version}")
        return _strategy_version(row)

    def get_strategy_version_state(self, strategy: str = SWING_STRATEGY) -> StrategyVersionState:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_version_state WHERE strategy = ?",
                (strategy,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown strategy state: {strategy}")
        return _strategy_version_state(row)

    def get_active_strategy_version(self, strategy: str = SWING_STRATEGY) -> StrategyVersion:
        state = self.get_strategy_version_state(strategy)
        return self.get_strategy_version(strategy, state.active_version)

    def list_strategy_versions(self, strategy: str = SWING_STRATEGY, limit: int | None = 50) -> list[StrategyVersion]:
        sql = "SELECT * FROM strategy_versions WHERE strategy = ? ORDER BY created_at DESC, id DESC"
        params: list[Any] = [strategy]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_strategy_version(row) for row in rows]

    def create_strategy_version(
        self,
        *,
        strategy: str,
        params: dict[str, Any],
        rules_text: str,
        discussion: str,
        summary: str,
        data_start: str = "",
        data_end: str = "",
        metrics: dict[str, Any] | None = None,
        parent_version: str = "",
        status: str = "validated",
        auto_activate: bool = True,
        created_at: datetime | None = None,
    ) -> StrategyVersion:
        created_at = created_at or utc_now()
        now = _to_text(created_at)
        with self._lock:
            state = self._conn.execute(
                "SELECT * FROM strategy_version_state WHERE strategy = ?",
                (strategy,),
            ).fetchone()
            if state is None:
                raise KeyError(f"unknown strategy state: {strategy}")
            parent = parent_version or str(state["active_version"])
            version = self._next_strategy_version(strategy)
            activated_at = now if auto_activate and str(state["mode"]) == FOLLOW_LATEST else None
            cursor = self._conn.execute(
                """
                INSERT INTO strategy_versions
                    (strategy, version, parent_version, status, params_json, rules_text,
                     discussion, summary, data_start, data_end, metrics_json, created_at, activated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy,
                    version,
                    parent,
                    status,
                    json.dumps(params, ensure_ascii=False),
                    rules_text,
                    discussion,
                    summary,
                    data_start,
                    data_end,
                    json.dumps(metrics or {}, ensure_ascii=False),
                    now,
                    activated_at,
                ),
            )
            if activated_at is not None:
                self._conn.execute(
                    "UPDATE strategy_version_state SET active_version = ?, mode = ?, updated_at = ? WHERE strategy = ?",
                    (version, FOLLOW_LATEST, now, strategy),
                )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM strategy_versions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _strategy_version(row)

    def set_strategy_version_state(self, strategy: str, active_version: str, mode: str) -> None:
        if mode not in {FOLLOW_LATEST, "manual_lock"}:
            raise ValueError("unknown strategy version mode")
        now = _to_text(utc_now())
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM strategy_versions WHERE strategy = ? AND version = ?",
                (strategy, active_version),
            ).fetchone()
            if exists is None:
                raise KeyError(f"unknown strategy version: {strategy} {active_version}")
            self._conn.execute(
                """
                INSERT INTO strategy_version_state(strategy, active_version, mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy) DO UPDATE SET
                    active_version = excluded.active_version,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (strategy, active_version, mode, now),
            )
            self._conn.execute(
                "UPDATE strategy_versions SET activated_at = COALESCE(activated_at, ?) WHERE strategy = ? AND version = ?",
                (now, strategy, active_version),
            )
            self._conn.commit()

    def follow_latest_strategy_version(self, strategy: str = SWING_STRATEGY) -> StrategyVersion:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_versions WHERE strategy = ? AND status = 'validated' ORDER BY created_at DESC, id DESC LIMIT 1",
                (strategy,),
            ).fetchone()
        if row is None:
            raise KeyError(f"no validated strategy version: {strategy}")
        version = _strategy_version(row)
        self.set_strategy_version_state(strategy, version.version, FOLLOW_LATEST)
        return self.get_strategy_version(strategy, version.version)

    def promote_strategy_version(self, strategy: str, version: str, *, activate: bool = False) -> StrategyVersion:
        now = _to_text(utc_now())
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM strategy_versions WHERE strategy = ? AND version = ?",
                (strategy, version),
            ).fetchone()
            if exists is None:
                raise KeyError(f"unknown strategy version: {strategy} {version}")
            self._conn.execute(
                "UPDATE strategy_versions SET status = 'validated' WHERE strategy = ? AND version = ?",
                (strategy, version),
            )
            if activate:
                self._conn.execute(
                    """
                    INSERT INTO strategy_version_state(strategy, active_version, mode, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(strategy) DO UPDATE SET
                        active_version = excluded.active_version,
                        mode = excluded.mode,
                        updated_at = excluded.updated_at
                    """,
                    (strategy, version, MANUAL_LOCK, now),
                )
                self._conn.execute(
                    "UPDATE strategy_versions SET activated_at = COALESCE(activated_at, ?) WHERE strategy = ? AND version = ?",
                    (now, strategy, version),
                )
            self._conn.commit()
        return self.get_strategy_version(strategy, version)

    def insert_review_evidence(
        self,
        *,
        review_date: str,
        db_path: str,
        runtime_mode: str,
        evidence_hash: str,
        evidence: dict[str, Any],
        redaction_report: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO review_evidence(
                    review_date, db_path, runtime_mode, evidence_hash,
                    evidence_json, redaction_report_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_date,
                    redact_text(db_path),
                    runtime_mode,
                    evidence_hash,
                    json.dumps(redact_json(evidence), ensure_ascii=False),
                    json.dumps(redact_json(redaction_report or {}), ensure_ascii=False),
                    _to_text(created_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_review_evidence(self, evidence_id: int) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute("SELECT * FROM review_evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown review evidence: {evidence_id}")
        return row

    def create_review_run(
        self,
        *,
        run_key: str,
        review_date: str,
        backend: str,
        model: str,
        db_path: str,
        runtime_mode: str,
        evidence_id: int,
        input_hash: str,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS attempt_no FROM review_runs WHERE run_key = ?",
                (run_key,),
            ).fetchone()
            attempt_no = int(row["attempt_no"]) + 1
            cursor = self._conn.execute(
                """
                INSERT INTO review_runs(
                    run_key, attempt_no, review_date, status, backend, model, db_path,
                    runtime_mode, evidence_id, input_hash, created_at
                )
                VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    attempt_no,
                    review_date,
                    backend,
                    model,
                    redact_text(db_path),
                    runtime_mode,
                    evidence_id,
                    input_hash,
                    _to_text(created_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def acquire_review_run_lease(
        self,
        *,
        run_key: str,
        owner: str,
        ttl_seconds: int = 600,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM review_run_leases WHERE run_key = ?",
                (run_key,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_expires = _from_text(str(existing["expires_at"]))
                except ValueError:
                    existing_expires = now - timedelta(seconds=1)
                if existing_expires > now:
                    return False
            self._conn.execute(
                """
                INSERT INTO review_run_leases(run_key, owner, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                (run_key, owner, _to_text(expires_at), _to_text(now)),
            )
            self._conn.commit()
            return True

    def release_review_run_lease(self, *, run_key: str, owner: str | None = None) -> None:
        with self._lock:
            if owner is None:
                self._conn.execute("DELETE FROM review_run_leases WHERE run_key = ?", (run_key,))
            else:
                self._conn.execute("DELETE FROM review_run_leases WHERE run_key = ? AND owner = ?", (run_key, owner))
            self._conn.commit()

    def update_review_run(
        self,
        review_run_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        completed_at: datetime | None = None,
    ) -> None:
        completed_at = completed_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE review_runs
                SET status = ?, result_json = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(redact_json(result or {}), ensure_ascii=False),
                    redact_text(error),
                    _to_text(completed_at),
                    review_run_id,
                ),
            )
            self._conn.commit()

    def add_agent_review(
        self,
        *,
        review_run_id: int,
        agent_name: str,
        status: str,
        output: dict[str, Any] | None,
        action: str = "",
        evidence_quality: str = "",
        confidence: float | None = None,
        input_hash: str = "",
        output_hash: str = "",
        prompt_hash: str = "",
        error: str = "",
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO agent_reviews(
                    review_run_id, agent_name, status, action, evidence_quality, confidence,
                    input_hash, output_hash, prompt_hash, output_json, error, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_run_id,
                    agent_name,
                    status,
                    action,
                    evidence_quality,
                    confidence,
                    input_hash,
                    output_hash,
                    prompt_hash,
                    json.dumps(redact_json(output or {}), ensure_ascii=False),
                    redact_text(error),
                    _to_text(created_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def add_strategy_proposal(
        self,
        *,
        review_run_id: int,
        strategy: str,
        status: str,
        proposed_params: dict[str, Any] | None = None,
        rules_text: str = "",
        summary: str = "",
        validation: dict[str, Any] | None = None,
        replay: dict[str, Any] | None = None,
        risk_gate: dict[str, Any] | None = None,
        strategy_version_id: int | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO strategy_proposals(
                    review_run_id, strategy, status, proposed_params_json, rules_text,
                    summary, validation_json, replay_json, risk_gate_json,
                    strategy_version_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_run_id,
                    strategy,
                    status,
                    json.dumps(redact_json(proposed_params or {}), ensure_ascii=False),
                    redact_text(rules_text),
                    redact_text(summary),
                    json.dumps(redact_json(validation or {}), ensure_ascii=False),
                    json.dumps(redact_json(replay or {}), ensure_ascii=False),
                    json.dumps(redact_json(risk_gate or {}), ensure_ascii=False),
                    strategy_version_id,
                    _to_text(created_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def add_news_context_review(
        self,
        *,
        review_run_id: int,
        symbol: str,
        query_hash: str,
        source_urls: list[str],
        summary: str,
        context: dict[str, Any] | None = None,
        status: str = "ok",
        retrieved_at: datetime | None = None,
    ) -> int:
        retrieved_at = retrieved_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO news_context_reviews(
                    review_run_id, symbol, query_hash, source_urls_json,
                    summary, context_json, status, retrieved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_run_id,
                    symbol.upper(),
                    query_hash,
                    json.dumps(redact_json(source_urls), ensure_ascii=False),
                    redact_text(summary),
                    json.dumps(redact_json(context or {}), ensure_ascii=False),
                    status,
                    _to_text(retrieved_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_review_runs(self, *, review_date: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        sql = "SELECT * FROM review_runs"
        params: list[Any] = []
        if review_date:
            sql += " WHERE review_date = ?"
            params.append(review_date)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def list_agent_reviews(self, review_run_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM agent_reviews WHERE review_run_id = ? ORDER BY id",
                (review_run_id,),
            ).fetchall()

    def list_strategy_proposals(self, review_run_id: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM strategy_proposals"
        params: list[Any] = []
        if review_run_id is not None:
            sql += " WHERE review_run_id = ?"
            params.append(review_run_id)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def list_news_context_reviews(self, review_run_id: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM news_context_reviews"
        params: list[Any] = []
        if review_run_id is not None:
            sql += " WHERE review_run_id = ?"
            params.append(review_run_id)
        sql += " ORDER BY retrieved_at DESC, id DESC"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def add_llm_decision(
        self,
        *,
        strategy: str,
        decision_type: str,
        response: dict[str, Any] | None,
        status: str,
        error: str = "",
        symbol: str = "",
        prompt_hash: str = "",
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO llm_decisions(strategy, symbol, decision_type, prompt_hash, response_json, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy,
                    symbol.upper(),
                    decision_type,
                    prompt_hash,
                    json.dumps(redact_json(response or {}), ensure_ascii=False),
                    status,
                    redact_text(error),
                    _to_text(created_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def _next_strategy_version(self, strategy: str) -> str:
        rows = self._conn.execute(
            "SELECT version FROM strategy_versions WHERE strategy = ?",
            (strategy,),
        ).fetchall()
        prefix = f"{strategy}-v"
        maximum = 0
        for row in rows:
            value = str(row["version"])
            if not value.startswith(prefix):
                continue
            try:
                maximum = max(maximum, int(value.removeprefix(prefix)))
            except ValueError:
                continue
        return f"{strategy}-v{maximum + 1}"

    def add_risk_event(self, account_id: str | None, strategy: str, symbol: str, severity: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO risk_events(account_id, strategy, symbol, severity, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account_id, strategy, symbol.upper(), severity, redact_text(reason), _to_text(utc_now())),
            )
            self._conn.commit()

    def upsert_daily_review(
        self,
        review_date: str,
        strategy: str,
        summary: str,
        metrics: dict[str, Any],
        *,
        proposal_status: str = "none",
        strategy_version: str = "",
        llm_summary: str = "",
        llm_discussion: str = "",
        llm_result: dict[str, Any] | None = None,
    ) -> None:
        now = _to_text(utc_now())
        safe_metrics = redact_json(metrics)
        safe_summary = redact_text(summary)
        safe_llm_summary = redact_text(llm_summary)
        safe_llm_discussion = redact_text(llm_discussion)
        safe_llm_result = redact_json(llm_result or {})
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_reviews(
                    review_date, strategy, summary, metrics_json, proposal_status,
                    strategy_version, llm_summary, llm_discussion, llm_result_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_date, strategy) DO UPDATE SET
                    summary = excluded.summary,
                    metrics_json = excluded.metrics_json,
                    proposal_status = excluded.proposal_status,
                    strategy_version = excluded.strategy_version,
                    llm_summary = excluded.llm_summary,
                    llm_discussion = excluded.llm_discussion,
                    llm_result_json = excluded.llm_result_json,
                    created_at = excluded.created_at
                """,
                (
                    review_date,
                    strategy,
                    safe_summary,
                    json.dumps(safe_metrics, ensure_ascii=False),
                    proposal_status,
                    strategy_version,
                    safe_llm_summary,
                    safe_llm_discussion,
                    json.dumps(safe_llm_result, ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()

    def list_daily_reviews(self, limit: int = 60) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM daily_reviews ORDER BY review_date DESC, strategy LIMIT ?",
                (limit,),
            ).fetchall()

    def monitor_summary(self, trade_date: str) -> MonitorSummary:
        start_dt = datetime.fromisoformat(f"{trade_date}T00:00:00+00:00")
        start = _to_text(start_dt)
        end = _to_text(start_dt + timedelta(days=1))
        with self._lock:
            candidates = self._conn.execute(
                "SELECT COUNT(*) AS value FROM candidates WHERE trade_date = ? AND status = 'active'",
                (trade_date,),
            ).fetchone()
            open_orders = self._conn.execute(
                "SELECT COUNT(*) AS value FROM orders WHERE status = 'open'",
            ).fetchone()
            fills = self._conn.execute(
                "SELECT COUNT(*) AS value, COALESCE(SUM(realized_pnl), 0) AS pnl FROM fills WHERE filled_at >= ? AND filled_at < ?",
                (start, end),
            ).fetchone()
            positions = self._conn.execute("SELECT COUNT(*) AS value FROM positions").fetchone()
            warnings = self._conn.execute(
                "SELECT COUNT(*) AS value FROM risk_events WHERE severity = 'warning' AND created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchone()
            errors = self._conn.execute(
                "SELECT COUNT(*) AS value FROM risk_events WHERE severity = 'error' AND created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchone()
        return MonitorSummary(
            candidates=int(candidates["value"]),
            open_orders=int(open_orders["value"]),
            fills=int(fills["value"]),
            realized_pnl=float(fills["pnl"] or 0),
            positions=int(positions["value"]),
            warnings=int(warnings["value"]),
            errors=int(errors["value"]),
        )


def _to_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _account(row: sqlite3.Row) -> Account:
    return Account(
        id=str(row["id"]),
        name=str(row["name"]),
        strategy=str(row["strategy"]),
        capital=float(row["capital"]),
        cash=float(row["cash"]),
        reserved_cash=float(row["reserved_cash"]),
        realized_pnl=float(row["realized_pnl"]),
        updated_at=_from_text(str(row["updated_at"])),
    )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _market_event_key(channel: str, symbol: str, event_time: datetime, payload: dict[str, Any]) -> str:
    source = json.dumps(redact_json(payload), ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"{channel.lower()}:{symbol.upper()}:{_to_text(event_time)}:{digest}"


def _level_price(levels: list[dict[str, Any]], index: int) -> float | None:
    if index >= len(levels):
        return None
    value = levels[index].get("price")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _active_strategy_version(conn: sqlite3.Connection, strategy: str) -> str:
    row = conn.execute(
        "SELECT active_version FROM strategy_version_state WHERE strategy = ?",
        (strategy,),
    ).fetchone()
    if row is not None and str(row["active_version"] or ""):
        return str(row["active_version"])
    return f"{strategy}-v1"


def _order_attribution_status(
    *,
    side: str,
    strategy_version: str,
    candidate_id: int | None,
    entry_order_id: int | None,
    candidate_source: str,
    scout_version: str,
) -> str:
    missing: list[str] = []
    if not strategy_version:
        missing.append("strategy_version")
    if candidate_id is None:
        missing.append("candidate")
    if side == "sell" and entry_order_id is None:
        missing.append("entry_order")
    if candidate_source == "auto_scout" and not scout_version:
        missing.append("scout_version")
    return "complete" if not missing else "partial_missing_" + "_".join(missing)


def _fill_attribution_status(order: OrderRecord, entry_order_id: int | None) -> str:
    return _order_attribution_status(
        side=order.side,
        strategy_version=order.strategy_version,
        candidate_id=order.candidate_id,
        entry_order_id=entry_order_id,
        candidate_source=order.candidate_source,
        scout_version=order.scout_version,
    )


def _position_attribution_status(
    *,
    strategy_version: str,
    candidate_id: int | None,
    entry_order_id: int | None,
) -> str:
    missing: list[str] = []
    if not strategy_version:
        missing.append("strategy_version")
    if candidate_id is None:
        missing.append("candidate")
    if entry_order_id is None:
        missing.append("entry_order")
    return "complete" if not missing else "partial_missing_" + "_".join(missing)


def _candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        id=int(row["id"]),
        trade_date=str(row["trade_date"]),
        strategy=str(row["strategy"]),
        symbol=str(row["symbol"]),
        name=str(row["name"]),
        score=float(row["score"]),
        reason=str(row["reason"]),
        source=str(row["source"]),
        scout_version=_row_text(row, "scout_version"),
        status=str(row["status"]),
        created_at=_from_text(str(row["created_at"])),
    )


def _order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        id=int(row["id"]),
        account_id=str(row["account_id"]),
        strategy=str(row["strategy"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        price=float(row["price"]),
        qty=int(row["qty"]),
        status=str(row["status"]),
        reason=str(row["reason"]),
        reserved_cash=float(row["reserved_cash"]),
        candidate_id=_row_int(row, "candidate_id"),
        entry_order_id=_row_int(row, "entry_order_id"),
        stop_loss=_optional_float(row["stop_loss"]),
        take_profit=_optional_float(row["take_profit"]),
        strategy_version=_row_text(row, "strategy_version"),
        scout_version=_row_text(row, "scout_version"),
        candidate_score=_row_float(row, "candidate_score"),
        candidate_source=_row_text(row, "candidate_source"),
        candidate_reason=_row_text(row, "candidate_reason"),
        attribution_status=_row_text(row, "attribution_status"),
        created_at=_from_text(str(row["created_at"])),
        expires_at=_from_text(str(row["expires_at"])),
    )


def _position(row: sqlite3.Row) -> Position:
    return Position(
        account_id=str(row["account_id"]),
        strategy=str(row["strategy"]),
        symbol=str(row["symbol"]),
        name=_row_text(row, "stock_name"),
        qty=int(row["qty"]),
        avg_cost=float(row["avg_cost"]),
        stop_loss=_optional_float(row["stop_loss"]),
        take_profit=_optional_float(row["take_profit"]),
        realized_pnl=float(row["realized_pnl"]),
        strategy_version=_row_text(row, "strategy_version"),
        candidate_id=_row_int(row, "candidate_id"),
        entry_order_id=_row_int(row, "entry_order_id"),
        scout_version=_row_text(row, "scout_version"),
        candidate_score=_row_float(row, "candidate_score"),
        candidate_source=_row_text(row, "candidate_source"),
        candidate_reason=_row_text(row, "candidate_reason"),
        attribution_status=_row_text(row, "attribution_status"),
        updated_at=_from_text(str(row["updated_at"])),
    )


def _strategy_version(row: sqlite3.Row) -> StrategyVersion:
    return StrategyVersion(
        id=int(row["id"]),
        strategy=str(row["strategy"]),
        version=str(row["version"]),
        parent_version=str(row["parent_version"]),
        status=str(row["status"]),
        params=json.loads(str(row["params_json"] or "{}")),
        rules_text=str(row["rules_text"]),
        discussion=str(row["discussion"]),
        summary=str(row["summary"]),
        data_start=str(row["data_start"]),
        data_end=str(row["data_end"]),
        metrics=json.loads(str(row["metrics_json"] or "{}")),
        created_at=_from_text(str(row["created_at"])),
        activated_at=_from_text(str(row["activated_at"])) if row["activated_at"] else None,
    )


def _strategy_version_state(row: sqlite3.Row) -> StrategyVersionState:
    return StrategyVersionState(
        strategy=str(row["strategy"]),
        active_version=str(row["active_version"]),
        mode=str(row["mode"]),
        updated_at=_from_text(str(row["updated_at"])),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None or value == "" else float(value)


def _row_int(row: sqlite3.Row | None, key: str) -> int | None:
    if row is None or key not in row.keys() or row[key] is None:
        return None
    return int(row[key])


def _row_float(row: sqlite3.Row | None, key: str) -> float | None:
    if row is None or key not in row.keys() or row[key] is None:
        return None
    return float(row[key])


def _row_text(row: sqlite3.Row | None, key: str) -> str:
    return str(row[key]) if row is not None and key in row.keys() and row[key] is not None else ""


def _default_version(strategy: str) -> str:
    return f"{strategy}-v1" if strategy else ""


def _buy_order_reserve(price: float, qty: int) -> float:
    return round(price * qty * (1 + BUY_ORDER_RESERVE_RATE), 2)


def _severity_rank(value: str) -> int:
    return {"info": 0, "warning": 1, "error": 2}.get(value, 0)
