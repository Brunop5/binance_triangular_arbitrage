from arbitrage.models import Side
from arbitrage.route_graph import PairMeta, RouteGraph
from arbitrage.universe import UniverseBuilder


def symbol(name: str, base: str, quote: str) -> dict[str, object]:
    return {
        "symbol": name,
        "baseAsset": base,
        "quoteAsset": quote,
        "status": "TRADING",
        "isSpotTradingAllowed": True,
    }


def test_selects_lower_rank_bridgeable_alts_not_top_volume() -> None:
    # Ranks by USDC volume: BTC/ETH mega, then mid alts, then low alts.
    # Only LOW* have BTC crosses in this fixture; they should be preferred.
    assets = [
        ("BTC", "10000"),
        ("ETH", "9000"),
        ("HOT1", "800"),
        ("HOT2", "700"),
        ("LOW1", "200"),
        ("LOW2", "100"),
    ]
    exchange_info = {
        "symbols": [
            *(symbol(f"{asset}USDC", asset, "USDC") for asset, _ in assets),
            symbol("ETHBTC", "ETH", "BTC"),
            symbol("LOW1BTC", "LOW1", "BTC"),
            symbol("LOW2BTC", "LOW2", "BTC"),
            symbol("HOT1BTC", "HOT1", "BTC"),
            symbol("HOT2BTC", "HOT2", "BTC"),
        ]
    }
    tickers = [
        {"symbol": f"{asset}USDC", "quoteVolume": volume}
        for asset, volume in assets
    ]
    builder = UniverseBuilder(
        "https://example.test",
        volume_rank_start=3,
        min_volume_rank_floor=3,
        max_symbols=10,
        max_extended_routes=0,
    )
    graph, floor = builder.build(exchange_info, tickers)
    assert floor == 3
    assert "HOT1" not in graph.selected_assets or "LOW1" in graph.selected_assets
    assert "LOW1" in graph.selected_assets
    assert "LOW2" in graph.selected_assets
    # Pure BTC-ETH triangle is skipped.
    assert "USDC-BTC-ETH-USDC" not in {route.name for route in graph.routes}
    assert {route.name for route in graph.routes} >= {
        "USDC-LOW1-BTC-USDC",
        "USDC-BTC-LOW1-USDC",
        "USDC-LOW2-BTC-USDC",
        "USDC-BTC-LOW2-USDC",
    }


def test_adds_extended_wraps_for_multi_bridge_alts() -> None:
    pairs = {
        frozenset(("USDC", "ARB")): PairMeta("ARBUSDC", "ARB", "USDC"),
        frozenset(("USDC", "BTC")): PairMeta("BTCUSDC", "BTC", "USDC"),
        frozenset(("USDC", "ETH")): PairMeta("ETHUSDC", "ETH", "USDC"),
        frozenset(("ARB", "BTC")): PairMeta("ARBBTC", "ARB", "BTC"),
        frozenset(("ARB", "ETH")): PairMeta("ARBETH", "ARB", "ETH"),
        frozenset(("BTC", "ETH")): PairMeta("ETHBTC", "ETH", "BTC"),
    }
    graph = RouteGraph.build(
        "USDC",
        ("BTC", "ETH", "ARB"),
        pairs,
        connector_assets=frozenset({"BTC", "ETH", "BNB"}),
        max_extended_routes=8,
    )
    names = {route.name for route in graph.routes}
    assert "USDC-ARB-BTC-USDC" in names
    assert any(name.startswith("USDC-ARB-") and name.count("-") == 5 for name in names)
    five_leg = [
        route for route in graph.routes if len(route.legs) == 5
    ]
    four_leg = [
        route for route in graph.routes if len(route.legs) == 4
    ]
    assert five_leg
    assert four_leg
    sample = next(
        route
        for route in five_leg
        if route.name.startswith("USDC-ARB-")
    )
    assert sample.legs[0].side is Side.BUY
    assert sample.legs[-1].side is Side.SELL


def test_excludes_stables_and_leverage_from_ranking() -> None:
    exchange_info = {
        "symbols": [
            symbol("USDTUSDC", "USDT", "USDC"),
            symbol("BTCUPUSDC", "BTCUP", "USDC"),
            symbol("BTCUSDC", "BTC", "USDC"),
            symbol("ALTUSDC", "ALT", "USDC"),
            symbol("ALTBTC", "ALT", "BTC"),
        ]
    }
    tickers = [
        {"symbol": "USDTUSDC", "quoteVolume": "999999"},
        {"symbol": "BTCUPUSDC", "quoteVolume": "888888"},
        {"symbol": "BTCUSDC", "quoteVolume": "1000"},
        {"symbol": "ALTUSDC", "quoteVolume": "50"},
    ]
    graph, _ = UniverseBuilder(
        "https://example.test",
        volume_rank_start=1,
        min_volume_rank_floor=1,
        max_symbols=10,
        max_extended_routes=0,
    ).build(exchange_info, tickers)
    assert graph.selected_assets == ("BTC", "ALT")
    assert graph.symbols == ("ALTBTC", "ALTUSDC", "BTCUSDC")
