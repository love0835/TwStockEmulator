# 台股即時看盤桌面版

這是一個和 `台股研究團隊` 完全脫勾的獨立桌面程式。它不開本機 Web server，不使用 WebView，也不 import 舊專案任何模組。

## 功能邊界

- 只做看盤攻略與模擬建議，不送真實委託。
- 預設只接受台新 Nova 即時資料；未設定 Nova 或 SDK 不可用時，只顯示阻擋原因，不產生假攻略。
- 五檔、價位、成交量必須在 `TW_WATCH_STALE_SECONDS` 內，預設 70 秒。
- 外資 / 投信第一版只保留為「官方日報背景欄位」，不當成盤中即時交易條件。
- 當沖可用本機資格清單加嚴篩選；未設定清單時仍會依 Nova 普通股排行自動選股。
- 交易實驗室只做模擬交易與紀錄，不呼叫真實下單 API。

## 開發啟動

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env.local
.\.venv\Scripts\tw-watchdesk.exe
```

## 交易實驗室

桌面程式內建四角色模擬交易實驗室：

- 抓盤手：建立當日候選清單。候選只會來自「手動加入」或正式自動選股；單檔看盤股票不會被自動加入候選。
- 當沖模擬交易員：交易時段每 5 分鐘檢查候選，13:20 後不開新倉，13:25-13:30 只建立出場模擬單，13:30 日結前強制平倉。
- 短線模擬交易員：交易時段每 30 分鐘檢查候選，最多同時持有 5 檔。
- 風控員：用固定硬限制控管資金；短線策略可在 UI 啟用會後自我修正，但只會套用通過驗證的版本化參數。

模擬資料存在 app base dir 底下：

```text
開發模式：data/trading_lab.sqlite3
exe 模式：dist/data/trading_lab.sqlite3
```

上方按鈕：

- `加入候選`：把目前股票依目前策略加入今日候選。
- `啟動交易實驗室`：啟動背景 worker。
- `停止交易實驗室`：停止背景 worker。
- `啟用自動選股`：允許 09:05 依 Nova REST 行情自動篩選候選。
- `立即選股`：立即執行一次抓盤手自動選股，方便盤中測試或補跑。
- `多 Agent 檢討`：盤後用 ScoutAgent、DaytradeAgent、SwingAgent、RiskAgent、CoachAgent 對 evidence 做結構化檢討，通過 deterministic gate 後只建立 pending 版本。

下方頁籤會顯示帳戶資金、候選清單、持倉、委託、成交、每日檢討、五檔診斷與三種策略版本。

### 自動選股

自動選股第一版使用台新 Nova 登入後的 marketdata REST snapshot 資料，不依賴 Codex MCP，也不呼叫 LLM。預設關閉，需在 UI 勾選 `啟用自動選股` 或設定：

```text
TW_WATCH_ENABLE_AUTO_SCOUT=true
TW_WATCH_AUTO_SCOUT_TIME=09:05
TW_WATCH_SCOUT_MAX_DAYTRADE=5
TW_WATCH_SCOUT_MAX_SWING=5
TW_WATCH_SCOUT_EXCLUDED_SYMBOLS_FILE=data/scout_excluded_symbols.txt
```

選股來源會寫入候選清單的 `source=auto_scout`，手動加入的候選不會被自動選股覆蓋。當沖資格清單是可選加嚴條件：檔案存在且有內容時，只選清單內股票；檔案不存在或空白時，仍會從 Nova `COMMONSTOCK` 普通股排行自動選出當沖候選，並在監控 Log 標註此狀態。排除清單可放在 `data/scout_excluded_symbols.txt`，一行一檔或逗號分隔。

風控預設：

- 當沖單筆風險 0.35%。
- 短線單筆風險 1%。
- 單檔最大曝險 25%。
- 短線總曝險 80%。
- 當沖每日虧損達子帳戶 2% 後停止新倉。

成交與成本預設：

- 採保守下一根成交：委託建立後，必須由下一根或更晚的 K 線碰價才成交。
- v1 不做部分成交。
- 當沖掛單有效到下一個 5 分鐘 tick，短線掛單有效到下一個 30 分鐘 tick。
- 手續費 0.1425% 每邊。
- 股票賣出證交稅 0.3%；同日現股當沖賣出證交稅 0.15% 至 2027-12-31。

LLM adapter 不在盤中高頻操盤使用，只能用於盤後檢討。核心策略 Agent 預設關閉 web search；只有 NewsContextAgent 可在 `TW_WATCH_ENABLE_NEWS_CONTEXT=true` 時做受控新聞 / 公告背景查詢，且結果只作 context-only，不得作為策略版本通過依據。

```text
TW_WATCH_ENABLE_CODEX_LLM=false
TW_WATCH_ENABLE_SWING_SELF_CORRECTION=false
TW_WATCH_ENABLE_MULTI_AGENT_REVIEW=false
TW_WATCH_ENABLE_NEWS_CONTEXT=false
TW_WATCH_LLM_BACKEND=codex_cli
TW_WATCH_CODEX_MODEL=gpt-5.5
TW_WATCH_CODEX_TIMEOUT_SECONDS=60
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
```

`TW_WATCH_LLM_BACKEND` 可用 `codex_cli`、`openai_api`、`anthropic_api`。Codex CLI backend 使用 `codex exec --output-schema`；核心 Agent 會帶 `--disable web_search`。OpenAI / Anthropic API backend 會走同一個 JSON backend interface，方便之後切換模型與測試 fake backend。

### 策略版本與多 Agent 檢討

系統會自動初始化 `scout-v1`、`daytrade-v1`、`swing-v1`。每筆候選、委託、成交、持倉、每日檢討與多 Agent proposal 都會記錄當時的策略版本。

多 Agent 檢討會建立 `review_evidence`、`review_runs`、`agent_reviews`、`strategy_proposals`，並把通過 validator / replay / risk gate 的結果寫成 `pending` strategy version。pending 版本不會自動 active；需在「策略版本」分頁選擇策略與版本後按 `套用此版本`，系統才會 promote 成 validated 並手動鎖定啟用。`回到跟隨最新版` 只會跟隨 validated 版本。

舊的 `立即當沖討論` / `立即短線討論` 仍可用於單一角色檢討；完整盤後學習應使用 `多 Agent 檢討` 或設定 `TW_WATCH_ENABLE_MULTI_AGENT_REVIEW=true` 讓收盤日結自動執行。

建立測試資料：

```powershell
.\scripts\seed_demo.ps1 -Reset
```

這會建立獨立 demo DB，包含短線獲利、停損、掛單過期、風控阻擋、多日檢討與多個策略版本，不會污染正式 `trading_lab.sqlite3`。

用假資料啟動桌面程式：

```powershell
.\scripts\run_demo.ps1 -Reset
```

這會把 `TW_WATCH_DB_PATH` 暫時指向 `data/trading_lab_demo.sqlite3` 後啟動 `dist\TwWatchDesk.exe`。正式 DB 不會被覆蓋；關掉 demo 視窗後，平常直接開 `dist\TwWatchDesk.exe` 仍會使用正式 `dist\data\trading_lab.sqlite3`。若要用開發模式測 demo，可執行：

```powershell
.\scripts\run_demo.ps1 -Reset -Dev
```

Demo 模式可測多種立即檢討：

- `立即當沖討論`：使用最近 14 天當沖 demo 成交、委託與風控事件；完成後「每日檢討」會顯示提案狀態 `已檢討`。
- `立即短線討論`：使用最近 14 天短線 demo 成交、委託與風控事件；完成後「每日檢討」會顯示 `不改版` 或建立新版結果，並可在「策略版本」查看短線版本歷史。
- `多 Agent 檢討`：使用同一份 redacted evidence 跑 ScoutAgent / DaytradeAgent / SwingAgent / RiskAgent / CoachAgent；通過後會在「策略版本」看到 `待套用` 版本，並在「每日檢討」看到 `已建立待套用版`。

目前 demo seed 預期包含：當沖 12 筆成交、短線 12 筆成交、當沖與短線每日檢討、多個短線策略版本。若在「每日檢討」看到 `無`，代表你開到正式 DB 或檢討仍在執行中；請確認是用 `scripts\run_demo.ps1 -Reset` 啟動。

## 打包 exe

```powershell
.\scripts\build_exe.ps1
```

輸出：

```text
dist\TwWatchDesk.exe
dist\TwWatchDeskSetup.exe
```

## 移機 / Nova 環境精靈

換電腦或重建環境時，先執行：

```text
dist\TwWatchDeskSetup.exe
```

這個精靈會在同一個視窗完成：

- 檢查並匯入台新憑證檔，預設複製到本機 `data/certs/`。
- 寫入 TwWatchDesk 使用的 `.env.local`。
- 驗證 `taishin_sdk`、Nova `login`、帳戶取得與 realtime 初始化。
- 視需要呼叫 SDK 的 API 權限開通方法確認 / 開通 Nova API 權限。
- 檢查 LLM backend 環境；`codex_cli` 會確認 Codex CLI 在 PATH、可執行 `--version`，且 `codex login status` 已登入；`openai_api` / `anthropic_api` 會確認必要 API key 已設定。
- 檢查 Node.js / npm；缺少時可透過 winget 安裝 Node.js LTS。
- 安裝 / 更新 `@fugle/mcp-server@0.1.1`。
- 寫入 Codex 的 `[mcp_servers.fugle]` 與 `[mcp_servers.fugle.env]` 設定。
- 執行 Fugle MCP `initialize` / `tools/list` smoke test。

安全邊界：

- MCP 設定固定寫入 `ENABLE_ORDER=false`，不開啟真實下單。
- 視窗 log 會遮蔽身分證字號、登入密碼、憑證密碼與憑證路徑。
- 台新憑證下載 / 申請仍走台新官方互動頁面；精靈提供入口並驗證匯出的 `.pfx` / `.p12`，不在背景代填台新網站。

## Nova 設定

開啟程式後按右上角 `台新 API 設定`，可以直接填入並儲存身分證字號、登入密碼、憑證路徑與憑證密碼。登入後會依台新 SDK 回傳結果使用第一個帳戶；第一版不手動輸入交易帳號。設定會寫入 `.env.local`，不要提交到 Git。

Python 版 Nova SDK 不是 PyPI 套件，專案依賴已固定使用台新官方 Windows wheel。執行 `scripts\build_exe.ps1` 時會安裝並打包 `taishin_sdk` 到 `TwWatchDesk.exe` 與 `TwWatchDeskSetup.exe`。

程式也支援手動編輯 `.env.local`，會依序搜尋：

- exe 所在資料夾，例如 `dist\.env.local`
- 目前工作目錄的 `.env.local`
- 如果 exe 在 `dist\`，也會搜尋專案根目錄 `.env.local`

```text
TAISHIN_NOVA_USER=
TAISHIN_NOVA_PASSWORD=
TAISHIN_NOVA_CERT_PATH=
TAISHIN_NOVA_CERT_PASSWORD=
```

## 當沖資格清單

這個清單是可選的，不是啟動自動選股的必要條件。預設讀取：

```text
data/daytrade_eligible_symbols.txt
```

格式可以一行一檔或逗號分隔：

```text
2330
2317,2454
```
