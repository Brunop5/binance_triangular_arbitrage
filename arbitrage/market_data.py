from __future__ import annotations

import queue
import struct
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import requests
import websocket

from .config import Settings
from .models import BookSnapshot, Level, SymbolRules


class SbeDecodeError(ValueError):
    pass


class BookSequenceGap(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BookBatch:
    dirty_symbols: frozenset[str]
    published_ns: int
    sequence: int


@dataclass(frozen=True, slots=True)
class DepthEvent:
    symbol: str
    event_time_us: int
    first_update_id: int
    last_update_id: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    received_ns: int = field(default_factory=time.time_ns)


class SbeDepthDecoder:
    """Small schema-specific decoder for Binance stream schema 1:0."""

    HEADER = struct.Struct("<HHHH")
    DEPTH_ROOT = struct.Struct("<qqqbb")
    GROUP_HEADER = struct.Struct("<HH")
    LEVEL = struct.Struct("<qq")

    def decode(self, payload: bytes) -> DepthEvent:
        if len(payload) < self.HEADER.size:
            raise SbeDecodeError("SBE payload is shorter than its header")

        block_length, template_id, schema_id, version = self.HEADER.unpack_from(payload)
        if schema_id != 1:
            raise SbeDecodeError(f"Unsupported schema ID {schema_id}")
        if version > 0:
            raise SbeDecodeError(f"Unsupported schema version {version}")

        root_offset = self.HEADER.size
        if template_id != 10003:
            raise SbeDecodeError(f"Unexpected SBE template ID {template_id}")
        required = root_offset + self.DEPTH_ROOT.size
        if len(payload) < required:
            raise SbeDecodeError("Truncated DepthDiffStreamEvent root")
        (
            event_time_us,
            first_update_id,
            last_update_id,
            price_exponent,
            qty_exponent,
        ) = self.DEPTH_ROOT.unpack_from(payload, root_offset)

        cursor = root_offset + block_length
        bids, cursor = self._decode_levels(
            payload, cursor, price_exponent, qty_exponent
        )
        asks, cursor = self._decode_levels(
            payload, cursor, price_exponent, qty_exponent
        )
        symbol, cursor = self._decode_string(payload, cursor)
        if cursor != len(payload):
            raise SbeDecodeError(
                f"Unexpected {len(payload) - cursor} trailing SBE bytes"
            )

        return DepthEvent(
            symbol=symbol,
            event_time_us=event_time_us,
            first_update_id=first_update_id,
            last_update_id=last_update_id,
            bids=bids,
            asks=asks,
        )

    def _decode_levels(
        self,
        payload: bytes,
        cursor: int,
        price_exponent: int,
        qty_exponent: int,
    ) -> tuple[tuple[Level, ...], int]:
        if len(payload) < cursor + self.GROUP_HEADER.size:
            raise SbeDecodeError("Truncated SBE group header")
        block_length, count = self.GROUP_HEADER.unpack_from(payload, cursor)
        cursor += self.GROUP_HEADER.size
        if block_length < self.LEVEL.size:
            raise SbeDecodeError(f"Invalid level block length {block_length}")

        levels: list[Level] = []
        for _ in range(count):
            if len(payload) < cursor + block_length:
                raise SbeDecodeError("Truncated SBE level group")
            price_mantissa, qty_mantissa = self.LEVEL.unpack_from(payload, cursor)
            levels.append(
                Level(
                    price=Decimal(price_mantissa).scaleb(price_exponent),
                    quantity=Decimal(qty_mantissa).scaleb(qty_exponent),
                )
            )
            cursor += block_length
        return tuple(levels), cursor

    @staticmethod
    def _decode_string(payload: bytes, cursor: int) -> tuple[str, int]:
        if len(payload) <= cursor:
            raise SbeDecodeError("Missing SBE symbol")
        length = payload[cursor]
        cursor += 1
        end = cursor + length
        if len(payload) < end:
            raise SbeDecodeError("Truncated SBE symbol")
        return payload[cursor:end].decode("utf-8"), end


class MutableOrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.update_id = 0
        self.event_time_us = 0
        self.received_ns = time.time_ns()
        self._bridged = False

    def load_snapshot(self, payload: dict[str, Any]) -> None:
        self.update_id = int(payload["lastUpdateId"])
        self.bids = {
            Decimal(price): Decimal(qty)
            for price, qty in payload["bids"]
            if Decimal(qty) > 0
        }
        self.asks = {
            Decimal(price): Decimal(qty)
            for price, qty in payload["asks"]
            if Decimal(qty) > 0
        }
        self.event_time_us = int(time.time_ns() // 1_000)
        self.received_ns = time.time_ns()
        self._bridged = False

    def apply(self, event: DepthEvent) -> bool:
        if event.symbol != self.symbol:
            raise ValueError(f"Cannot apply {event.symbol} event to {self.symbol} book")
        if event.last_update_id <= self.update_id:
            return False
        expected = self.update_id + 1
        if self._bridged and event.first_update_id != expected:
            raise BookSequenceGap(
                f"{self.symbol}: expected {expected}, "
                f"received {event.first_update_id}..{event.last_update_id}"
            )
        if not self._bridged:
            if event.first_update_id > expected:
                raise BookSequenceGap(
                    f"{self.symbol}: expected bridge containing {expected}, "
                    f"received {event.first_update_id}..{event.last_update_id}"
                )
            if not event.first_update_id <= expected <= event.last_update_id:
                return False

        self._apply_side(self.bids, event.bids)
        self._apply_side(self.asks, event.asks)
        self._bridged = True
        self.update_id = event.last_update_id
        self.event_time_us = event.event_time_us
        self.received_ns = event.received_ns
        return True

    @staticmethod
    def _apply_side(side: dict[Decimal, Decimal], levels: tuple[Level, ...]) -> None:
        for level in levels:
            if level.quantity == 0:
                side.pop(level.price, None)
            else:
                side[level.price] = level.quantity

    def freeze(self) -> BookSnapshot:
        bids = tuple(
            Level(price, quantity)
            for price, quantity in sorted(self.bids.items(), reverse=True)
        )
        asks = tuple(
            Level(price, quantity) for price, quantity in sorted(self.asks.items())
        )
        return BookSnapshot(
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            update_id=self.update_id,
            event_time_us=self.event_time_us,
            received_ns=self.received_ns,
        )


class BinanceSbeMarketData:
    """Maintains synchronized local books from Binance's 20 ms SBE feed."""

    def __init__(self, settings: Settings) -> None:
        if not settings.api_key:
            raise ValueError("An Ed25519 Binance API key identifier is required")
        self.settings = settings
        self.decoder = SbeDepthDecoder()
        self.session = requests.Session()
        self.books = {symbol: MutableOrderBook(symbol) for symbol in settings.symbols}
        self._synced: set[str] = set()
        self._lock = threading.RLock()
        self._batches: queue.Queue[BookBatch | BaseException] = queue.Queue(
            maxsize=1
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_message_received_ns = 0
        self._batch_sequence = 0
        self._last_resync_ns: dict[str, int] = {}
        self._metrics: dict[str, int] = {
            "messages_received": 0,
            "events_applied": 0,
            "sequence_gaps": 0,
            "queue_overflows": 0,
            "resyncs": 0,
            "max_queue_depth": 0,
            "max_apply_lag_us": 0,
            "lag_invalidations": 0,
        }

    def latest(self, symbol: str) -> BookSnapshot:
        with self._lock:
            if symbol not in self._synced:
                raise RuntimeError(f"{symbol} book is not synchronized")
            return self.books[symbol].freeze()

    def all_latest(self) -> dict[str, BookSnapshot]:
        with self._lock:
            if len(self._synced) != len(self.settings.symbols):
                raise RuntimeError("Not all books are synchronized")
            return {
                symbol: self.books[symbol].freeze()
                for symbol in self.settings.symbols
            }

    def is_healthy(self, max_silence_ms: int) -> bool:
        with self._lock:
            if self._last_message_received_ns == 0:
                return False
            silence_ns = time.time_ns() - self._last_message_received_ns
        return silence_ns <= max_silence_ms * 1_000_000

    def synchronized_symbols(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._synced)

    def metrics_snapshot(self) -> dict[str, int]:
        with self._lock:
            result = dict(self._metrics)
            result["synchronized_symbols"] = len(self._synced)
            return result

    def fetch_symbol_rules(self) -> dict[str, SymbolRules]:
        response = self.session.get(
            f"{self.settings.rest_base_url}/api/v3/exchangeInfo",
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        result: dict[str, SymbolRules] = {}
        for info in response.json()["symbols"]:
            if info["symbol"] not in self.books:
                continue
            filters = {item["filterType"]: item for item in info.get("filters", [])}
            price_filter = filters["PRICE_FILTER"]
            lot_filter = filters["LOT_SIZE"]
            notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            min_notional = (
                Decimal(notional_filter["minNotional"])
                if notional_filter
                else Decimal("0")
            )
            max_notional = (
                Decimal(notional_filter["maxNotional"])
                if notional_filter and "maxNotional" in notional_filter
                else None
            )
            result[info["symbol"]] = SymbolRules(
                symbol=info["symbol"],
                base_asset=info["baseAsset"],
                quote_asset=info["quoteAsset"],
                tick_size=Decimal(price_filter["tickSize"]),
                step_size=Decimal(lot_filter["stepSize"]),
                min_qty=Decimal(lot_filter["minQty"]),
                max_qty=Decimal(lot_filter["maxQty"]),
                min_notional=min_notional,
                max_notional=max_notional,
                min_price=Decimal(price_filter["minPrice"]),
                max_price=Decimal(price_filter["maxPrice"]),
            )
        missing = set(self.settings.symbols) - result.keys()
        if missing:
            raise RuntimeError(f"Missing exchange information for {sorted(missing)}")
        return result

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def consume() -> None:
            try:
                for batch in self.batch_stream(self._stop_event):
                    self._publish_batch(batch)
            except BaseException as error:
                self._publish_batch(error)

        self._thread = threading.Thread(
            target=consume, name="binance-market-data", daemon=True
        )
        self._thread.start()

    def _publish_batch(self, item: BookBatch | BaseException) -> None:
        try:
            self._batches.put_nowait(item)
        except queue.Full:
            merged = item
            with suppress(queue.Empty):
                previous = self._batches.get_nowait()
                if isinstance(item, BookBatch) and isinstance(previous, BookBatch):
                    merged = BookBatch(
                        item.dirty_symbols | previous.dirty_symbols,
                        item.published_ns,
                        item.sequence,
                    )
            self._batches.put_nowait(merged)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def batches(self, timeout: float = 1.0) -> Iterator[BookBatch]:
        while not self._stop_event.is_set():
            try:
                item = self._batches.get(timeout=timeout)
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            yield item

    def updates(self, timeout: float = 1.0) -> Iterator[BookSnapshot]:
        for batch in self.batches(timeout):
            for symbol in sorted(batch.dirty_symbols):
                with suppress(RuntimeError):
                    yield self.latest(symbol)

    def stream(
        self,
        stop_event: threading.Event | None = None,
    ) -> Iterator[BookSnapshot]:
        stop = stop_event or threading.Event()
        for batch in self.batch_stream(stop):
            for symbol in sorted(batch.dirty_symbols):
                with suppress(RuntimeError):
                    yield self.latest(symbol)

    def batch_stream(
        self,
        stop_event: threading.Event | None = None,
    ) -> Iterator[BookBatch]:
        stop = stop_event or threading.Event()
        delay = 1.0
        while not stop.is_set():
            try:
                yield from self._connected_batches(stop)
                delay = 1.0
            except KeyboardInterrupt:
                stop.set()
                return
            except Exception as error:
                self._invalidate_latest()
                if stop.is_set():
                    return
                print(
                    f"market-data reconnect in {delay:.1f}s: "
                    f"{type(error).__name__}: {error}"
                )
                stop.wait(delay)
                delay = min(delay * 2, 30.0)

    def _connected_batches(self, stop_event: threading.Event) -> Iterator[BookBatch]:
        self._invalidate_latest()
        ws = websocket.create_connection(
            self.settings.sbe_url,
            header=[f"X-MBX-APIKEY: {self.settings.api_key}"],
            timeout=self.settings.request_timeout_seconds,
            enable_multithread=True,
        )
        buffers = {
            symbol: queue.Queue(maxsize=self.settings.inbound_queue_size)
            for symbol in self.settings.symbols
        }
        receiver_errors: queue.Queue[BaseException] = queue.Queue(maxsize=1)
        overflowed: set[str] = set()
        overflow_lock = threading.Lock()
        catching_up = set(self.settings.symbols)

        def receive() -> None:
            try:
                while not stop_event.is_set():
                    payload = ws.recv()
                    if payload in ("", None):
                        raise ConnectionError("SBE WebSocket closed")
                    if isinstance(payload, str):
                        continue
                    event = self.decoder.decode(bytes(payload))
                    with self._lock:
                        self._last_message_received_ns = event.received_ns
                        self._metrics["messages_received"] += 1
                    self._enqueue_event(
                        event, buffers, overflowed, overflow_lock
                    )
            except BaseException as error:
                with suppress(queue.Full):
                    receiver_errors.put_nowait(error)

        receiver = threading.Thread(
            target=receive, name="binance-sbe-receiver", daemon=True
        )
        receiver.start()
        try:
            self._bootstrap_books()
            last_publish_ns = time.monotonic_ns()
            dirty: set[str] = set()
            while not stop_event.is_set():
                with suppress(queue.Empty):
                    raise receiver_errors.get_nowait()
                made_progress = False
                for symbol, buffer in buffers.items():
                    with overflow_lock:
                        needs_resync = symbol in overflowed
                    if needs_resync:
                        self._resync_symbol(symbol, buffer, overflowed, overflow_lock)
                        catching_up.add(symbol)
                        dirty.add(symbol)
                        made_progress = True
                        continue
                    for _ in range(64):
                        try:
                            event = buffer.get_nowait()
                        except queue.Empty:
                            break
                        made_progress = True
                        lag_us = max(0, (time.time_ns() - event.received_ns) // 1_000)
                        with self._lock:
                            already_synchronized = symbol in self._synced
                        if (
                            already_synchronized
                            and symbol not in catching_up
                            and lag_us > self.settings.max_apply_lag_ms * 1_000
                        ):
                            with self._lock:
                                self._synced.discard(symbol)
                                self._metrics["lag_invalidations"] += 1
                            with overflow_lock:
                                overflowed.add(symbol)
                            break
                        try:
                            with self._lock:
                                applied = self.books[symbol].apply(event)
                                if applied:
                                    self._synced.add(symbol)
                                    self._metrics["events_applied"] += 1
                                    self._metrics["max_apply_lag_us"] = max(
                                        self._metrics["max_apply_lag_us"], lag_us
                                    )
                        except BookSequenceGap:
                            with self._lock:
                                self._synced.discard(symbol)
                                self._metrics["sequence_gaps"] += 1
                            with overflow_lock:
                                overflowed.add(symbol)
                            break
                        if applied:
                            dirty.add(symbol)
                            if (
                                buffer.qsize() <= 1
                                and lag_us
                                <= self.settings.max_apply_lag_ms * 1_000
                            ):
                                catching_up.discard(symbol)
                now_ns = time.monotonic_ns()
                if (
                    dirty
                    and now_ns - last_publish_ns
                    >= self.settings.eval_coalesce_ms * 1_000_000
                ):
                    self._batch_sequence += 1
                    yield BookBatch(
                        frozenset(dirty), time.time_ns(), self._batch_sequence
                    )
                    dirty.clear()
                    last_publish_ns = now_ns
                if not made_progress:
                    stop_event.wait(0.001)
        finally:
            ws.close()
            receiver.join(timeout=1)

    def _enqueue_event(
        self,
        event: DepthEvent,
        buffers: dict[str, queue.Queue[DepthEvent]],
        overflowed: set[str],
        overflow_lock: threading.Lock,
    ) -> None:
        target = buffers.get(event.symbol)
        if target is None:
            return
        try:
            target.put_nowait(event)
            depth = target.qsize()
            with self._lock:
                self._metrics["max_queue_depth"] = max(
                    self._metrics["max_queue_depth"], depth
                )
        except queue.Full:
            with overflow_lock:
                overflowed.add(event.symbol)
            with self._lock:
                self._synced.discard(event.symbol)
                self._metrics["queue_overflows"] += 1

    def _invalidate_latest(self) -> None:
        with self._lock:
            self._synced.clear()
            self._last_message_received_ns = 0

    def _bootstrap_books(self) -> None:
        with ThreadPoolExecutor(
            max_workers=self.settings.snapshot_concurrency
        ) as executor:
            futures = {
                executor.submit(self._fetch_depth_snapshot, symbol): symbol
                for symbol in self.settings.symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                book = MutableOrderBook(symbol)
                book.load_snapshot(future.result())
                with self._lock:
                    self.books[symbol] = book

    def _resync_symbol(
        self,
        symbol: str,
        buffer: queue.Queue[DepthEvent],
        overflowed: set[str],
        overflow_lock: threading.Lock,
    ) -> None:
        while True:
            with suppress(queue.Empty):
                while True:
                    buffer.get_nowait()
            with overflow_lock:
                overflowed.discard(symbol)
            now_ns = time.monotonic_ns()
            earliest_ns = self._last_resync_ns.get(symbol, 0) + (
                self.settings.resync_cooldown_ms * 1_000_000
            )
            if now_ns < earliest_ns:
                self._stop_event.wait((earliest_ns - now_ns) / 1_000_000_000)
            snapshot = self._fetch_depth_snapshot(symbol)
            self._last_resync_ns[symbol] = time.monotonic_ns()
            book = MutableOrderBook(symbol)
            book.load_snapshot(snapshot)
            with overflow_lock:
                repeated_overflow = symbol in overflowed
            if repeated_overflow:
                continue
            with self._lock:
                self.books[symbol] = book
                self._metrics["resyncs"] += 1
            return

    def _fetch_depth_snapshot(self, symbol: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.rest_base_url}/api/v3/depth",
            params={"symbol": symbol, "limit": self.settings.snapshot_limit},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
