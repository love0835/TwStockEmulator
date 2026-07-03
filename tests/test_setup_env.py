from __future__ import annotations

import time
from io import BytesIO

import pytest

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


def test_redact_setup_text_hides_short_passwords_and_cert_paths() -> None:
    creds = credentials(r"C:\Users\me\Desktop\nova.pfx")
    text = "NATIONAL_ID=A123456789 ACCOUNT_PASS=shortpw CERT_PASS=certpw CERT_PATH=C:\\Users\\me\\Desktop\\nova.pfx"

    redacted = redact_setup_text(text, creds)

    assert "A123456789" not in redacted
    assert "shortpw" not in redacted
    assert "certpw" not in redacted
    assert "nova.pfx" not in redacted


def test_mcp_message_roundtrip_uses_content_length_frames() -> None:
    stream = BytesIO()
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    send_mcp_message(stream, payload)
    stream.seek(0)

    assert read_mcp_message(stream, timeout_seconds=1) == payload


def test_call_with_timeout_raises_instead_of_blocking_forever() -> None:
    with pytest.raises(TimeoutError):
        call_with_timeout(lambda: (time.sleep(1), b"")[1], timeout_seconds=0.01)
