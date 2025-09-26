# execution/live_order_manager.py
import asyncio, logging
import ccxt.async_support as ccxt
from config import API_KEYS, PAPER_TRADING_MODE, MAX_TRADE_SIZE_USD

class LiveOrderManager:
    def __init__(self, notifier):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchanges = {}
        self.fees = {}
        self.notifier = notifier

    async def initialize(self):
        self.logger.info("Initializing LiveOrderManager...")
        for name, keys in API_KEYS.items():
            if not keys['apiKey'] or 'YOUR' in keys['apiKey']:
                self.logger.warning(f"Invalid API keys for {name}. This exchange will be skipped."); continue
            try:
                config = {'apiKey': keys['apiKey'], 'secret': keys['secret'], 'enableRateLimit': True, 'adjustForTimeDifference': True}
                if name == 'OKX': config['password'] = keys['password']
                exchange_class = getattr(ccxt, name.lower())
                instance = exchange_class(config)
                if PAPER_TRADING_MODE: instance.set_sandbox_mode(True); self.logger.info(f"Paper Trading (Testnet) mode enabled for {name}.")
                await instance.load_markets(reload=True)
                self.exchanges[name] = instance
                self.logger.info(f"Successfully connected and synced with: {name}")
                symbol_to_trade = 'BTC/USDT'
                if symbol_to_trade in instance.markets:
                    market = instance.markets[symbol_to_trade]
                    self.fees[name] = {'maker': market['maker'] * 100, 'taker': market['taker'] * 100}
                    self.logger.info(f"Fees for {name} ({symbol_to_trade}): Maker {self.fees[name]['maker']:.4f}%, Taker {self.fees[name]['taker']:.4f}%")
                else: self.logger.error(f"Could not find market {symbol_to_trade} for {name} to fetch fees.")
            except Exception as e: self.logger.error(f"Failed to initialize {name}: {e}", exc_info=True)

    def get_fees(self, platform: str) -> dict:
        return self.fees.get(platform, {'maker': 0.1, 'taker': 0.1})

    async def get_balance(self, platform: str, currency: str):
        try:
            balance = await self.exchanges[platform].fetch_free_balance()
            return balance.get(currency, 0.0)
        except Exception as e:
            self.logger.error(f"Error fetching balance for {currency} on {platform}: {e}"); return None

    async def execute_arbitrage(self, volume: float, platform_buy: str, platform_sell: str, max_buy_price: float, min_sell_price: float, symbol: str):
        # --- CORRECTION APPLIQUÉE ICI ---
        # La ligne fautive "buy_" a été supprimée et la logique a été clarifiée.
        self.logger.info(f"Executing TAKER arbitrage: BUY {volume:.6f} {symbol} on {platform_buy}, SELL on {platform_sell}")

        # 1. Vérifier les soldes avant de trader
        buy_currency = symbol.split('/')[0]  # BTC
        sell_currency = symbol.split('/')[1] # USDT
        
        cost_estimate = volume * max_buy_price
        
        buy_platform_balance = await self.get_balance(platform_buy, sell_currency)
        sell_platform_balance = await self.get_balance(platform_sell, buy_currency)

        if buy_platform_balance is None or sell_platform_balance is None:
            self.logger.error("Could not verify balances. Aborting trade for safety.")
            await self.notifier.send_message("⚠️ *Trade Aborted* ⚠️\nCould not verify account balances before execution.")
            return

        if buy_platform_balance < cost_estimate:
            self.logger.error(f"Insufficient {sell_currency} on {platform_buy}. Needed: ~{cost_estimate:.2f}, Have: {buy_platform_balance:.2f}. Aborting.")
            await self.notifier.send_message(f"⚠️ *Trade Aborted* ⚠️\nInsufficient {sell_currency} on {platform_buy} to execute buy order.")
            return
        
        if sell_platform_balance < volume:
            self.logger.error(f"Insufficient {buy_currency} on {platform_sell}. Needed: {volume:.6f}, Have: {sell_platform_balance:.6f}. Aborting.")
            await self.notifier.send_message(f"⚠️ *Trade Aborted* ⚠️\nInsufficient {buy_currency} on {platform_sell} to execute sell order.")
            return

        # 2. Créer les tâches pour les deux ordres en parallèle
        buy_order_task = asyncio.create_task(self.create_limit_order(platform_buy, symbol, 'buy', volume, max_buy_price))
        sell_order_task = asyncio.create_task(self.create_limit_order(platform_sell, symbol, 'sell', volume, min_sell_price))

        # 3. Attendre l'exécution des deux ordres
        buy_result, sell_result = await asyncio.gather(buy_order_task, sell_order_task, return_exceptions=True)

        # 4. Gérer les résultats (Post-trade reconciliation)
        # (Cette partie est déjà implémentée dans les versions suivantes du code)
        # ...

    async def create_limit_order(self, platform: str, symbol: str, side: str, amount: float, price: float, post_only: bool = False):
        try:
            params = {}
            if post_only:
                params['postOnly'] = True
            
            self.logger.info(f"Placing LIMIT {side} order: {amount:.6f} {symbol} @ {price:.2f} on {platform} {'(Post-Only)' if post_only else ''}")
            order = await self.exchanges[platform].create_limit_order(symbol, side, amount, price, params)
            self.logger.info(f"Successfully placed order on {platform}. Order ID: {order['id']}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order on {platform}: {e}")
            await self.notifier.send_message(f"🔥 *ORDER FAILED* 🔥\nFailed to place {side} order on {platform}.\nReason: `{e}`")
            return None

    async def cancel_order(self, platform: str, order_id: str, symbol: str):
        try:
            self.logger.warning(f"Cancelling order {order_id} on {platform}")
            await self.exchanges[platform].cancel_order(order_id, symbol)
            return True
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id} on {platform}: {e}")
            return False

    async def fetch_order_status(self, platform: str, order_id: str, symbol: str):
        try:
            return await self.exchanges[platform].fetch_order(order_id, symbol)
        except Exception as e:
            self.logger.error(f"Failed to fetch status for order {order_id} on {platform}: {e}")
            return None

    async def close_all(self):
        self.logger.info("Closing all exchange connections...")
        for name, instance in self.exchanges.items():
            try:
                await instance.close()
                self.logger.info(f"Connection to {name} closed.")
            except Exception as e:
                self.logger.error(f"Error closing connection to {name}: {e}")
