# execution/live_order_manager.py
import asyncio, logging, datetime
import ccxt.async_support as ccxt
from config import (
    API_KEYS, PAPER_TRADING_MODE, MAX_TRADE_SIZE_USD,
    MAX_DAILY_LOSS_USD, MAX_CONSECUTIVE_LEG_RISK_EVENTS,
    MIN_BASE_CURRENCY_BALANCE, LOW_BALANCE_WARNING_COOLDOWN_S,
)

BALANCE_CHECK_INTERVAL_S = 60.0

# Rough slippage+fees penalty (in fraction of notional) applied when a leg is
# emergency-flattened at market. We don't know the exact fill price ahead of
# time, so this is a conservative estimate used only to feed the kill switch.
EMERGENCY_FLATTEN_PENALTY_PCT = 0.2  # 0.2% of notional

# How long we're willing to wait for a "marketable" limit order to actually
# fill before we give up, cancel the remainder, and treat whatever quantity
# did fill as a position that needs reconciling against the other leg.
FILL_CONFIRMATION_TIMEOUT_S = 5.0
FILL_POLL_INTERVAL_S = 0.5

# Quantities smaller than this are treated as filled/unfilled exactly (dust
# from float arithmetic), so we don't try to flatten a residue too small for
# an exchange to even accept as an order.
DUST_QTY = 1e-8

# Safety margin over MAX_TRADE_SIZE_USD before we refuse to place an order at
# all. This is a defense-in-depth check: the strategy engine is supposed to
# size trades under the cap already, but a bug there should never be able to
# push a real order past the limit.
HARD_CAP_TOLERANCE_PCT = 5.0  # allow 5% slack for price movement between sizing and execution

class LiveOrderManager:
    def __init__(self, notifier, trade_logger):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchanges = {}
        self.fees = {}
        self.notifier = notifier
        self.trade_logger = trade_logger

        # --- Kill switch state ---
        self.trading_halted = False
        self._daily_pnl_usd = 0.0
        self._daily_pnl_date = datetime.date.today()
        self._consecutive_leg_risk_events = 0

        # --- Low-balance alerting state (platform, currency) -> last warned monotonic time ---
        self._low_balance_last_warned = {}

    async def initialize(self):
        self.logger.info("Initializing LiveOrderManager...")
        for name, keys in API_KEYS.items():
            if not keys['apiKey'] or 'YOUR' in keys['apiKey']:
                self.logger.warning(f"Invalid API keys for {name}. This exchange will be skipped."); continue
            try:
                # Configuration de base
                config = {'apiKey': keys['apiKey'], 'secret': keys['secret'], 'enableRateLimit': True}
                if name == 'OKX': config['password'] = keys['password']
                
                exchange_class = getattr(ccxt, name.lower())
                instance = exchange_class(config)

                # --- CORRECTION DÉFINITIVE APPLIQUÉE ICI ---
                # Si on est en Paper Trading, on doit ajouter des options spécifiques
                if PAPER_TRADING_MODE:
                    self.logger.info(f"Paper Trading (Testnet) mode enabled for {name}.")
                    if name == 'OKX':
                        # Solution trouvée par vous ! Nécessaire pour le Paper Trading OKX.
                        instance.options['x-simulated-trading'] = '1'
                    
                    # La méthode set_sandbox_mode est plus générale pour les autres plateformes
                    if instance.has['test']:
                        instance.set_sandbox_mode(True)
                    else:
                        if name != 'OKX': # OKX est géré manuellement, on ne log que pour les autres
                           self.logger.warning(f"Exchange {name} does not have a standard testnet via ccxt.set_sandbox_mode().")

                await instance.load_markets(reload=True)
                self.exchanges[name] = instance
                self.logger.info(f"Successfully connected and synced with: {name}")
                
                symbol_to_trade = "BTC/USDC"

                if symbol_to_trade in instance.markets:
                    market = instance.markets[symbol_to_trade]
                    self.fees[name] = {'maker': market['maker'] * 100, 'taker': market['taker'] * 100}
                    self.logger.info(f"Fees for {name} ({symbol_to_trade}): Maker {self.fees[name]['maker']:.4f}%, Taker {self.fees[name]['taker']:.4f}%")
                else: self.logger.error(f"Could not find market {symbol_to_trade} for {name} to fetch fees.")
            except Exception as e: self.logger.error(f"Failed to initialize {name}: {e}", exc_info=True)

    # ... (le reste du fichier ne change pas) ...
    def get_fees(self, platform: str) -> dict:
        return self.fees.get(platform, {'maker': 0.1, 'taker': 0.1})

    async def get_balance(self, platform: str, currency: str):
        if platform not in self.exchanges: return None
        try:
            balance = await self.exchanges[platform].fetch_free_balance()
            return balance.get(currency, 0.0)
        except Exception as e:
            self.logger.error(f"Error fetching balance for {currency} on {platform}: {e}"); return None

    async def _warn_if_low(self, platform: str, currency: str, balance: float, threshold: float, loop):
        if balance >= threshold:
            return
        key = (platform, currency)
        # loop.time() is a monotonic clock with no fixed epoch (often
        # system-uptime-based) — a 0.0 sentinel for "never warned" would
        # wrongly suppress the very first alert on a freshly started
        # process/container where loop.time() itself is still small.
        last_warned = self._low_balance_last_warned.get(key, float('-inf'))
        if loop.time() - last_warned < LOW_BALANCE_WARNING_COOLDOWN_S:
            return  # already warned about this recently, don't spam
        self._low_balance_last_warned[key] = loop.time()
        self.logger.warning(f"LOW BALANCE: {platform} has only {balance:.6f} {currency} (below {threshold:.6f}).")
        await self.notifier.send_message(
            f"💸 *LOW BALANCE* 💸\n{platform}: {balance:.6f} {currency} — below the {threshold:.6f} needed to keep trading this pair.\n"
            f"This bot does not rebalance automatically — a manual transfer/rebalance may be needed."
        )

    async def monitor_balances(self, symbol: str):
        """Background loop: periodically checks balances on every configured
        exchange and proactively alerts (rate-limited) when one drops below
        what's needed for another trade, instead of trades just silently
        failing the pre-trade balance check with no one aware why."""
        base_currency, quote_currency = symbol.split('/')
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(BALANCE_CHECK_INTERVAL_S)
            for platform in list(self.exchanges.keys()):
                quote_balance = await self.get_balance(platform, quote_currency)
                if quote_balance is not None:
                    await self._warn_if_low(platform, quote_currency, quote_balance, MAX_TRADE_SIZE_USD, loop)

                base_balance = await self.get_balance(platform, base_currency)
                if base_balance is not None:
                    await self._warn_if_low(platform, base_currency, base_balance, MIN_BASE_CURRENCY_BALANCE, loop)

    def _reset_daily_pnl_if_needed(self):
        today = datetime.date.today()
        if today != self._daily_pnl_date:
            self._daily_pnl_date = today
            self._daily_pnl_usd = 0.0
            self._consecutive_leg_risk_events = 0
            self.logger.info("Daily PnL counter reset for the new day.")

    def _trip_kill_switch(self, reason: str):
        if self.trading_halted:
            return
        self.trading_halted = True
        self.logger.critical(f"KILL SWITCH TRIGGERED: {reason}. Trading halted — manual restart required.")
        asyncio.create_task(self.notifier.send_message(
            f"🛑 *KILL SWITCH TRIGGERED* 🛑\n{reason}\nTrading has been halted. A manual restart is required after review."
        ))

    def record_pnl(self, amount_usd: float, is_leg_risk_event: bool = False):
        """Feed an estimated PnL outcome (positive or negative) into the daily
        loss kill switch, and track consecutive leg-risk events."""
        self._reset_daily_pnl_if_needed()
        self._daily_pnl_usd += amount_usd
        self._consecutive_leg_risk_events = self._consecutive_leg_risk_events + 1 if is_leg_risk_event else 0

        if self._daily_pnl_usd <= -MAX_DAILY_LOSS_USD:
            self._trip_kill_switch(f"Daily loss limit reached: estimated PnL {self._daily_pnl_usd:.2f} USD <= -{MAX_DAILY_LOSS_USD} USD.")
        elif self._consecutive_leg_risk_events >= MAX_CONSECUTIVE_LEG_RISK_EVENTS:
            self._trip_kill_switch(f"{self._consecutive_leg_risk_events} consecutive leg-risk events detected.")

    async def _flatten_leg(self, platform: str, symbol: str, closing_side: str, volume: float):
        """Emergency-close an unhedged position with a market order after the
        opposite leg of an arbitrage trade failed to place."""
        try:
            order = await self.exchanges[platform].create_market_order(symbol, closing_side, volume)
            self.logger.warning(f"Flattened unhedged leg on {platform}: {closing_side} {volume:.6f} {symbol}. Order ID: {order.get('id')}")
            await self.notifier.send_message(f"✅ Unhedged position flattened on {platform} ({closing_side} {volume:.6f} {symbol}).")
        except Exception as e:
            self.logger.critical(f"FAILED TO FLATTEN UNHEDGED POSITION on {platform} ({closing_side} {volume:.6f} {symbol}): {e}", exc_info=True)
            await self.notifier.send_message(
                f"🆘 *CRITICAL* 🆘\nFailed to flatten an unhedged position on {platform}!\n"
                f"Side: {closing_side}, Volume: {volume:.6f} {symbol}\nManual intervention required immediately.\nReason: `{e}`"
            )
            # We could not confirm the position was closed: treat it as a leg-risk event for the kill switch.
            self.record_pnl(0.0, is_leg_risk_event=True)

    async def _wait_for_fill(self, platform: str, order_id: str, symbol: str,
                              timeout_s: float = None, poll_interval_s: float = None):
        """Poll an order until it reaches a terminal state (closed/canceled/
        expired/rejected) or the timeout is reached. Returns the last known
        order dict (or None if it could never be fetched)."""
        # Read the module-level defaults at call time (not def time) so tests
        # can shrink them for speed without monkeypatching every call site.
        timeout_s = FILL_CONFIRMATION_TIMEOUT_S if timeout_s is None else timeout_s
        poll_interval_s = FILL_POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        order = None
        while True:
            order = await self.fetch_order_status(platform, order_id, symbol)
            if order and order.get('status') in ('closed', 'canceled', 'expired', 'rejected'):
                return order
            if loop.time() >= deadline:
                return order
            await asyncio.sleep(poll_interval_s)

    async def _place_and_confirm(self, platform: str, symbol: str, side: str, volume: float, price: float) -> dict:
        """Place a leg and wait to see how much of it actually filled. Never
        assumes a placed order is a filled order. Returns
        {'order_id', 'filled', 'status'}; filled is 0.0 on any failure."""
        order = await self.create_limit_order(platform, symbol, side, volume, price)
        if not order or not order.get('id'):
            return {'order_id': None, 'filled': 0.0, 'status': 'FAILED'}

        order_id = order['id']
        final = await self._wait_for_fill(platform, order_id, symbol)
        filled = float((final or order).get('filled') or 0.0)
        order_status = (final or order).get('status', 'unknown')

        if order_status == 'open' and filled < volume - DUST_QTY:
            # Still open after our patience ran out: stop it from filling later, unmonitored.
            self.logger.warning(f"Order {order_id} on {platform} still open after {FILL_CONFIRMATION_TIMEOUT_S}s ({filled:.8f}/{volume:.8f} filled) — cancelling remainder.")
            await self.cancel_order(platform, order_id, symbol)

        return {'order_id': order_id, 'filled': filled, 'status': order_status}

    def exceeds_hard_cap(self, notional_usd: float) -> tuple:
        """Shared defense-in-depth check, used by both the taker and maker
        paths: an order's notional value must never exceed MAX_TRADE_SIZE_USD
        by more than a small tolerance, regardless of what sized it."""
        hard_cap = MAX_TRADE_SIZE_USD * (1 + HARD_CAP_TOLERANCE_PCT / 100)
        return notional_usd > hard_cap, hard_cap

    async def reject_if_over_cap(self, notional_usd: float, context: str) -> bool:
        """Returns True (and alerts) if the order must be refused."""
        over_cap, hard_cap = self.exceeds_hard_cap(notional_usd)
        if over_cap:
            self.logger.critical(f"ORDER REJECTED ({context}): requested notional {notional_usd:.2f} USD exceeds the hard cap of {hard_cap:.2f} USD (MAX_TRADE_SIZE_USD={MAX_TRADE_SIZE_USD}). This should never happen — refusing to trade.")
            await self.notifier.send_message(f"🛑 *ORDER REJECTED* 🛑\n[{context}] Requested notional {notional_usd:.2f} USD exceeds the configured cap of {MAX_TRADE_SIZE_USD} USD. Refusing to trade — check strategy sizing logic.")
        return over_cap

    async def check_sufficient_balance(self, platform_buy: str, platform_sell: str, symbol: str, volume: float, max_buy_price: float) -> bool:
        base_currency, quote_currency = symbol.split('/')
        quote_balance, base_balance = await asyncio.gather(
            self.get_balance(platform_buy, quote_currency),
            self.get_balance(platform_sell, base_currency),
        )
        if quote_balance is None or base_balance is None:
            self.logger.error("Could not verify account balances before trading. Aborting for safety.")
            await self.notifier.send_message("⚠️ *Trade Aborted* ⚠️\nCould not verify account balances before execution.")
            return False

        required_quote = volume * max_buy_price
        if quote_balance < required_quote:
            self.logger.error(f"Insufficient {quote_currency} on {platform_buy}. Needed ~{required_quote:.2f}, have {quote_balance:.2f}.")
            await self.notifier.send_message(f"⚠️ *Trade Aborted* ⚠️\nInsufficient {quote_currency} on {platform_buy} to execute buy order.")
            return False
        if base_balance < volume:
            self.logger.error(f"Insufficient {base_currency} on {platform_sell}. Needed {volume:.6f}, have {base_balance:.6f}.")
            await self.notifier.send_message(f"⚠️ *Trade Aborted* ⚠️\nInsufficient {base_currency} on {platform_sell} to execute sell order.")
            return False
        return True

    async def execute_arbitrage(self, volume: float, platform_buy: str, platform_sell: str, max_buy_price: float, min_sell_price: float, symbol: str, estimated_profit_usd: float = 0.0):
        if self.trading_halted:
            self.logger.warning("Kill switch is active — skipping arbitrage execution.")
            return

        notional_usd = volume * max(max_buy_price, min_sell_price)
        if await self.reject_if_over_cap(notional_usd, context='TAKER'):
            return

        if not await self.check_sufficient_balance(platform_buy, platform_sell, symbol, volume, max_buy_price):
            return

        buy_leg, sell_leg = await asyncio.gather(
            self._place_and_confirm(platform_buy, symbol, 'buy', volume, max_buy_price),
            self._place_and_confirm(platform_sell, symbol, 'sell', volume, min_sell_price),
        )
        buy_id, sell_id = buy_leg['order_id'], sell_leg['order_id']
        net_exposure = round(buy_leg['filled'] - sell_leg['filled'], 10)

        status = 'ATTEMPTED'
        profit_usd = None

        if abs(net_exposure) <= DUST_QTY:
            if buy_leg['filled'] > DUST_QTY:
                # Both legs filled (fully or by the same partial amount): hedged.
                fill_ratio = buy_leg['filled'] / volume if volume > 0 else 1.0
                status = 'FILLED' if fill_ratio >= 1 - 1e-6 else 'PARTIAL_FILL_HEDGED'
                profit_usd = estimated_profit_usd * fill_ratio
                self.record_pnl(profit_usd, is_leg_risk_event=False)
            else:
                # Neither leg filled at all: no position was ever taken, nothing to flatten.
                status = 'FAILED'
        elif net_exposure > 0:
            # Bought more base currency than we managed to sell: close the excess on the buy platform.
            self.logger.error(f"LEG RISK: net unhedged exposure of +{net_exposure:.8f} {symbol.split('/')[0]} on {platform_buy} (buy filled {buy_leg['filled']:.8f}, sell filled {sell_leg['filled']:.8f}). Flattening.")
            await self.notifier.send_message(f"🚨 *LEG RISK* 🚨\nUnhedged exposure of +{net_exposure:.8f} {symbol} on {platform_buy} (buy leg outran sell leg). Flattening the position now.")
            estimated_loss = -abs(net_exposure * max_buy_price * EMERGENCY_FLATTEN_PENALTY_PCT / 100)
            await self._flatten_leg(platform_buy, symbol, 'sell', net_exposure)
            status = 'LEG_RISK_FLATTENED'
            profit_usd = estimated_loss
            self.record_pnl(estimated_loss, is_leg_risk_event=True)
        else:
            # Sold more base currency than we managed to buy back: close the shortfall on the sell platform.
            shortfall = abs(net_exposure)
            self.logger.error(f"LEG RISK: net unhedged exposure of -{shortfall:.8f} {symbol.split('/')[0]} on {platform_sell} (buy filled {buy_leg['filled']:.8f}, sell filled {sell_leg['filled']:.8f}). Flattening.")
            await self.notifier.send_message(f"🚨 *LEG RISK* 🚨\nUnhedged exposure of -{shortfall:.8f} {symbol} on {platform_sell} (sell leg outran buy leg). Flattening the position now.")
            estimated_loss = -abs(shortfall * min_sell_price * EMERGENCY_FLATTEN_PENALTY_PCT / 100)
            await self._flatten_leg(platform_sell, symbol, 'buy', shortfall)
            status = 'LEG_RISK_FLATTENED'
            profit_usd = estimated_loss
            self.record_pnl(estimated_loss, is_leg_risk_event=True)

        self.trade_logger.log_trade(event_type='TAKER_ATTEMPT', strategy_type='TAKER', symbol=symbol, volume=volume, buy_platform=platform_buy, sell_platform=platform_sell, buy_order_id=buy_id, sell_order_id=sell_id, status=status, profit_usd=profit_usd)

    async def create_limit_order(self, platform: str, symbol: str, side: str, amount: float, price: float, post_only: bool = False):
        if platform not in self.exchanges:
            self.logger.error(f"Attempted to place order on uninitialized platform: {platform}")
            return None
        try:
            params = {}
            if post_only: params['postOnly'] = True
            self.logger.info(f"Placing LIMIT {side} order: {amount:.6f} {symbol} @ {price:.2f} on {platform} {'(Post-Only)' if post_only else ''}")
            order = await self.exchanges[platform].create_limit_order(symbol, side, amount, price, params)
            self.logger.info(f"Successfully placed order on {platform}. Order ID: {order['id']}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order on {platform}: {e}")
            await self.notifier.send_message(f"🔥 *ORDER FAILED* 🔥\nFailed to place {side} order on {platform}.\nReason: `{e}`")
            return None

    async def cancel_order(self, platform: str, order_id: str, symbol: str):
        if platform not in self.exchanges: return False
        try:
            self.logger.warning(f"Cancelling order {order_id} on {platform}")
            await self.exchanges[platform].cancel_order(order_id, symbol)
            return True
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id} on {platform}: {e}"); return False

    async def fetch_order_status(self, platform: str, order_id: str, symbol: str):
        if platform not in self.exchanges: return None
        try:
            return await self.exchanges[platform].fetch_order(order_id, symbol)
        except Exception as e:
            self.logger.error(f"Failed to fetch status for order {order_id} on {platform}: {e}"); return None

    async def close_all(self):
        self.logger.info("Closing all exchange connections...")
        for name, instance in self.exchanges.items():
            try:
                await instance.close()
                self.logger.info(f"Connection to {name} closed.")
            except Exception as e:
                self.logger.error(f"Error closing connection to {name}: {e}")
