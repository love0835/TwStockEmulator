from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from pathlib import Path
from tkinter import BooleanVar, END, StringVar, Text, Tk, filedialog, messagebox
from tkinter import ttk

from tw_watchdesk.config import app_base_dir, default_settings_file, load_settings
from tw_watchdesk.redaction import redact_text
from tw_watchdesk.setup_env import (
    TAISHIN_CERT_CENTER_URL,
    TAISHIN_NOVA_PREPARE_URL,
    SetupCredentials,
    SetupOptions,
    StepResult,
    default_codex_config_path,
    redact_setup_text,
    run_full_setup,
    validate_setup_inputs,
)


class SetupWizard:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.base_dir = app_base_dir()
        self.settings = load_settings(self.base_dir)
        self.codex_config_path = default_codex_config_path()
        self.running = False

        self.national_id_var = StringVar(value=self.settings.nova_user)
        self.account_password_var = StringVar(value=self.settings.nova_password)
        self.cert_path_var = StringVar(value=self.settings.nova_cert_path)
        self.cert_password_var = StringVar(value=self.settings.nova_cert_password)
        self.quote_wait_var = StringVar(value=str(self.settings.nova_quote_wait_seconds or 8))
        self.copy_cert_var = BooleanVar(value=True)
        self.install_node_var = BooleanVar(value=True)
        self.install_mcp_var = BooleanVar(value=True)
        self.configure_mcp_var = BooleanVar(value=True)
        self.verify_mcp_var = BooleanVar(value=True)
        self.verify_nova_var = BooleanVar(value=True)
        self.register_api_var = BooleanVar(value=True)

        self._build_ui()

    def _build_ui(self) -> None:
        self.root.title("TwWatchDesk Nova 環境精靈")
        self.root.geometry("920x680")
        self.root.minsize(820, 600)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Nova / Fugle MCP 環境安裝與檢測", font=("", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=(
                f"TwWatchDesk 設定：{default_settings_file(self.base_dir)}\n"
                f"Codex MCP 設定：{self.codex_config_path}"
            ),
            foreground="#444",
            wraplength=840,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        form = ttk.LabelFrame(self.root, text="台新 Nova 帳密與憑證", padding=12)
        form.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        form.columnconfigure(1, weight=1)
        rows = [
            ("身分證字號", self.national_id_var, False),
            ("登入密碼", self.account_password_var, True),
            ("憑證檔", self.cert_path_var, False),
            ("憑證密碼", self.cert_password_var, True),
            ("報價等待秒數", self.quote_wait_var, False),
        ]
        for row, (label, var, masked) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            entry = ttk.Entry(form, textvariable=var, show="*" if masked else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if label == "憑證檔":
                ttk.Button(form, text="瀏覽", command=self.browse_cert).grid(row=row, column=2, padx=(8, 0), pady=4)

        link_buttons = ttk.Frame(form)
        link_buttons.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(8, 0))
        ttk.Button(link_buttons, text="台新憑證中心", command=lambda: webbrowser.open(TAISHIN_CERT_CENTER_URL)).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(link_buttons, text="Nova 準備文件", command=lambda: webbrowser.open(TAISHIN_NOVA_PREPARE_URL)).grid(row=1, column=0, sticky="ew")

        options = ttk.LabelFrame(self.root, text="一鍵流程", padding=12)
        options.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 10))
        options.columnconfigure(0, weight=1)
        options.rowconfigure(1, weight=1)

        checks = ttk.Frame(options)
        checks.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for col in range(3):
            checks.columnconfigure(col, weight=1)
        ttk.Checkbutton(checks, text="匯入憑證到本機資料夾", variable=self.copy_cert_var).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(checks, text="缺 Node.js 時用 winget 安裝", variable=self.install_node_var).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Checkbutton(checks, text="安裝/更新 Fugle MCP", variable=self.install_mcp_var).grid(row=0, column=2, sticky="w", pady=2)
        ttk.Checkbutton(checks, text="寫入 Codex MCP 設定", variable=self.configure_mcp_var).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Checkbutton(checks, text="驗證 Fugle MCP initialize/tools", variable=self.verify_mcp_var).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Checkbutton(checks, text="驗證 Nova login/initRealtime", variable=self.verify_nova_var).grid(row=1, column=2, sticky="w", pady=2)
        ttk.Checkbutton(checks, text="確認/開通 Nova API 權限", variable=self.register_api_var).grid(row=2, column=0, sticky="w", pady=2)
        ttk.Label(checks, text="MCP 下單固定關閉：ENABLE_ORDER=false", foreground="#555").grid(row=2, column=1, columnspan=2, sticky="w", pady=2)

        log_frame = ttk.Frame(options)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = Text(log_frame, height=16, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("ok", foreground="#126b2e")
        self.log.tag_configure("warning", foreground="#9a5b00")
        self.log.tag_configure("error", foreground="#b00020")
        self.log.tag_configure("skipped", foreground="#555")

        buttons = ttk.Frame(self.root, padding=(14, 0, 14, 14))
        buttons.grid(row=3, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        self.run_button = ttk.Button(buttons, text="一鍵檢測並修復", command=self.run_setup)
        self.run_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="檢查欄位", command=self.check_fields).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(buttons, text="關閉", command=self.root.destroy).grid(row=0, column=3)

        self.append_log(StepResult("就緒", "ok", "填入帳密與憑證後，按「一鍵檢測並修復」。所有 log 會自動遮蔽敏感內容。"))

    def browse_cert(self) -> None:
        path = filedialog.askopenfilename(
            title="選擇台新憑證檔",
            filetypes=[("憑證檔", "*.pfx *.p12 *.pem *.crt"), ("所有檔案", "*.*")],
        )
        if path:
            self.cert_path_var.set(path)

    def credentials(self) -> SetupCredentials:
        return SetupCredentials(
            national_id=self.national_id_var.get().strip(),
            account_password=self.account_password_var.get(),
            cert_path=self.cert_path_var.get().strip(),
            cert_password=self.cert_password_var.get(),
            quote_wait_seconds=self.quote_wait_var.get().strip() or "8",
        )

    def options(self) -> SetupOptions:
        return SetupOptions(
            install_node_with_winget=self.install_node_var.get(),
            install_fugle_mcp=self.install_mcp_var.get(),
            configure_codex_mcp=self.configure_mcp_var.get(),
            verify_mcp=self.verify_mcp_var.get(),
            verify_nova=self.verify_nova_var.get(),
            register_api_auth=self.register_api_var.get(),
            copy_certificate=self.copy_cert_var.get(),
            enable_order=False,
        )

    def check_fields(self) -> None:
        self.clear_log()
        for result in validate_setup_inputs(self.credentials()):
            self.append_log(result)

    def run_setup(self) -> None:
        if self.running:
            return
        self.running = True
        self.run_button.configure(state="disabled")
        self.clear_log()
        thread = threading.Thread(target=self._run_setup_thread, daemon=True)
        thread.start()

    def _run_setup_thread(self) -> None:
        credentials = self.credentials()
        try:
            results = run_full_setup(
                credentials,
                self.options(),
                base_dir=self.base_dir,
                codex_config_path=self.codex_config_path,
                progress=lambda result: self.root.after(0, self.append_log, result),
            )
            success = bool(results) and all(result.ok for result in results)
            self.root.after(0, self._finish_run, success)
        except Exception as exc:  # noqa: BLE001 - top-level GUI guard must report any unexpected failure.
            self.root.after(0, self.append_log, StepResult("未預期錯誤", "error", redact_setup_text(redact_text(str(exc)), credentials)))
            self.root.after(0, self._finish_run, False)

    def _finish_run(self, success: bool) -> None:
        self.running = False
        self.run_button.configure(state="normal")
        if success:
            messagebox.showinfo("完成", "Nova 環境與 Fugle MCP 已完成設定並通過檢測。")
        else:
            messagebox.showwarning("需要處理", "環境精靈已停止在錯誤項目；請看下方 log。")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", END)
        self.log.configure(state="disabled")

    def append_log(self, result: StepResult) -> None:
        prefix = {"ok": "OK", "warning": "WARN", "error": "ERROR", "skipped": "SKIP"}[result.status]
        credentials = self.credentials()
        detail = redact_setup_text(result.detail, credentials)
        remediation = redact_setup_text(result.remediation, credentials)
        line = f"[{prefix}] {result.name}: {detail}"
        if remediation:
            line += f"\n    處理：{remediation}"
        line += "\n"
        self.log.configure(state="normal")
        self.log.insert(END, line, result.status)
        self.log.see(END)
        self.log.configure(state="disabled")


def static_self_check() -> int:
    settings = load_settings(app_base_dir())
    payload = {
        "settings_file": str(default_settings_file(app_base_dir())),
        "codex_config": str(default_codex_config_path()),
        "has_nova_user": bool(settings.nova_user),
        "has_cert_path": bool(settings.nova_cert_path),
        "market_data_mode": settings.market_data_mode,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TwWatchDesk Nova/Fugle MCP setup wizard")
    parser.add_argument("--self-check", action="store_true", help="print non-secret static setup information and exit")
    args = parser.parse_args()
    if args.self_check:
        raise SystemExit(static_self_check())
    root = Tk()
    SetupWizard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
