from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .route_graph import PairMeta, RouteGraph
from .universe import DEFAULT_EXCLUDED_ASSETS

DEFAULT_HUB_ASSETS = ("BTC", "ETH", "BNB", "SOL")


@dataclass(frozen=True, slots=True)
class HubUniverseResult:
    graph: RouteGraph
    exchange_info: dict[str, Any]
    crypto_crypto_pairs: int


class HubUniverseBuilder:
    """Discover crypto-crypto spot pairs and all 4-hub directed triads."""

    def __init__(
        self,
        rest_base_url: str,
        hub_assets: tuple[str, ...] = DEFAULT_HUB_ASSETS,
        max_symbols: int = 64,
        max_routes: int = 500,
        excluded_assets: frozenset[str] = DEFAULT_EXCLUDED_ASSETS,
        timeout_seconds: int = 10,
        session: requests.Session | None = None,
    ) -> None:
        self.rest_base_url = rest_base_url.rstrip("/")
        self.hub_assets = tuple(asset.upper() for asset in hub_assets)
        self.max_symbols = max_symbols
        self.max_routes = max_routes
        self.excluded_assets = excluded_assets
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def discover(self) -> HubUniverseResult:
        exchange_info = self._fetch_exchange_info()
        pairs = self._crypto_crypto_pairs(exchange_info)
        graph = RouteGraph.build_hub_triangles(self.hub_assets, pairs)
        if len(graph.routes) > self.max_routes:
            raise RuntimeError(
                f"Discovered {len(graph.routes)} hub routes; "
                f"MAX_ROUTES is {self.max_routes}"
            )
        if len(graph.symbols) > self.max_symbols:
            raise RuntimeError(
                f"Discovered {len(graph.symbols)} crypto-crypto symbols; "
                f"MAX_SYMBOLS is {self.max_symbols}. "
                "Raise MAX_SYMBOLS or reduce HUB_ASSETS."
            )
        if not graph.routes:
            raise RuntimeError(
                f"No hub triangles found for {','.join(self.hub_assets)}"
            )
        return HubUniverseResult(
            graph=graph,
            exchange_info=exchange_info,
            crypto_crypto_pairs=len(pairs),
        )

    def _fetch_exchange_info(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self.rest_base_url}/api/v3/exchangeInfo",
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _crypto_crypto_pairs(
        self, exchange_info: dict[str, Any]
    ) -> dict[frozenset[str], PairMeta]:
        pairs: dict[frozenset[str], PairMeta] = {}
        for info in exchange_info.get("symbols", []):
            if info.get("status") != "TRADING":
                continue
            if not info.get("isSpotTradingAllowed", True):
                continue
            base = info["baseAsset"]
            quote = info["quoteAsset"]
            if base in self.excluded_assets or quote in self.excluded_assets:
                continue
            pairs[frozenset((base, quote))] = PairMeta(
                symbol=info["symbol"],
                base_asset=base,
                quote_asset=quote,
            )
        return pairs
