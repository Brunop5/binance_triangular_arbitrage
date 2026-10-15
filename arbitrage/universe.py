from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import sleep
from typing import Any

import requests

from .route_graph import PairMeta, RouteGraph

DEFAULT_EXCLUDED_ASSETS = frozenset(
    {
        "USDC",
        "USDT",
        "FDUSD",
        "TUSD",
        "BUSD",
        "DAI",
        "USDP",
        "U",
        "USD1",
        "USDE",
        "USDS",
        "PYUSD",
        "RLUSD",
        "XUSD",
        "USTC",
        "BFUSD",
        "EUR",
        "AEUR",
        "TRY",
        "BRL",
        "GBP",
        "JPY",
        "RUB",
        "UAH",
    }
)
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
DEFAULT_CONNECTORS = ("BTC", "ETH", "BNB")


@dataclass(frozen=True, slots=True)
class UniverseResult:
    graph: RouteGraph
    exchange_info: dict[str, Any]
    volume_rank_floor_used: int = 0


class UniverseBuilder:
    """Build a lower-liquidity USDC universe with optional long wraps.

    USDC-quoted assets are ranked by 24h quote volume (most → least). Alts are
    taken from ``volume_rank_start`` and later (e.g. place 70+), never from the
    mega-liquid head. BTC/ETH/BNB are included only as bridges when an alt has
    a cross market against them — pure connector triangles are skipped.
    """

    def __init__(
        self,
        rest_base_url: str,
        anchor_asset: str = "USDC",
        top_n: int = 0,
        min_quote_volume: Decimal = Decimal("0"),
        core_assets: tuple[str, ...] = DEFAULT_CONNECTORS,
        max_symbols: int = 40,
        max_routes: int = 500,
        max_stream_url_length: int = 16_000,
        volume_rank_start: int = 70,
        min_volume_rank_floor: int = 25,
        max_extended_routes: int = 12,
        excluded_assets: frozenset[str] = DEFAULT_EXCLUDED_ASSETS,
        timeout_seconds: int = 10,
        session: requests.Session | None = None,
    ) -> None:
        self.rest_base_url = rest_base_url.rstrip("/")
        self.anchor_asset = anchor_asset.upper()
        self.top_n = top_n
        self.min_quote_volume = min_quote_volume
        self.core_assets = core_assets
        self.max_symbols = max_symbols
        self.max_routes = max_routes
        self.max_stream_url_length = max_stream_url_length
        self.volume_rank_start = volume_rank_start
        self.min_volume_rank_floor = min_volume_rank_floor
        self.max_extended_routes = max_extended_routes
        self.excluded_assets = excluded_assets | {self.anchor_asset}
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def discover(self) -> UniverseResult:
        exchange_info = self._get_json("/api/v3/exchangeInfo")
        tickers = self._get_json("/api/v3/ticker/24hr")
        graph, floor_used = self.build(exchange_info, tickers)
        return UniverseResult(
            graph=graph,
            exchange_info=exchange_info,
            volume_rank_floor_used=floor_used,
        )

    def build(
        self,
        exchange_info: dict[str, Any],
        tickers: list[dict[str, Any]],
    ) -> tuple[RouteGraph, int]:
        active = {
            info["symbol"]: info
            for info in exchange_info["symbols"]
            if info.get("status") == "TRADING"
            and info.get("isSpotTradingAllowed", True)
        }
        volumes: list[tuple[Decimal, str]] = []
        for ticker in tickers:
            info = active.get(ticker.get("symbol", ""))
            if not info or info.get("quoteAsset") != self.anchor_asset:
                continue
            asset = info["baseAsset"]
            if self._excluded(asset):
                continue
            try:
                volume = Decimal(ticker.get("quoteVolume", "0"))
            except InvalidOperation:
                continue
            if volume < self.min_quote_volume:
                continue
            volumes.append((volume, asset))
        volumes.sort(key=lambda item: (-item[0], item[1]))
        if not volumes:
            raise RuntimeError(f"No {self.anchor_asset} markets found")

        rank = {asset: index + 1 for index, (_, asset) in enumerate(volumes)}
        connectors = frozenset(
            asset
            for asset in self.core_assets
            if any(asset == name for _, name in volumes)
        )
        allowed = {asset for _, asset in volumes} | {self.anchor_asset}
        all_pairs: dict[frozenset[str], PairMeta] = {}
        for info in active.values():
            base = info["baseAsset"]
            quote = info["quoteAsset"]
            if base not in allowed or quote not in allowed or base == quote:
                continue
            key = frozenset((base, quote))
            all_pairs[key] = PairMeta(info["symbol"], base, quote)

        bridgeable: list[tuple[int, str, tuple[str, ...]]] = []
        for _, asset in volumes:
            if asset in connectors:
                continue
            if frozenset((asset, self.anchor_asset)) not in all_pairs:
                continue
            bridges = tuple(
                connector
                for connector in self.core_assets
                if connector in connectors
                and frozenset((asset, connector)) in all_pairs
            )
            if bridges:
                bridgeable.append((rank[asset], asset, bridges))

        # Prefer the least-liquid bridgeable alts at/after the configured rank.
        # If that cannot fill near max_symbols, walk the floor toward more liquid
        # ranks until the symbol budget is used, but never into the hard head.
        preferred = max(self.volume_rank_start, 1)
        hard_floor = max(self.min_volume_rank_floor, 1)
        best: tuple[RouteGraph, int] | None = None
        last_error = "No bridgeable lower-liquidity alts found"
        target_symbols = max(self.max_symbols - 2, 1)
        for floor in range(preferred, hard_floor - 1, -1):
            pool = [
                (asset_rank, asset, bridges)
                for asset_rank, asset, bridges in bridgeable
                if asset_rank >= floor
            ]
            # Least volume first: highest rank numbers first.
            pool.sort(key=lambda item: (-item[0], item[1]))
            graph = self._fill_graph(pool, connectors, all_pairs)
            if graph is None:
                last_error = (
                    f"No triangle-capable universe at volume rank >={floor}"
                )
                continue
            stream_url_length = 55 + sum(
                len(symbol) + len("@depth/") for symbol in graph.symbols
            )
            if (
                len(graph.symbols) > self.max_symbols
                or len(graph.routes) > self.max_routes
                or stream_url_length > self.max_stream_url_length
            ):
                last_error = (
                    f"Universe at rank >={floor} exceeded limits "
                    f"(symbols={len(graph.symbols)} routes={len(graph.routes)})"
                )
                continue
            if best is None or len(graph.symbols) > len(best[0].symbols):
                best = (graph, floor)
            if len(graph.symbols) >= target_symbols:
                return graph, floor
        if best is not None:
            return best
        raise RuntimeError(last_error)

    def _fill_graph(
        self,
        pool: list[tuple[int, str, tuple[str, ...]]],
        connectors: frozenset[str],
        all_pairs: dict[frozenset[str], PairMeta],
    ) -> RouteGraph | None:
        chosen_alts: list[str] = []
        used_connectors: set[str] = set()
        best: RouteGraph | None = None
        for _, asset, bridges in pool:
            trial_alts = chosen_alts + [asset]
            trial_connectors = used_connectors | set(bridges)
            selected = tuple(
                dict.fromkeys((*sorted(trial_connectors), *trial_alts))
            )
            selected_set = set(selected) | {self.anchor_asset}
            pairs = {
                key: pair
                for key, pair in all_pairs.items()
                if pair.base_asset in selected_set
                and pair.quote_asset in selected_set
            }
            graph = RouteGraph.build(
                self.anchor_asset,
                selected,
                pairs,
                connector_assets=connectors,
                max_extended_routes=self.max_extended_routes,
            )
            if not graph.routes:
                continue
            if len(graph.symbols) > self.max_symbols:
                continue
            if len(graph.routes) > self.max_routes:
                continue
            chosen_alts.append(asset)
            used_connectors = trial_connectors
            best = graph
            # Stop once we are near the symbol budget.
            if len(graph.symbols) >= max(self.max_symbols - 2, 1):
                break
        return best

    def _excluded(self, asset: str) -> bool:
        return (
            not asset.isascii()
            or not asset.isalnum()
            or asset in self.excluded_assets
            or asset.endswith(LEVERAGED_SUFFIXES)
        )

    def _get_json(self, path: str) -> Any:
        for attempt in range(3):
            try:
                response = self.session.get(
                    f"{self.rest_base_url}{path}",
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                if attempt == 2:
                    raise
                sleep(2**attempt)
        raise RuntimeError("unreachable")
