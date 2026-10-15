from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from arbitrage.config import Settings
from arbitrage.demo import DemoArbitrageEngine
from arbitrage.engine import SequentialArbitrageEngine, floor_increment
from arbitrage.models import (
    ZERO,
    BookSnapshot,
    CandidateAudit,
    CycleResult,
    EpisodeMetrics,
    FillResult,
    FillStatus,
    Level,
    OrderIntent,
    RatioRecord,
    SymbolRules,
    TheoreticalOpportunityMetrics,
    utc_now,
)
from arbitrage.route_graph import PairMeta, RouteGraph


def graph() -> RouteGraph:
    pairs = {
        frozenset(("BTC", "USDC")): PairMeta("BTCUSDC", "BTC", "USDC"),
        frozenset(("ETH", "USDC")): PairMeta("ETHUSDC", "ETH", "USDC"),
        frozenset(("BTC", "ETH")): PairMeta("ETHBTC", "ETH", "BTC"),
    }
    return RouteGraph.build("USDC", ("BTC", "ETH"), pairs)


def book(symbol: str, bid: str, ask: str, quantity: str = "100") -> BookSnapshot:
    return BookSnapshot(
        symbol,
        (Level(Decimal(bid), Decimal(quantity)),),
        (Level(Decimal(ask), Decimal(quantity)),),
        1,
        1,
    )


def profitable_books() -> dict[str, BookSnapshot]:
    return {
        "BTCUSDC": book("BTCUSDC", "99", "100"),
        "ETHBTC": book("ETHBTC", "0.49", "0.50"),
        "ETHUSDC": book("ETHUSDC", "60", "61"),
    }


def rules() -> dict[str, SymbolRules]:
    result: dict[str, SymbolRules] = {}
    for symbol, base, quote in (
        ("BTCUSDC", "BTC", "USDC"),
        ("ETHUSDC", "ETH", "USDC"),
        ("ETHBTC", "ETH", "BTC"),
    ):
        result[symbol] = SymbolRules(
            symbol,
            base,
            quote,
            Decimal("0.0001"),
            Decimal("0.0001"),
            Decimal("0.0001"),
            Decimal("100"),
            Decimal("0.0001"),
        )
    return result


def settings() -> Settings:
    return Settings(
        api_key="test",
        starting_capital=Decimal("20"),
        simulated_latency_ms=0,
        cycle_refresh_timeout_ms=2,
    ).with_symbols(graph().symbols)


class FakeMarketData:
    def __init__(self, books: dict[str, BookSnapshot]) -> None:
        self.books = books

    def latest(self, symbol: str) -> BookSnapshot:
        return self.books[symbol]

    def all_latest(self) -> dict[str, BookSnapshot]:
        return dict(self.books)

    def is_healthy(self, _: int) -> bool:
        return True


class AdvancingMarket(FakeMarketData):
    def latest(self, symbol: str) -> BookSnapshot:
        book = self.books[symbol]
        advanced = replace(book, update_id=book.update_id + 1)
        self.books[symbol] = advanced
        return advanced


class Harness(SequentialArbitrageEngine):
    def __init__(
        self,
        fail_on_call: int | None = None,
        engine_settings: Settings | None = None,
        market: FakeMarketData | None = None,
    ) -> None:
        super().__init__(
            engine_settings or settings(),
            market or FakeMarketData(profitable_books()),  # type: ignore[arg-type]
            graph(),
            rules(),
        )
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.records: list[RatioRecord] = []
        self.episodes: list[EpisodeMetrics] = []
        self.audits: list[CandidateAudit] = []
        self.big_pnl_results: list[EpisodeMetrics] = []

    def execute_order(self, intent: OrderIntent) -> FillResult:
        self.calls += 1
        if self.calls == self.fail_on_call:
            return FillResult(
                FillStatus.EXPIRED,
                intent.leg.symbol,
                intent.leg.side,
                intent.input_amount,
                ZERO,
                ZERO,
                ZERO,
                ZERO,
                ZERO,
                intent.leg.to_asset,
                0,
                "forced",
            )
        return self.calculate_fill(intent, self.market_data.latest(intent.leg.symbol))

    def record_fill(self, intent: OrderIntent, fill: FillResult) -> None:
        pass

    def report_episode(self, metrics: EpisodeMetrics) -> None:
        self.episodes.append(metrics)

    def report_ratio_record(self, record: RatioRecord) -> None:
        self.records.append(record)

    def report_theoretical_opportunity_started(
        self, metrics: TheoreticalOpportunityMetrics
    ) -> None:
        pass

    def report_theoretical_opportunity_ended(
        self, metrics: TheoreticalOpportunityMetrics
    ) -> None:
        pass

    def report_candidate_audit(self, audit: CandidateAudit) -> None:
        self.audits.append(audit)

    def report_big_pnl_result(self, metrics: EpisodeMetrics) -> None:
        self.big_pnl_results.append(metrics)


def test_finds_and_executes_dynamic_usdc_cycle() -> None:
    engine = Harness()
    opportunity = engine.find_best_opportunity()
    assert opportunity is not None
    assert opportunity.route.name == "USDC-BTC-ETH-USDC"
    result = engine._execute_cycle("test", 1, opportunity)
    assert result.completed
    assert result.pnl > 0
    assert engine.balances["USDC"] > Decimal("20")


def test_dirty_symbol_only_recomputes_indexed_routes() -> None:
    engine = Harness()
    assert engine.find_best_opportunity({"ETHBTC"}) is not None
    assert engine.find_best_opportunity({"UNKNOWN"}) is None


def test_failed_leg_recovers_non_anchor_inventory() -> None:
    engine = Harness(fail_on_call=2)
    opportunity = engine.find_best_opportunity()
    assert opportunity is not None
    result = engine._execute_cycle("recovery", 1, opportunity)
    assert not result.completed
    assert result.recovered
    assert result.recovery_fills
    assert engine._only_anchor_or_dust()


def test_graph_recovery_chooses_best_direct_or_two_hop_path() -> None:
    engine = Harness()
    chosen = engine._best_recovery_route("BTC", Decimal("1"))
    assert chosen is not None
    assert chosen.name == "BTC-ETH-USDC-recovery"


def test_ratio_session_high_is_not_duplicated() -> None:
    engine = Harness()
    engine.find_best_opportunity()
    engine.find_best_opportunity()
    assert len(engine.records) == 1
    assert engine.records[0].gross_ratio == Decimal("1.2")


def test_positive_opportunity_runs_delayed_shadow_audit() -> None:
    engine = Harness()
    for _ in range(4):
        engine.find_best_opportunity()
    assert len(engine.audits) == 1
    assert engine.audits[0].projected_pnl > 0
    assert engine.audits[0].immediate_pnl > 0
    assert engine.audits[0].delayed_pnl > 0


def test_exact_increment_and_anchor_metrics() -> None:
    assert floor_increment(Decimal("1.2345"), Decimal("0.001")) == Decimal("1.234")
    engine = Harness()
    metrics = EpisodeMetrics("id", "route", utc_now())
    result = CycleResult(
        False,
        True,
        Decimal("20"),
        Decimal("15"),
        recovery_loss_anchor=Decimal("1"),
    )
    engine._accumulate_cycle_metrics(metrics, result)
    assert result.pnl == Decimal("-5")
    assert metrics.recovery_loss_anchor == Decimal("1")


def test_demo_csv_uses_generic_anchor_and_inventory_fields(tmp_path: Path) -> None:
    configured = settings().model_copy(update={"results_dir": tmp_path})
    market = FakeMarketData(profitable_books())
    engine = DemoArbitrageEngine(
        configured,
        market,  # type: ignore[arg-type]
        graph(),
        rules(),
    )
    opportunity = engine.find_best_opportunity()
    assert opportunity is not None
    metrics = EpisodeMetrics(
        "csv",
        opportunity.route.name,
        opportunity.detected_at,
        starting_anchor=Decimal("20"),
        cycles_attempted=1,
        cycles_completed=1,
    )
    metrics.finish(Decimal("20.01"), engine.balances, Decimal("20.01"))
    engine.report_episode(metrics)
    text = (tmp_path / "opportunity_episodes.csv").read_text()
    assert "starting_anchor" in text
    assert "ending_inventory_json" in text
    assert "20.01" in text


def test_episode_runs_multiple_cycles_while_route_stays_profitable() -> None:
    configured = settings().model_copy(
        update={"cycle_cooldown_ms": 0, "max_cycles_per_episode": 3}
    )
    engine = Harness(
        engine_settings=configured,
        market=AdvancingMarket(profitable_books()),
    )
    opportunity = engine.find_best_opportunity()
    assert opportunity is not None
    engine._run_opportunity_episode(opportunity)
    assert len(engine.episodes) == 1
    assert engine.episodes[0].cycles_completed == 3
    assert len(engine.big_pnl_results) == 1
    assert engine.big_pnl_results[0].net_pnl_rate > 0


def test_big_pnl_csv_records_equity_and_pnl_percentages(tmp_path: Path) -> None:
    configured = settings().model_copy(
        update={
            "results_dir": tmp_path,
            "cycle_cooldown_ms": 0,
            "max_cycles_per_episode": 1,
        }
    )
    market = AdvancingMarket(profitable_books())
    engine = DemoArbitrageEngine(
        configured,
        market,  # type: ignore[arg-type]
        graph(),
        rules(),
    )
    opportunity = engine.find_best_opportunity()
    assert opportunity is not None
    engine._run_opportunity_episode(opportunity)
    text = (tmp_path / "big_pnl_results.csv").read_text()
    assert text.startswith("equity,")
    assert "result_pnl_percent" in text
    assert "clean_result_pnl_percent" in text
