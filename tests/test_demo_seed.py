from tw_watchdesk.demo_seed import seed_all
from tw_watchdesk.storage import TradingStore


def test_demo_seed_creates_strategy_versions_and_reviews(tmp_path) -> None:
    store = TradingStore(tmp_path / "demo.sqlite3")
    store.initialize()

    seed_all(store)

    versions = store.list_strategy_versions("swing", limit=None)
    assert [version.version for version in versions[:3]] == ["swing-v3", "swing-v2", "swing-v1"]
    assert store.get_strategy_version_state("swing").active_version == "swing-v3"
    assert any(row["strategy"] == "swing" for row in store.list_daily_reviews(limit=20))
    assert any(row["strategy"] == "daytrade" and row["proposal_status"] == "reviewed" for row in store.list_daily_reviews(limit=30))
    assert any(row["strategy"] == "swing" for row in store.list_fills(limit=50))
    assert any(row["strategy"] == "daytrade" for row in store.list_fills(limit=50))
