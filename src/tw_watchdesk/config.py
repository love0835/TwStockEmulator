from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


NOVA_ENV_KEYS = (
    "TW_WATCH_MARKET_DATA_MODE",
    "TAISHIN_NOVA_USER",
    "TAISHIN_NOVA_PASSWORD",
    "TAISHIN_NOVA_CERT_PATH",
    "TAISHIN_NOVA_CERT_PASSWORD",
    "TAISHIN_NOVA_QUOTE_WAIT_SECONDS",
)
APP_ENV_KEYS = NOVA_ENV_KEYS + (
    "TW_WATCH_DB_PATH",
    "TW_WATCH_ENABLE_AUTO_SCOUT",
    "TW_WATCH_ENABLE_MULTI_AGENT_REVIEW",
    "TW_WATCH_ENABLE_NEWS_CONTEXT",
    "TW_WATCH_AUTO_SCOUT_TIME",
    "TW_WATCH_SCOUT_MAX_DAYTRADE",
    "TW_WATCH_SCOUT_MAX_SWING",
    "TW_WATCH_SCOUT_EXCLUDED_SYMBOLS_FILE",
    "TW_WATCH_ENABLE_SWING_SELF_CORRECTION",
)


@dataclass
class Settings:
    market_data_mode: str = "live"
    timezone: str = "Asia/Taipei"
    poll_seconds: int = 60
    stale_seconds: int = 70
    db_path: Path | None = None
    codex_model: str = "gpt-5.5"
    codex_timeout_seconds: int = 60
    llm_backend: str = "codex_cli"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    enable_codex_llm: bool = False
    enable_multi_agent_review: bool = False
    enable_news_context: bool = False
    enable_auto_scout: bool = False
    enable_swing_self_correction: bool = False
    auto_scout_time: str = "09:05"
    scout_max_daytrade: int = 5
    scout_max_swing: int = 5
    scout_excluded_symbols_file: Path | None = None
    daytrade_eligible_symbols_file: Path | None = None
    nova_user: str = ""
    nova_password: str = ""
    nova_cert_path: str = ""
    nova_cert_password: str = ""
    nova_quote_wait_seconds: float = 8.0
    loaded_env_files: tuple[Path, ...] = ()


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def load_settings(base_dir: Path | None = None) -> Settings:
    base_dir = base_dir or app_base_dir()
    env: dict[str, str] = {}
    loaded: list[Path] = []
    for folder in _settings_dirs(base_dir):
        folder_loaded = False
        for name in (".env", ".env.local"):
            path = folder / name
            values = _read_env_file(path)
            if values:
                env.update(values)
                loaded.append(path)
                folder_loaded = True
        if folder_loaded:
            break
    env.update(os.environ)
    daytrade_file = env.get("TW_WATCH_DAYTRADE_ELIGIBLE_SYMBOLS_FILE", "data/daytrade_eligible_symbols.txt").strip()
    scout_excluded_file = env.get("TW_WATCH_SCOUT_EXCLUDED_SYMBOLS_FILE", "data/scout_excluded_symbols.txt").strip()
    db_file = env.get("TW_WATCH_DB_PATH", "").strip()
    return Settings(
        market_data_mode=env.get("TW_WATCH_MARKET_DATA_MODE", "live").strip().lower(),
        timezone=env.get("TW_WATCH_TIMEZONE", "Asia/Taipei").strip(),
        poll_seconds=_int(env.get("TW_WATCH_POLL_SECONDS"), 60),
        stale_seconds=_int(env.get("TW_WATCH_STALE_SECONDS"), 70),
        db_path=_resolve_path(base_dir, db_file) if db_file else None,
        codex_model=env.get("TW_WATCH_CODEX_MODEL", "gpt-5.5").strip(),
        codex_timeout_seconds=_int(env.get("TW_WATCH_CODEX_TIMEOUT_SECONDS"), 60),
        llm_backend=env.get("TW_WATCH_LLM_BACKEND", "codex_cli").strip().lower(),
        openai_api_key=env.get("OPENAI_API_KEY", "").strip(),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
        enable_codex_llm=_bool(env.get("TW_WATCH_ENABLE_CODEX_LLM"), False),
        enable_multi_agent_review=_bool(env.get("TW_WATCH_ENABLE_MULTI_AGENT_REVIEW"), False),
        enable_news_context=_bool(env.get("TW_WATCH_ENABLE_NEWS_CONTEXT"), False),
        enable_auto_scout=_bool(env.get("TW_WATCH_ENABLE_AUTO_SCOUT"), False),
        enable_swing_self_correction=_bool(env.get("TW_WATCH_ENABLE_SWING_SELF_CORRECTION"), False),
        auto_scout_time=env.get("TW_WATCH_AUTO_SCOUT_TIME", "09:05").strip(),
        scout_max_daytrade=_int(env.get("TW_WATCH_SCOUT_MAX_DAYTRADE"), 5),
        scout_max_swing=_int(env.get("TW_WATCH_SCOUT_MAX_SWING"), 5),
        scout_excluded_symbols_file=_resolve_path(base_dir, scout_excluded_file) if scout_excluded_file else None,
        daytrade_eligible_symbols_file=_resolve_path(base_dir, daytrade_file) if daytrade_file else None,
        nova_user=env.get("TAISHIN_NOVA_USER", "").strip(),
        nova_password=env.get("TAISHIN_NOVA_PASSWORD", "").strip(),
        nova_cert_path=env.get("TAISHIN_NOVA_CERT_PATH", "").strip(),
        nova_cert_password=env.get("TAISHIN_NOVA_CERT_PASSWORD", "").strip(),
        nova_quote_wait_seconds=_float(env.get("TAISHIN_NOVA_QUOTE_WAIT_SECONDS"), 8.0),
        loaded_env_files=tuple(loaded),
    )


def settings_search_dirs(base_dir: Path | None = None) -> list[Path]:
    return _settings_dirs(base_dir or app_base_dir())


def default_settings_file(base_dir: Path | None = None) -> Path:
    base_dir = base_dir or app_base_dir()
    if base_dir.name.lower() == "dist":
        return base_dir.parent / ".env.local"
    return base_dir / ".env.local"


def nova_settings_file(settings: Settings | None = None, base_dir: Path | None = None) -> Path:
    if settings and settings.loaded_env_files:
        for path in settings.loaded_env_files:
            if path.name == ".env.local":
                return path
        return settings.loaded_env_files[0]
    return default_settings_file(base_dir)


def save_nova_settings(path: Path, values: dict[str, str]) -> None:
    updates = {key: values.get(key, "") for key in NOVA_ENV_KEYS if key in values}
    if "TW_WATCH_MARKET_DATA_MODE" not in updates:
        updates["TW_WATCH_MARKET_DATA_MODE"] = "live"
    _merge_env_file(path, updates)


def save_app_settings(path: Path, values: dict[str, str]) -> None:
    updates = {key: values.get(key, "") for key in APP_ENV_KEYS if key in values}
    _merge_env_file(path, updates)


def _settings_dirs(base_dir: Path) -> list[Path]:
    candidates = [
        base_dir,
        Path.cwd(),
    ]
    if base_dir.name.lower() == "dist":
        candidates.append(base_dir.parent)
    candidates.append(base_dir.parent)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = str(resolved).lower()
        if key not in seen:
            result.append(resolved)
            seen.add(key)
    return result


def _merge_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _value = line.split("=", 1)
        clean_key = key.strip()
        if clean_key in updates:
            output.append(f"{clean_key}={_escape_env_value(updates[clean_key])}")
            seen.add(clean_key)
        else:
            output.append(line)
    missing = [key for key in updates if key not in seen]
    if missing and output and output[-1].strip():
        output.append("")
    for key in missing:
        output.append(f"{key}={_escape_env_value(updates[key])}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _escape_env_value(value: str) -> str:
    value = value.replace("\n", "").replace("\r", "").strip()
    if any(char.isspace() for char in value) or "#" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if base_dir.name.lower() == "dist":
        return base_dir.parent / path
    return base_dir / path


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
