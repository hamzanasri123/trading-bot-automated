"""Unit test verifying StrategyEngine actually honors the order-book
staleness guard: it must not evaluate (and therefore never trade on) a pair
where either side's order book hasn't updated recently."""
import time

from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.data_engine import OrderBook
from engine.strategy_engine import StrategyEngine


@pytest.fixture
def engine():
    order_manager = MagicMock()
    order_manager.trading_halted = False
    order_manager.get_fees.return_value = {'maker': 0.1, 'taker': 0.1}
    notifier = AsyncMock()
    strategy_engine = StrategyEngine(order_books={}, order_manager=order_manager, notifier=notifier)
    yield strategy_engine
    strategy_engine.process_pool.shutdown(wait=False)


def fresh_book(bid=50100.0, ask=50000.0):
    book = OrderBook()
    book.update(bids=[(bid, 1.0)], asks=[(ask, 1.0)])
    return book


async def test_evaluate_market_pair_skips_when_buy_book_is_stale(engine):
    buy_book = fresh_book()
    buy_book.last_update_ts = time.time() - 10.0  # stale
    sell_book = fresh_book()

    await engine.evaluate_market_pair(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

    engine._order_manager.get_fees.assert_not_called()


async def test_evaluate_market_pair_skips_when_sell_book_is_stale(engine):
    buy_book = fresh_book()
    sell_book = fresh_book()
    sell_book.last_update_ts = time.time() - 10.0  # stale

    await engine.evaluate_market_pair(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

    engine._order_manager.get_fees.assert_not_called()


async def test_evaluate_market_pair_proceeds_when_both_books_are_fresh(engine):
    buy_book = fresh_book()
    sell_book = fresh_book()

    await engine.evaluate_market_pair(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

    engine._order_manager.get_fees.assert_called()
