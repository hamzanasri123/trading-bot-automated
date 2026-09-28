"""Unit tests for the order-book staleness guard added to OrderBook/DataEngine."""
import time

from engine.data_engine import DataEngine, OrderBook


def test_fresh_order_book_is_not_stale():
    book = OrderBook()
    book.update(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
    assert book.is_stale(max_age_s=3.0) is False


def test_order_book_becomes_stale_after_max_age():
    book = OrderBook()
    book.update(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
    book.last_update_ts = time.time() - 10.0  # simulate 10s since the last websocket update
    assert book.is_stale(max_age_s=3.0) is True


def test_never_updated_order_book_is_stale():
    book = OrderBook()  # last_update_ts starts at 0.0 (epoch)
    assert book.is_stale(max_age_s=3.0) is True


def test_data_engine_process_update_refreshes_staleness():
    engine = DataEngine()
    engine.process_update({
        'platform': 'Binance', 'symbol': 'BTC/USDC',
        'data': {'bids': [['100.0', '1.0']], 'asks': [['101.0', '1.0']]},
    })
    book = engine.order_books[('Binance', 'BTC/USDC')]
    assert book.is_stale(max_age_s=3.0) is False
