from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from arbitrage import DemoArbitrageEngine, Settings
from arbitrage.hub_universe import HubUniverseBuilder
from arbitrage.market_data import BinanceSbeMarketData
from arbitrage.universe import UniverseBuilder


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.json")
    try:
        settings = Settings.load(config_path, require_api_key=True)
    except (RuntimeError, ValueError, ValidationError, OSError) as error:
        raise SystemExit(str(error)) from error

    if settings.universe_mode == "hub":
        builder = HubUniverseBuilder(
            settings.rest_base_url,
            hub_assets=settings.hub_assets,
            max_symbols=settings.max_symbols,
            max_routes=settings.max_routes,
            timeout_seconds=settings.request_timeout_seconds,
        )
        refresh_message = (
            f"Refreshing the {','.join(settings.hub_assets)} hub universe..."
        )
    else:
        builder = UniverseBuilder(
            settings.rest_base_url,
            anchor_asset=settings.anchor_asset,
            top_n=settings.universe_size,
            min_quote_volume=settings.min_usdc_quote_volume,
            max_symbols=settings.max_symbols,
            max_routes=settings.max_routes,
            volume_rank_start=settings.volume_rank_start,
            min_volume_rank_floor=settings.min_volume_rank_floor,
            max_extended_routes=settings.max_extended_routes,
            timeout_seconds=settings.request_timeout_seconds,
        )
        refresh_message = "Refreshing the USDC volume-ranked universe..."

    while True:
        discovered = builder.discover()
        graph = discovered.graph
        if len(graph.routes) > settings.max_routes:
            raise SystemExit(
                f"Discovered {len(graph.routes)} routes; "
                f"MAX_ROUTES is {settings.max_routes}"
            )
        session_settings = settings.with_symbols(graph.symbols)
        market_data = BinanceSbeMarketData(session_settings)
        engine = DemoArbitrageEngine(
            session_settings,
            market_data,
            graph,
        )
        if settings.universe_mode == "hub":
            hub_summary = ",".join(
                f"{hub}={balance}"
                for hub, balance in settings.hub_starting_balances().items()
            )
            print(
                f"Paper demo (hub mode): hubs={','.join(settings.hub_assets)}; "
                f"inventory={hub_summary}; "
                f"symbols={len(graph.symbols)} routes={len(graph.routes)}; "
                f"crypto_crypto_pairs="
                f"{getattr(discovered, 'crypto_crypto_pairs', 'n/a')}. "
                "Ctrl+C to stop."
            )
        else:
            extended = sum(1 for route in graph.routes if len(route.legs) > 3)
            print(
                f"Paper demo: {session_settings.starting_capital} "
                f"{session_settings.anchor_asset}; "
                f"assets={','.join(graph.selected_assets)}; "
                f"symbols={len(graph.symbols)} routes={len(graph.routes)} "
                f"(extended={extended}); "
                f"volume_rank_floor>={discovered.volume_rank_floor_used}. "
                "Ctrl+C to stop."
            )
        refresh = engine.run(session_settings.discovery_refresh_seconds)
        if not refresh:
            break
        print(refresh_message)


if __name__ == "__main__":
    main()
