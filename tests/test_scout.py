import pytest

from tw_watchdesk.config import Settings
from tw_watchdesk.scout import NovaRestScoutDataProvider, ScoutStock, select_candidates
from tw_watchdesk.strategy_versions import default_scout_params


class StaticProvider:
    def __init__(self, stocks: list[ScoutStock]) -> None:
        self.stocks = stocks

    def fetch_universe(self) -> list[ScoutStock]:
        return self.stocks


def _stock(symbol: str, *, name: str = "測試股", change_pct: float = 0.03, volume: float = 100_000, turnover: float = 500_000_000) -> ScoutStock:
    return ScoutStock(
        symbol=symbol,
        name=name,
        market="TSE",
        price=100,
        previous_close=100 / (1 + change_pct),
        change_pct=change_pct,
        volume=volume,
        turnover=turnover,
        bid_price=99.9,
        ask_price=100.0,
        source_tags=("unit",),
    )


def test_select_candidates_filters_and_scores(tmp_path) -> None:
    eligible = tmp_path / "eligible.txt"
    eligible.write_text("2330\n", encoding="utf-8")
    excluded = tmp_path / "excluded.txt"
    excluded.write_text("2317\n", encoding="utf-8")
    settings = Settings(daytrade_eligible_symbols_file=eligible, scout_excluded_symbols_file=excluded)

    result = select_candidates(
        settings,
        StaticProvider(
            [
                _stock("2330", name="台積電"),
                _stock("2317", name="鴻海"),
                _stock("0050", name="元大台灣50 ETF"),
                _stock("2303", name="聯電", volume=0),
            ]
        ),
    )

    assert [pick.symbol for pick in result.daytrade] == ["2330"]
    assert [pick.symbol for pick in result.swing] == ["2330"]
    assert result.excluded_counts["手動排除"] == 1
    assert result.excluded_counts["排除 ETF/ETN"] == 1
    assert result.excluded_counts["缺量"] == 1


def test_select_candidates_daytrade_runs_without_eligible_file() -> None:
    result = select_candidates(
        Settings(daytrade_eligible_symbols_file=None),
        StaticProvider(
            [
                _stock("2330", name="台積電", change_pct=0.02, volume=200_000, turnover=1_000_000_000),
                _stock("2317", name="鴻海", change_pct=0.01, volume=100_000, turnover=800_000_000),
            ]
        ),
    )

    assert [pick.symbol for pick in result.daytrade] == ["2330", "2317"]
    assert result.notes == ("當沖資格清單未設定，使用 Nova COMMONSTOCK 普通股排行選股",)


def test_select_candidates_uses_scout_strategy_params() -> None:
    params = default_scout_params()
    strict = type(params)(
        **{
            **params.to_json(),
            "min_turnover": 900_000_000,
            "max_candidates_daytrade": 1,
            "max_candidates_swing": 1,
        }
    )

    result = select_candidates(
        Settings(daytrade_eligible_symbols_file=None, scout_max_daytrade=5, scout_max_swing=5),
        StaticProvider(
            [
                _stock("2330", turnover=1_000_000_000),
                _stock("2317", turnover=500_000_000),
                _stock("2303", turnover=950_000_000),
            ]
        ),
        strict,
    )

    assert [pick.symbol for pick in result.daytrade] == ["2330"]
    assert [pick.symbol for pick in result.swing] == ["2330"]
    assert result.excluded_counts["成交值低於抓盤門檻"] == 1


def test_nova_rest_provider_parses_snapshot_rows() -> None:
    calls = []

    class Snapshot:
        def actives(self, **params):
            calls.append(("actives", params))
            return {"data": [{"symbol": "2330", "name": "台積電", "price": 100, "previousClose": 98, "volume": 1000, "turnover": 100_000, "changePercent": 2.04}]}

        def movers(self, **params):
            calls.append(("movers", params))
            return {"data": [{"symbol": "2317", "name": "鴻海", "price": 200, "previousClose": 198, "volume": 900, "turnover": 180_000, "changePercent": 1.01}]}

    class StockClient:
        snapshot = Snapshot()

    class Provider:
        def get_stock_rest_client(self):
            return StockClient()

    rows = NovaRestScoutDataProvider(Provider()).fetch_universe()

    assert {row.symbol for row in rows} == {"2330", "2317"}
    assert rows[0].market in {"TSE", "OTC"}
    assert ("actives", {"market": "TSE", "trade": "value", "type": "COMMONSTOCK"}) in calls
    assert ("actives", {"market": "TSE", "trade": "volume", "type": "COMMONSTOCK"}) in calls
    assert ("movers", {"market": "TSE", "direction": "up", "change": "percent", "type": "COMMONSTOCK"}) in calls


def test_nova_rest_provider_parses_documented_snapshot_fields() -> None:
    class Snapshot:
        def actives(self, **params):
            return {"data": [{"symbol": "2330", "name": "台積電", "closePrice": 568, "change": 2, "changePercent": 0.35, "tradeVolume": 54538, "tradeValue": 31_019_803_000}]}

        def movers(self, **params):
            return {"data": []}

    class StockClient:
        snapshot = Snapshot()

    class Provider:
        def get_stock_rest_client(self):
            return StockClient()

    rows = NovaRestScoutDataProvider(Provider()).fetch_universe()

    assert len(rows) == 1
    assert rows[0].symbol == "2330"
    assert rows[0].price == 568
    assert rows[0].previous_close == 566
    assert rows[0].volume == 54538
    assert rows[0].turnover == 31_019_803_000
    assert rows[0].change_pct == pytest.approx(0.0035)
