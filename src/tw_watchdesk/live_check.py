from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tw_watchdesk.config import app_base_dir, load_settings
from tw_watchdesk.llm import CodexExecAdapter
from tw_watchdesk.models import OrderBookLevel, Quote
from tw_watchdesk.nova import TaishinNovaProvider
from tw_watchdesk.redaction import redact_text
from tw_watchdesk.setup_env import SetupCredentials, redact_setup_text, verify_nova_login
from tw_watchdesk.storage import DAYTRADE_ACCOUNT, TradingStore
from tw_watchdesk.worker import TradingLabWorker


def run_live_check_cli(argv: list[str]) -> int:
    output = _option_value(argv, "--live-check")
    output_path = Path(output) if output else app_base_dir() / "live_check_result.json"
    result = run_live_check()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def run_live_check() -> dict[str, Any]:
    base = app_base_dir()
    settings = load_settings(base)
    settings.codex_timeout_seconds = max(settings.codex_timeout_seconds, 120)
    checks: list[dict[str, Any]] = []

    _check_llm_adapter(base, settings, checks)
    _check_nova(settings, checks)
    _check_daytrade_review(settings, checks)

    return {
        "ok": all(check["ok"] for check in checks),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(base),
        "checks": checks,
    }


def _check_llm_adapter(base: Path, settings: Any, checks: list[dict[str, Any]]) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ok": {"type": "boolean"},
            "answer": {"type": "string"},
        },
        "required": ["ok", "answer"],
    }
    result = CodexExecAdapter(cwd=base, model=settings.codex_model, timeout_seconds=settings.codex_timeout_seconds).run_json(
        'Return JSON only. Use Traditional Chinese (Taiwan) for answer. Set ok true and answer to "TwWatchDesk 繁體中文檢查通過".',
        schema,
    )
    checks.append(
        {
            "name": "llm_codex_exec",
            "ok": result.ok and result.data.get("ok") is True and _contains_cjk(str(result.data.get("answer", ""))),
            "detail": result.data if result.ok else redact_text(result.error),
        }
    )


def _check_nova(settings: Any, checks: list[dict[str, Any]]) -> None:
    credentials = SetupCredentials(
        national_id=settings.nova_user,
        account_password=settings.nova_password,
        cert_path=settings.nova_cert_path,
        cert_password=settings.nova_cert_password,
        quote_wait_seconds=str(settings.nova_quote_wait_seconds),
    )
    login = verify_nova_login(credentials, register_api_auth=False)
    checks.append({"name": "nova_login_realtime", "ok": login.status == "ok", "detail": login.detail or login.remediation})
    try:
        provider = TaishinNovaProvider(settings)
        payload = provider.get_stock_rest_client().snapshot.actives(market="TSE", trade="value", type="COMMONSTOCK")
        rows = _payload_rows(payload)
        checks.append({"name": "nova_rest_actives", "ok": len(rows) > 0, "detail": {"rows": len(rows)}})
    except Exception as exc:  # noqa: BLE001 - live diagnostic should capture provider-specific failures.
        checks.append({"name": "nova_rest_actives", "ok": False, "detail": redact_setup_text(str(exc), credentials)})


def _check_daytrade_review(settings: Any, checks: list[dict[str, Any]]) -> None:
    settings.enable_codex_llm = True
    with tempfile.TemporaryDirectory(prefix="tw-watchdesk-live-review-") as folder:
        store = TradingStore(Path(folder) / "trading_lab.sqlite3")
        try:
            store.initialize()
            at = datetime(2026, 7, 3, 1, 0, tzinfo=timezone.utc)
            _seed_daytrade_roundtrip(store, at)
            worker = TradingLabWorker(settings=settings, store=store, provider=_FakeProvider())
            message = worker.run_full_review_now("2026-07-03")
            rows = [dict(row) for row in store.list_daily_reviews(limit=20) if row["strategy"] == "daytrade"]
            decisions = store._conn.execute("SELECT status, error FROM llm_decisions ORDER BY id DESC LIMIT 1").fetchall()
            llm_summary = str(rows[0]["llm_summary"] if rows else "")
            llm_discussion = str(rows[0]["llm_discussion"] if rows else "")
            language_ok = _contains_cjk(llm_summary) and _contains_cjk(llm_discussion)
            ok = bool(rows and rows[0]["proposal_status"] == "reviewed" and decisions and decisions[0]["status"] == "ok" and language_ok)
            checks.append(
                {
                    "name": "daytrade_review_actual_path",
                    "ok": ok,
                    "detail": {
                        "message": message,
                        "proposal_status": rows[0]["proposal_status"] if rows else "",
                        "llm_status": decisions[0]["status"] if decisions else "",
                        "llm_error": redact_text(decisions[0]["error"]) if decisions else "",
                        "language_ok": language_ok,
                        "llm_summary_preview": llm_summary[:120],
                    },
                }
            )
        finally:
            store.close()


class _FakeProvider:
    def get_quote(self, symbol: str) -> Quote:
        now = datetime.now(timezone.utc)
        return Quote(
            symbol=symbol,
            name="台積電",
            price=102.0,
            previous_close=100.0,
            volume=1000,
            turnover=102000000,
            bid_levels=[OrderBookLevel(price=101.5, size=5)],
            ask_levels=[OrderBookLevel(price=102.0, size=5)],
            exchange_time=now,
            received_at=now,
            source="live-check",
            is_realtime=True,
            flags={},
        )


def _seed_daytrade_roundtrip(store: TradingStore, at: datetime) -> None:
    buy_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        price=100,
        qty=1000,
        reason="live check daytrade buy",
        expires_at=at + timedelta(minutes=5),
        created_at=at,
    )
    buy_order = store.get_order(buy_id)
    store.record_fill(order=buy_order, price=100, qty=1000, fee=143, tax=0, net_cash_delta=-100143, realized_pnl=0, filled_at=at + timedelta(seconds=1))
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="buy",
        qty=1000,
        price=100,
        fee=143,
        realized_pnl=0,
        stop_loss=98,
        take_profit=103,
        at=at + timedelta(seconds=1),
    )
    sell_id = store.create_order(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="sell",
        price=102,
        qty=1000,
        reason="live check daytrade sell",
        expires_at=at + timedelta(hours=3),
        created_at=at + timedelta(hours=3),
    )
    sell_order = store.get_order(sell_id)
    store.record_fill(order=sell_order, price=102, qty=1000, fee=145, tax=153, net_cash_delta=101702, realized_pnl=1702, filled_at=at + timedelta(hours=3, seconds=1))
    store.upsert_position_after_fill(
        account_id=DAYTRADE_ACCOUNT,
        strategy="daytrade",
        symbol="2330",
        side="sell",
        qty=1000,
        price=102,
        fee=145,
        realized_pnl=1702,
        stop_loss=None,
        take_profit=None,
        at=at + timedelta(hours=3, seconds=1),
    )


def _payload_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "list", "quotes", "actives", "stocks"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _option_value(argv: list[str], option: str) -> str:
    try:
        index = argv.index(option)
    except ValueError:
        return ""
    if index + 1 >= len(argv):
        return ""
    return argv[index + 1]


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
