# execution/live_order_manager.py
import asyncio, logging
import ccxt.async_support as ccxt
from config import API_KEYS, PAPER_TRADING_MODE, MAX_TRADE_SIZE_USD

class LiveOrderManager:
    def __init__(self, notifier, trade_logger):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchanges = {}
        self.fees = {}
        self.notifier = notifier
        self.trade_logger = trade_logger

    async def initialize(self):
        self.logger.info("Initializing LiveOrderManager...")
        for name, keys in API_KEYS.items():
            if name == 'BinanceFutures':
                # Pas un exchange spot -- ccxt n'a même pas d'identifiant
                # "binancefutures" (le testnet futures USD-M s'appelle
                # `binanceusdm`). Connexion dédiée gérée séparément par
                # FuturesOrderManager, jamais par ce manager spot.
                continue
            if not keys['apiKey'] or 'YOUR' in keys['apiKey']:
                self.logger.warning(f"Invalid API keys for {name}. This exchange will be skipped."); continue
            try:
                # Configuration de base
                config = {'apiKey': keys['apiKey'], 'secret': keys['secret'], 'enableRateLimit': True}
                if name == 'OKX':
                    config['password'] = keys['password']
                    # Les comptes OKX régulés EEA (Europe) ont des clés API séparées de
                    # www.okx.com et doivent utiliser le domaine eea.okx.com. Sans ce
                    # réglage, une clé EEA renvoie "API key doesn't exist" (code 50119).
                    if keys.get('hostname'):
                        config['hostname'] = keys['hostname']
                
                exchange_class = getattr(ccxt, name.lower())
                instance = exchange_class(config)

                # --- CORRECTION DÉFINITIVE APPLIQUÉE ICI ---
                # Si on est en Paper Trading, on doit activer le mode sandbox de ccxt
                if PAPER_TRADING_MODE:
                    self.logger.info(f"Paper Trading (Testnet) mode enabled for {name}.")
                    # ccxt utilise la clé 'sandbox' dans has (l'ancienne clé 'test' n'existe plus).
                    # set_sandbox_mode gère lui-même les spécificités par exchange
                    # (ex: header x-simulated-trading pour OKX).
                    if instance.has.get('sandbox', False):
                        instance.set_sandbox_mode(True)
                    else:
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

    def round_amount(self, platform: str, symbol: str, amount: float) -> float:
        if platform not in self.exchanges: return amount
        try:
            return float(self.exchanges[platform].amount_to_precision(symbol, amount))
        except Exception:
            return amount

    def get_min_notional(self, platform: str, symbol: str) -> float:
        # Valeur minimale (en devise de cotation) qu'un ordre doit atteindre
        # pour être accepté par l'exchange. Sans marge de sécurité au-dessus,
        # l'arrondi de précision peut faire retomber un ordre juste sous ce
        # seuil et se faire rejeter (code -1013 "Filter failure: NOTIONAL" sur
        # Binance).
        if platform not in self.exchanges: return 0.0
        try:
            market = self.exchanges[platform].markets.get(symbol, {})
            return market.get('limits', {}).get('cost', {}).get('min') or 0.0
        except Exception:
            return 0.0

    async def get_balance(self, platform: str, currency: str):
        if platform not in self.exchanges: return None
        try:
            balance = await self.exchanges[platform].fetch_free_balance()
            return balance.get(currency, 0.0)
        except Exception as e:
            self.logger.error(f"Error fetching balance for {currency} on {platform}: {e}"); return None

    async def execute_arbitrage(self, volume: float, platform_buy: str, platform_sell: str, max_buy_price: float, min_sell_price: float, symbol: str):
        # ... (logique de vérification des soldes, etc.) ...
        buy_order_task = asyncio.create_task(self.create_limit_order(platform_buy, symbol, 'buy', volume, max_buy_price))
        sell_order_task = asyncio.create_task(self.create_limit_order(platform_sell, symbol, 'sell', volume, min_sell_price))
        buy_result, sell_result = await asyncio.gather(buy_order_task, sell_order_task, return_exceptions=True)
        buy_id = buy_result.get('id') if isinstance(buy_result, dict) else None
        sell_id = sell_result.get('id') if isinstance(sell_result, dict) else None
        # Les colonnes doivent correspondre à la table 'trades' créée par TradeLogger._init_db ;
        # les champs sans colonne dédiée (IDs d'ordres) vont dans 'details'.
        self.trade_logger.log_trade(event_type='TAKER_ATTEMPT', platform_buy=platform_buy, platform_sell=platform_sell, symbol=symbol, volume=volume, buy_price=max_buy_price, sell_price=min_sell_price, details=f"buy_order_id={buy_id}, sell_order_id={sell_id}")

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
            # Certains exchanges (ex: OKX) ne renvoient ni le prix ni la quantité dans la
            # réponse de création d'ordre ; on les complète avec les valeurs demandées.
            if order.get('price') is None: order['price'] = price
            if order.get('amount') is None: order['amount'] = amount
            if order.get('symbol') is None: order['symbol'] = symbol
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order on {platform}: {e}")
            await self.notifier.send_message(f"🔥 *ORDER FAILED* 🔥\nFailed to place {side} order on {platform}.\nReason: `{e}`")
            return None

    async def create_market_order(self, platform: str, symbol: str, side: str, amount: float = None, cost: float = None):
        """
        Place un vrai ordre MARKET (exécution immédiate garantie ou rejet),
        contrairement à create_limit_order dont l'ordre peut rester posé sur
        le carnet réel si le prix "agressif" calculé ne franchit pas le
        spread au moment de l'envoi -- ce qui a causé un vidage silencieux
        du solde ETH lors du premier test de l'arbitrage triangulaire.

        Pour un achat, préférer `cost` (montant en devise de cotation à
        dépenser) quand on le connaît : ça évite d'avoir à pré-estimer la
        quantité de devise de base, qui peut différer du montant réellement
        obtenu par la jambe précédente. Pour une vente, `amount` (quantité
        de devise de base) est requis.
        """
        if platform not in self.exchanges:
            self.logger.error(f"Attempted to place order on uninitialized platform: {platform}")
            return None
        exchange = self.exchanges[platform]
        try:
            if side == 'buy' and cost is not None:
                precise_cost = float(exchange.cost_to_precision(symbol, cost))
                self.logger.info(f"Placing MARKET buy order: spend {precise_cost:.6f} (quote) on {symbol} on {platform}")
                order = await exchange.create_market_buy_order_with_cost(symbol, precise_cost)
            else:
                precise_amount = float(exchange.amount_to_precision(symbol, amount))
                self.logger.info(f"Placing MARKET {side} order: {precise_amount:.8f} {symbol} on {platform}")
                order = await exchange.create_market_order(symbol, side, precise_amount)

            # Un market order devrait se remplir immédiatement ; si la réponse
            # initiale ne le montre pas encore, on interroge quelques fois de
            # plus avant d'abandonner, plutôt que de supposer un remplissage.
            filled = order.get('filled') or 0
            if not filled:
                for _ in range(5):
                    await asyncio.sleep(0.3)
                    fresh = await self.fetch_order_status(platform, order['id'], symbol)
                    if fresh and (fresh.get('filled') or 0) > 0:
                        order = fresh
                        break

            self.logger.info(f"Market order on {platform} ({symbol}): id={order.get('id')}, filled={order.get('filled')}, cost={order.get('cost')}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place market {side} order on {platform} ({symbol}): {e}")
            await self.notifier.send_message(f"🔥 *ORDER FAILED* 🔥\nFailed to place market {side} order on {platform} ({symbol}).\nReason: `{e}`")
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
