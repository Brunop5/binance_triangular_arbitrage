from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

DEFAULT_CONFIG_PATH = Path("config.json")


class Settings(BaseModel):
    """Runtime settings loaded from JSON; API key comes from the environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: str = ""
    universe_mode: Literal["usdc", "hub"] = "usdc"
    anchor_asset: str = "USDC"
    hub_assets: tuple[str, ...] = ("BTC", "ETH", "BNB", "SOL")
    starting_capital: Decimal = Decimal("20")
    starting_btc: Decimal = Decimal("0.0003")
    starting_eth: Decimal = Decimal("0.005")
    starting_bnb: Decimal = Decimal("0.03")
    starting_sol: Decimal = Decimal("0.1")
    universe_size: int = 0
    min_usdc_quote_volume: Decimal = Decimal("0")
    max_capital_utilization: Decimal = Decimal("0.95")
    taker_fee_rate: Decimal = Decimal("0.00075")
    min_net_profit_rate: Decimal = Decimal("0.0005")
    max_slippage_rate: Decimal = Decimal("0.001")
    max_recovery_slippage_rate: Decimal = Decimal("0.005")
    max_stream_silence_ms: int = 250
    simulated_latency_ms: int = 40
    max_cycles_per_episode: int = 100
    cycle_cooldown_ms: int = 100
    cycle_refresh_timeout_ms: int = 500
    max_recovery_rounds: int = 100
    snapshot_limit: int = 100
    snapshot_concurrency: int = 16
    eval_coalesce_ms: int = 10
    inbound_queue_size: int = 1024
    max_apply_lag_ms: int = 250
    resync_cooldown_ms: int = 1000
    max_depth_quotes_per_batch: int = 8
    max_symbols: int = 64
    max_routes: int = 500
    volume_rank_start: int = 70
    min_volume_rank_floor: int = 25
    max_extended_routes: int = 12
    discovery_refresh_seconds: int = 86400
    metrics_interval_seconds: int = 300
    request_timeout_seconds: int = 10
    results_dir: Path = Path("results")

    symbols: tuple[str, ...] = ()
    sbe_url: str = ""
    sbe_base_url: str = "wss://stream-sbe.binance.com:9443/stream"
    rest_base_url: str = "https://api.binance.com"

    @field_validator("universe_mode", mode="before")
    @classmethod
    def _normalize_universe_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("anchor_asset", mode="before")
    @classmethod
    def _normalize_anchor(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("hub_assets", mode="before")
    @classmethod
    def _normalize_hub_assets(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [
                part.strip().upper()
                for part in value.split(",")
                if part.strip()
            ]
        if isinstance(value, (list, tuple)):
            return [str(item).strip().upper() for item in value if str(item).strip()]
        return value

    @model_validator(mode="before")
    @classmethod
    def _resolve_hub_anchor(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        mode = str(data.get("universe_mode", "usdc")).strip().lower()
        hubs = data.get("hub_assets", ("BTC", "ETH", "BNB", "SOL"))
        if isinstance(hubs, str):
            hubs = [
                part.strip().upper()
                for part in hubs.split(",")
                if part.strip()
            ]
        elif isinstance(hubs, (list, tuple)):
            hubs = [
                str(item).strip().upper()
                for item in hubs
                if str(item).strip()
            ]
        if mode == "hub" and hubs:
            return {**data, "anchor_asset": hubs[0]}
        return data

    @model_validator(mode="after")
    def _validate_settings(self) -> Self:
        if self.universe_mode == "usdc":
            if self.anchor_asset != "USDC":
                raise ValueError("USDC mode requires anchor_asset=USDC")
            if self.starting_capital <= 0:
                raise ValueError("starting_capital must be positive")
        else:
            if not self.hub_assets:
                raise ValueError("hub_assets must list at least one hub asset")
            if len(set(self.hub_assets)) != len(self.hub_assets):
                raise ValueError("hub_assets must not contain duplicates")
            if not any(
                balance > 0
                for balance in self.hub_starting_balances().values()
            ):
                raise ValueError(
                    "Hub mode requires at least one positive "
                    "starting_btc/eth/bnb/sol balance"
                )
        if self.universe_size == 1 or self.universe_size < 0:
            raise ValueError("universe_size must be 0 (auto) or at least 2")
        if self.min_usdc_quote_volume < 0:
            raise ValueError("min_usdc_quote_volume cannot be negative")
        if not Decimal("0") < self.max_capital_utilization <= Decimal("1"):
            raise ValueError("max_capital_utilization must be in (0, 1]")
        for name, value in (
            ("taker_fee_rate", self.taker_fee_rate),
            ("min_net_profit_rate", self.min_net_profit_rate),
            ("max_slippage_rate", self.max_slippage_rate),
            (
                "max_recovery_slippage_rate",
                self.max_recovery_slippage_rate,
            ),
        ):
            if value < 0 or value >= 1:
                raise ValueError(f"{name} must be in [0, 1)")
        if self.max_stream_silence_ms <= 0:
            raise ValueError("max_stream_silence_ms must be positive")
        if self.simulated_latency_ms < 0:
            raise ValueError("simulated_latency_ms cannot be negative")
        if self.max_cycles_per_episode <= 0:
            raise ValueError("max_cycles_per_episode must be positive")
        if self.cycle_refresh_timeout_ms <= 0:
            raise ValueError("cycle_refresh_timeout_ms must be positive")
        if self.max_recovery_rounds <= 0:
            raise ValueError("max_recovery_rounds must be positive")
        for name, value in (
            ("snapshot_concurrency", self.snapshot_concurrency),
            ("cycle_cooldown_ms", self.cycle_cooldown_ms),
            ("eval_coalesce_ms", self.eval_coalesce_ms),
            ("inbound_queue_size", self.inbound_queue_size),
            ("max_apply_lag_ms", self.max_apply_lag_ms),
            ("resync_cooldown_ms", self.resync_cooldown_ms),
            ("max_depth_quotes_per_batch", self.max_depth_quotes_per_batch),
            ("max_symbols", self.max_symbols),
            ("max_routes", self.max_routes),
            ("volume_rank_start", self.volume_rank_start),
            ("min_volume_rank_floor", self.min_volume_rank_floor),
            ("max_extended_routes", self.max_extended_routes),
            ("discovery_refresh_seconds", self.discovery_refresh_seconds),
            ("metrics_interval_seconds", self.metrics_interval_seconds),
            ("request_timeout_seconds", self.request_timeout_seconds),
            ("snapshot_limit", self.snapshot_limit),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.min_volume_rank_floor > self.volume_rank_start:
            raise ValueError(
                "min_volume_rank_floor cannot exceed volume_rank_start"
            )
        return self

    @classmethod
    def load(
        cls,
        path: Path | str = DEFAULT_CONFIG_PATH,
        *,
        require_api_key: bool = True,
    ) -> Settings:
        load_dotenv()
        config_path = Path(path)
        if not config_path.is_file():
            raise RuntimeError(
                f"Config file not found: {config_path}. "
                "Copy config.example.json to config.json and edit it."
            )
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Invalid JSON in {config_path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise RuntimeError(f"{config_path} must contain a JSON object")
        if "api_key" in raw:
            raise RuntimeError(
                f"{config_path} must not contain api_key; "
                "set BINANCE_API_KEY in the environment or .env instead"
            )

        api_key = os.getenv("BINANCE_API_KEY", "").strip()
        if require_api_key and not api_key:
            raise RuntimeError(
                "BINANCE_API_KEY is required for Binance's 20 ms SBE feed. "
                "Create a fresh Ed25519 API key and export it in your shell "
                "or .env file."
            )
        return cls.model_validate({**raw, "api_key": api_key})

    def hub_starting_balances(self) -> dict[str, Decimal]:
        defaults = {
            "BTC": self.starting_btc,
            "ETH": self.starting_eth,
            "BNB": self.starting_bnb,
            "SOL": self.starting_sol,
        }
        return {
            hub: defaults[hub]
            for hub in self.hub_assets
            if hub in defaults and defaults[hub] > 0
        }

    def with_symbols(self, symbols: tuple[str, ...]) -> Settings:
        if len(symbols) > self.max_symbols:
            raise ValueError(
                f"Discovered {len(symbols)} symbols; limit is "
                f"{self.max_symbols}"
            )
        streams = "/".join(f"{symbol.lower()}@depth" for symbol in symbols)
        url = f"{self.sbe_base_url}?streams={streams}"
        if len(url) > 16_000:
            raise ValueError(f"SBE stream URL is too long ({len(url)} bytes)")
        return self.model_copy(update={"symbols": symbols, "sbe_url": url})
