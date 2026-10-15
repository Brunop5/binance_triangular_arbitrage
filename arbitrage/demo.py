from __future__ import annotations

import csv
import json
import time
from decimal import Decimal
from pathlib import Path

from .config import Settings
from .engine import SequentialArbitrageEngine
from .market_data import BinanceSbeMarketData
from .models import (
    ZERO,
    CandidateAudit,
    EpisodeMetrics,
    FillResult,
    FillStatus,
    OrderIntent,
    RatioRecord,
    SymbolRules,
    TheoreticalOpportunityMetrics,
    utc_now,
)
from .route_graph import RouteGraph


class DemoArbitrageEngine(SequentialArbitrageEngine):
    """Paper IOC executor and CSV reporter. It has no order-placement client."""

    def __init__(
        self,
        settings: Settings,
        market_data: BinanceSbeMarketData,
        route_graph: RouteGraph,
        rules: dict[str, SymbolRules] | None = None,
    ) -> None:
        super().__init__(settings, market_data, route_graph, rules)
        self.results_dir = settings.results_dir
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.results_dir / "paper_trades.csv"
        self.episodes_path = self.results_dir / "opportunity_episodes.csv"
        self.theoretical_path = self.results_dir / "theoretical_opportunities.csv"
        self.candidate_audits_path = (
            self.results_dir / "candidate_opportunities.csv"
        )
        self.big_pnl_path = self.results_dir / "big_pnl_results.csv"
        self._episode_count = 0
        self._total_cycles = 0
        self._completed_cycles = 0
        self._total_pnl = ZERO

    def execute_order(self, intent: OrderIntent) -> FillResult:
        if self.settings.simulated_latency_ms:
            time.sleep(self.settings.simulated_latency_ms / 1000)
        try:
            book = self.market_data.latest(intent.leg.symbol)
        except RuntimeError:
            return self._rejected(intent, "book unavailable after simulated latency")
        health_check = getattr(self.market_data, "is_healthy", None)
        if health_check is not None and not health_check(
            self.settings.max_stream_silence_ms
        ):
            return self._rejected(
                intent, "market-data stream became stale before virtual IOC"
            )
        return self.calculate_fill(intent, book)

    @staticmethod
    def _rejected(intent: OrderIntent, reason: str) -> FillResult:
        return FillResult(
            status=FillStatus.REJECTED,
            symbol=intent.leg.symbol,
            side=intent.leg.side,
            requested_input=intent.input_amount,
            input_spent=ZERO,
            gross_output=ZERO,
            net_output=ZERO,
            average_price=ZERO,
            fee_amount=ZERO,
            fee_asset=intent.leg.to_asset,
            level_count=0,
            reason=reason,
        )

    def record_fill(self, intent: OrderIntent, fill: FillResult) -> None:
        row = {
            "timestamp_utc": utc_now().isoformat(),
            "opportunity_id": intent.opportunity_id,
            "cycle_number": intent.cycle_number,
            "leg_number": intent.leg_number,
            "recovery": intent.recovery,
            "symbol": fill.symbol,
            "side": fill.side.value,
            "from_asset": intent.leg.from_asset,
            "to_asset": intent.leg.to_asset,
            "status": fill.status.value,
            "requested_input": fill.requested_input,
            "order_quantity": intent.order_quantity,
            "limit_price": intent.limit_price,
            "input_spent": fill.input_spent,
            "gross_output": fill.gross_output,
            "net_output": fill.net_output,
            "average_price": fill.average_price,
            "fee_amount": fill.fee_amount,
            "fee_asset": fill.fee_asset,
            "levels_consumed": fill.level_count,
            "reason": fill.reason,
        }
        self._append_csv(self.trades_path, row)
        kind = "RECOVERY" if intent.recovery else "NORMAL"
        print(
            f"{kind} cycle={intent.cycle_number} leg={intent.leg_number} "
            f"{fill.side.value} {fill.symbol} {fill.status.value} "
            f"in={fill.input_spent} out={fill.net_output} "
            f"fee={fill.fee_amount} {fill.fee_asset}"
        )

    def report_episode(self, metrics: EpisodeMetrics) -> None:
        ended_at = metrics.ended_at or utc_now()
        duration_ms = Decimal(
            str((ended_at - metrics.started_at).total_seconds())
        ) * Decimal("1000")
        row = {
            "opportunity_id": metrics.opportunity_id,
            "route": metrics.route_name,
            "started_at_utc": metrics.started_at.isoformat(),
            "ended_at_utc": ended_at.isoformat(),
            "duration_ms": duration_ms,
            "anchor_asset": self.settings.anchor_asset,
            "starting_anchor": metrics.starting_anchor,
            "ending_anchor_marked": metrics.ending_anchor,
            "net_pnl_anchor": metrics.net_pnl,
            "net_pnl_percent": metrics.net_pnl_rate,
            "cycles_attempted": metrics.cycles_attempted,
            "cycles_completed": metrics.cycles_completed,
            "cycles_recovered": metrics.cycles_recovered,
            "cycles_failed": metrics.cycles_failed,
            "completion_rate_percent": metrics.completion_rate,
            "normal_trade_count": metrics.normal_trade_count,
            "recovery_trade_count": metrics.recovery_trade_count,
            "partial_fill_count": metrics.partial_fill_count,
            "partial_fill_rate_percent": metrics.partial_fill_rate,
            "average_cycle_pnl_anchor": metrics.average_cycle_pnl,
            "fees_anchor_marked": metrics.total_fees_anchor,
            "recovery_loss_anchor": metrics.recovery_loss_anchor,
            "ending_inventory_json": json.dumps(
                {
                    asset: str(amount)
                    for asset, amount in metrics.ending_inventory.items()
                    if amount != ZERO
                },
                sort_keys=True,
            ),
        }
        self._append_csv(self.episodes_path, row)

        self._episode_count += 1
        self._total_cycles += metrics.cycles_attempted
        self._completed_cycles += metrics.cycles_completed
        self._total_pnl += metrics.net_pnl
        running_success = (
            Decimal(self._completed_cycles)
            / Decimal(self._total_cycles)
            * Decimal("100")
            if self._total_cycles
            else ZERO
        )
        average_episode_pnl = self._total_pnl / Decimal(self._episode_count)
        print(
            "\nOPPORTUNITY SUMMARY "
            f"id={metrics.opportunity_id} route={metrics.route_name} "
            f"cycles={metrics.cycles_completed}/{metrics.cycles_attempted} "
            f"recovered={metrics.cycles_recovered} "
            f"trades={metrics.normal_trade_count + metrics.recovery_trade_count} "
            f"pnl={metrics.net_pnl} {self.settings.anchor_asset} "
            f"({metrics.net_pnl_rate}%) "
            f"completion={metrics.completion_rate}%"
        )
        print(
            "RUNNING SUMMARY "
            f"episodes={self._episode_count} cycles={self._total_cycles} "
            f"success={running_success}% "
            f"avg_episode_pnl={average_episode_pnl} "
            f"{self.settings.anchor_asset}\n"
        )

    def report_ratio_record(self, record: RatioRecord) -> None:
        fee_adjusted_percent = (record.fee_adjusted_ratio - Decimal("1")) * Decimal(
            "100"
        )
        print(
            f"{utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')} "
            "NEW MAX RATIO "
            f"ratio={record.gross_ratio:.5f} "
            f"after_fee_pnl={fee_adjusted_percent:.5f}% "
            f"route={record.route.name}"
        )

    def report_theoretical_opportunity_started(
        self, metrics: TheoreticalOpportunityMetrics
    ) -> None:
        print(
            "THEORETICAL OPPORTUNITY START "
            f"id={metrics.opportunity_id} "
            f"route={metrics.route_name} "
            f"gross={metrics.start_gross_ratio:.12f} "
            f"after_fees={metrics.start_fee_adjusted_ratio:.12f}"
        )

    def report_theoretical_opportunity_ended(
        self, metrics: TheoreticalOpportunityMetrics
    ) -> None:
        row = {
            "opportunity_id": metrics.opportunity_id,
            "route": metrics.route_name,
            "started_at_utc": metrics.started_at.isoformat(),
            "ended_at_utc": (metrics.ended_at.isoformat() if metrics.ended_at else ""),
            "duration_ms": metrics.duration_ms,
            "end_reason": metrics.end_reason,
            "samples": metrics.samples,
            "start_gross_ratio": metrics.start_gross_ratio,
            "start_fee_adjusted_ratio": (metrics.start_fee_adjusted_ratio),
            "max_gross_ratio": metrics.max_gross_ratio,
            "max_fee_adjusted_ratio": metrics.max_fee_adjusted_ratio,
            "max_executable_ratio": metrics.max_executable_ratio,
            "bot_executable_at_least_once": (metrics.bot_executable_at_least_once),
        }
        self._append_csv(self.theoretical_path, row)
        print(
            "THEORETICAL OPPORTUNITY END "
            f"id={metrics.opportunity_id} "
            f"route={metrics.route_name} "
            f"duration={metrics.duration_ms:.3f}ms "
            f"samples={metrics.samples} "
            f"max_after_fees={metrics.max_fee_adjusted_ratio:.12f} "
            f"max_executable={metrics.max_executable_ratio:.12f} "
            f"bot_executable={metrics.bot_executable_at_least_once} "
            f"reason={metrics.end_reason}"
        )

    def report_market_data_metrics(self, metrics: dict[str, int]) -> None:
        print(
            "MARKET DATA "
            f"messages={metrics['messages_received']} "
            f"applied={metrics['events_applied']} "
            f"synced={metrics['synchronized_symbols']}/"
            f"{len(self.settings.symbols)} "
            f"max_queue={metrics['max_queue_depth']} "
            f"max_lag_us={metrics['max_apply_lag_us']} "
            f"gaps={metrics['sequence_gaps']} "
            f"resyncs={metrics['resyncs']} "
            f"overflows={metrics['queue_overflows']}"
        )

    def report_candidate_audit(self, audit: CandidateAudit) -> None:
        flags = "|".join(sorted(audit.flags)) if audit.flags else "NONE"
        row = {
            "audit_id": audit.audit_id,
            "route": audit.route_name,
            "detected_at_utc": audit.detected_at.isoformat(),
            "ended_at_utc": (
                audit.ended_at.isoformat() if audit.ended_at else ""
            ),
            "starting_anchor": audit.starting_amount,
            "gross_ratio": audit.gross_ratio,
            "fee_adjusted_ratio": audit.fee_adjusted_ratio,
            "projected_pnl_anchor": audit.projected_pnl,
            "immediate_output_anchor": audit.immediate_output,
            "immediate_pnl_anchor": audit.immediate_pnl,
            "delayed_output_anchor": audit.delayed_output,
            "delayed_pnl_anchor": audit.delayed_pnl,
            "flags": flags,
        }
        self._append_csv(self.candidate_audits_path, row)
        print(
            "POSITIVE OPPORTUNITY AUDIT "
            f"route={audit.route_name} "
            f"projected_pnl={audit.projected_pnl:.8f} "
            f"immediate_pnl={audit.immediate_pnl:.8f} "
            f"delayed_pnl={audit.delayed_pnl:.8f} "
            f"flags={flags}"
        )

    def report_big_pnl_result(self, metrics: EpisodeMetrics) -> None:
        row = {
            "equity": metrics.ending_anchor,
            "result_pnl_percent": metrics.net_pnl_rate,
            "clean_result_pnl_percent": metrics.clean_pnl_rate,
            "opportunity_id": metrics.opportunity_id,
            "route": metrics.route_name,
            "starting_anchor": metrics.starting_anchor,
            "ending_usdc_cash": metrics.ending_usdc_cash,
            "cycles_completed": metrics.cycles_completed,
            "cycles_attempted": metrics.cycles_attempted,
        }
        self._append_csv(self.big_pnl_path, row)
        print(
            "BIG PNL "
            f"equity={metrics.ending_anchor} "
            f"marked_pnl={metrics.net_pnl_rate}% "
            f"clean_pnl={metrics.clean_pnl_rate}% "
            f"route={metrics.route_name} "
            f"cycles={metrics.cycles_completed}/{metrics.cycles_attempted}"
        )

    @staticmethod
    def _append_csv(path: Path, row: dict[str, object]) -> None:
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
