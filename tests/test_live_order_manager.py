"""Unit tests for LiveOrderManager's safety-critical execution path: the
hard notional cap, the pre-trade balance check, fill confirmation /
partial-fill handling, the leg-risk emergency flatten, and the kill switch.
All exchange calls are mocked — no network access and no real API keys are
used or required.
"""
import asyncio
import datetime

from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import execution.live_order_manager as live_order_manager
from execution.live_order_manager import LiveOrderManager


@pytest.fixture(autouse=True)
def fast_fill_polling(monkeypatch):
    """Shrink the fill-confirmation timeout/poll interval so tests that
    exercise an order which never fills don't have to wait out the real
    (multi-second) production timeout."""
    monkeypatch.setattr(live_order_manager, 'FILL_CONFIRMATION_TIMEOUT_S', 0.05)
    monkeypatch.setattr(live_order_manager, 'FILL_POLL_INTERVAL_S', 0.01)


def make_manager():
    notifier = AsyncMock()
    trade_logger = MagicMock()
    manager = LiveOrderManager(notifier, trade_logger)
    return manager, notifier, trade_logger


def make_exchange(free_balance=None, order_id='order-1', final_order=None, place_order_exception=None):
    """A mock ccxt exchange instance with sane defaults: enough balance,
    an order that places successfully and is immediately fully filled."""
    exchange = AsyncMock()
    exchange.fetch_free_balance.return_value = free_balance if free_balance is not None else {
        'USDC': 1_000_000.0, 'BTC': 1_000.0,
    }
    if place_order_exception is not None:
        exchange.create_limit_order.side_effect = place_order_exception
    else:
        exchange.create_limit_order.return_value = {'id': order_id}
    exchange.fetch_order.return_value = final_order if final_order is not None else {'status': 'closed', 'filled': 0.0}
    exchange.create_market_order.return_value = {'id': 'flatten-1'}
    return exchange


def wire(manager, binance=None, okx=None):
    manager.exchanges = {'Binance': binance or make_exchange(), 'OKX': okx or make_exchange()}
    return manager.exchanges['Binance'], manager.exchanges['OKX']


def sent_messages(notifier):
    return [call.args[0] for call in notifier.send_message.await_args_list]


async def settle():
    """Kill-switch alerts are fired via asyncio.create_task() from sync
    methods; give the event loop one tick so that task actually runs."""
    await asyncio.sleep(0)


ARB_KWARGS = dict(
    volume=0.0002, platform_buy='Binance', platform_sell='OKX',
    max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
    estimated_profit_usd=0.42,
)  # notional = 0.0002 * 50000 = $10, comfortably under MAX_TRADE_SIZE_USD ($15)


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
    manager.record_pnl(1000.0)  # a later profitable trade must not silently clear the halt
    await settle()
    assert manager.trading_halted is True
    notifier.send_message.assert_not_awaited()


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


async def test_daily_pnl_resets_on_new_day():
    manager, _, _ = make_manager()
    manager.record_pnl(-10.0)
    assert manager._daily_pnl_usd == -10.0
    manager._daily_pnl_date = datetime.date.today() - datetime.timedelta(days=1)
    manager.record_pnl(-5.0)
    assert manager._daily_pnl_usd == -5.0  # old loss cleared, not accumulated


# --- Hard notional cap (defense in depth) ----------------------------------

async def test_hard_cap_rejects_oversized_order_before_touching_exchanges():
    manager, notifier, trade_logger = make_manager()
    binance, okx = wire(manager)

    await manager.execute_arbitrage(
        volume=1.0, platform_buy='Binance', platform_sell='OKX',  # 1 BTC @ 50000 => way over the cap
        max_buy_price=50000, min_sell_price=50100, symbol='BTC/USDC',
        estimated_profit_usd=100.0,
    )

    binance.fetch_free_balance.assert_not_called()
    binance.create_limit_order.assert_not_called()
    okx.create_limit_order.assert_not_called()
    trade_logger.log_trade.assert_not_called()
    assert any("ORDER REJECTED" in m for m in sent_messages(notifier))


# --- Pre-trade balance check -------------------------------------------------

async def test_insufficient_quote_balance_aborts_before_any_order():
    manager, notifier, trade_logger = make_manager()
    binance, okx = wire(manager, binance=make_exchange(free_balance={'USDC': 1.0, 'BTC': 1000.0}))

    await manager.execute_arbitrage(**ARB_KWARGS)

    binance.create_limit_order.assert_not_called()
    okx.create_limit_order.assert_not_called()
    trade_logger.log_trade.assert_not_called()
    assert any("Insufficient USDC" in m for m in sent_messages(notifier))


async def test_insufficient_base_balance_aborts_before_any_order():
    manager, notifier, trade_logger = make_manager()
    binance, okx = wire(manager, okx=make_exchange(free_balance={'USDC': 1_000_000.0, 'BTC': 0.0}))

    await manager.execute_arbitrage(**ARB_KWARGS)

    binance.create_limit_order.assert_not_called()
    okx.create_limit_order.assert_not_called()
    assert any("Insufficient BTC" in m for m in sent_messages(notifier))


async def test_unverifiable_balance_aborts_safely():
    manager, notifier, trade_logger = make_manager()
    binance, okx = wire(manager, binance=make_exchange())
    binance.fetch_free_balance.side_effect = Exception("exchange unreachable")

    await manager.execute_arbitrage(**ARB_KWARGS)

    binance.create_limit_order.assert_not_called()
    okx.create_limit_order.assert_not_called()
    assert any("Could not verify account balances" in m for m in sent_messages(notifier))


# --- Happy path: both legs fully fill ---------------------------------------

async def test_both_legs_fully_fill_records_profit_no_flatten():
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    binance, okx = wire(
        manager,
        binance=make_exchange(order_id='buy-1', final_order={'status': 'closed', 'filled': volume}),
        okx=make_exchange(order_id='sell-1', final_order={'status': 'closed', 'filled': volume}),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    assert manager.trading_halted is False
    assert manager._daily_pnl_usd == pytest.approx(0.42)
    binance.create_market_order.assert_not_called()
    okx.create_market_order.assert_not_called()

    trade_logger.log_trade.assert_called_once()
    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'FILLED'
    assert kwargs['profit_usd'] == pytest.approx(0.42)


# --- Leg risk: one side never even places -----------------------------------

async def test_buy_leg_fails_to_place_sell_fills_gets_flattened():
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    binance, okx = wire(
        manager,
        binance=make_exchange(place_order_exception=Exception("insufficient funds")),
        okx=make_exchange(order_id='sell-1', final_order={'status': 'closed', 'filled': volume}),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    # We sold on OKX but never bought on Binance: buy back the shortfall on OKX.
    okx.create_market_order.assert_awaited_once_with('BTC/USDC', 'buy', pytest.approx(volume))
    binance.create_market_order.assert_not_called()
    assert manager._consecutive_leg_risk_events == 1

    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'LEG_RISK_FLATTENED'
    assert any("LEG RISK" in m for m in sent_messages(notifier))


async def test_sell_leg_fails_to_place_buy_fills_gets_flattened():
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    binance, okx = wire(
        manager,
        binance=make_exchange(order_id='buy-1', final_order={'status': 'closed', 'filled': volume}),
        okx=make_exchange(place_order_exception=Exception("rejected")),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    # We bought on Binance but never sold on OKX: sell the excess back on Binance.
    binance.create_market_order.assert_awaited_once_with('BTC/USDC', 'sell', pytest.approx(volume))
    okx.create_market_order.assert_not_called()
    assert manager._consecutive_leg_risk_events == 1


async def test_both_legs_fail_to_place_no_flatten_no_pnl_impact():
    manager, notifier, trade_logger = make_manager()
    binance, okx = wire(
        manager,
        binance=make_exchange(place_order_exception=Exception("down")),
        okx=make_exchange(place_order_exception=Exception("down")),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    assert manager._daily_pnl_usd == 0.0
    assert manager._consecutive_leg_risk_events == 0
    binance.create_market_order.assert_not_called()
    okx.create_market_order.assert_not_called()

    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'FAILED'


# --- Leg risk: both place, but fill unevenly (partial fill) ----------------

async def test_partial_fill_mismatch_flattens_only_the_net_exposure():
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    half = volume / 2
    binance, okx = wire(
        manager,
        binance=make_exchange(order_id='buy-1', final_order={'status': 'closed', 'filled': volume}),
        okx=make_exchange(order_id='sell-1', final_order={'status': 'closed', 'filled': half}),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    # Bought the full volume but only sold half: flatten just the unsold half, on the buy platform.
    binance.create_market_order.assert_awaited_once_with('BTC/USDC', 'sell', pytest.approx(half))
    okx.create_market_order.assert_not_called()
    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'LEG_RISK_FLATTENED'


async def test_equal_partial_fills_are_hedged_no_flatten():
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    half = volume / 2
    binance, okx = wire(
        manager,
        binance=make_exchange(order_id='buy-1', final_order={'status': 'closed', 'filled': half}),
        okx=make_exchange(order_id='sell-1', final_order={'status': 'closed', 'filled': half}),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    binance.create_market_order.assert_not_called()
    okx.create_market_order.assert_not_called()
    kwargs = trade_logger.log_trade.call_args.kwargs
    assert kwargs['status'] == 'PARTIAL_FILL_HEDGED'
    # Profit should be scaled down proportionally to the actual filled ratio (50%).
    assert kwargs['profit_usd'] == pytest.approx(0.21)


async def test_unfilled_open_order_gets_cancelled():
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    binance, okx = wire(
        manager,
        binance=make_exchange(order_id='buy-1', final_order={'status': 'open', 'filled': 0.0}),
        okx=make_exchange(order_id='sell-1', final_order={'status': 'closed', 'filled': volume}),
    )

    await manager.execute_arbitrage(**ARB_KWARGS)

    # The buy leg never filled and timed out still "open": it must be
    # cancelled so it can't fill later, unmonitored, after we've already
    # flattened against the sell leg that did fill.
    binance.cancel_order.assert_awaited_once_with('buy-1', 'BTC/USDC')
    okx.create_market_order.assert_awaited_once_with('BTC/USDC', 'buy', pytest.approx(volume))


# --- Kill switch gating ------------------------------------------------------

async def test_execute_arbitrage_skips_entirely_when_already_halted():
    manager, notifier, trade_logger = make_manager()
    manager.trading_halted = True
    binance, okx = wire(manager)

    await manager.execute_arbitrage(**ARB_KWARGS)

    binance.fetch_free_balance.assert_not_called()
    binance.create_limit_order.assert_not_called()
    okx.create_limit_order.assert_not_called()
    trade_logger.log_trade.assert_not_called()


async def test_flatten_failure_sends_critical_alert_and_trips_kill_switch():
    """If we can't even confirm the emergency flatten went through, that's a
    second leg-risk signal on top of the original one — with
    MAX_CONSECUTIVE_LEG_RISK_EVENTS=2 this must trip the kill switch."""
    manager, notifier, trade_logger = make_manager()
    volume = ARB_KWARGS['volume']
    binance = make_exchange(order_id='buy-1', final_order={'status': 'closed', 'filled': volume})
    binance.create_market_order.side_effect = Exception("exchange down, cannot flatten")
    okx = make_exchange(place_order_exception=Exception("rejected"))
    wire(manager, binance=binance, okx=okx)

    await manager.execute_arbitrage(**ARB_KWARGS)
    await settle()

    messages = sent_messages(notifier)
    assert any("CRITICAL" in m for m in messages)
    assert manager._consecutive_leg_risk_events == config.MAX_CONSECUTIVE_LEG_RISK_EVENTS
    assert manager.trading_halted is True
    assert any("KILL SWITCH TRIGGERED" in m for m in messages)
