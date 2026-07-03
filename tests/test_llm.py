import subprocess
from pathlib import Path

import tw_watchdesk.llm as llm
from tw_watchdesk.llm import CodexExecAdapter, swing_strategy_review_schema, trading_decision_schema


def test_codex_adapter_parses_output_file(tmp_path) -> None:
    captured: dict[str, object] = {}

    def runner(command: list[str], *, input_text: str, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"action":"hold","confidence":0.7,"reason":"ok","risk_note":"none"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="noise", stderr="")

    adapter = CodexExecAdapter(cwd=tmp_path, runner=runner)
    result = adapter.run_json("prompt", trading_decision_schema())

    assert result.ok is True
    assert result.data["action"] == "hold"
    assert captured["command"][-2:] == ["--disable", "web_search"]


def test_codex_adapter_allows_news_agent_web_search(tmp_path) -> None:
    captured: dict[str, object] = {}

    def runner(command: list[str], *, input_text: str, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"action":"hold","confidence":0.7,"reason":"ok","risk_note":"none"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adapter = CodexExecAdapter(cwd=tmp_path, runner=runner)
    result = adapter.run_json("prompt", trading_decision_schema(), allow_web_search=True)

    assert result.ok is True
    assert "--disable" not in captured["command"]


def test_codex_adapter_degrades_on_invalid_json(tmp_path) -> None:
    def runner(command: list[str], *, input_text: str, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("not-json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adapter = CodexExecAdapter(cwd=tmp_path, runner=runner)
    result = adapter.run_json("prompt", trading_decision_schema())

    assert result.ok is False
    assert "invalid codex json" in result.error


def test_codex_adapter_uses_cmd_shim_on_windows(tmp_path, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_which(name: str) -> str | None:
        if name == "codex.cmd":
            return r"C:\node\codex.cmd"
        if name == "codex.exe":
            return r"C:\node\codex.exe"
        return None

    def runner(command: list[str], *, input_text: str, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
        captured["executable"] = command[0]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"action":"hold","confidence":0.7,"reason":"ok","risk_note":"none"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(llm.os, "name", "nt")
    monkeypatch.setattr(llm.shutil, "which", fake_which)

    adapter = CodexExecAdapter(cwd=tmp_path, runner=runner)
    result = adapter.run_json("prompt", trading_decision_schema())

    assert result.ok is True
    assert captured["executable"] == r"C:\node\codex.cmd"


def test_run_command_uses_utf8_and_hides_window_on_windows(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(llm.os, "name", "nt")
    monkeypatch.setattr(llm.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    result = llm._run_command(["codex.cmd", "exec"], input_text="短線會後討論", timeout=10, cwd=tmp_path)

    assert result.returncode == 0
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["creationflags"] == 0x08000000


def test_swing_strategy_review_schema_requires_all_parameter_keys() -> None:
    schema = swing_strategy_review_schema()
    parameter_changes = schema["properties"]["parameter_changes"]

    assert sorted(parameter_changes["required"]) == sorted(parameter_changes["properties"])
    assert parameter_changes["properties"]["stop_loss_pct"]["type"] == ["number", "null"]
    assert parameter_changes["properties"]["long_holding_months"]["type"] == ["integer", "null"]
