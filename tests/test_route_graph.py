from arbitrage.models import Side
from arbitrage.route_graph import PairMeta, RouteGraph


def test_generates_both_triangle_directions_with_arbitrary_pair_orientation() -> None:
    pairs = {
        frozenset(("USDC", "A")): PairMeta("AUSDC", "A", "USDC"),
        frozenset(("USDC", "B")): PairMeta("USDCB", "USDC", "B"),
        frozenset(("A", "B")): PairMeta("BA", "B", "A"),
    }
    graph = RouteGraph.build("USDC", ("A", "B"), pairs)
    assert {route.name for route in graph.routes} == {
        "USDC-A-B-USDC",
        "USDC-B-A-USDC",
    }
    forward = next(route for route in graph.routes if route.name == "USDC-A-B-USDC")
    assert [leg.side for leg in forward.legs] == [
        Side.BUY,
        Side.BUY,
        Side.BUY,
    ]
    assert graph.routes_by_symbol["BA"] == graph.routes


def test_skips_pure_connector_triangles() -> None:
    pairs = {
        frozenset(("USDC", "BTC")): PairMeta("BTCUSDC", "BTC", "USDC"),
        frozenset(("USDC", "ETH")): PairMeta("ETHUSDC", "ETH", "USDC"),
        frozenset(("USDC", "ALT")): PairMeta("ALTUSDC", "ALT", "USDC"),
        frozenset(("BTC", "ETH")): PairMeta("ETHBTC", "ETH", "BTC"),
        frozenset(("ALT", "BTC")): PairMeta("ALTBTC", "ALT", "BTC"),
    }
    graph = RouteGraph.build(
        "USDC",
        ("BTC", "ETH", "ALT"),
        pairs,
        connector_assets=frozenset({"BTC", "ETH", "BNB"}),
    )
    names = {route.name for route in graph.routes}
    assert "USDC-BTC-ETH-USDC" not in names
    assert "USDC-ETH-BTC-USDC" not in names
    assert "USDC-ALT-BTC-USDC" in names


def test_recovery_graph_contains_direct_and_two_hop_paths() -> None:
    pairs = {
        frozenset(("USDC", "A")): PairMeta("AUSDC", "A", "USDC"),
        frozenset(("USDC", "B")): PairMeta("BUSDC", "B", "USDC"),
        frozenset(("A", "B")): PairMeta("AB", "A", "B"),
    }
    graph = RouteGraph.build("USDC", ("A", "B"), pairs)
    assert {route.name for route in graph.recovery_routes("A")} == {
        "A-USDC-recovery",
        "A-B-USDC-recovery",
    }
