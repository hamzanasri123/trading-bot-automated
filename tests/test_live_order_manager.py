"""Unit tests for the safety logic added to LiveOrderManager: the leg-risk
emergency flatten and the daily kill switch. All exchange calls are mocked —
no network access and no real API keys are used or required.
"""
import asyncio
import datetime

from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from execution.live_order_manager import LiveOrderManager


def make_manager():
    notifier = AsyncMock()
    trade_logger = MagicMock()
    manager = LiveOrderManager(notifier, trade_logger)
    return manager, notifier, trade_logger


def sent_messages(notifier):
    return [call.args[0] for call in notifier.send_message.await_args_list]


async def settle():
    """record_pnl()/_trip_kill_switch() fire their Telegram alert via
    asyncio.create_task() (they're sync methods and can't await it directly).
    Give the event loop one tick so that task actually runs before we assert
    on it."""
    await asyncio.sleep(0)


# --- Kill switch: daily loss limit ---------------------------------------

async def test_kill_switch_trips_on_daily_loss_limit():
    manager, notifier, _ = make_manager()
    manager.record_pnl(-config.MAX_DAILY_LOSS_USD)
    await settle()
    assert manager.trading_halted is True
    assert any("Daily loss limit" in m for m in sent_messages(notifier))


async def test_kill_switch_does_not_trip_below_threshold():
    manager, notifier, _ = make_manager()
    manager.record_pnl(-(config.MAX_DAILY_LOSS_USD - 1))
    assert manager.trading_halted is False
    notifier.send_message.assert_not_awaited()


async def test_kill_switch_trip_is_permanent_until_restart():
    manager, notifier, _ = make_manager()
    manager.record_pnl(-config.MAX_DAILY_LOSS_USD)
    await settle()
    assert manager.trading_halted is True
    notifier.send_message.reset_mock()
    # A later profitable trade must NOT silently clear the halt.
    manager.record_pnl(1000.0)
    await settle()
    assert manager.trading_halted is True
    notifier.send_message.assert_not_awaited()  # no duplicate kill-switch alert


# --- Kill switch: consecutive leg-risk events -----------------------------

async def test_kill_switch_trips_on_consecutive_leg_risk_events():
    manager, notifier, _ = make_manager()
    for _ in range(config.MAX_CONSECUTIVE_LEG_RISK_EVENTS - 1):
        manager.record_pnl(-0.01, is_leg_risk_event=True)
        assert manager.trading_halted is False
    manager.record_pnl(-0.01, is_leg_risk_event=True)
    await settle()
    assert manager.trading_halted is True
    assert any("leg-risk events" in m for m in sent_messages(notifier))


async def test_non_leg_risk_event_resets_consecutive_counter():
    manager, _, _ = make_manager()
    manager.record_pnl(-0.01, is_leg_risk_event=True)
    assert manager._consecutive_leg_risk_events == 1
    manager.record_pnl(1.0, is_leg_risk_event=False)
    assert manager._consecutive_leg_risk_events == 0


# --- Daily PnL bookkeeping --------------------------------------------------

async def test_daily_pnl_resets_on_new_day():
    manager, _, _ = make_manager()
    manager.record_pnl(-10.0)
    assert manager._daily_pnl_usd == -10.0
    manager._daily_pnl_date = datetime.date.today() - datetime.timedelta(days=1)
    manager.record_pnl(-5.0)
    assert manager._daily_pnl_usd == -5.0  # old loss cleared, not accumulated


# --- execute_arbitrage: happy path -----------------------------------------

async def test_both_legs_succeed_records_profit_no_flatten():
    manager, notifier, trade_logger = make_manager()
    binance = AsyncMock()
    okx = AsyncMock()
    binance.create_limit_order.return_value = {'id': 'buy-1'}
    okx.create_limit_order.return_value = {'id': 'sell-1'}
    manager.exchanges = {'Binance': binance, 'OKX': okx}

    await manager.execute_arbitrage(
        volume=0.001, platform_buy='Binance', platform_sell='OKX',
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
        estimated_profit_usd=0.42,
    )

    assert manager.trading_halted is False
    assert manager._daily_pnl_usd == pytest.approx(0.42)
    binance.create_market_order.assert_not_called()
    okx.create_market_order.assert_not_called()

    trade_logger.log_trade.assert_called_once()
    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'ATTEMPTED'
    assert kwargs['profit_usd'] == pytest.approx(0.42)


# --- execute_arbitrage: leg risk --------------------------------------------

async def test_buy_leg_only_flattens_on_buy_platform():
    manager, notifier, trade_logger = make_manager()
    binance = AsyncMock()
    okx = AsyncMock()
    binance.create_limit_order.return_value = {'id': 'buy-1'}
    binance.create_market_order.return_value = {'id': 'flatten-1'}
    okx.create_limit_order.side_effect = Exception("insufficient funds")
    manager.exchanges = {'Binance': binance, 'OKX': okx}

    await manager.execute_arbitrage(
        volume=0.001, platform_buy='Binance', platform_sell='OKX',
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
        estimated_profit_usd=0.42,
    )

    # The buy leg that went through must be closed with an opposite market SELL
    # on the SAME platform (Binance), not on the platform whose order failed.
    binance.create_market_order.assert_awaited_once_with('BTC/USDC', 'sell', 0.001)
    okx.create_market_order.assert_not_called()

    assert manager._consecutive_leg_risk_events == 1
    assert manager._daily_pnl_usd < 0

    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'LEG_RISK_FLATTENED'
    assert any("LEG RISK" in m for m in sent_messages(notifier))


async def test_sell_leg_only_flattens_on_sell_platform():
    manager, notifier, trade_logger = make_manager()
    binance = AsyncMock()
    okx = AsyncMock()
    binance.create_limit_order.side_effect = Exception("rejected")
    okx.create_limit_order.return_value = {'id': 'sell-1'}
    okx.create_market_order.return_value = {'id': 'flatten-1'}
    manager.exchanges = {'Binance': binance, 'OKX': okx}

    await manager.execute_arbitrage(
        volume=0.002, platform_buy='Binance', platform_sell='OKX',
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
        estimated_profit_usd=0.10,
    )

    okx.create_market_order.assert_awaited_once_with('BTC/USDC', 'buy', 0.002)
    binance.create_market_order.assert_not_called()
    assert manager._consecutive_leg_risk_events == 1


async def test_both_legs_fail_no_flatten_no_pnl_impact():
    manager, notifier, trade_logger = make_manager()
    binance = AsyncMock()
    okx = AsyncMock()
    binance.create_limit_order.side_effect = Exception("down")
    okx.create_limit_order.side_effect = Exception("down")
    manager.exchanges = {'Binance': binance, 'OKX': okx}

    await manager.execute_arbitrage(
        volume=0.001, platform_buy='Binance', platform_sell='OKX',
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
        estimated_profit_usd=0.42,
    )

    assert manager._daily_pnl_usd == 0.0
    assert manager._consecutive_leg_risk_events == 0
    binance.create_market_order.assert_not_called()
    okx.create_market_order.assert_not_called()

    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'FAILED'


async def test_execute_arbitrage_skips_when_already_halted():
    manager, notifier, trade_logger = make_manager()
    manager.trading_halted = True
    binance = AsyncMock()
    okx = AsyncMock()
    manager.exchanges = {'Binance': binance, 'OKX': okx}

    await manager.execute_arbitrage(
        volume=0.001, platform_buy='Binance', platform_sell='OKX',
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
    )

    binance.create_limit_order.assert_not_called()
    okx.create_limit_order.assert_not_called()
    trade_logger.log_trade.assert_not_called()


async def test_flatten_failure_sends_critical_alert_and_trips_kill_switch():
    """If we can't even confirm the emergency flatten went through, that's a
    second leg-risk signal on top of the original one — with
    MAX_CONSECUTIVE_LEG_RISK_EVENTS=2 this must trip the kill switch."""
    manager, notifier, trade_logger = make_manager()
    binance = AsyncMock()
    okx = AsyncMock()
    binance.create_limit_order.return_value = {'id': 'buy-1'}
    binance.create_market_order.side_effect = Exception("exchange down, cannot flatten")
    okx.create_limit_order.side_effect = Exception("rejected")
    manager.exchanges = {'Binance': binance, 'OKX': okx}

    await manager.execute_arbitrage(
        volume=0.001, platform_buy='Binance', platform_sell='OKX',
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
        estimated_profit_usd=0.42,
    )

    await settle()
    messages = sent_messages(notifier)
    assert any("CRITICAL" in m for m in messages)
    assert manager._consecutive_leg_risk_events == config.MAX_CONSECUTIVE_LEG_RISK_EVENTS
    assert manager.trading_halted is True
    assert any("KILL SWITCH TRIGGERED" in m for m in messages)
