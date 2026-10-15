from arbitrage.route_graph import PairMeta, RouteGraph


def test_build_hub_triangles_generates_all_directed_cycles() -> None:
    pairs = {
        frozenset(("BTC", "ETH")): PairMeta("ETHBTC", "ETH", "BTC"),
        frozenset(("BTC", "ALT")): PairMeta("ALTBTC", "ALT", "BTC"),
        frozenset(("ETH", "ALT")): PairMeta("ALTETH", "ALT", "ETH"),
    }
    graph = RouteGraph.build_hub_triangles(("BTC", "ETH"), pairs)
    assert {route.name for route in graph.routes} == {
        "BTC-ALT-ETH-BTC",
        "BTC-ETH-ALT-BTC",
        "ETH-ALT-BTC-ETH",
        "ETH-BTC-ALT-ETH",
    }
    assert graph.hub_assets == ("BTC", "ETH")
    assert set(graph.symbols) == {"ALTBTC", "ALTETH", "ETHBTC"}


def test_hub_recovery_routes_target_any_hub() -> None:
    pairs = {
        frozenset(("BTC", "ETH")): PairMeta("ETHBTC", "ETH", "BTC"),
        frozenset(("BTC", "ALT")): PairMeta("ALTBTC", "ALT", "BTC"),
        frozenset(("ETH", "ALT")): PairMeta("ALTETH", "ALT", "ETH"),
    }
    graph = RouteGraph.build_hub_triangles(("BTC", "ETH"), pairs)
    assert {route.name for route in graph.recovery_routes("ALT")} == {
        "ALT-BTC-recovery",
        "ALT-ETH-recovery",
    }
    assert graph.recovery_routes("BTC") == ()
    assert {route.name for route in graph.recovery_routes("ALT", "ETH")} == {
        "ALT-ETH-recovery",
    }
