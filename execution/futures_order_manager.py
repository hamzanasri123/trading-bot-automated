# execution/futures_order_manager.py
import asyncio, logging
import ccxt.async_support as ccxt
from config import API_KEYS, PAPER_TRADING_MODE

class FuturesOrderManager:
    """
    Connexion dédiée au testnet futures Binance (USD-M), séparée de
    LiveOrderManager (spot) -- ce sont deux comptes/clés différents
    (testnet.binancefuture.com vs testnet.binance.vision). Sert de jambe
    d'exécution "perpétuel" pour FundingArbEngine (short perp + long spot).

    Même discipline que LiveOrderManager : ordres MARKET vérifiés
    (remplissage réel confirmé, jamais juste "accepté"). En plus, ici,
    get_position() sert à vérifier après coup que la position résultante
    sur l'exchange correspond vraiment à ce qui était attendu -- le mode de
    position (one-way vs hedge) peut faire qu'un ordre "accepté" ne
    produise pas l'exposition attendue.
    """
    def __init__(self, notifier):
        self.notifier = notifier
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchange = None

    def is_configured(self) -> bool:
        keys = API_KEYS.get('BinanceFutures', {})
        return bool(keys.get('apiKey')) and 'YOUR' not in keys.get('apiKey', '')

    async def initialize(self):
        keys = API_KEYS['BinanceFutures']
        self.exchange = ccxt.binanceusdm({'apiKey': keys['apiKey'], 'secret': keys['secret'], 'enableRateLimit': True})
        if PAPER_TRADING_MODE and self.exchange.has.get('sandbox', False):
            self.exchange.set_sandbox_mode(True)
        await self.exchange.load_markets(reload=True)
        try:
            await self.exchange.set_position_mode(False)  # force one-way (pas hedge mode)
        except Exception as e:
            self.logger.warning(f"Could not force one-way position mode (may already be set, or unsupported on this account): {e}")
        self.logger.info(f"FuturesOrderManager connected to Binance USD-M Futures ({'testnet' if PAPER_TRADING_MODE else 'LIVE'}).")

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self.exchange.set_leverage(leverage, symbol)
            return True
        except Exception as e:
            self.logger.error(f"Failed to set leverage {leverage}x on {symbol}: {e}")
            return False

    def round_amount(self, symbol: str, amount: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception:
            return amount

    async def get_balance(self, currency='USDT'):
        try:
            balance = await self.exchange.fetch_free_balance()
            return balance.get(currency, 0.0)
        except Exception as e:
            self.logger.error(f"Error fetching futures balance for {currency}: {e}")
            return None

    async def get_mark_price(self, symbol: str):
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return ticker.get('last') or ticker.get('close')
        except Exception as e:
            self.logger.error(f"Failed to fetch mark price for {symbol}: {e}")
            return None

    async def get_position(self, symbol: str):
        try:
            positions = await self.exchange.fetch_positions([symbol])
            for pos in positions:
                if pos.get('contracts'):
                    return pos
            return None
        except Exception as e:
            self.logger.error(f"Failed to fetch position for {symbol}: {e}")
            return None

    async def create_market_order(self, symbol: str, side: str, amount: float, reduce_only: bool = False):
        try:
            precise_amount = self.round_amount(symbol, amount)
            params = {'reduceOnly': True} if reduce_only else {}
            self.logger.info(f"Placing FUTURES MARKET {side} order: {precise_amount} {symbol}{' (reduceOnly)' if reduce_only else ''}")
            order = await self.exchange.create_market_order(symbol, side, precise_amount, params)

            filled = order.get('filled') or 0
            if not filled:
                for _ in range(5):
                    await asyncio.sleep(0.3)
                    fresh = await self.fetch_order_status(order['id'], symbol)
                    if fresh and (fresh.get('filled') or 0) > 0:
                        order = fresh
                        break

            self.logger.info(f"Futures market order ({symbol}): id={order.get('id')}, filled={order.get('filled')}, avg={order.get('average')}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place futures market {side} order on {symbol}: {e}")
            await self.notifier.send_message(f"🔥 *FUTURES ORDER FAILED* 🔥\nFailed to place {side} order on {symbol}.\nReason: `{e}`")
            return None

    async def fetch_order_status(self, order_id: str, symbol: str):
        try:
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            self.logger.error(f"Failed to fetch futures order status {order_id}: {e}")
            return None

    async def fetch_funding_since(self, symbol: str, since_ms: int):
        """
        Somme des paiements de funding réellement reçus/payés depuis
        `since_ms` (ms epoch), via l'historique de revenus de l'exchange --
        pas une estimation à partir du taux affiché, le vrai montant
        crédité/débité. Retourne None si l'historique n'a pas pu être
        récupéré (ne PAS supposer 0 dans ce cas : le P&L qui en dépend doit
        rester marqué comme incertain plutôt que silencieusement sous-évalué).
        """
        try:
            history = await self.exchange.fetch_funding_history(symbol, since_ms)
            return sum(entry.get('amount', 0.0) or 0.0 for entry in history)
        except Exception as e:
            self.logger.error(f"Failed to fetch funding history for {symbol} since {since_ms}: {e}")
            return None

    async def close(self):
        if self.exchange:
            try:
                await self.exchange.close()
            except Exception as e:
                self.logger.error(f"Error closing futures exchange connection: {e}")
