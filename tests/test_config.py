from tw_watchdesk.config import load_settings, save_app_settings, save_nova_settings, settings_search_dirs


def test_load_settings_from_env_file(tmp_path) -> None:
    (tmp_path / ".env.local").write_text(
        "\n".join(
            [
                "TW_WATCH_MARKET_DATA_MODE=live",
                "TW_WATCH_POLL_SECONDS=15",
                "TW_WATCH_STALE_SECONDS=30",
                "TW_WATCH_DB_PATH=data/demo.sqlite3",
                "TW_WATCH_ENABLE_AUTO_SCOUT=true",
                "TW_WATCH_AUTO_SCOUT_TIME=09:08",
                "TW_WATCH_SCOUT_MAX_DAYTRADE=3",
                "TW_WATCH_SCOUT_MAX_SWING=4",
                "TW_WATCH_SCOUT_EXCLUDED_SYMBOLS_FILE=data/custom_excluded.txt",
                "TW_WATCH_ENABLE_SWING_SELF_CORRECTION=true",
                "TAISHIN_NOVA_USER=u",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.market_data_mode == "live"
    assert settings.poll_seconds == 15
    assert settings.stale_seconds == 30
    assert settings.db_path == tmp_path / "data/demo.sqlite3"
    assert settings.enable_auto_scout is True
    assert settings.auto_scout_time == "09:08"
    assert settings.scout_max_daytrade == 3
    assert settings.scout_max_swing == 4
    assert settings.scout_excluded_symbols_file == tmp_path / "data/custom_excluded.txt"
    assert settings.enable_swing_self_correction is True
    assert settings.nova_user == "u"


def test_dist_dir_searches_project_root_env(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text("TAISHIN_NOVA_USER=root-user\n", encoding="utf-8")

    settings = load_settings(dist)

    assert settings.nova_user == "root-user"
    assert settings.loaded_env_files == (tmp_path / ".env.local",)
    assert tmp_path in settings_search_dirs(dist)


def test_dist_dir_resolves_relative_db_path_from_project_root(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text("TW_WATCH_DB_PATH=data/trading_lab_demo.sqlite3\n", encoding="utf-8")

    settings = load_settings(dist)

    assert settings.db_path == tmp_path / "data/trading_lab_demo.sqlite3"


def test_process_env_overrides_env_file_for_demo_db(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env.local").write_text("TW_WATCH_DB_PATH=data/formal.sqlite3\n", encoding="utf-8")
    monkeypatch.setenv("TW_WATCH_DB_PATH", "data/trading_lab_demo.sqlite3")

    settings = load_settings(tmp_path)

    assert settings.db_path == tmp_path / "data/trading_lab_demo.sqlite3"


def test_save_nova_settings_preserves_other_values(tmp_path) -> None:
    path = tmp_path / ".env.local"
    path.write_text("TW_WATCH_POLL_SECONDS=15\nTAISHIN_NOVA_USER=old\n", encoding="utf-8")

    save_nova_settings(
        path,
        {
            "TAISHIN_NOVA_USER": "new-user",
            "TAISHIN_NOVA_PASSWORD": "secret",
            "TAISHIN_NOVA_CERT_PATH": r"C:\certs\nova.pfx",
            "TAISHIN_NOVA_CERT_PASSWORD": "cert-secret",
            "TAISHIN_NOVA_QUOTE_WAIT_SECONDS": "9",
        },
    )
    content = path.read_text(encoding="utf-8")
    settings = load_settings(tmp_path)

    assert "TW_WATCH_POLL_SECONDS=15" in content
    assert settings.nova_user == "new-user"
    assert settings.nova_password == "secret"
    assert settings.nova_cert_path == r"C:\certs\nova.pfx"
    assert settings.nova_quote_wait_seconds == 9


def test_save_app_settings_updates_auto_scout(tmp_path) -> None:
    path = tmp_path / ".env.local"
    path.write_text("TAISHIN_NOVA_USER=old\n", encoding="utf-8")

    save_app_settings(
        path,
        {
            "TW_WATCH_ENABLE_AUTO_SCOUT": "true",
            "TW_WATCH_AUTO_SCOUT_TIME": "09:06",
        },
    )

    settings = load_settings(tmp_path)
    assert settings.nova_user == "old"
    assert settings.enable_auto_scout is True
    assert settings.auto_scout_time == "09:06"
