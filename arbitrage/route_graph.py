from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .models import DirectedLeg, Route, Side


@dataclass(frozen=True, slots=True)
class PairMeta:
    symbol: str
    base_asset: str
    quote_asset: str

    def leg(self, from_asset: str, to_asset: str) -> DirectedLeg:
        if from_asset == self.base_asset and to_asset == self.quote_asset:
            return DirectedLeg(
                self.symbol, Side.SELL, from_asset, to_asset
            )
        if from_asset == self.quote_asset and to_asset == self.base_asset:
            return DirectedLeg(
                self.symbol, Side.BUY, from_asset, to_asset
            )
        raise ValueError(
            f"{self.symbol} cannot convert {from_asset} to {to_asset}"
        )


@dataclass(frozen=True, slots=True)
class RouteGraph:
    anchor_asset: str
    selected_assets: tuple[str, ...]
    routes: tuple[Route, ...]
    symbols: tuple[str, ...]
    pairs: dict[frozenset[str], PairMeta]
    routes_by_symbol: dict[str, tuple[Route, ...]]
    hub_assets: tuple[str, ...] = ()

    def pair(self, asset_a: str, asset_b: str) -> PairMeta | None:
        return self.pairs.get(frozenset((asset_a, asset_b)))

    def conversion_leg(
        self, from_asset: str, to_asset: str
    ) -> DirectedLeg | None:
        pair = self.pair(from_asset, to_asset)
        return pair.leg(from_asset, to_asset) if pair else None

    def route_hub(self, route: Route) -> str:
        return route.legs[0].from_asset

    def recovery_targets(self, target_hub: str | None = None) -> tuple[str, ...]:
        if target_hub:
            return (target_hub,)
        if self.hub_assets:
            return self.hub_assets
        return (self.anchor_asset,)

    def recovery_routes(
        self, asset: str, target_hub: str | None = None
    ) -> tuple[Route, ...]:
        targets = self.recovery_targets(target_hub)
        if asset in targets:
            return ()
        candidates: list[Route] = []
        seen: set[str] = set()
        for hub in targets:
            direct = self.conversion_leg(asset, hub)
            if direct:
                name = f"{asset}-{hub}-recovery"
                if name not in seen:
                    seen.add(name)
                    candidates.append(Route(name, (direct,)))
            for via in self.selected_assets:
                if via in (asset, hub):
                    continue
                first = self.conversion_leg(asset, via)
                second = self.conversion_leg(via, hub)
                if first and second:
                    name = f"{asset}-{via}-{hub}-recovery"
                    if name not in seen:
                        seen.add(name)
                        candidates.append(
                            Route(
                                name,
                                (first, second),
                            )
                        )
        return tuple(candidates)

    @classmethod
    def build(
        cls,
        anchor_asset: str,
        selected_assets: tuple[str, ...],
        pairs: dict[frozenset[str], PairMeta],
        *,
        connector_assets: frozenset[str] = frozenset(),
        max_extended_routes: int = 0,
    ) -> RouteGraph:
        routes: list[Route] = []
        for asset_a, asset_b in combinations(selected_assets, 2):
            # Skip pure mega-connector triangles (e.g. BTC-ETH-USDC);
            # those have been untradable in paper soaks.
            if connector_assets and {asset_a, asset_b} <= connector_assets:
                continue
            anchor_a = pairs.get(frozenset((anchor_asset, asset_a)))
            anchor_b = pairs.get(frozenset((anchor_asset, asset_b)))
            cross = pairs.get(frozenset((asset_a, asset_b)))
            if not (anchor_a and anchor_b and cross):
                continue
            routes.extend(
                (
                    Route(
                        f"{anchor_asset}-{asset_a}-{asset_b}-{anchor_asset}",
                        (
                            anchor_a.leg(anchor_asset, asset_a),
                            cross.leg(asset_a, asset_b),
                            anchor_b.leg(asset_b, anchor_asset),
                        ),
                    ),
                    Route(
                        f"{anchor_asset}-{asset_b}-{asset_a}-{anchor_asset}",
                        (
                            anchor_b.leg(anchor_asset, asset_b),
                            cross.leg(asset_b, asset_a),
                            anchor_a.leg(asset_a, anchor_asset),
                        ),
                    ),
                )
            )
        if max_extended_routes > 0:
            routes.extend(
                cls._extended_routes(
                    anchor_asset,
                    selected_assets,
                    pairs,
                    connector_assets=connector_assets,
                    limit=max_extended_routes,
                )
            )
        routes.sort(key=lambda route: route.name)
        symbols = tuple(
            sorted({leg.symbol for route in routes for leg in route.legs})
        )
        by_symbol: dict[str, list[Route]] = {symbol: [] for symbol in symbols}
        for route in routes:
            for symbol in {leg.symbol for leg in route.legs}:
                by_symbol[symbol].append(route)
        return cls(
            anchor_asset=anchor_asset,
            selected_assets=selected_assets,
            routes=tuple(routes),
            symbols=symbols,
            pairs=pairs,
            routes_by_symbol={
                symbol: tuple(symbol_routes)
                for symbol, symbol_routes in by_symbol.items()
            },
            hub_assets=(),
        )

    @classmethod
    def build_hub_triangles(
        cls,
        hub_assets: tuple[str, ...],
        pairs: dict[frozenset[str], PairMeta],
    ) -> RouteGraph:
        """All directed H->A->B->H cycles for each hub H over crypto-crypto pairs."""
        hub_set = frozenset(hub_assets)
        assets = sorted(
            {
                asset
                for pair in pairs.values()
                for asset in (pair.base_asset, pair.quote_asset)
            }
        )
        routes: list[Route] = []
        for hub in hub_assets:
            for asset_a in assets:
                if asset_a == hub:
                    continue
                leg_ha = pairs.get(frozenset((hub, asset_a)))
                if leg_ha is None:
                    continue
                for asset_b in assets:
                    if asset_b in (hub, asset_a):
                        continue
                    leg_ab = pairs.get(frozenset((asset_a, asset_b)))
                    leg_bh = pairs.get(frozenset((asset_b, hub)))
                    if leg_ab is None or leg_bh is None:
                        continue
                    routes.append(
                        Route(
                            f"{hub}-{asset_a}-{asset_b}-{hub}",
                            (
                                leg_ha.leg(hub, asset_a),
                                leg_ab.leg(asset_a, asset_b),
                                leg_bh.leg(asset_b, hub),
                            ),
                        )
                    )
        routes.sort(key=lambda route: route.name)
        symbols = tuple(
            sorted({leg.symbol for route in routes for leg in route.legs})
        )
        by_symbol: dict[str, list[Route]] = {symbol: [] for symbol in symbols}
        for route in routes:
            for symbol in {leg.symbol for leg in route.legs}:
                by_symbol[symbol].append(route)
        selected_assets = tuple(
            sorted(asset for asset in assets if asset not in hub_set)
        )
        return cls(
            anchor_asset=hub_assets[0],
            hub_assets=hub_assets,
            selected_assets=selected_assets,
            routes=tuple(routes),
            symbols=symbols,
            pairs=pairs,
            routes_by_symbol={
                symbol: tuple(symbol_routes)
                for symbol, symbol_routes in by_symbol.items()
            },
        )

    @classmethod
    def _extended_routes(
        cls,
        anchor_asset: str,
        selected_assets: tuple[str, ...],
        pairs: dict[frozenset[str], PairMeta],
        *,
        connector_assets: frozenset[str],
        limit: int,
    ) -> list[Route]:
        """Add a few 4-leg and 5-leg USDC-wrapped cycles for worthy alts.

        4-leg: USDC -> A -> B -> C -> USDC
        5-leg: USDC -> A -> B -> C -> A -> USDC
        """
        alts = [
            asset
            for asset in selected_assets
            if asset not in connector_assets
        ]
        # Prefer alts that touch multiple connectors (more path options).
        scored: list[tuple[int, str]] = []
        for alt in alts:
            bridges = sum(
                1
                for connector in connector_assets
                if frozenset((alt, connector)) in pairs
            )
            if bridges and frozenset((alt, anchor_asset)) in pairs:
                scored.append((bridges, alt))
        scored.sort(reverse=True)

        built: list[Route] = []
        seen: set[str] = set()

        def add(route: Route) -> bool:
            if route.name in seen:
                return False
            seen.add(route.name)
            built.append(route)
            return len(built) >= limit

        for _, entry in scored:
            if len(built) >= limit:
                break
            for mid_a, mid_b in combinations(connector_assets, 2):
                if len(built) >= limit:
                    break
                if not (
                    frozenset((entry, mid_a)) in pairs
                    and frozenset((mid_a, mid_b)) in pairs
                    and frozenset((mid_b, entry)) in pairs
                ):
                    continue
                # 5-leg wrap returning through the entry alt.
                for first, second in ((mid_a, mid_b), (mid_b, mid_a)):
                    legs = (
                        pairs[frozenset((anchor_asset, entry))].leg(
                            anchor_asset, entry
                        ),
                        pairs[frozenset((entry, first))].leg(entry, first),
                        pairs[frozenset((first, second))].leg(first, second),
                        pairs[frozenset((second, entry))].leg(second, entry),
                        pairs[frozenset((entry, anchor_asset))].leg(
                            entry, anchor_asset
                        ),
                    )
                    if add(
                        Route(
                            f"{anchor_asset}-{entry}-{first}-{second}-"
                            f"{entry}-{anchor_asset}",
                            legs,
                        )
                    ):
                        return built
                # 4-leg exit through a USDC-listed connector when possible.
                for first, second in ((mid_a, mid_b), (mid_b, mid_a)):
                    if frozenset((second, anchor_asset)) not in pairs:
                        continue
                    legs = (
                        pairs[frozenset((anchor_asset, entry))].leg(
                            anchor_asset, entry
                        ),
                        pairs[frozenset((entry, first))].leg(entry, first),
                        pairs[frozenset((first, second))].leg(first, second),
                        pairs[frozenset((second, anchor_asset))].leg(
                            second, anchor_asset
                        ),
                    )
                    if add(
                        Route(
                            f"{anchor_asset}-{entry}-{first}-{second}-"
                            f"{anchor_asset}",
                            legs,
                        )
                    ):
                        return built
        return built
