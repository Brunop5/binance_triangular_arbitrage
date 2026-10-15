from __future__ import annotations

import queue
import struct
import threading
import time
from decimal import Decimal

import pytest

from arbitrage.config import Settings
from arbitrage.market_data import (
    BinanceSbeMarketData,
    BookBatch,
    BookSequenceGap,
    DepthEvent,
    MutableOrderBook,
    SbeDecodeError,
    SbeDepthDecoder,
)
from arbitrage.models import Level


def make_depth_payload() -> bytes:
    header = struct.pack("<HHHH", 26, 10003, 1, 0)
    root = struct.pack("<qqqbb", 1_700_000_000_000_000, 101, 102, -2, -3)
    bids = struct.pack("<HHqq", 16, 1, 12345, 2500)
    asks = struct.pack("<HHqq", 16, 1, 12355, 1750)
    symbol = b"BTCUSDT"
    return header + root + bids + asks + bytes([len(symbol)]) + symbol


def test_decodes_binance_depth_diff_fixture() -> None:
    event = SbeDepthDecoder().decode(make_depth_payload())

    assert event.symbol == "BTCUSDT"
    assert event.first_update_id == 101
    assert event.last_update_id == 102
    assert event.bids == (Level(Decimal("123.45"), Decimal("2.500")),)
    assert event.asks == (Level(Decimal("123.55"), Decimal("1.750")),)


def test_order_book_applies_updates_and_deletes_zero_levels() -> None:
    book = MutableOrderBook("BTCUSDT")
    book.load_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
        }
    )
    event = DepthEvent(
        symbol="BTCUSDT",
        event_time_us=123,
        first_update_id=101,
        last_update_id=102,
        bids=(Level(Decimal("100"), Decimal("0")),),
        asks=(Level(Decimal("100.5"), Decimal("4")),),
    )

    assert book.apply(event)
    frozen = book.freeze()
    assert frozen.bids == ()
    assert frozen.best_ask == Level(Decimal("100.5"), Decimal("4"))
    assert frozen.update_id == 102


def test_order_book_rejects_sequence_gap() -> None:
    book = MutableOrderBook("ETHUSDT")
    book.load_snapshot(
        {
            "lastUpdateId": 10,
            "bids": [["2000", "1"]],
            "asks": [["2001", "1"]],
        }
    )
    event = DepthEvent(
        symbol="ETHUSDT",
        event_time_us=123,
        first_update_id=12,
        last_update_id=12,
        bids=(),
        asks=(),
    )

    with pytest.raises(BookSequenceGap):
        book.apply(event)


def test_order_book_requires_exact_continuity_after_initial_bridge() -> None:
    book = MutableOrderBook("BTCUSDT")
    book.load_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
        }
    )
    bridge = DepthEvent(
        "BTCUSDT",
        123,
        99,
        101,
        (),
        (),
    )
    assert book.apply(bridge)

    overlapping = DepthEvent(
        "BTCUSDT",
        124,
        101,
        102,
        (),
        (),
    )
    with pytest.raises(BookSequenceGap):
        book.apply(overlapping)


def test_decoder_rejects_snapshot_template_on_diff_subscription() -> None:
    payload = bytearray(make_depth_payload())
    struct.pack_into("<H", payload, 2, 10002)

    with pytest.raises(SbeDecodeError):
        SbeDepthDecoder().decode(bytes(payload))


def test_disconnect_invalidation_removes_tradeable_books() -> None:
    client = BinanceSbeMarketData(
        Settings(api_key="test-only").with_symbols(("BTCUSDC",))
    )
    book = MutableOrderBook("BTCUSDT")
    book.load_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
        }
    )
    client.books["BTCUSDC"] = book
    client._synced.add("BTCUSDC")

    client._invalidate_latest()

    with pytest.raises(RuntimeError):
        client.latest("BTCUSDC")


def test_batch_backpressure_merges_all_dirty_symbols() -> None:
    client = BinanceSbeMarketData(
        Settings(api_key="test").with_symbols(("AUSDC", "BUSDC"))
    )
    client._publish_batch(BookBatch(frozenset(("AUSDC",)), 1, 1))
    client._publish_batch(BookBatch(frozenset(("BUSDC",)), 2, 2))
    merged = client._batches.get_nowait()
    assert isinstance(merged, BookBatch)
    assert merged.dirty_symbols == frozenset(("AUSDC", "BUSDC"))


def test_stream_health_does_not_require_every_symbol_to_be_synchronized() -> None:
    client = BinanceSbeMarketData(
        Settings(api_key="test").with_symbols(("AUSDC", "BUSDC"))
    )
    client._last_message_received_ns = time.time_ns()
    client._synced.add("AUSDC")
    assert client.is_healthy(250)


def test_per_symbol_queue_overflow_invalidates_book() -> None:
    client = BinanceSbeMarketData(
        Settings(api_key="test").with_symbols(("BTCUSDC",))
    )
    client._synced.add("BTCUSDC")
    buffers: dict[str, queue.Queue[DepthEvent]] = {
        "BTCUSDC": queue.Queue(maxsize=1)
    }
    overflowed: set[str] = set()
    lock = threading.Lock()
    event = DepthEvent("BTCUSDC", 1, 1, 1, (), ())
    client._enqueue_event(event, buffers, overflowed, lock)
    client._enqueue_event(event, buffers, overflowed, lock)
    assert "BTCUSDC" in overflowed
    assert "BTCUSDC" not in client.synchronized_symbols()
    assert client.metrics_snapshot()["queue_overflows"] == 1


def test_synthetic_multi_symbol_flood_preserves_monotonic_ids() -> None:
    symbols = tuple(f"ASSET{index}USDC" for index in range(15))
    client = BinanceSbeMarketData(
        Settings(api_key="test", inbound_queue_size=64).with_symbols(symbols)
    )
    buffers = {
        symbol: queue.Queue[DepthEvent](maxsize=64) for symbol in symbols
    }
    overflowed: set[str] = set()
    lock = threading.Lock()
    for symbol in symbols:
        client.books[symbol].load_snapshot(
            {"lastUpdateId": 0, "bids": [["1", "1"]], "asks": [["2", "1"]]}
        )
    for update_id in range(1, 501):
        for symbol in symbols:
            client._enqueue_event(
                DepthEvent(symbol, update_id, update_id, update_id, (), ()),
                buffers,
                overflowed,
                lock,
            )
        for symbol in symbols:
            event = buffers[symbol].get_nowait()
            assert client.books[symbol].apply(event)
            assert client.books[symbol].update_id == update_id
    assert not overflowed
    assert client.metrics_snapshot()["max_queue_depth"] <= 1
