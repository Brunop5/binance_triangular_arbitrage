from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from time import time_ns

ZERO = Decimal("0")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class FillStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    symbol: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    update_id: int
    event_time_us: int
    received_ns: int = field(default_factory=time_ns)

    @property
    def best_bid(self) -> Level:
        if not self.bids:
            raise ValueError(f"{self.symbol} has no bids")
        return self.bids[0]

    @property
    def best_ask(self) -> Level:
        if not self.asks:
            raise ValueError(f"{self.symbol} has no asks")
        return self.asks[0]

    def age_ms(self, now_ns: int | None = None) -> Decimal:
        current = time_ns() if now_ns is None else now_ns
        return Decimal(current - self.received_ns) / Decimal("1000000")


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None = None
    min_price: Decimal = ZERO
    max_price: Decimal = Decimal("Infinity")


@dataclass(frozen=True, slots=True)
class DirectedLeg:
    symbol: str
    side: Side
    from_asset: str
    to_asset: str


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    legs: tuple[DirectedLeg, ...]


@dataclass(frozen=True, slots=True)
class OrderIntent:
    opportunity_id: str
    cycle_number: int
    leg_number: int
    leg: DirectedLeg
    input_amount: Decimal
    order_quantity: Decimal
    limit_price: Decimal
    recovery: bool = False


@dataclass(frozen=True, slots=True)
class FillResult:
    status: FillStatus
    symbol: str
    side: Side
    requested_input: Decimal
    input_spent: Decimal
    gross_output: Decimal
    net_output: Decimal
    average_price: Decimal
    fee_amount: Decimal
    fee_asset: str
    level_count: int
    reason: str = ""

    @property
    def completed(self) -> bool:
        return self.status is FillStatus.FILLED


@dataclass(frozen=True, slots=True)
class PathQuote:
    route: Route
    input_amount: Decimal
    output_amount: Decimal
    complete: bool
    fills: tuple[FillResult, ...]

    @property
    def return_rate(self) -> Decimal:
        if self.input_amount == 0:
            return ZERO
        return self.output_amount / self.input_amount


@dataclass(frozen=True, slots=True)
class Opportunity:
    route: Route
    input_amount: Decimal
    expected_output: Decimal
    detected_at: datetime = field(default_factory=utc_now)

    @property
    def expected_pnl(self) -> Decimal:
        return self.expected_output - self.input_amount

    @property
    def expected_return_rate(self) -> Decimal:
        if self.input_amount == 0:
            return ZERO
        return self.expected_output / self.input_amount - Decimal("1")


@dataclass(frozen=True, slots=True)
class RatioRecord:
    route: Route
    gross_ratio: Decimal
    fee_adjusted_ratio: Decimal
    executable_ratio: Decimal
    input_amount: Decimal
    output_amount: Decimal


@dataclass(slots=True)
class CandidateAudit:
    audit_id: str
    route_name: str
    detected_at: datetime
    starting_amount: Decimal
    gross_ratio: Decimal
    fee_adjusted_ratio: Decimal
    immediate_output: Decimal = ZERO
    delayed_output: Decimal = ZERO
    flags: set[str] = field(default_factory=set)
    ended_at: datetime | None = None

    @property
    def projected_pnl(self) -> Decimal:
        return self.starting_amount * (
            self.fee_adjusted_ratio - Decimal("1")
        )

    @property
    def immediate_pnl(self) -> Decimal:
        return self.immediate_output - self.starting_amount

    @property
    def delayed_pnl(self) -> Decimal:
        return self.delayed_output - self.starting_amount


@dataclass(slots=True)
class TheoreticalOpportunityMetrics:
    opportunity_id: str
    route_name: str
    started_at: datetime
    started_monotonic_ns: int
    start_gross_ratio: Decimal
    start_fee_adjusted_ratio: Decimal
    samples: int = 0
    max_gross_ratio: Decimal = ZERO
    max_fee_adjusted_ratio: Decimal = ZERO
    max_executable_ratio: Decimal = ZERO
    bot_executable_at_least_once: bool = False
    ended_at: datetime | None = None
    duration_ms: Decimal = ZERO
    end_reason: str = ""

    def observe(
        self,
        gross_ratio: Decimal,
        fee_adjusted_ratio: Decimal,
        executable_ratio: Decimal,
        bot_executable: bool,
    ) -> None:
        self.samples += 1
        self.max_gross_ratio = max(self.max_gross_ratio, gross_ratio)
        self.max_fee_adjusted_ratio = max(
            self.max_fee_adjusted_ratio, fee_adjusted_ratio
        )
        self.max_executable_ratio = max(self.max_executable_ratio, executable_ratio)
        self.bot_executable_at_least_once = (
            self.bot_executable_at_least_once or bot_executable
        )

    def finish(self, monotonic_ns: int, reason: str) -> None:
        self.ended_at = utc_now()
        self.duration_ms = Decimal(monotonic_ns - self.started_monotonic_ns) / Decimal(
            "1000000"
        )
        self.end_reason = reason


@dataclass(slots=True)
class CycleResult:
    completed: bool
    recovered: bool
    start_anchor: Decimal
    end_anchor: Decimal
    normal_fills: list[FillResult] = field(default_factory=list)
    recovery_fills: list[FillResult] = field(default_factory=list)
    dust: dict[str, Decimal] = field(default_factory=dict)
    recovery_loss_anchor: Decimal = ZERO

    @property
    def pnl(self) -> Decimal:
        return self.end_anchor - self.start_anchor


@dataclass(slots=True)
class EpisodeMetrics:
    opportunity_id: str
    route_name: str
    started_at: datetime
    ended_at: datetime | None = None
    starting_anchor: Decimal = ZERO
    ending_anchor: Decimal = ZERO
    starting_usdc_cash: Decimal = ZERO
    ending_usdc_cash: Decimal = ZERO
    cycles_attempted: int = 0
    cycles_completed: int = 0
    cycles_recovered: int = 0
    cycles_failed: int = 0
    normal_trade_count: int = 0
    recovery_trade_count: int = 0
    partial_fill_count: int = 0
    total_fees_anchor: Decimal = ZERO
    recovery_loss_anchor: Decimal = ZERO
    cycle_pnls: list[Decimal] = field(default_factory=list)
    ending_inventory: dict[str, Decimal] = field(default_factory=dict)

    @property
    def completion_rate(self) -> Decimal:
        if not self.cycles_attempted:
            return ZERO
        return (
            Decimal(self.cycles_completed)
            / Decimal(self.cycles_attempted)
            * Decimal("100")
        )

    @property
    def partial_fill_rate(self) -> Decimal:
        trades = self.normal_trade_count + self.recovery_trade_count
        if not trades:
            return ZERO
        return Decimal(self.partial_fill_count) / Decimal(trades) * Decimal("100")

    @property
    def net_pnl(self) -> Decimal:
        return self.ending_anchor - self.starting_anchor

    @property
    def net_pnl_rate(self) -> Decimal:
        if self.starting_anchor == 0:
            return ZERO
        return self.net_pnl / self.starting_anchor * Decimal("100")

    @property
    def clean_pnl(self) -> Decimal:
        return self.ending_usdc_cash - self.starting_usdc_cash

    @property
    def clean_pnl_rate(self) -> Decimal:
        if self.starting_anchor == 0:
            return ZERO
        return self.clean_pnl / self.starting_anchor * Decimal("100")

    @property
    def average_cycle_pnl(self) -> Decimal:
        if not self.cycle_pnls:
            return ZERO
        return sum(self.cycle_pnls, ZERO) / Decimal(len(self.cycle_pnls))

    def finish(
        self,
        ending_anchor: Decimal,
        inventory: Mapping[str, Decimal],
        ending_usdc_cash: Decimal,
    ) -> None:
        self.ended_at = utc_now()
        self.ending_anchor = ending_anchor
        self.ending_usdc_cash = ending_usdc_cash
        self.ending_inventory = dict(inventory)
