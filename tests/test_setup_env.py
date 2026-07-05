from __future__ import annotations

import subprocess
import sys
import time
import types
from io import BytesIO

import pytest

import tw_watchdesk.setup_env as setup_env
from tw_watchdesk.setup_env import (
    SetupCredentials,
    build_fugle_mcp_env,
    call_with_timeout,
    copy_certificate_to_project_store,
    read_mcp_message,
    redact_setup_text,
    replace_fugle_mcp_block,
    send_mcp_message,
    validate_setup_inputs,
    verify_nova_login,
    verify_llm_environment,
)


def credentials(cert_path: str = r"C:\certs\nova.pfx") -> SetupCredentials:
    return SetupCredentials(
        national_id="A123456789",
        account_password="shortpw",
        cert_path=cert_path,
        cert_password="certpw",
        quote_wait_seconds="8",
    )


def test_validate_setup_inputs_accepts_complete_values(tmp_path) -> None:
    cert = tmp_path / "nova.pfx"
    cert.write_bytes(b"fake")

    results = validate_setup_inputs(credentials(str(cert)))

    assert all(result.ok for result in results)


def test_validate_setup_inputs_blocks_missing_required_values() -> None:
    results = validate_setup_inputs(SetupCredentials("", "", "", "", "0"))

    assert [result.status for result in results].count("error") == 5


def test_copy_certificate_to_project_store_copies_into_data_certs(tmp_path) -> None:
    source = tmp_path / "source.pfx"
    source.write_bytes(b"secret cert")
    base = tmp_path / "app"

    result, target = copy_certificate_to_project_store(source, base)

    assert result.status == "ok"
    assert target == base / "data" / "certs" / "source.pfx"
    assert target.read_bytes() == b"secret cert"


def test_replace_fugle_mcp_block_replaces_only_fugle_sections() -> None:
    original = """
model = "gpt-5.5"

[mcp_servers.claude_bridge]
url = "http://127.0.0.1:8000/mcp"

[mcp_servers.fugle]
enabled = false
command = "old"

[mcp_servers.fugle.env]
NATIONAL_ID = "OLD"
ACCOUNT_PASS = "OLD"

[desktop]
keepRemoteControlAwakeWhilePluggedIn = true
""".strip()

    updated = replace_fugle_mcp_block(original, credentials(), r"C:\node\fugle-mcp-server.cmd")

    assert '[mcp_servers.claude_bridge]' in updated
    assert '[desktop]' in updated
    assert 'command = "old"' not in updated
    assert 'NATIONAL_ID = "OLD"' not in updated
    assert '[mcp_servers.fugle]' in updated
    assert '[mcp_servers.fugle.env]' in updated
    assert 'command = "C:\\\\node\\\\fugle-mcp-server.cmd"' in updated
    assert 'ENABLE_ORDER = "false"' in updated
    assert 'NATIONAL_ID = "A123456789"' in updated


def test_replace_fugle_mcp_block_removes_old_fugle_comments() -> None:
    original = """
[mcp_servers.deepseek]
command = "npx"

# Fugle MCP installed globally with npm.
# Keep disabled until real credentials are configured.
[mcp_servers.fugle]
enabled = false

[mcp_servers.fugle.env]
NATIONAL_ID = "OLD"
""".strip()

    updated = replace_fugle_mcp_block(original, credentials(), "fugle-mcp-server.cmd")

    assert "Keep disabled" not in updated
    assert "Managed by TwWatchDeskSetup" in updated


def test_build_fugle_mcp_env_defaults_orders_disabled() -> None:
    env = build_fugle_mcp_env(credentials(), enable_order=False)

    assert env["SDK_TYPE"] == "taishin"
    assert env["ENABLE_ORDER"] == "false"
    assert env["NATIONAL_ID"] == "A123456789"


def test_verify_nova_login_supports_snake_case_taishin_sdk(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeTaishinSDK:
        def login(self, user: str, password: str, cert_path: str, cert_password: str) -> list[str]:
            calls.append(("login", user))
            return ["acct-1"]

        def register_api_auth(self, account: object) -> None:
            calls.append(("register_api_auth", account))

        def init_realtime(self, account: object) -> None:
            calls.append(("init_realtime", account))

    monkeypatch.setitem(sys.modules, "taishin_sdk", types.SimpleNamespace(TaishinSDK=FakeTaishinSDK))

    result = verify_nova_login(credentials())

    assert result.status == "ok"
    assert ("login", "A123456789") in calls
    assert ("register_api_auth", "acct-1") in calls
    assert ("init_realtime", "acct-1") in calls


def test_verify_nova_login_supports_camel_case_taishin_sdk(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeTaishinSDK:
        def login(self, user: str, password: str, cert_path: str, cert_password: str) -> list[str]:
            calls.append(("login", user))
            return ["acct-1"]

        def registerApiAuth(self, account: object) -> None:  # noqa: N802 - SDK compatibility shim.
            calls.append(("registerApiAuth", account))

        def initRealtime(self, account: object) -> None:  # noqa: N802 - SDK compatibility shim.
            calls.append(("initRealtime", account))

    monkeypatch.setitem(sys.modules, "taishin_sdk", types.SimpleNamespace(TaishinSDK=FakeTaishinSDK))

    result = verify_nova_login(credentials())

    assert result.status == "ok"
    assert ("registerApiAuth", "acct-1") in calls
    assert ("initRealtime", "acct-1") in calls


def test_verify_llm_environment_accepts_codex_cli_login(tmp_path, monkeypatch) -> None:
    _clear_llm_process_env(monkeypatch)
    (tmp_path / ".env.local").write_text(
        "TW_WATCH_LLM_BACKEND=codex_cli\nTW_WATCH_CODEX_MODEL=gpt-test\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run_subprocess(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, stdout="codex-cli 0.125.0\n", stderr="")
        if args[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="Logged in using ChatGPT\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(setup_env, "find_codex_cli_command", lambda: r"C:\node\codex.cmd")
    monkeypatch.setattr(setup_env, "run_subprocess", fake_run_subprocess)

    result = verify_llm_environment(tmp_path)

    assert result.status == "ok"
    assert r"C:\node\codex.cmd" in result.detail
    assert "codex-cli 0.125.0" in result.detail
    assert "gpt-test" in result.detail
    assert "Logged in" not in result.detail
    assert calls == [[r"C:\node\codex.cmd", "--version"], [r"C:\node\codex.cmd", "login", "status"]]


def test_verify_llm_environment_errors_when_codex_missing(tmp_path, monkeypatch) -> None:
    _clear_llm_process_env(monkeypatch)
    (tmp_path / ".env.local").write_text("TW_WATCH_LLM_BACKEND=codex_cli\n", encoding="utf-8")
    monkeypatch.setattr(setup_env, "find_codex_cli_command", lambda: None)

    result = verify_llm_environment(tmp_path)

    assert result.status == "error"
    assert "找不到 Codex CLI" in result.detail
    assert "codex --version" in result.remediation


def test_verify_llm_environment_errors_when_codex_not_logged_in(tmp_path, monkeypatch) -> None:
    _clear_llm_process_env(monkeypatch)
    (tmp_path / ".env.local").write_text("TW_WATCH_LLM_BACKEND=codex_cli\n", encoding="utf-8")

    def fake_run_subprocess(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, stdout="codex-cli 0.125.0\n", stderr="")
        if args[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="Not logged in\n")
        raise AssertionError(args)

    monkeypatch.setattr(setup_env, "find_codex_cli_command", lambda: r"C:\node\codex.cmd")
    monkeypatch.setattr(setup_env, "run_subprocess", fake_run_subprocess)

    result = verify_llm_environment(tmp_path)

    assert result.status == "error"
    assert "登入狀態未通過" in result.detail
    assert "codex login" in result.remediation
    assert "Not logged in" in result.remediation


def test_verify_llm_environment_requires_openai_api_key(tmp_path, monkeypatch) -> None:
    _clear_llm_process_env(monkeypatch)
    (tmp_path / ".env.local").write_text("TW_WATCH_LLM_BACKEND=openai_api\n", encoding="utf-8")

    result = verify_llm_environment(tmp_path)

    assert result.status == "error"
    assert "OPENAI_API_KEY" in result.detail
    assert "OPENAI_API_KEY" in result.remediation


def test_redact_setup_text_hides_short_passwords_and_cert_paths() -> None:
    creds = credentials(r"C:\Users\me\Desktop\nova.pfx")
    text = "NATIONAL_ID=A123456789 ACCOUNT_PASS=shortpw CERT_PASS=certpw CERT_PATH=C:\\Users\\me\\Desktop\\nova.pfx"

    redacted = redact_setup_text(text, creds)

    assert "A123456789" not in redacted
    assert "shortpw" not in redacted
    assert "certpw" not in redacted
    assert "nova.pfx" not in redacted


def test_mcp_message_roundtrip_uses_newline_json_frames() -> None:
    stream = BytesIO()
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    send_mcp_message(stream, payload)
    assert stream.getvalue().endswith(b"\n")
    assert b"Content-Length" not in stream.getvalue()
    stream.seek(0)

    assert read_mcp_message(stream, timeout_seconds=1) == payload


def test_read_mcp_message_skips_notifications_until_expected_id() -> None:
    stream = BytesIO()
    stream.write(b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n')
    stream.write(b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}\n')
    stream.seek(0)

    assert read_mcp_message(stream, timeout_seconds=1, expected_id=1)["id"] == 1


def test_call_with_timeout_raises_instead_of_blocking_forever() -> None:
    with pytest.raises(TimeoutError):
        call_with_timeout(lambda: (time.sleep(1), b"")[1], timeout_seconds=0.01)


def _clear_llm_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "TW_WATCH_LLM_BACKEND",
        "TW_WATCH_CODEX_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
