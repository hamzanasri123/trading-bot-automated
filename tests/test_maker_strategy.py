"""Unit tests for the safety checks added to StrategyEngine.execute_maker_strategy:
the hard notional cap and the pre-trade balance check, reused from
LiveOrderManager, must gate order placement exactly like they do for the
taker path. All exchange/order-manager interaction is mocked.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from engine.data_engine import OrderBook
from engine.strategy_engine import StrategyEngine


def make_order_manager(over_cap=False, sufficient_balance=True, place_results=None):
    order_manager = MagicMock()
    order_manager.trading_halted = False
    order_manager.get_fees.return_value = {'maker': 0.1, 'taker': 0.1}
    order_manager.reject_if_over_cap = AsyncMock(return_value=over_cap)
    order_manager.check_sufficient_balance = AsyncMock(return_value=sufficient_balance)
    if place_results is None:
        place_results = [{'id': 'buy-1', 'price': 0}, {'id': 'sell-1', 'price': 0}]
    order_manager.create_limit_order = AsyncMock(side_effect=place_results)
    order_manager.cancel_order = AsyncMock(return_value=True)
    return order_manager


def crossed_books(bid=50000.0, ask=50010.0):
    buy_book = OrderBook()
    buy_book.update(bids=[(bid, 1.0)], asks=[(ask, 1.0)])
    sell_book = OrderBook()
    sell_book.update(bids=[(bid, 1.0)], asks=[(ask, 1.0)])
    return buy_book, sell_book


async def test_maker_strategy_refuses_when_over_hard_cap():
    order_manager = make_order_manager(over_cap=True)
    notifier = AsyncMock()
    engine = StrategyEngine(order_books={}, order_manager=order_manager, notifier=notifier)
    try:
        buy_book, sell_book = crossed_books()
        await engine.execute_maker_strategy(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

        order_manager.check_sufficient_balance.assert_not_called()
        order_manager.create_limit_order.assert_not_called()
        assert engine.active_maker_trade is None
        # Refusing the order must not have left trading disabled for no reason.
        assert engine._is_trading_enabled is True
    finally:
        engine.process_pool.shutdown(wait=False)


async def test_maker_strategy_aborts_on_insufficient_balance():
    order_manager = make_order_manager(over_cap=False, sufficient_balance=False)
    notifier = AsyncMock()
    engine = StrategyEngine(order_books={}, order_manager=order_manager, notifier=notifier)
    try:
        buy_book, sell_book = crossed_books()
        await engine.execute_maker_strategy(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

        order_manager.create_limit_order.assert_not_called()
        assert engine.active_maker_trade is None
        assert engine._is_trading_enabled is True
    finally:
        engine.process_pool.shutdown(wait=False)


async def test_maker_strategy_places_both_legs_when_checks_pass(monkeypatch):
    order_manager = make_order_manager(over_cap=False, sufficient_balance=True)
    notifier = AsyncMock()
    engine = StrategyEngine(order_books={}, order_manager=order_manager, notifier=notifier)
    monkeypatch.setattr(engine, 'maker_trade_monitoring_loop', AsyncMock())
    try:
        buy_book, sell_book = crossed_books()
        await engine.execute_maker_strategy(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

        assert order_manager.create_limit_order.await_count == 2
        assert engine.active_maker_trade is not None
        assert engine.active_maker_trade['buy_leg']['id'] == 'buy-1'
        assert engine.active_maker_trade['sell_leg']['id'] == 'sell-1'
    finally:
        engine.process_pool.shutdown(wait=False)


async def test_maker_strategy_notional_matches_max_trade_size():
    """The cap check must be called with (roughly) MAX_TRADE_SIZE_USD, since
    volume is sized as MAX_TRADE_SIZE_USD / our_buy_price by construction."""
    order_manager = make_order_manager()
    notifier = AsyncMock()
    engine = StrategyEngine(order_books={}, order_manager=order_manager, notifier=notifier)
    try:
        buy_book, sell_book = crossed_books()
        await engine.execute_maker_strategy(buy_book, sell_book, 'Binance', 'OKX', 'BTC/USDC')

        notional_checked = order_manager.reject_if_over_cap.call_args.args[0]
        assert notional_checked == pytest.approx(config.MAX_TRADE_SIZE_USD, rel=1e-6)
    finally:
        engine.process_pool.shutdown(wait=False)
