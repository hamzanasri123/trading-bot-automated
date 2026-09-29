# engine/market_making_engine.py
import asyncio, logging, time
from config import MAX_TRADE_SIZE_USD, MM_HALF_SPREAD_PCT, MM_MAX_INVENTORY_USD, MM_STOP_LOSS_PCT

class MarketMakingEngine:
    """
    Market making algorithmique sur une seule paire, une seule plateforme.

    DIFFÉRENT DE NATURE des moteurs d'arbitrage (triangulaire, inter-exchange) :
    il n'y a pas d'écart de prix à corriger ici, donc pas de "profit garanti si
    exécuté à temps". On pose en permanence un ordre d'achat et un ordre de
    vente autour du prix médian, et on gagne le spread SI les deux côtés se
    remplissent. Le risque principal est l'INVENTAIRE : si le marché tend dans
    une direction, un seul côté se remplit en boucle et on accumule une
    position directionnelle non voulue, qui perd de l'argent si la tendance
    continue. Ce n'est pas plus sûr que l'arbitrage, juste un risque différent.

    Choix délibéré : pas de "chase to flatten" agressif comme dans la toute
    première version du Maker cross-exchange (qui avait justement perdu de
    l'argent en essayant de liquider trop vite à un prix dégradé). Ici,
    l'inventaire est géré par un skew progressif du prix coté (moins
    attractif d'acheter, plus attractif de vendre au fur et à mesure qu'on
    accumule), avec un vrai stop-loss au marché seulement en cas de perte
    latente sévère -- pas à chaque déséquilibre mineur.

    Ne short jamais : on ne vend que ce qu'on détient déjà (acheté par cette
    stratégie), donc l'inventaire reste toujours >= 0.
    """
    def __init__(self, data_engine, order_manager, notifier, trade_logger=None, platform='Binance', symbol='DOT/USDC'):
        self._data_engine = data_engine
        self._order_books = data_engine.order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.platform = platform
        self.symbol = symbol
        self.base_asset = symbol.split('/')[0]
        self.logger = logging.getLogger(self.__class__.__name__)

        self.half_spread_pct = MM_HALF_SPREAD_PCT
        self.max_inventory_usd = MM_MAX_INVENTORY_USD
        self.stop_loss_pct = MM_STOP_LOSS_PCT
        self.quote_notional_usd = MAX_TRADE_SIZE_USD / 3
        # Inventaire skew : à pleine capacité d'inventaire, décale le prix
        # médian coté de ce % vers le bas (moins attractif d'acheter encore,
        # plus attractif de vendre) pour encourager un retour naturel à zéro.
        self.max_skew_pct = 0.15
        self.requote_min_interval = 9  # secondes entre deux recotations

        self.inventory_qty = 0.0
        self.inventory_cost_usd = 0.0
        self.session_pnl_usd = 0.0
        self.max_session_loss_usd = MAX_TRADE_SIZE_USD * 10
        self._halted = False

        self.active_bid = None  # {'id', 'price', 'amount'}
        self.active_ask = None

        self._is_trading_enabled = True
        self._last_requote_time = 0
        self._last_print_time = 0
        self._print_interval = 5

        self._new_data_event = asyncio.Event()
        data_engine.add_listener(self._on_book_update)

    def _on_book_update(self, platform, symbol):
        if platform == self.platform and symbol == self.symbol:
            self._new_data_event.set()

    def _book(self):
        return self._order_books.get((self.platform, self.symbol))

    def _fee_pct(self):
        return self._order_manager.get_fees(self.platform).get('maker', 0.1)

    async def run(self):
        self.logger.info(f"Market Making Engine is running on {self.platform} {self.symbol}. Half-spread: {self.half_spread_pct}%, max inventory: ${self.max_inventory_usd}.")
        while True:
            try:
                await asyncio.wait_for(self._new_data_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._new_data_event.clear()
            current_time = time.time()

            book = self._book()
            if not book:
                if current_time - self._last_print_time > self._print_interval:
                    self.logger.info("[MarketMaking] Waiting for order book...")
                    self._last_print_time = current_time
                continue

            bids, asks = book.get_bids(1), book.get_asks(1)
            if not bids or not asks:
                continue
            mid_price = (float(bids[0][0]) + float(asks[0][0])) / 2

            if current_time - self._last_print_time > self._print_interval:
                unrealized = self._unrealized_pnl(mid_price)
                self.logger.info(f"[MarketMaking] {self.symbol} mid={mid_price:.6f} | inventory={self.inventory_qty:.6f} {self.base_asset} (~${self.inventory_qty*mid_price:.4f}) | unrealized P&L=${unrealized:.4f} | session P&L=${self.session_pnl_usd:+.4f}")
                self._last_print_time = current_time

            if self._halted or not self._is_trading_enabled:
                continue

            # Stop-loss avant toute recotation : une perte latente sévère se
            # liquide immédiatement, indépendamment du timer de recotation.
            if self.inventory_qty > 0:
                unrealized = self._unrealized_pnl(mid_price)
                if unrealized <= -(self.max_inventory_usd * self.stop_loss_pct / 100):
                    await self._stop_loss_liquidate(mid_price)
                    continue

            if current_time - self._last_requote_time >= self.requote_min_interval:
                await self._requote(mid_price)
                self._last_requote_time = current_time

    def _unrealized_pnl(self, mid_price):
        if self.inventory_qty <= 0:
            return 0.0
        return (mid_price * self.inventory_qty) - self.inventory_cost_usd

    async def _requote(self, mid_price):
        # Vérifie d'abord si les ordres actifs ont été remplis (met à jour
        # l'inventaire) avant de les annuler et d'en poser de nouveaux.
        await self._check_fill(self.active_bid, is_buy=True)
        await self._check_fill(self.active_ask, is_buy=False)

        if self.active_bid:
            await self._order_manager.cancel_order(self.platform, self.active_bid['id'], self.symbol)
            self.active_bid = None
        if self.active_ask:
            await self._order_manager.cancel_order(self.platform, self.active_ask['id'], self.symbol)
            self.active_ask = None

        skew = min(self.inventory_qty * mid_price / self.max_inventory_usd, 1.0) if self.max_inventory_usd > 0 else 0.0
        skewed_mid = mid_price * (1 - skew * self.max_skew_pct / 100)

        bid_price = skewed_mid * (1 - self.half_spread_pct / 100)
        ask_price = skewed_mid * (1 + self.half_spread_pct / 100)

        # Marge de sécurité au-dessus du minimum réel de l'exchange : sans
        # elle, l'arrondi de précision peut faire retomber l'ordre juste sous
        # le seuil et se faire rejeter (vu en test : 4.99 USDC rejeté pour un
        # minimum de 5 USDC sur DOT/USDC).
        min_notional = self._order_manager.get_min_notional(self.platform, self.symbol)
        effective_notional = max(self.quote_notional_usd, min_notional * 1.15)

        inventory_usd = self.inventory_qty * mid_price
        can_buy_more = inventory_usd < self.max_inventory_usd

        if can_buy_more:
            bid_amount = self._order_manager.round_amount(self.platform, self.symbol, effective_notional / bid_price)
            order = await self._order_manager.create_limit_order(self.platform, self.symbol, 'buy', bid_amount, bid_price, post_only=True)
            if order and order.get('id'):
                self.active_bid = {'id': order['id'], 'price': bid_price, 'amount': bid_amount}

        if self.inventory_qty > 0 and self.inventory_qty * ask_price >= min_notional:
            ask_amount = self._order_manager.round_amount(self.platform, self.symbol, min(effective_notional / ask_price, self.inventory_qty))
            if ask_amount > 0:
                order = await self._order_manager.create_limit_order(self.platform, self.symbol, 'sell', ask_amount, ask_price, post_only=True)
                if order and order.get('id'):
                    self.active_ask = {'id': order['id'], 'price': ask_price, 'amount': ask_amount}

    async def _check_fill(self, active_order, is_buy):
        if not active_order:
            return
        status = await self._order_manager.fetch_order_status(self.platform, active_order['id'], self.symbol)
        if not status or status.get('status') != 'closed':
            return
        filled = status.get('filled') or active_order['amount']
        price = status.get('average') or active_order['price']

        if is_buy:
            self.inventory_qty += filled
            self.inventory_cost_usd += filled * price
            self.logger.info(f"[MarketMaking] Bid filled: +{filled:.6f} {self.base_asset} @ {price:.6f}. Inventory now {self.inventory_qty:.6f}.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='MM_BUY_FILLED', platform_buy=self.platform, symbol=self.symbol, volume=filled, buy_price=price, details=f"order_id={active_order['id']}")
        else:
            avg_cost = (self.inventory_cost_usd / self.inventory_qty) if self.inventory_qty > 0 else price
            realized_pnl = filled * (price - avg_cost)
            self.inventory_qty = max(0.0, self.inventory_qty - filled)
            self.inventory_cost_usd = max(0.0, self.inventory_cost_usd - filled * avg_cost)
            self.session_pnl_usd += realized_pnl
            self.logger.info(f"[MarketMaking] Ask filled: -{filled:.6f} {self.base_asset} @ {price:.6f}. Realized P&L: ${realized_pnl:+.4f}. Inventory now {self.inventory_qty:.6f}.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='MM_SELL_FILLED', platform_sell=self.platform, symbol=self.symbol, volume=filled, sell_price=price, profit_usd=realized_pnl, details=f"order_id={active_order['id']}")
            await self._check_breaker()

    async def _stop_loss_liquidate(self, mid_price):
        self.logger.critical(f"[MarketMaking] STOP-LOSS: unrealized loss on {self.inventory_qty:.6f} {self.base_asset} exceeds {self.stop_loss_pct}% of max inventory. Liquidating at market.")
        if self.active_bid:
            await self._order_manager.cancel_order(self.platform, self.active_bid['id'], self.symbol)
            self.active_bid = None
        if self.active_ask:
            await self._order_manager.cancel_order(self.platform, self.active_ask['id'], self.symbol)
            self.active_ask = None

        qty = self.inventory_qty
        order = await self._order_manager.create_market_order(self.platform, self.symbol, 'sell', amount=qty)
        if order and order.get('filled'):
            proceeds = order.get('cost') or (order['filled'] * (order.get('average') or mid_price))
            realized_pnl = proceeds - self.inventory_cost_usd
            self.session_pnl_usd += realized_pnl
            self.inventory_qty = 0.0
            self.inventory_cost_usd = 0.0
            self.logger.warning(f"[MarketMaking] Stop-loss executed. Realized P&L: ${realized_pnl:+.4f}")
            await self.notifier.send_message(f"⚠️ *Market Making Stop-Loss* ⚠️\n{self.symbol}: liquidated {qty:.6f} {self.base_asset}\nRealized P&L: ${realized_pnl:+.4f}")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='MM_STOPPED_OUT', platform_sell=self.platform, symbol=self.symbol, volume=qty, profit_usd=realized_pnl, details=f"order_id={order['id']}")
            await self._check_breaker()
        else:
            self.logger.error(f"[MarketMaking] UNHEDGED POSITION: stop-loss market sell failed on {self.platform}. Manual intervention required.")
            await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Market Making)* 🔥\nStop-loss sell failed on {self.platform} for {qty:.6f} {self.base_asset}. Manual intervention required.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='MM_UNHEDGED', platform_sell=self.platform, symbol=self.symbol, volume=qty, details="Stop-loss market sell failed")

    async def shutdown(self):
        # Contrairement aux moteurs d'arbitrage (ordres market uniquement),
        # celui-ci laisse des ordres limit posés sur l'exchange -- il faut les
        # annuler explicitement à l'arrêt, sinon ils restent actifs sans
        # supervision une fois le bot coupé.
        if self.active_bid:
            await self._order_manager.cancel_order(self.platform, self.active_bid['id'], self.symbol)
            self.active_bid = None
        if self.active_ask:
            await self._order_manager.cancel_order(self.platform, self.active_ask['id'], self.symbol)
            self.active_ask = None
        self.logger.info("[MarketMaking] Outstanding orders cancelled on shutdown.")

    async def _check_breaker(self):
        if not self._halted and self.session_pnl_usd <= -self.max_session_loss_usd:
            self._halted = True
            self.logger.critical(f"[MarketMaking] CIRCUIT BREAKER TRIGGERED: session P&L ${self.session_pnl_usd:.4f} <= -${self.max_session_loss_usd:.4f}. Trading halted.")
            # Retire tout ordre encore posé : pas de raison de continuer à
            # coter une fois le trading arrêté.
            if self.active_bid:
                await self._order_manager.cancel_order(self.platform, self.active_bid['id'], self.symbol)
                self.active_bid = None
            if self.active_ask:
                await self._order_manager.cancel_order(self.platform, self.active_ask['id'], self.symbol)
                self.active_ask = None
            await self.notifier.send_message(f"🛑 *CIRCUIT BREAKER (Market Making)* 🛑\nSession P&L: ${self.session_pnl_usd:.4f}\nTrading has been halted and will NOT resume automatically. Restart the bot after review.")
