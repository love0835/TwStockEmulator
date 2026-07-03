from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


CODEX_MODEL = "gpt-5.5"


class CommandRunner(Protocol):
    def __call__(self, command: list[str], *, input_text: str, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
        ...


@dataclass(frozen=True)
class CodexResult:
    ok: bool
    data: dict[str, Any]
    error: str = ""


class LlmJsonBackend(Protocol):
    backend_name: str
    model: str

    def run_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        use_user_config: bool = False,
        allow_web_search: bool = False,
    ) -> CodexResult:
        ...


class CodexExecAdapter:
    def __init__(
        self,
        *,
        cwd: Path,
        model: str = CODEX_MODEL,
        timeout_seconds: int = 60,
        runner: CommandRunner | None = None,
    ) -> None:
        self.backend_name = "codex_cli"
        self.cwd = cwd
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.runner = runner or _run_command

    def run_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        use_user_config: bool = False,
        allow_web_search: bool = False,
    ) -> CodexResult:
        with tempfile.TemporaryDirectory(prefix="tw-watchdesk-codex-") as folder:
            temp = Path(folder)
            schema_path = temp / "schema.json"
            output_path = temp / "last-message.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = [
                _codex_executable(),
                "exec",
                "--ephemeral",
                "--model",
                self.model,
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if not allow_web_search:
                command.extend(["--disable", "web_search"])
            if not use_user_config:
                command.insert(3, "--ignore-user-config")
            try:
                completed = self.runner(command, input_text=prompt, timeout=self.timeout_seconds, cwd=self.cwd)
            except subprocess.TimeoutExpired:
                return CodexResult(False, {}, "codex exec timeout")
            except OSError as exc:
                return CodexResult(False, {}, f"codex exec launch failed: {exc}")
            if completed.returncode != 0:
                return CodexResult(False, {}, (completed.stderr or completed.stdout or "codex exec failed").strip())
            text = output_path.read_text(encoding="utf-8") if output_path.exists() else completed.stdout
            try:
                parsed = json.loads(_extract_json(text))
            except json.JSONDecodeError as exc:
                return CodexResult(False, {}, f"invalid codex json: {exc}")
            if not isinstance(parsed, dict):
                return CodexResult(False, {}, "codex output is not a JSON object")
            return CodexResult(True, parsed)


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = CODEX_MODEL,
        timeout_seconds: int = 60,
        endpoint: str = "https://api.openai.com/v1/responses",
    ) -> None:
        self.backend_name = "openai_responses_api"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.endpoint = endpoint

    def run_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        use_user_config: bool = False,
        allow_web_search: bool = False,
    ) -> CodexResult:
        del use_user_config
        tools = [{"type": "web_search_preview"}] if allow_web_search else []
        body = {
            "model": self.model,
            "input": prompt,
            "tools": tools,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "tw_watchdesk_schema",
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            return CodexResult(False, {}, f"openai api http {exc.code}: {detail}")
        except (OSError, json.JSONDecodeError) as exc:
            return CodexResult(False, {}, f"openai api failed: {exc}")
        try:
            text = _extract_response_text(payload)
            parsed = json.loads(_extract_json(text))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return CodexResult(False, {}, f"invalid openai json: {exc}")
        if not isinstance(parsed, dict):
            return CodexResult(False, {}, "openai output is not a JSON object")
        return CodexResult(True, parsed)


class AnthropicMessagesAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        endpoint: str = "https://api.anthropic.com/v1/messages",
    ) -> None:
        self.backend_name = "anthropic_messages_api"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.endpoint = endpoint

    def run_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        use_user_config: bool = False,
        allow_web_search: bool = False,
    ) -> CodexResult:
        del use_user_config
        if allow_web_search:
            return CodexResult(False, {}, "anthropic web search backend is not enabled in this build")
        body = {
            "model": self.model,
            "max_tokens": 2000,
            "system": "Return JSON only. Follow the supplied JSON Schema exactly.",
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"prompt": prompt, "json_schema": schema},
                        ensure_ascii=False,
                    ),
                }
            ],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            return CodexResult(False, {}, f"anthropic api http {exc.code}: {detail}")
        except (OSError, json.JSONDecodeError) as exc:
            return CodexResult(False, {}, f"anthropic api failed: {exc}")
        try:
            text = "".join(str(part.get("text", "")) for part in payload.get("content", []) if isinstance(part, dict))
            parsed = json.loads(_extract_json(text))
        except (TypeError, json.JSONDecodeError) as exc:
            return CodexResult(False, {}, f"invalid anthropic json: {exc}")
        if not isinstance(parsed, dict):
            return CodexResult(False, {}, "anthropic output is not a JSON object")
        return CodexResult(True, parsed)


class FakeJsonBackend:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None, *, model: str = "fake") -> None:
        self.backend_name = "fake"
        self.model = model
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    def run_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        use_user_config: bool = False,
        allow_web_search: bool = False,
    ) -> CodexResult:
        del schema, use_user_config
        self.calls.append({"prompt": prompt, "allow_web_search": allow_web_search})
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError:
            payload = {}
        agent_name = str(payload.get("agent_name") or payload.get("task") or "default")
        return CodexResult(True, dict(self.responses.get(agent_name, self.responses.get("default", {}))))


def trading_decision_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["buy", "sell", "hold", "skip"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
            "risk_note": {"type": "string"},
        },
        "required": ["action", "confidence", "reason", "risk_note"],
    }


def daily_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "mistakes": {"type": "array", "items": {"type": "string"}},
            "next_rules_to_test": {"type": "array", "items": {"type": "string"}},
            "capital_suggestion": {"type": "string"},
        },
        "required": ["summary", "mistakes", "next_rules_to_test", "capital_suggestion"],
    }


def swing_strategy_review_schema() -> dict[str, Any]:
    parameter_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "stop_loss_pct": {"type": ["number", "null"]},
            "take_profit_pct_short": {"type": ["number", "null"]},
            "take_profit_pct_long": {"type": ["number", "null"]},
            "long_holding_months": {"type": ["integer", "null"]},
            "risk_pct": {"type": ["number", "null"]},
            "max_position_pct": {"type": ["number", "null"]},
            "min_turnover": {"type": ["number", "null"]},
            "max_spread_pct": {"type": ["number", "null"]},
        },
        "required": [
            "stop_loss_pct",
            "take_profit_pct_short",
            "take_profit_pct_long",
            "long_holding_months",
            "risk_pct",
            "max_position_pct",
            "min_turnover",
            "max_spread_pct",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "discussion": {"type": "string"},
            "should_create_version": {"type": "boolean"},
            "parameter_changes": parameter_schema,
            "rules_text": {"type": "string"},
            "expected_effect": {"type": "string"},
            "risk_note": {"type": "string"},
            "no_change_reason": {"type": "string"},
        },
        "required": [
            "summary",
            "discussion",
            "should_create_version",
            "parameter_changes",
            "rules_text",
            "expected_effect",
            "risk_note",
            "no_change_reason",
        ],
    }


def strategy_agent_review_schema(strategy: str) -> dict[str, Any]:
    allowed_params: dict[str, dict[str, Any]]
    if strategy == "scout":
        allowed_params = {
            "min_turnover": {"type": ["number", "null"]},
            "max_spread_pct": {"type": ["number", "null"]},
            "daytrade_change_min": {"type": ["number", "null"]},
            "daytrade_change_max": {"type": ["number", "null"]},
            "swing_change_min": {"type": ["number", "null"]},
            "swing_change_max": {"type": ["number", "null"]},
            "liquidity_weight": {"type": ["number", "null"]},
            "momentum_weight": {"type": ["number", "null"]},
            "spread_weight": {"type": ["number", "null"]},
            "limit_up_policy": {"type": ["string", "null"]},
            "limit_down_policy": {"type": ["string", "null"]},
            "missing_depth_policy": {"type": ["string", "null"]},
            "max_candidates_daytrade": {"type": ["integer", "null"]},
            "max_candidates_swing": {"type": ["integer", "null"]},
            "eligible_list_policy": {"type": ["string", "null"]},
        }
    elif strategy == "daytrade":
        allowed_params = {
            "stop_loss_pct": {"type": ["number", "null"]},
            "take_profit_pct": {"type": ["number", "null"]},
            "risk_pct": {"type": ["number", "null"]},
            "max_position_pct": {"type": ["number", "null"]},
            "max_daily_loss_pct": {"type": ["number", "null"]},
            "entry_start_time": {"type": ["string", "null"]},
            "entry_end_time": {"type": ["string", "null"]},
            "force_exit_time": {"type": ["string", "null"]},
            "order_ttl_minutes": {"type": ["integer", "null"]},
            "max_spread_pct": {"type": ["number", "null"]},
            "max_quote_age_seconds": {"type": ["integer", "null"]},
            "missing_depth_policy": {"type": ["string", "null"]},
            "reentry_cooldown_minutes": {"type": ["integer", "null"]},
            "consecutive_loss_stop": {"type": ["integer", "null"]},
            "allow_limit_up_entry": {"type": ["boolean", "null"]},
            "allow_limit_down_entry": {"type": ["boolean", "null"]},
        }
    elif strategy == "swing":
        allowed_params = {
            "stop_loss_pct": {"type": ["number", "null"]},
            "take_profit_pct_short": {"type": ["number", "null"]},
            "take_profit_pct_long": {"type": ["number", "null"]},
            "long_holding_months": {"type": ["integer", "null"]},
            "risk_pct": {"type": ["number", "null"]},
            "max_position_pct": {"type": ["number", "null"]},
            "min_turnover": {"type": ["number", "null"]},
            "max_spread_pct": {"type": ["number", "null"]},
            "max_total_exposure_pct": {"type": ["number", "null"]},
            "max_position_symbols": {"type": ["integer", "null"]},
        }
    else:
        raise KeyError(f"unknown strategy: {strategy}")
    return _agent_schema(allowed_params)


def risk_agent_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "verdict": {"type": "string", "enum": ["pass", "warn", "reject"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rejections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "strategy": {"type": "string", "enum": ["scout", "daytrade", "swing", "all"]},
                        "source": {"type": "string", "enum": ["risk_agent", "precheck", "validator"]},
                        "source_ref": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["strategy", "source", "source_ref", "reason"],
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "verdict", "confidence", "rejections", "warnings"],
    }


def coach_agent_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "strategy": {"type": "string", "enum": ["scout", "daytrade", "swing"]},
                        "action": {"type": "string", "enum": ["propose_change", "record_review_only", "reject", "insufficient_evidence"]},
                        "summary": {"type": "string"},
                        "supporting_agent": {"type": "string"},
                    },
                    "required": ["strategy", "action", "summary", "supporting_agent"],
                },
            },
            "rejected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "strategy": {"type": "string", "enum": ["scout", "daytrade", "swing", "all"]},
                        "source": {"type": "string", "enum": ["risk_agent", "precheck", "validator"]},
                        "source_ref": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["strategy", "source", "source_ref", "reason"],
                },
            },
        },
        "required": ["summary", "confidence", "proposals", "rejected"],
    }


def news_context_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "contexts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "symbol": {"type": "string"},
                        "summary": {"type": "string"},
                        "source_urls": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["symbol", "summary", "source_urls"],
                },
            }
        },
        "required": ["contexts"],
    }


def _agent_schema(parameter_changes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "action": {"type": "string", "enum": ["propose_change", "record_review_only", "reject", "insufficient_evidence"]},
            "evidence_quality": {"type": "string", "enum": ["none", "weak", "limited", "sufficient", "strong"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "parameter_changes": {
                "type": "object",
                "additionalProperties": False,
                "properties": parameter_changes,
                "required": list(parameter_changes),
            },
            "rules_text": {"type": "string"},
            "expected_effect": {"type": "string"},
            "risk_note": {"type": "string"},
            "reject_reasons": {"type": "array", "items": {"type": "string"}},
            "supporting_event_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "summary",
            "action",
            "evidence_quality",
            "confidence",
            "parameter_changes",
            "rules_text",
            "expected_effect",
            "risk_note",
            "reject_reasons",
            "supporting_event_ids",
        ],
    }


def _run_command(command: list[str], *, input_text: str, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
        **_windows_subprocess_options(),
    )


def _codex_executable() -> str:
    if os.name == "nt":
        return shutil.which("codex.cmd") or shutil.which("codex.exe") or "codex.cmd"
    return shutil.which("codex") or "codex"


def _windows_subprocess_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    options: dict[str, Any] = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        options["creationflags"] = creationflags
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    hide_window = getattr(subprocess, "SW_HIDE", 0)
    if startupinfo_cls is not None and use_show_window:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= use_show_window
        startupinfo.wShowWindow = hide_window
        options["startupinfo"] = startupinfo
    return options


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return stripped
    return stripped[start : end + 1]


def _extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return str(payload["output_text"])
    chunks: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(str(content["text"]))
    return "".join(chunks)
