from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import Settings
from .market_data import BinanceSbeMarketData
from .models import (
    ZERO,
    BookSnapshot,
    CandidateAudit,
    CycleResult,
    DirectedLeg,
    EpisodeMetrics,
    FillResult,
    FillStatus,
    Opportunity,
    OrderIntent,
    PathQuote,
    RatioRecord,
    Route,
    Side,
    SymbolRules,
    TheoreticalOpportunityMetrics,
    utc_now,
)
from .route_graph import RouteGraph


@dataclass(slots=True)
class _ShadowAudit:
    audit: CandidateAudit
    route: Route
    balances: dict[str, Decimal]
    leg_index: int
    pending_intent: OrderIntent
    due_ns: int


def floor_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("Increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def ceil_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("Increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


class SequentialArbitrageEngine(ABC):
    """All strategy and recovery logic; subclasses only provide execution/I/O."""

    def __init__(
        self,
        settings: Settings,
        market_data: BinanceSbeMarketData,
        route_graph: RouteGraph,
        rules: dict[str, SymbolRules] | None = None,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.route_graph = route_graph
        self.rules = rules or {}
        tracked_assets = {
            *self._hub_assets(),
            *route_graph.selected_assets,
        }
        self.balances: dict[str, Decimal] = {
            asset: ZERO for asset in tracked_assets
        }
        if route_graph.hub_assets:
            for hub, amount in settings.hub_starting_balances().items():
                self.balances[hub] = amount
        else:
            self.balances[settings.anchor_asset] = settings.starting_capital
        self.routes = route_graph.routes
        self._best_ratio_seen = ZERO
        self._theoretical_opportunities: dict[str, TheoreticalOpportunityMetrics] = {}
        self._shadow_audits: dict[str, _ShadowAudit] = {}

    def _hub_assets(self) -> frozenset[str]:
        if self.route_graph.hub_assets:
            return frozenset(self.route_graph.hub_assets)
        return frozenset({self.settings.anchor_asset})

    def _route_hub(self, route: Route) -> str:
        return self.route_graph.route_hub(route)

    def _route_budget(self, route: Route) -> Decimal:
        hub = self._route_hub(route)
        return self.balances.get(hub, ZERO) * self.settings.max_capital_utilization

    def run(self, refresh_after_seconds: int | None = None) -> bool:
        if not self.rules:
            self.rules = self.market_data.fetch_symbol_rules()
        self.market_data.start()
        started = time.monotonic()
        last_metrics = started
        refresh_requested = False
        try:
            for batch in self.market_data.batches():
                now = time.monotonic()
                if (
                    refresh_after_seconds is not None
                    and now - started >= refresh_after_seconds
                ):
                    refresh_requested = True
                    break
                if now - last_metrics >= self.settings.metrics_interval_seconds:
                    self.report_market_data_metrics(
                        self.market_data.metrics_snapshot()
                    )
                    last_metrics = now
                if not self._books_ready_and_fresh():
                    continue
                opportunity = self.find_best_opportunity(batch.dirty_symbols)
                if opportunity:
                    self._run_opportunity_episode(opportunity)
        except KeyboardInterrupt:
            return False
        finally:
            reason = "universe_refresh" if refresh_requested else "shutdown"
            self._finish_all_theoretical_opportunities(reason)
            self._finish_shadow_audits()
            self.market_data.stop()
        return refresh_requested

    def find_best_opportunity(
        self, dirty_symbols: frozenset[str] | set[str] | None = None
    ) -> Opportunity | None:
        self._advance_shadow_audits()
        if dirty_symbols is None:
            routes = self.routes
        else:
            affected = {
                route
                for symbol in dirty_symbols
                for route in self.route_graph.routes_by_symbol.get(symbol, ())
            }
            routes = tuple(affected)
        route_ratios: list[tuple[Route, Decimal]] = []
        books_by_route: dict[str, dict[str, BookSnapshot]] = {}
        for route in routes:
            if self._route_budget(route) <= 0:
                continue
            try:
                books = {
                    leg.symbol: self.market_data.latest(leg.symbol)
                    for leg in route.legs
                }
            except RuntimeError:
                continue
            books_by_route[route.name] = books
            route_ratios.append((route, self._top_of_book_ratio(route, books)))
        if not route_ratios:
            return None

        def fee_adjusted(route: Route, gross: Decimal) -> Decimal:
            return gross * self._fee_multiplier(len(route.legs))

        depth_candidates = sorted(
            route_ratios,
            key=lambda item: fee_adjusted(item[0], item[1]),
            reverse=True,
        )
        quotes: list[PathQuote] = []
        for route, gross_ratio in depth_candidates[
            : self.settings.max_depth_quotes_per_batch
        ]:
            legs = Decimal(len(route.legs))
            near_threshold = Decimal("1") - (
                self.settings.max_slippage_rate * legs
            )
            if fee_adjusted(route, gross_ratio) < near_threshold and quotes:
                break
            quote = self._largest_complete_quote(
                route,
                self._route_budget(route),
                books_by_route[route.name],
            )
            if quote and quote.complete:
                quotes.append(quote)
        quotes_by_route = {quote.route.name: quote for quote in quotes}

        for route, gross_ratio in route_ratios:
            fee_adjusted_ratio = fee_adjusted(route, gross_ratio)
            self._observe_theoretical_opportunity(
                route,
                gross_ratio,
                fee_adjusted_ratio,
                quotes_by_route.get(route.name),
            )

        best_route, gross_ratio = max(route_ratios, key=lambda item: item[1])
        if gross_ratio > self._best_ratio_seen:
            self._best_ratio_seen = gross_ratio
            executable = quotes_by_route.get(best_route.name)
            fee_adjusted_ratio = fee_adjusted(best_route, gross_ratio)
            self.report_ratio_record(
                RatioRecord(
                    route=best_route,
                    gross_ratio=gross_ratio,
                    fee_adjusted_ratio=fee_adjusted_ratio,
                    executable_ratio=(executable.return_rate if executable else ZERO),
                    input_amount=(executable.input_amount if executable else ZERO),
                    output_amount=(executable.output_amount if executable else ZERO),
                )
            )

        candidates = [
            Opportunity(
                route=quote.route,
                input_amount=quote.input_amount,
                expected_output=quote.output_amount,
            )
            for quote in quotes
            if quote.return_rate - Decimal("1") >= self.settings.min_net_profit_rate
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.expected_return_rate)

    def _fee_multiplier(self, legs: int) -> Decimal:
        return (Decimal("1") - self.settings.taker_fee_rate) ** legs

    @staticmethod
    def _top_of_book_ratio(route: Route, books: dict[str, BookSnapshot]) -> Decimal:
        ratio = Decimal("1")
        for leg in route.legs:
            book = books[leg.symbol]
            if leg.side is Side.BUY:
                ratio /= book.best_ask.price
            else:
                ratio *= book.best_bid.price
        return ratio

    def _observe_theoretical_opportunity(
        self,
        route: Route,
        gross_ratio: Decimal,
        fee_adjusted_ratio: Decimal,
        quote: PathQuote | None,
    ) -> None:
        active = self._theoretical_opportunities.get(route.name)
        if fee_adjusted_ratio <= Decimal("1"):
            if active is not None:
                active.finish(time.monotonic_ns(), "ratio_closed")
                self.report_theoretical_opportunity_ended(active)
                del self._theoretical_opportunities[route.name]
            return

        executable_ratio = quote.return_rate if quote else ZERO
        bot_executable = (
            quote is not None
            and executable_ratio - Decimal("1") >= self.settings.min_net_profit_rate
        )
        if active is None:
            active = TheoreticalOpportunityMetrics(
                opportunity_id=uuid.uuid4().hex,
                route_name=route.name,
                started_at=utc_now(),
                started_monotonic_ns=time.monotonic_ns(),
                start_gross_ratio=gross_ratio,
                start_fee_adjusted_ratio=fee_adjusted_ratio,
            )
            self._theoretical_opportunities[route.name] = active
            self.report_theoretical_opportunity_started(active)
            self._start_candidate_audit(
                active.opportunity_id,
                route,
                gross_ratio,
                fee_adjusted_ratio,
            )
        active.observe(
            gross_ratio,
            fee_adjusted_ratio,
            executable_ratio,
            bot_executable,
        )

    def _finish_all_theoretical_opportunities(self, reason: str) -> None:
        now_ns = time.monotonic_ns()
        for metrics in tuple(self._theoretical_opportunities.values()):
            metrics.finish(now_ns, reason)
            self.report_theoretical_opportunity_ended(metrics)
        self._theoretical_opportunities.clear()

    def _start_candidate_audit(
        self,
        audit_id: str,
        route: Route,
        gross_ratio: Decimal,
        fee_adjusted_ratio: Decimal,
    ) -> None:
        starting = self._route_budget(route)
        if starting <= 0 or route.name in self._shadow_audits:
            return
        audit = CandidateAudit(
            audit_id=audit_id,
            route_name=route.name,
            detected_at=utc_now(),
            starting_amount=starting,
            gross_ratio=gross_ratio,
            fee_adjusted_ratio=fee_adjusted_ratio,
        )
        audit.immediate_output = self._immediate_audit_output(
            audit, route, starting
        )
        balances = {
            asset: ZERO
            for asset in (
                self.settings.anchor_asset,
                *self.route_graph.selected_assets,
            )
        }
        balances[self.settings.anchor_asset] = starting
        try:
            book = self.market_data.latest(route.legs[0].symbol)
        except RuntimeError:
            audit.flags.add("STALE_BOOK")
            audit.delayed_output = starting
            self._report_finished_audit(audit)
            return
        intent = self._build_intent(
            audit_id,
            0,
            1,
            route.legs[0],
            starting,
            book,
        )
        if intent is None:
            audit.flags.add("ROUNDING_OR_FILTER_FAILURE")
            audit.delayed_output = starting
            self._report_finished_audit(audit)
            return
        self._shadow_audits[route.name] = _ShadowAudit(
            audit=audit,
            route=route,
            balances=balances,
            leg_index=0,
            pending_intent=intent,
            due_ns=time.monotonic_ns()
            + self.settings.simulated_latency_ms * 1_000_000,
        )

    def _immediate_audit_output(
        self, audit: CandidateAudit, route: Route, starting: Decimal
    ) -> Decimal:
        balances = {self.settings.anchor_asset: starting}
        current = starting
        for index, leg in enumerate(route.legs, start=1):
            balances.setdefault(leg.from_asset, ZERO)
            balances.setdefault(leg.to_asset, ZERO)
            try:
                book = self.market_data.latest(leg.symbol)
            except RuntimeError:
                audit.flags.add("STALE_BOOK")
                return self._shadow_recovery_value(audit, balances)
            intent = self._build_intent(
                audit.audit_id, 0, index, leg, current, book
            )
            if intent is None:
                audit.flags.add("ROUNDING_OR_FILTER_FAILURE")
                return self._shadow_recovery_value(audit, balances)
            fill = self.calculate_fill(intent, book)
            balances[leg.from_asset] -= fill.input_spent
            balances[leg.to_asset] += fill.net_output
            if not fill.completed:
                audit.flags.add("DEPTH_ILLUSION")
                if fill.status is FillStatus.PARTIAL:
                    audit.flags.add("PARTIAL_FILL_RISK")
                return self._shadow_recovery_value(audit, balances)
            current = fill.net_output
        output = balances.get(self.settings.anchor_asset, ZERO)
        if output <= starting:
            audit.flags.add("DEPTH_ILLUSION")
        return output

    def _advance_shadow_audits(self) -> None:
        now_ns = time.monotonic_ns()
        for route_name, state in tuple(self._shadow_audits.items()):
            if now_ns < state.due_ns:
                continue
            leg = state.route.legs[state.leg_index]
            try:
                book = self.market_data.latest(leg.symbol)
            except RuntimeError:
                state.audit.flags.add("EXPIRED_BEFORE_ARRIVAL")
                self._finish_failed_shadow(route_name, state)
                continue
            fill = self.calculate_fill(state.pending_intent, book)
            state.balances[leg.from_asset] -= fill.input_spent
            state.balances[leg.to_asset] = (
                state.balances.get(leg.to_asset, ZERO) + fill.net_output
            )
            if not fill.completed:
                if fill.status is FillStatus.PARTIAL:
                    state.audit.flags.add("PARTIAL_FILL_RISK")
                else:
                    state.audit.flags.add("EXPIRED_BEFORE_ARRIVAL")
                self._finish_failed_shadow(route_name, state)
                continue
            state.leg_index += 1
            if state.leg_index == len(state.route.legs):
                state.audit.delayed_output = state.balances.get(
                    self.settings.anchor_asset, ZERO
                )
                if state.audit.delayed_output <= state.audit.starting_amount:
                    state.audit.flags.add("LATENCY_NEGATIVE")
                del self._shadow_audits[route_name]
                self._report_finished_audit(state.audit)
                continue
            next_leg = state.route.legs[state.leg_index]
            current = state.balances.get(next_leg.from_asset, ZERO)
            try:
                next_book = self.market_data.latest(next_leg.symbol)
            except RuntimeError:
                state.audit.flags.add("EXPIRED_BEFORE_ARRIVAL")
                self._finish_failed_shadow(route_name, state)
                continue
            intent = self._build_intent(
                state.audit.audit_id,
                0,
                state.leg_index + 1,
                next_leg,
                current,
                next_book,
            )
            if intent is None:
                state.audit.flags.add("ROUNDING_OR_FILTER_FAILURE")
                self._finish_failed_shadow(route_name, state)
                continue
            state.pending_intent = intent
            state.due_ns = now_ns + (
                self.settings.simulated_latency_ms * 1_000_000
            )

    def _finish_failed_shadow(
        self, route_name: str, state: _ShadowAudit
    ) -> None:
        state.audit.delayed_output = self._shadow_recovery_value(
            state.audit, state.balances
        )
        del self._shadow_audits[route_name]
        self._report_finished_audit(state.audit)

    def _shadow_recovery_value(
        self, audit: CandidateAudit, balances: dict[str, Decimal]
    ) -> Decimal:
        hubs = self._hub_assets()
        total = sum(balances.get(hub, ZERO) for hub in hubs)
        for asset, amount in balances.items():
            if asset in hubs or amount <= 0:
                continue
            best = ZERO
            for route in self.route_graph.recovery_routes(asset):
                try:
                    books = {
                        leg.symbol: self.market_data.latest(leg.symbol)
                        for leg in route.legs
                    }
                except RuntimeError:
                    continue
                quote = self.quote_route(route, amount, books, recovery=True)
                if quote.complete:
                    best = max(best, quote.output_amount)
            if best == ZERO:
                audit.flags.add("RECOVERY_UNAVAILABLE")
            total += best
        if total < audit.starting_amount:
            audit.flags.add("RECOVERY_NEGATIVE")
        return total

    def _report_finished_audit(self, audit: CandidateAudit) -> None:
        audit.ended_at = utc_now()
        self.report_candidate_audit(audit)

    def _finish_shadow_audits(self) -> None:
        for state in tuple(self._shadow_audits.values()):
            state.audit.flags.add("SHADOW_INCOMPLETE")
            state.audit.delayed_output = self._shadow_recovery_value(
                state.audit, state.balances
            )
            self._report_finished_audit(state.audit)
        self._shadow_audits.clear()

    def _largest_complete_quote(
        self,
        route: Route,
        budget: Decimal,
        books: dict[str, BookSnapshot],
    ) -> PathQuote | None:
        candidate = budget
        minimum = max(
            (self.rules[leg.symbol].min_notional for leg in route.legs),
            default=ZERO,
        )
        best: PathQuote | None = None
        for _ in range(16):
            quote = self.quote_route(route, candidate, books)
            if quote.complete:
                best = quote
                break
            candidate /= Decimal("2")
            if candidate < minimum:
                break
        return best

    def quote_route(
        self,
        route: Route,
        input_amount: Decimal,
        books: dict[str, BookSnapshot] | None = None,
        *,
        recovery: bool = False,
    ) -> PathQuote:
        current = input_amount
        actual_start = ZERO
        fills: list[FillResult] = []
        snapshots = books or self.market_data.all_latest()
        for index, leg in enumerate(route.legs, start=1):
            intent = self._build_intent(
                opportunity_id="quote",
                cycle_number=0,
                leg_number=index,
                leg=leg,
                input_amount=current,
                book=snapshots[leg.symbol],
                recovery=recovery,
            )
            if intent is None:
                return PathQuote(
                    route, actual_start or input_amount, ZERO, False, tuple(fills)
                )
            fill = self.calculate_fill(intent, snapshots[leg.symbol])
            fills.append(fill)
            if index == 1:
                actual_start = fill.input_spent
            if not fill.completed:
                return PathQuote(
                    route,
                    actual_start or input_amount,
                    fill.net_output,
                    False,
                    tuple(fills),
                )
            current = fill.net_output
        return PathQuote(route, actual_start, current, True, tuple(fills))

    def _run_opportunity_episode(self, first: Opportunity) -> None:
        opportunity_id = uuid.uuid4().hex
        route_hub = self._route_hub(first.route)
        starting_equity = self.portfolio_value_anchor()
        metrics = EpisodeMetrics(
            opportunity_id=opportunity_id,
            route_name=first.route.name,
            started_at=utc_now(),
            starting_anchor=starting_equity,
            starting_usdc_cash=self.balances.get(route_hub, ZERO),
        )
        opportunity = first

        for cycle_number in range(1, self.settings.max_cycles_per_episode + 1):
            metrics.cycles_attempted += 1
            result = self._execute_cycle(opportunity_id, cycle_number, opportunity)
            self._accumulate_cycle_metrics(metrics, result)
            if not result.completed:
                break
            if cycle_number >= self.settings.max_cycles_per_episode:
                break
            next_opportunity = self._prepare_next_cycle(
                opportunity_id, first.route, cycle_number
            )
            if next_opportunity is None:
                break
            opportunity = next_opportunity

        metrics.finish(
            self.portfolio_value_anchor(),
            self.balances,
            self.balances.get(route_hub, ZERO),
        )
        self.report_episode(metrics)
        if metrics.normal_trade_count > 0:
            self.report_big_pnl_result(metrics)

    def _prepare_next_cycle(
        self, opportunity_id: str, route: Route, cycle_number: int
    ) -> Opportunity | None:
        time.sleep(self.settings.cycle_cooldown_ms / 1000)
        if not self._books_ready_and_fresh(route):
            self._recover_with_loss(opportunity_id, cycle_number)
            return None
        previous_ids = self._route_update_ids(route)
        if not self._wait_for_route_refresh(route, previous_ids):
            self._recover_with_loss(opportunity_id, cycle_number)
            return None
        opportunity = self._find_route_opportunity(route)
        if opportunity is None:
            if not self._only_anchor_or_dust():
                self._recover_with_loss(opportunity_id, cycle_number)
            return None
        return opportunity

    def _find_route_opportunity(self, route: Route) -> Opportunity | None:
        available = self._route_budget(route)
        if available <= 0:
            return None
        try:
            books = {
                leg.symbol: self.market_data.latest(leg.symbol)
                for leg in route.legs
            }
        except RuntimeError:
            return None
        quote = self._largest_complete_quote(route, available, books)
        if not quote or not quote.complete:
            return None
        if quote.return_rate - Decimal("1") < self.settings.min_net_profit_rate:
            return None
        return Opportunity(
            route=route,
            input_amount=quote.input_amount,
            expected_output=quote.output_amount,
        )

    def _execute_cycle(
        self,
        opportunity_id: str,
        cycle_number: int,
        opportunity: Opportunity,
    ) -> CycleResult:
        start_equity = self.portfolio_value_anchor()
        normal_fills: list[FillResult] = []
        route_hub = self._route_hub(opportunity.route)
        current = min(
            opportunity.input_amount,
            self._route_budget(opportunity.route),
        )

        for leg_number, leg in enumerate(opportunity.route.legs, start=1):
            if not self._books_ready_and_fresh(opportunity.route):
                recovery, recovery_loss = self._recover_with_loss(
                    opportunity_id, cycle_number
                )
                return CycleResult(
                    completed=False,
                    recovered=self._only_anchor_or_dust(),
                    start_anchor=start_equity,
                    end_anchor=self.portfolio_value_anchor(),
                    normal_fills=normal_fills,
                    recovery_fills=recovery,
                    dust=self._dust_inventory(),
                    recovery_loss_anchor=recovery_loss,
                )

            book = self.market_data.latest(leg.symbol)
            intent = self._build_intent(
                opportunity_id,
                cycle_number,
                leg_number,
                leg,
                current,
                book,
            )
            if intent is None:
                recovery, recovery_loss = self._recover_with_loss(
                    opportunity_id, cycle_number
                )
                return CycleResult(
                    completed=False,
                    recovered=self._only_anchor_or_dust(),
                    start_anchor=start_equity,
                    end_anchor=self.portfolio_value_anchor(),
                    normal_fills=normal_fills,
                    recovery_fills=recovery,
                    dust=self._dust_inventory(),
                    recovery_loss_anchor=recovery_loss,
                )

            fill = self.execute_order(intent)
            normal_fills.append(fill)
            self._apply_fill(leg, fill)
            self.record_fill(intent, fill)
            if not fill.completed:
                recovery, recovery_loss = self._recover_with_loss(
                    opportunity_id, cycle_number
                )
                return CycleResult(
                    completed=False,
                    recovered=self._only_anchor_or_dust(),
                    start_anchor=start_equity,
                    end_anchor=self.portfolio_value_anchor(),
                    normal_fills=normal_fills,
                    recovery_fills=recovery,
                    dust=self._dust_inventory(),
                    recovery_loss_anchor=recovery_loss,
                )
            current = fill.net_output

        return CycleResult(
            completed=True,
            recovered=False,
            start_anchor=start_equity,
            end_anchor=self.portfolio_value_anchor(),
            normal_fills=normal_fills,
            dust=self._dust_inventory(),
        )

    def _recover_with_loss(
        self, opportunity_id: str, cycle_number: int
    ) -> tuple[list[FillResult], Decimal]:
        before_recovery = self.portfolio_value_anchor()
        fills = self._recover_to_anchor(opportunity_id, cycle_number)
        after_recovery = self.portfolio_value_anchor()
        return fills, max(before_recovery - after_recovery, ZERO)

    def _recover_to_anchor(
        self, opportunity_id: str, cycle_number: int
    ) -> list[FillResult]:
        recovery_fills: list[FillResult] = []
        for recovery_round in range(1, self.settings.max_recovery_rounds + 1):
            made_progress = False
            for asset in self.route_graph.selected_assets:
                amount = self.balances[asset]
                if amount <= 0:
                    continue
                route = self._best_recovery_route(asset, amount)
                if route is None:
                    continue
                current = amount
                for leg_number, leg in enumerate(route.legs, start=1):
                    book = self.market_data.latest(leg.symbol)
                    intent = self._build_intent(
                        opportunity_id,
                        cycle_number,
                        recovery_round * 10 + leg_number,
                        leg,
                        current,
                        book,
                        recovery=True,
                    )
                    if intent is None:
                        break
                    fill = self.execute_order(intent)
                    recovery_fills.append(fill)
                    self._apply_fill(leg, fill)
                    self.record_fill(intent, fill)
                    made_progress = made_progress or fill.input_spent > 0
                    current = fill.net_output
                    if not fill.completed:
                        break
            if self._only_anchor_or_dust() or not made_progress:
                break
        return recovery_fills

    def _best_recovery_route(self, asset: str, amount: Decimal) -> Route | None:
        candidates = self.route_graph.recovery_routes(asset)
        quotes: list[PathQuote] = []
        for route in candidates:
            try:
                books = {
                    leg.symbol: self.market_data.latest(leg.symbol)
                    for leg in route.legs
                }
            except RuntimeError:
                continue
            quotes.append(self.quote_route(route, amount, books, recovery=True))
        if not quotes:
            return None
        complete = [quote for quote in quotes if quote.complete]
        if complete:
            return max(complete, key=lambda quote: quote.output_amount).route

        direct_quote = next(
            (quote for quote in quotes if len(quote.route.legs) == 1), None
        )
        if (
            direct_quote
            and direct_quote.fills
            and direct_quote.fills[0].input_spent > 0
        ):
            return direct_quote.route
        return None

    def _build_intent(
        self,
        opportunity_id: str,
        cycle_number: int,
        leg_number: int,
        leg: DirectedLeg,
        input_amount: Decimal,
        book: BookSnapshot,
        recovery: bool = False,
    ) -> OrderIntent | None:
        rules = self.rules[leg.symbol]
        slippage = (
            self.settings.max_recovery_slippage_rate
            if recovery
            else self.settings.max_slippage_rate
        )
        if leg.side is Side.BUY:
            raw_limit = book.best_ask.price * (Decimal("1") + slippage)
            limit_price = floor_increment(raw_limit, rules.tick_size)
            base_capacity = self._buy_capacity(book, input_amount, limit_price)
            quantity = floor_increment(base_capacity, rules.step_size)
        else:
            raw_limit = book.best_bid.price * (Decimal("1") - slippage)
            limit_price = ceil_increment(raw_limit, rules.tick_size)
            quantity = floor_increment(input_amount, rules.step_size)

        quantity = min(quantity, rules.max_qty)
        if rules.max_notional is not None:
            max_notional_quantity = floor_increment(
                rules.max_notional / limit_price, rules.step_size
            )
            quantity = min(quantity, max_notional_quantity)

        if quantity < rules.min_qty:
            return None
        if limit_price < rules.min_price or limit_price > rules.max_price:
            return None
        notional = quantity * limit_price
        if notional < rules.min_notional:
            return None
        if rules.max_notional is not None and notional > rules.max_notional:
            return None
        return OrderIntent(
            opportunity_id=opportunity_id,
            cycle_number=cycle_number,
            leg_number=leg_number,
            leg=leg,
            input_amount=input_amount,
            order_quantity=quantity,
            limit_price=limit_price,
            recovery=recovery,
        )

    @staticmethod
    def _buy_capacity(
        book: BookSnapshot, quote_budget: Decimal, limit_price: Decimal
    ) -> Decimal:
        remaining = quote_budget
        base = ZERO
        for level in book.asks:
            if level.price > limit_price or remaining <= 0:
                break
            affordable = remaining / level.price
            quantity = min(level.quantity, affordable)
            base += quantity
            remaining -= quantity * level.price
        return min(base, quote_budget / limit_price)

    def calculate_fill(self, intent: OrderIntent, book: BookSnapshot) -> FillResult:
        remaining = intent.order_quantity
        base_filled = ZERO
        quote_filled = ZERO
        level_count = 0
        levels = book.asks if intent.leg.side is Side.BUY else book.bids
        for level in levels:
            acceptable = (
                level.price <= intent.limit_price
                if intent.leg.side is Side.BUY
                else level.price >= intent.limit_price
            )
            if not acceptable or remaining <= 0:
                break
            quantity = min(level.quantity, remaining)
            base_filled += quantity
            quote_filled += quantity * level.price
            remaining -= quantity
            level_count += 1

        if base_filled == 0:
            status = FillStatus.EXPIRED
        elif remaining == 0:
            status = FillStatus.FILLED
        else:
            status = FillStatus.PARTIAL

        if intent.leg.side is Side.BUY:
            input_spent = quote_filled
            gross_output = base_filled
        else:
            input_spent = base_filled
            gross_output = quote_filled
        fee = gross_output * self.settings.taker_fee_rate
        average_price = quote_filled / base_filled if base_filled else ZERO
        return FillResult(
            status=status,
            symbol=intent.leg.symbol,
            side=intent.leg.side,
            requested_input=intent.input_amount,
            input_spent=input_spent,
            gross_output=gross_output,
            net_output=gross_output - fee,
            average_price=average_price,
            fee_amount=fee,
            fee_asset=intent.leg.to_asset,
            level_count=level_count,
            reason="" if status is FillStatus.FILLED else "IOC liquidity exhausted",
        )

    def _apply_fill(self, leg: DirectedLeg, fill: FillResult) -> None:
        if fill.input_spent > self.balances[leg.from_asset]:
            raise RuntimeError(
                f"Paper fill overspent {leg.from_asset}: "
                f"{fill.input_spent} > {self.balances[leg.from_asset]}"
            )
        self.balances[leg.from_asset] -= fill.input_spent
        self.balances[leg.to_asset] += fill.net_output

    def portfolio_value_anchor(self) -> Decimal:
        hubs = self._hub_assets()
        value = sum(self.balances.get(hub, ZERO) for hub in hubs)
        for asset, amount in self.balances.items():
            if asset in hubs or amount <= 0:
                continue
            direct = next(
                (
                    route
                    for route in self.route_graph.recovery_routes(asset)
                    if len(route.legs) == 1
                ),
                None,
            )
            if direct is None:
                continue
            leg = direct.legs[0]
            try:
                book = self.market_data.latest(leg.symbol)
            except RuntimeError:
                continue
            if leg.side is Side.SELL:
                value += amount * book.best_bid.price
            else:
                value += amount / book.best_ask.price
        return value

    def _books_ready_and_fresh(self, route: Route | None = None) -> bool:
        synchronized = getattr(self.market_data, "synchronized_symbols", None)
        required = (
            {leg.symbol for leg in route.legs}
            if route is not None
            else set()
        )
        if synchronized is not None and required:
            if not required.issubset(synchronized()):
                return False
        elif synchronized is None:
            try:
                if route is None:
                    self.market_data.all_latest()
                else:
                    for symbol in required:
                        self.market_data.latest(symbol)
            except RuntimeError:
                return False
        health_check = getattr(self.market_data, "is_healthy", None)
        if health_check is not None:
            return bool(health_check(self.settings.max_stream_silence_ms))
        return True

    def _route_update_ids(self, route: Route) -> dict[str, int]:
        return {
            leg.symbol: self.market_data.latest(leg.symbol).update_id
            for leg in route.legs
        }

    def _wait_for_route_refresh(
        self, route: Route, previous_ids: dict[str, int]
    ) -> bool:
        deadline = time.monotonic() + self.settings.cycle_refresh_timeout_ms / 1000
        symbols = {leg.symbol for leg in route.legs}
        while time.monotonic() < deadline:
            if not self._books_ready_and_fresh(route):
                time.sleep(0.005)
                continue
            if all(
                self.market_data.latest(symbol).update_id > previous_ids[symbol]
                for symbol in symbols
            ):
                return True
            time.sleep(0.005)
        return False

    def _only_anchor_or_dust(self) -> bool:
        return not any(
            self._can_place_direct_recovery(asset, self.balances[asset])
            for asset in self.route_graph.selected_assets
            if self.balances.get(asset, ZERO) > 0
        )

    def _can_place_direct_recovery(self, asset: str, amount: Decimal) -> bool:
        if amount <= 0:
            return False
        route = next(
            (
                candidate
                for candidate in self.route_graph.recovery_routes(asset)
                if len(candidate.legs) == 1
            ),
            None,
        )
        if route is None:
            return False
        leg = route.legs[0]
        try:
            return (
                self._build_intent(
                    "dust-check",
                    0,
                    0,
                    leg,
                    amount,
                    self.market_data.latest(leg.symbol),
                    recovery=True,
                )
                is not None
            )
        except RuntimeError:
            return True

    def _dust_inventory(self) -> dict[str, Decimal]:
        hubs = self._hub_assets()
        return {
            asset: amount
            for asset, amount in self.balances.items()
            if asset not in hubs and amount > 0
        }

    def _accumulate_cycle_metrics(
        self, metrics: EpisodeMetrics, result: CycleResult
    ) -> None:
        metrics.normal_trade_count += len(result.normal_fills)
        metrics.recovery_trade_count += len(result.recovery_fills)
        all_fills = result.normal_fills + result.recovery_fills
        metrics.partial_fill_count += sum(
            fill.status is FillStatus.PARTIAL for fill in all_fills
        )
        metrics.total_fees_anchor += sum(
            (self._fee_value_anchor(fill) for fill in all_fills), ZERO
        )
        metrics.cycle_pnls.append(result.pnl)
        if result.completed:
            metrics.cycles_completed += 1
        elif result.recovered:
            metrics.cycles_recovered += 1
            metrics.recovery_loss_anchor += result.recovery_loss_anchor
        else:
            metrics.cycles_failed += 1

    def _fee_value_anchor(self, fill: FillResult) -> Decimal:
        anchor = self.settings.anchor_asset
        if fill.fee_asset == anchor:
            return fill.fee_amount
        leg = self.route_graph.conversion_leg(fill.fee_asset, anchor)
        if leg is None:
            return ZERO
        try:
            book = self.market_data.latest(leg.symbol)
            if leg.side is Side.SELL:
                return fill.fee_amount * book.best_bid.price
            return fill.fee_amount / book.best_ask.price
        except RuntimeError:
            return ZERO

    @abstractmethod
    def execute_order(self, intent: OrderIntent) -> FillResult:
        """Execute one IOC intent. A live child could implement this later."""

    @abstractmethod
    def record_fill(self, intent: OrderIntent, fill: FillResult) -> None:
        """Persist or display a completed virtual/live order attempt."""

    @abstractmethod
    def report_episode(self, metrics: EpisodeMetrics) -> None:
        """Persist and display aggregate opportunity-episode metrics."""

    @abstractmethod
    def report_ratio_record(self, record: RatioRecord) -> None:
        """Handle a new all-time-high triangle ratio for this process."""

    @abstractmethod
    def report_theoretical_opportunity_started(
        self, metrics: TheoreticalOpportunityMetrics
    ) -> None:
        """Display the start of a fee-adjusted top-of-book opportunity."""

    @abstractmethod
    def report_theoretical_opportunity_ended(
        self, metrics: TheoreticalOpportunityMetrics
    ) -> None:
        """Persist and display a completed theoretical opportunity."""

    def report_market_data_metrics(self, metrics: dict[str, int]) -> None:
        """Optionally persist periodic market-data health metrics."""
        _ = metrics

    def report_candidate_audit(self, audit: CandidateAudit) -> None:
        """Persist a positive projected opportunity and its illusion flags."""
        _ = audit

    def report_big_pnl_result(self, metrics: EpisodeMetrics) -> None:
        """Persist marked and clean PnL for a traded opportunity episode."""
        _ = metrics
