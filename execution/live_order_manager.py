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
        buy_
