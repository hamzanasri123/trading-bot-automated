# execution/live_order_manager.py
import asyncio, logging, datetime
import ccxt.async_support as ccxt
from config import (
    API_KEYS, PAPER_TRADING_MODE, MAX_TRADE_SIZE_USD,
    MAX_DAILY_LOSS_USD, MAX_CONSECUTIVE_LEG_RISK_EVENTS,
)

# Rough slippage+fees penalty (in fraction of notional) applied when a leg is
# emergency-flattened at market. We don't know the exact fill price ahead of
# time, so this is a conservative estimate used only to feed the kill switch.
EMERGENCY_FLATTEN_PENALTY_PCT = 0.2  # 0.2% of notional

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

    async def execute_arbitrage(self, volume: float, platform_buy: str, platform_sell: str, max_buy_price: float, min_sell_price: float, symbol: str, estimated_profit_usd: float = 0.0):
        if self.trading_halted:
            self.logger.warning("Kill switch is active — skipping arbitrage execution.")
            return

        buy_order_task = asyncio.create_task(self.create_limit_order(platform_buy, symbol, 'buy', volume, max_buy_price))
        sell_order_task = asyncio.create_task(self.create_limit_order(platform_sell, symbol, 'sell', volume, min_sell_price))
        buy_result, sell_result = await asyncio.gather(buy_order_task, sell_order_task, return_exceptions=True)
        buy_id = buy_result.get('id') if isinstance(buy_result, dict) else None
        sell_id = sell_result.get('id') if isinstance(sell_result, dict) else None

        status = 'ATTEMPTED'
        profit_usd = None

        if buy_id and sell_id:
            # Both legs placed as marketable limit orders: treat as filled and record the estimated profit.
            status = 'ATTEMPTED'
            profit_usd = estimated_profit_usd
            self.record_pnl(estimated_profit_usd, is_leg_risk_event=False)
        elif buy_id and not sell_id:
            self.logger.error(f"LEG RISK: buy leg placed on {platform_buy} but sell leg failed on {platform_sell}. Flattening buy leg.")
            await self.notifier.send_message(f"🚨 *LEG RISK* 🚨\nBuy leg went through on {platform_buy} but the sell leg on {platform_sell} failed to place. Flattening the position now.")
            estimated_loss = -abs(volume * max_buy_price * EMERGENCY_FLATTEN_PENALTY_PCT / 100)
            await self._flatten_leg(platform_buy, symbol, 'sell', volume)
            status = 'LEG_RISK_FLATTENED'
            profit_usd = estimated_loss
            self.record_pnl(estimated_loss, is_leg_risk_event=True)
        elif sell_id and not buy_id:
            self.logger.error(f"LEG RISK: sell leg placed on {platform_sell} but buy leg failed on {platform_buy}. Flattening sell leg.")
            await self.notifier.send_message(f"🚨 *LEG RISK* 🚨\nSell leg went through on {platform_sell} but the buy leg on {platform_buy} failed to place. Flattening the position now.")
            estimated_loss = -abs(volume * min_sell_price * EMERGENCY_FLATTEN_PENALTY_PCT / 100)
            await self._flatten_leg(platform_sell, symbol, 'buy', volume)
            status = 'LEG_RISK_FLATTENED'
            profit_usd = estimated_loss
            self.record_pnl(estimated_loss, is_leg_risk_event=True)
        else:
            status = 'FAILED'

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
