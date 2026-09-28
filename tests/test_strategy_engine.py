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


def _active_maker_trade(buy_price=49999.0, sell_price=50002.0):
    return {
        'buy_leg': {'id': 'buy-1', 'price': buy_price},
        'sell_leg': {'id': 'sell-1', 'price': sell_price},
        'status': 'active', 'creation_time': time.time(),
        'buy_platform': 'Binance', 'sell_platform': 'OKX', 'symbol': 'BTC/USDC',
    }


async def test_check_maker_trade_status_skips_queue_jump_on_stale_data(engine):
    buy_book = fresh_book(bid=50000.0, ask=50001.0)
    sell_book = fresh_book(bid=50000.0, ask=50001.0)
    sell_book.last_update_ts = time.time() - 10.0  # stale

    engine._order_books[('Binance', 'BTC/USDC')] = buy_book
    engine._order_books[('OKX', 'BTC/USDC')] = sell_book
    engine.active_maker_trade = _active_maker_trade(buy_price=49999.0, sell_price=50002.0)
    engine._order_manager.fetch_order_status = AsyncMock(return_value={'status': 'open'})
    engine.cancel_and_reset_maker_trade = AsyncMock()

    await engine.check_maker_trade_status()

    # The buy book's best bid (50000) is now above our resting buy price
    # (49999), which would normally be a queue-jump reposition trigger — but
    # the sell book is stale, so that decision must be suppressed.
    engine.cancel_and_reset_maker_trade.assert_not_called()
    engine._order_manager.fetch_order_status.assert_called()


async def test_check_maker_trade_status_still_queue_jumps_when_data_is_fresh(engine):
    buy_book = fresh_book(bid=50000.0, ask=50001.0)
    sell_book = fresh_book(bid=50000.0, ask=50001.0)

    engine._order_books[('Binance', 'BTC/USDC')] = buy_book
    engine._order_books[('OKX', 'BTC/USDC')] = sell_book
    engine.active_maker_trade = _active_maker_trade(buy_price=49999.0, sell_price=50002.0)
    engine._order_manager.fetch_order_status = AsyncMock(return_value={'status': 'open'})
    engine.cancel_and_reset_maker_trade = AsyncMock()

    await engine.check_maker_trade_status()

    engine.cancel_and_reset_maker_trade.assert_called_once()
