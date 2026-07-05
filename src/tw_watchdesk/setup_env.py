from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from tw_watchdesk.config import default_settings_file, load_settings, save_nova_settings
from tw_watchdesk.redaction import redact_text


TAISHIN_SDK_WHEEL_URL = "https://ml-fugle-api.tssco.com.tw/FugleSDK/sdk/taishin_sdk-1.0.2-cp37-abi3-win_amd64.whl"
FUGLE_MCP_PACKAGE = "@fugle/mcp-server@0.1.1"
TAISHIN_CERT_CENTER_URL = "https://www.tssco.com.tw/CAinfo/"
TAISHIN_NOVA_PREPARE_URL = "https://ml-fugle-api.masterlink.com.tw/FugleSDK/docs/trading/prepare/"
NODE_WINGET_ID = "OpenJS.NodeJS.LTS"
MCP_PROTOCOL_VERSION = "2024-11-05"
CODEX_LLM_BACKENDS = {"", "codex", "codex_cli"}
OPENAI_LLM_BACKENDS = {"openai", "openai_api", "openai_responses_api"}
ANTHROPIC_LLM_BACKENDS = {"anthropic", "anthropic_api", "anthropic_messages_api"}

StepStatus = Literal["ok", "warning", "error", "skipped"]
ProgressCallback = Callable[["StepResult"], None]


@dataclass(frozen=True)
class SetupCredentials:
    national_id: str
    account_password: str
    cert_path: str
    cert_password: str
    quote_wait_seconds: str = "8"


@dataclass(frozen=True)
class SetupOptions:
    install_node_with_winget: bool = True
    install_fugle_mcp: bool = True
    configure_codex_mcp: bool = True
    verify_mcp: bool = True
    verify_nova: bool = True
    verify_llm: bool = True
    register_api_auth: bool = True
    copy_certificate: bool = True
    enable_order: bool = False


@dataclass(frozen=True)
class StepResult:
    name: str
    status: StepStatus
    detail: str
    remediation: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "warning", "skipped"}


def run_full_setup(
    credentials: SetupCredentials,
    options: SetupOptions,
    base_dir: Path | None = None,
    codex_config_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[StepResult]:
    base_dir = base_dir or _app_base_for_setup()
    codex_config_path = codex_config_path or default_codex_config_path()
    results: list[StepResult] = []

    def emit(result: StepResult) -> None:
        clean = StepResult(
            name=result.name,
            status=result.status,
            detail=redact_setup_text(result.detail, credentials),
            remediation=redact_setup_text(result.remediation, credentials),
        )
        results.append(clean)
        if progress:
            progress(clean)

    validation = validate_setup_inputs(credentials)
    for result in validation:
        emit(result)
    if any(result.status == "error" for result in validation):
        emit(StepResult("停止", "error", "必要設定不完整，未寫入任何帳密或 MCP 設定。"))
        return results

    sdk_result = ensure_taishin_sdk()
    emit(sdk_result)
    if sdk_result.status == "error":
        return results

    effective_cert_path = Path(credentials.cert_path).expanduser()
    if options.copy_certificate:
        cert_result, effective_cert_path = copy_certificate_to_project_store(effective_cert_path, base_dir)
        emit(cert_result)

    env_result = write_nova_env(credentials, effective_cert_path, base_dir)
    emit(env_result)
    if env_result.status == "error":
        return results

    if options.verify_llm:
        emit(verify_llm_environment(base_dir))

    if options.verify_nova:
        nova_result = verify_nova_login(
            SetupCredentials(
                national_id=credentials.national_id,
                account_password=credentials.account_password,
                cert_path=str(effective_cert_path),
                cert_password=credentials.cert_password,
                quote_wait_seconds=credentials.quote_wait_seconds,
            ),
            register_api_auth=options.register_api_auth,
        )
        emit(nova_result)
        if nova_result.status == "error":
            return results

    npm_path = find_command("npm")
    if options.install_fugle_mcp or options.configure_codex_mcp or options.verify_mcp:
        node_result, npm_path = ensure_node_and_npm(npm_path, install_with_winget=options.install_node_with_winget)
        emit(node_result)
        if node_result.status == "error":
            return results

    if options.install_fugle_mcp:
        mcp_install_result = install_or_update_fugle_mcp(npm_path)
        emit(mcp_install_result)
        if mcp_install_result.status == "error":
            return results

    fugle_command = find_fugle_mcp_command()
    if options.configure_codex_mcp:
        if not fugle_command:
            emit(
                StepResult(
                    "Fugle MCP command",
                    "error",
                    "找不到 fugle-mcp-server，無法寫入可直接啟動的 Codex MCP 設定。",
                    f"請確認 npm install -g {FUGLE_MCP_PACKAGE} 成功。",
                )
            )
            return results
        config_result = update_codex_fugle_config(
            codex_config_path,
            credentials=SetupCredentials(
                national_id=credentials.national_id,
                account_password=credentials.account_password,
                cert_path=str(effective_cert_path),
                cert_password=credentials.cert_password,
                quote_wait_seconds=credentials.quote_wait_seconds,
            ),
            fugle_command=fugle_command,
            enable_order=options.enable_order,
        )
        emit(config_result)
        if config_result.status == "error":
            return results

    if options.verify_mcp:
        if not fugle_command:
            emit(StepResult("Fugle MCP smoke", "error", "找不到 fugle-mcp-server，無法做 MCP smoke。"))
            return results
        mcp_result = verify_fugle_mcp(
            fugle_command,
            build_fugle_mcp_env(
                SetupCredentials(
                    national_id=credentials.national_id,
                    account_password=credentials.account_password,
                    cert_path=str(effective_cert_path),
                    cert_password=credentials.cert_password,
                    quote_wait_seconds=credentials.quote_wait_seconds,
                ),
                enable_order=options.enable_order,
            ),
        )
        emit(mcp_result)

    if all(result.ok for result in results):
        emit(StepResult("完成", "ok", "Nova 環境、TwWatchDesk 設定與 Fugle MCP 設定已完成。"))
    return results


def validate_setup_inputs(credentials: SetupCredentials) -> list[StepResult]:
    results: list[StepResult] = []
    if not credentials.national_id.strip():
        results.append(StepResult("身分證字號", "error", "身分證字號未填。"))
    elif not re.fullmatch(r"[A-Za-z][12]\d{8}", credentials.national_id.strip()):
        results.append(StepResult("身分證字號", "warning", "身分證字號格式不像一般台灣自然人 ID；仍會照填入值嘗試登入。"))
    else:
        results.append(StepResult("身分證字號", "ok", "已填入。"))

    if not credentials.account_password:
        results.append(StepResult("登入密碼", "error", "台新網路登入密碼未填。"))
    else:
        results.append(StepResult("登入密碼", "ok", "已填入。"))

    cert_path = Path(credentials.cert_path).expanduser()
    if not str(credentials.cert_path).strip():
        results.append(StepResult("憑證檔", "error", "憑證檔未選擇。", f"請到台新憑證中心下載或匯出憑證：{TAISHIN_CERT_CENTER_URL}"))
    elif not cert_path.exists():
        results.append(StepResult("憑證檔", "error", f"憑證檔不存在：{cert_path}", "請重新選擇 .pfx 或 .p12 憑證檔。"))
    elif cert_path.suffix.lower() not in {".pfx", ".p12", ".pem", ".crt"}:
        results.append(StepResult("憑證檔", "warning", f"憑證副檔名不是常見格式：{cert_path.suffix}"))
    else:
        results.append(StepResult("憑證檔", "ok", f"憑證檔存在：{cert_path}"))

    if not credentials.cert_password:
        results.append(StepResult("憑證密碼", "error", "憑證密碼未填。"))
    else:
        results.append(StepResult("憑證密碼", "ok", "已填入。"))

    try:
        wait = float(credentials.quote_wait_seconds)
        if wait <= 0:
            raise ValueError
    except ValueError:
        results.append(StepResult("報價等待秒數", "error", "報價等待秒數必須是大於 0 的數字。"))
    else:
        results.append(StepResult("報價等待秒數", "ok", f"設定為 {wait:g} 秒。"))
    return results


def ensure_taishin_sdk() -> StepResult:
    if importlib.util.find_spec("taishin_sdk") is not None:
        return StepResult("Taishin SDK", "ok", "目前執行環境已可 import taishin_sdk。")
    if getattr(sys, "frozen", False):
        return StepResult("Taishin SDK", "error", "setup exe 未包含 taishin_sdk。", "請用 scripts\\build_exe.ps1 重新打包完整 exe。")

    python = Path(sys.executable)
    if not python.exists():
        return StepResult("Taishin SDK", "error", "找不到目前 Python 執行檔，無法自動安裝 SDK。")
    result = run_subprocess(
        [str(python), "-m", "pip", "install", TAISHIN_SDK_WHEEL_URL],
        timeout_seconds=300,
    )
    if result.returncode != 0:
        return StepResult("Taishin SDK", "error", f"SDK 安裝失敗：{result.stderr or result.stdout}", f"可手動執行：{python} -m pip install {TAISHIN_SDK_WHEEL_URL}")
    if importlib.util.find_spec("taishin_sdk") is None:
        return StepResult("Taishin SDK", "warning", "SDK 安裝指令成功，但目前程序尚未載入；重新開啟 setup 後會再檢查。")
    return StepResult("Taishin SDK", "ok", "已安裝並可 import taishin_sdk。")


def copy_certificate_to_project_store(cert_path: Path, base_dir: Path) -> tuple[StepResult, Path]:
    cert_path = cert_path.expanduser().resolve()
    target_dir = base_dir / "data" / "certs"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / cert_path.name
        if cert_path.resolve() != target.resolve():
            shutil.copy2(cert_path, target)
        return StepResult("憑證匯入", "ok", f"已將憑證匯入專案設定資料夾：{target}"), target
    except OSError as exc:
        return StepResult("憑證匯入", "error", f"複製憑證失敗：{exc}", "請確認檔案未被鎖定，或取消複製憑證後直接使用原路徑。"), cert_path


def write_nova_env(credentials: SetupCredentials, cert_path: Path, base_dir: Path) -> StepResult:
    target = default_settings_file(base_dir)
    try:
        save_nova_settings(
            target,
            {
                "TW_WATCH_MARKET_DATA_MODE": "live",
                "TAISHIN_NOVA_USER": credentials.national_id.strip(),
                "TAISHIN_NOVA_PASSWORD": credentials.account_password,
                "TAISHIN_NOVA_CERT_PATH": str(cert_path),
                "TAISHIN_NOVA_CERT_PASSWORD": credentials.cert_password,
                "TAISHIN_NOVA_QUOTE_WAIT_SECONDS": credentials.quote_wait_seconds.strip() or "8",
            },
        )
    except OSError as exc:
        return StepResult("TwWatchDesk 設定", "error", f"寫入 {target} 失敗：{exc}")
    return StepResult("TwWatchDesk 設定", "ok", f"已寫入 Nova 設定：{target}")


def verify_nova_login(credentials: SetupCredentials, register_api_auth: bool = True) -> StepResult:
    try:
        from taishin_sdk import TaishinSDK  # type: ignore
    except ImportError as exc:
        return StepResult("Nova 登入", "error", f"缺少 taishin_sdk：{exc}")

    try:
        sdk = TaishinSDK()
        accounts = sdk.login(
            credentials.national_id.strip(),
            credentials.account_password,
            credentials.cert_path,
            credentials.cert_password,
        )
        account = accounts[0] if accounts else None
        if account is None:
            return StepResult("Nova 登入", "error", "Nova 登入成功但沒有可用帳戶。")
        auth_note = ""
        if register_api_auth:
            try:
                sdk.registerApiAuth(account)
                auth_note = "；API 權限已送出或確認"
            except Exception as exc:  # noqa: BLE001 - SDK raises provider-specific exceptions.
                message = str(exc)
                if "OA0027" in message or "already" in message.lower() or "已" in message:
                    auth_note = "；API 權限已存在"
                else:
                    raise
        sdk.initRealtime(account)
    except Exception as exc:  # noqa: BLE001 - SDK raises provider-specific exceptions.
        return StepResult("Nova 登入", "error", f"Nova login/initRealtime 失敗：{redact_setup_text(str(exc), credentials)}")
    return StepResult("Nova 登入", "ok", f"登入、帳戶取得與 initRealtime 成功{auth_note}。")


def ensure_node_and_npm(npm_path: str | None, install_with_winget: bool) -> tuple[StepResult, str | None]:
    if npm_path:
        version = command_output([npm_path, "--version"], timeout_seconds=20)
        detail = f"npm 可用：{npm_path}"
        if version:
            detail += f" ({version})"
        return StepResult("Node/npm", "ok", detail), npm_path

    if not install_with_winget:
        return (
            StepResult(
                "Node/npm",
                "error",
                "找不到 npm，且未允許用 winget 自動安裝 Node.js LTS。",
                "請安裝 Node.js LTS 後重新執行 setup。",
            ),
            None,
        )

    winget = find_command("winget")
    if not winget:
        return (
            StepResult("Node/npm", "error", "找不到 npm，也找不到 winget，無法自動安裝 Node.js。", "請手動安裝 Node.js LTS。"),
            None,
        )

    result = run_subprocess(
        [
            winget,
            "install",
            "--id",
            NODE_WINGET_ID,
            "-e",
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        timeout_seconds=900,
    )
    if result.returncode != 0:
        return (
            StepResult("Node/npm", "error", f"winget 安裝 Node.js 失敗：{result.stderr or result.stdout}", "請手動安裝 Node.js LTS 後重跑。"),
            None,
        )

    refresh_path_from_winget_node()
    npm_path = find_command("npm")
    if not npm_path:
        return (
            StepResult("Node/npm", "warning", "Node.js 安裝完成，但目前程序仍找不到 npm；重新開啟 setup 後會再檢查。"),
            None,
        )
    return StepResult("Node/npm", "ok", f"Node.js/npm 已安裝並可用：{npm_path}"), npm_path


def install_or_update_fugle_mcp(npm_path: str | None) -> StepResult:
    if not npm_path:
        return StepResult("Fugle MCP 安裝", "error", "沒有 npm，無法安裝 Fugle MCP。")
    result = run_subprocess([npm_path, "install", "-g", FUGLE_MCP_PACKAGE], timeout_seconds=600)
    if result.returncode != 0:
        return StepResult("Fugle MCP 安裝", "error", f"npm install 失敗：{result.stderr or result.stdout}")
    refresh_path_from_winget_node()
    command = find_fugle_mcp_command()
    if not command:
        return StepResult("Fugle MCP 安裝", "warning", "npm install 成功，但 PATH 還找不到 fugle-mcp-server；重新開啟 setup 或 Codex 後再檢查。")
    return StepResult("Fugle MCP 安裝", "ok", f"已安裝 {FUGLE_MCP_PACKAGE}：{command}")


def verify_llm_environment(base_dir: Path | None = None) -> StepResult:
    settings = load_settings(base_dir or _app_base_for_setup())
    backend = (settings.llm_backend or "codex_cli").strip().lower()
    model = settings.codex_model or "gpt-5.5"

    if backend in CODEX_LLM_BACKENDS:
        command = find_codex_cli_command()
        if not command:
            return StepResult(
                "LLM 環境",
                "error",
                "TW_WATCH_LLM_BACKEND=codex_cli，但 PATH 找不到 Codex CLI。",
                "請安裝 Codex CLI / Codex Desktop，確認 PowerShell 可執行 `codex --version` 後重跑 setup。",
            )
        version_result = run_subprocess([command, "--version"], timeout_seconds=20)
        version = (version_result.stdout or version_result.stderr or "").strip()
        if version_result.returncode != 0:
            return StepResult(
                "LLM 環境",
                "error",
                f"Codex CLI 已找到但無法執行 --version：{command}",
                "請重新安裝 Codex CLI，或確認 `codex --version` 在 PowerShell 可正常執行。",
            )
        login_result = run_subprocess([command, "login", "status"], timeout_seconds=20)
        if login_result.returncode != 0:
            detail = redact_text((login_result.stderr or login_result.stdout or "").strip())
            remediation = "請在 PowerShell 執行 `codex login`，或開啟 Codex Desktop 完成登入後重跑 setup。"
            if detail:
                remediation += f" CLI 訊息：{detail}"
            return StepResult(
                "LLM 環境",
                "error",
                f"Codex CLI 可啟動但登入狀態未通過：{command}",
                remediation,
            )
        version_text = f"；版本：{version}" if version else ""
        return StepResult("LLM 環境", "ok", f"Codex CLI 可用：{command}{version_text}；登入狀態已確認；model={model}。")

    if backend in OPENAI_LLM_BACKENDS:
        if not settings.openai_api_key:
            return StepResult(
                "LLM 環境",
                "error",
                f"TW_WATCH_LLM_BACKEND={backend}，但 OPENAI_API_KEY 未設定。",
                "請在 .env.local 或系統環境變數設定 OPENAI_API_KEY，或改回 TW_WATCH_LLM_BACKEND=codex_cli。",
            )
        return StepResult("LLM 環境", "ok", f"OpenAI API key 已設定；backend={backend}；model={model}；未做網路呼叫。")

    if backend in ANTHROPIC_LLM_BACKENDS:
        if not settings.anthropic_api_key:
            return StepResult(
                "LLM 環境",
                "error",
                f"TW_WATCH_LLM_BACKEND={backend}，但 ANTHROPIC_API_KEY 未設定。",
                "請在 .env.local 或系統環境變數設定 ANTHROPIC_API_KEY，或改回 TW_WATCH_LLM_BACKEND=codex_cli。",
            )
        return StepResult("LLM 環境", "ok", f"Anthropic API key 已設定；backend={backend}；model={model}；未做網路呼叫。")

    return StepResult(
        "LLM 環境",
        "error",
        f"未知 LLM backend：{backend}",
        "支援值：codex_cli、openai_api、anthropic_api。",
    )


def update_codex_fugle_config(
    config_path: Path,
    credentials: SetupCredentials,
    fugle_command: str,
    enable_order: bool = False,
) -> StepResult:
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        old_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        new_text = replace_fugle_mcp_block(old_text, credentials, fugle_command, enable_order=enable_order)
        config_path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return StepResult("Codex MCP 設定", "error", f"寫入 {config_path} 失敗：{exc}")
    order_text = "true" if enable_order else "false"
    return StepResult("Codex MCP 設定", "ok", f"已寫入 Fugle MCP：{config_path}；ENABLE_ORDER={order_text}")


def replace_fugle_mcp_block(text: str, credentials: SetupCredentials, fugle_command: str, enable_order: bool = False) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skip = False
    fugle_header = re.compile(r"^\s*\[mcp_servers\.fugle(?:\.env)?\]\s*$")
    any_header = re.compile(r"^\s*\[.+\]\s*$")
    for line in lines:
        if fugle_header.match(line):
            while output and (not output[-1].strip() or output[-1].lstrip().startswith("#")):
                output.pop()
            skip = True
            continue
        if skip and any_header.match(line):
            skip = False
        if not skip:
            output.append(line)
    while output and not output[-1].strip():
        output.pop()
    if output:
        output.append("")
        output.append("")
    output.append(build_fugle_mcp_config_block(credentials, fugle_command, enable_order=enable_order))
    return "\n".join(output) + "\n"


def build_fugle_mcp_config_block(credentials: SetupCredentials, fugle_command: str, enable_order: bool = False) -> str:
    env = build_fugle_mcp_env(credentials, enable_order=enable_order)
    lines = [
        "# Managed by TwWatchDeskSetup. Orders stay disabled unless explicitly changed.",
        "[mcp_servers.fugle]",
        "enabled = true",
        f"command = {_toml_quote(fugle_command)}",
        "",
        "[mcp_servers.fugle.env]",
    ]
    for key in ("SDK_TYPE", "ENABLE_ORDER", "NATIONAL_ID", "ACCOUNT_PASS", "CERT_PASS", "CERT_PATH"):
        lines.append(f"{key} = {_toml_quote(env[key])}")
    lines.append('# ACCOUNT = "optional_account_if_you_have_multiple_accounts"')
    return "\n".join(lines)


def build_fugle_mcp_env(credentials: SetupCredentials, enable_order: bool = False) -> dict[str, str]:
    return {
        "SDK_TYPE": "taishin",
        "ENABLE_ORDER": "true" if enable_order else "false",
        "NATIONAL_ID": credentials.national_id.strip(),
        "ACCOUNT_PASS": credentials.account_password,
        "CERT_PASS": credentials.cert_password,
        "CERT_PATH": credentials.cert_path,
    }


def verify_fugle_mcp(command: str, env_values: dict[str, str], timeout_seconds: int = 45) -> StepResult:
    env = os.environ.copy()
    env.update(env_values)
    try:
        process = subprocess.Popen(
            [command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
        )
    except OSError as exc:
        return StepResult("Fugle MCP smoke", "error", f"無法啟動 fugle-mcp-server：{exc}")

    try:
        assert process.stdin is not None
        assert process.stdout is not None
        send_mcp_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "TwWatchDeskSetup", "version": "0.1.0"},
                },
            },
        )
        initialize = read_mcp_message(process.stdout, timeout_seconds=timeout_seconds)
        if initialize.get("error"):
            return StepResult("Fugle MCP smoke", "error", redact_text(f"MCP initialize 失敗：{json.dumps(initialize.get('error'), ensure_ascii=False)}"))
        send_mcp_message(process.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send_mcp_message(process.stdin, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = read_mcp_message(process.stdout, timeout_seconds=timeout_seconds)
        if tools.get("error"):
            return StepResult("Fugle MCP smoke", "error", redact_text(f"MCP tools/list 失敗：{json.dumps(tools.get('error'), ensure_ascii=False)}"))
        tool_count = len((tools.get("result") or {}).get("tools") or [])
        server_info = (initialize.get("result") or {}).get("serverInfo") or {}
        name = server_info.get("name", "fugle-mcp-server")
        version = server_info.get("version", "?")
        return StepResult("Fugle MCP smoke", "ok", f"{name} {version} initialize/tools-list 成功，工具數：{tool_count}。")
    except Exception as exc:  # noqa: BLE001 - protocol smoke must report any launch/protocol failure.
        stderr = collect_process_stderr(process)
        detail = f"MCP smoke 失敗：{exc}"
        if stderr:
            detail += f"；stderr={stderr}"
        return StepResult("Fugle MCP smoke", "error", redact_text(detail))
    finally:
        terminate_process(process)


def send_mcp_message(stream: object, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header + body)  # type: ignore[attr-defined]
    stream.flush()  # type: ignore[attr-defined]


def read_mcp_message(stream: object, timeout_seconds: int) -> dict[str, object]:
    start = time.monotonic()
    headers: dict[str, str] = {}
    while True:
        remaining = timeout_seconds - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError("等待 MCP response 逾時")
        line = call_with_timeout(lambda: stream.readline(), remaining)  # type: ignore[attr-defined]
        if not line:
            raise RuntimeError("MCP server closed stdout")
        decoded = line.decode("ascii", errors="replace").strip()
        if not decoded:
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise RuntimeError("MCP response 缺少 Content-Length")
    remaining = timeout_seconds - (time.monotonic() - start)
    if remaining <= 0:
        raise TimeoutError("等待 MCP response body 逾時")
    body = call_with_timeout(lambda: stream.read(length), remaining)  # type: ignore[attr-defined]
    return json.loads(body.decode("utf-8"))


def call_with_timeout(action: Callable[[], bytes], timeout_seconds: float) -> bytes:
    result_queue: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put(action())
        except BaseException as exc:  # noqa: BLE001 - preserve worker-thread exception.
            result_queue.put(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        result = result_queue.get(timeout=max(timeout_seconds, 0.01))
    except queue.Empty as exc:
        raise TimeoutError("等待 MCP response 逾時") from exc
    if isinstance(result, BaseException):
        raise result
    return result


def collect_process_stderr(process: subprocess.Popen[bytes]) -> str:
    try:
        if process.stderr is None:
            return ""
        if process.poll() is None:
            return ""
        return process.stderr.read(4000).decode("utf-8", errors="replace")
    except Exception:
        return ""


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def default_codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home) if codex_home else Path.home() / ".codex"
    return root / "config.toml"


def find_fugle_mcp_command() -> str | None:
    candidates = where_all("fugle-mcp-server")
    for candidate in candidates:
        if candidate.lower().endswith(".cmd"):
            return candidate
    if candidates:
        return candidates[0]
    return find_command("fugle-mcp-server")


def find_codex_cli_command() -> str | None:
    if os.name == "nt":
        for name in ("codex.cmd", "codex.exe", "codex"):
            command = find_command(name)
            if command:
                return command
        return None
    return find_command("codex")


def find_command(name: str) -> str | None:
    direct = shutil.which(name)
    if direct:
        return direct
    matches = where_all(name)
    return matches[0] if matches else None


def where_all(name: str) -> list[str]:
    try:
        result = subprocess.run(["where.exe", name], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def refresh_path_from_winget_node() -> None:
    node_bin = discover_winget_node_bin()
    if not node_bin:
        return
    current = os.environ.get("PATH", "")
    node_text = str(node_bin)
    if node_text.lower() not in current.lower():
        os.environ["PATH"] = node_text + os.pathsep + current


def discover_winget_node_bin() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    package_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not package_root.exists():
        return None
    for node in package_root.glob("OpenJS.NodeJS.LTS*/*/node.exe"):
        return node.parent
    return None


def run_subprocess(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, stdout=exc.stdout or "", stderr=f"timeout after {timeout_seconds}s")
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def command_output(args: list[str], timeout_seconds: int) -> str:
    result = run_subprocess(args, timeout_seconds)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def redact_setup_text(text: str, credentials: SetupCredentials | None = None) -> str:
    redacted = redact_text(text)
    if credentials:
        for secret in (
            credentials.national_id,
            credentials.account_password,
            credentials.cert_password,
            credentials.cert_path,
            str(Path(credentials.cert_path).expanduser()),
        ):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _app_base_for_setup() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()
