# engine/stat_arb_engine.py
import asyncio, logging, time
from config import (
    STAT_ARB_SYMBOL, STAT_ARB_PLATFORM, STAT_ARB_MIN_SAMPLES,
    STAT_ARB_ENTRY_ZSCORE, STAT_ARB_EXIT_ZSCORE, STAT_ARB_TRADE_SIZE_USD,
    STAT_ARB_STOP_LOSS_PCT, MAX_TRADE_SIZE_USD
)

class StatArbEngine:
    """
    Arbitrage statistique par retour à la moyenne (mean reversion), spot
    uniquement, long-only. Différent des moteurs d'arbitrage précédents :
    il n'y a pas de garantie de convergence -- on parie sur une régularité
    statistique observée récemment, pas sur un écart de prix qui DOIT se
    corriger. Si la corrélation/moyenne historique se casse (changement de
    régime de marché), la position peut rester perdante durablement, d'où
    le stop-loss.

    Principe : suit le prix médian de STAT_ARB_SYMBOL (ETH/BTC par défaut)
    dans le temps, calcule une moyenne et un écart-type glissants. Quand le
    prix dévie fortement sous la moyenne (z-score très négatif), achète en
    pariant sur un retour vers la moyenne ; revend quand le prix y est
    revenu. Ne vend jamais à découvert : uniquement ce qui a été acheté par
    cette stratégie elle-même.
    """
    def __init__(self, data_engine, order_manager, notifier, trade_logger=None, symbol=None, platform=None):
        self._data_engine = data_engine
        self._order_books = data_engine.order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.platform = platform or STAT_ARB_PLATFORM
        self.symbol = symbol or STAT_ARB_SYMBOL
        self.base_asset = self.symbol.split('/')[0]
        self.logger = logging.getLogger(self.__class__.__name__)

        from engine.price_history import PriceHistory
        self.history = PriceHistory(max_samples=500)

        self.min_samples = STAT_ARB_MIN_SAMPLES
        self.entry_zscore = STAT_ARB_ENTRY_ZSCORE
        self.exit_zscore = STAT_ARB_EXIT_ZSCORE
        self.trade_size_usd = STAT_ARB_TRADE_SIZE_USD
        self.stop_loss_pct = STAT_ARB_STOP_LOSS_PCT

        # STAT_ARB_SYMBOL (ETH/BTC par défaut) est coté en BTC, pas en USD --
        # contrairement à TrendFollowingEngine/FundingArbEngine qui utilisent
        # tous les deux des paires cotées en USDC. `trade_size_usd` (un
        # montant en dollars) ne peut donc jamais être passé tel quel comme
        # `cost` d'un ordre market sur ce symbole : ce serait dépenser
        # littéralement 150 BTC au lieu de 150$ de BTC. Conversion BTC<->USD
        # mise en cache (rafraîchie toutes les 30s) pour éviter d'interroger
        # l'exchange à chaque tick de la boucle événementielle.
        self._quote_usd_price = None
        self._quote_usd_price_updated = 0.0
        self._quote_usd_refresh_interval = 30

        self.position_qty = 0.0
        self.position_cost_usd = 0.0
        self.session_pnl_usd = 0.0
        self.max_session_loss_usd = MAX_TRADE_SIZE_USD * 10
        self._halted = False
        self._is_trading_enabled = True

        self._last_print_time = 0
        self._print_interval = 5
        self._new_data_event = asyncio.Event()
        data_engine.add_listener(self._on_book_update)

    def _on_book_update(self, platform, symbol):
        # Enregistre l'historique de prix directement ici, à chaque mise à
        # jour réelle du carnet -- pas dans run(), dont les réveils peuvent
        # fusionner plusieurs mises à jour rapprochées en un seul (l'event
        # ne met en file qu'un signal "il y a du nouveau", pas chaque valeur).
        # Sans ça, l'historique perdrait des échantillons intermédiaires.
        if platform != self.platform or symbol != self.symbol:
            return
        book = self._order_books.get((platform, symbol))
        if not book:
            return
        bids, asks = book.get_bids(1), book.get_asks(1)
        if bids and asks:
            mid_price = (float(bids[0][0]) + float(asks[0][0])) / 2
            self.history.record(platform, symbol, mid_price)
        self._new_data_event.set()

    def _book(self):
        return self._order_books.get((self.platform, self.symbol))

    def _quote_currency(self):
        return self.symbol.split('/')[1]

    async def _get_quote_usd_price(self, force=False):
        """
        Prix en USD d'une unité de la devise de cotation du symbole (BTC
        pour ETH/BTC). Retourne 1.0 directement pour une devise déjà
        stable-USD (pas d'appel réseau). `force=True` ignore le cache --
        utilisé à la sortie de position, où la précision compte pour le
        P&L réalisé (et donc le coupe-circuit), contrairement au sizing à
        l'entrée où un prix vieux de quelques secondes est sans conséquence.
        """
        quote = self._quote_currency()
        if quote in ('USDC', 'USDT', 'USD', 'BUSD', 'FDUSD'):
            return 1.0
        now = time.time()
        if not force and self._quote_usd_price is not None and (now - self._quote_usd_price_updated) < self._quote_usd_refresh_interval:
            return self._quote_usd_price
        try:
            ticker = await self._order_manager.exchanges[self.platform].fetch_ticker(f"{quote}/USDC")
            price = ticker.get('last') or ticker.get('close')
            if price:
                self._quote_usd_price = price
                self._quote_usd_price_updated = now
        except Exception as e:
            self.logger.error(f"[StatArb] Failed to fetch {quote}/USDC price: {e}")
        return self._quote_usd_price

    async def run(self):
        self.logger.info(f"Stat-Arb Engine is running on {self.platform} {self.symbol} (entry z={self.entry_zscore}, exit z={self.exit_zscore}).")
        while True:
            try:
                await asyncio.wait_for(self._new_data_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._new_data_event.clear()

            mid_price = self.history.latest(self.platform, self.symbol)
            if mid_price is None:
                continue

            current_time = time.time()
            mean, std = self.history.mean_std(self.platform, self.symbol)
            zscore = (mid_price - mean) / std if (mean is not None and std and std > 0) else None

            if current_time - self._last_print_time > self._print_interval:
                samples = self.history.count(self.platform, self.symbol)
                z_str = f"{zscore:.2f}" if zscore is not None else "n/a"
                pos_str = f"{self.position_qty:.6f} {self.base_asset} (cost ${self.position_cost_usd:.4f})" if self.position_qty > 0 else "none"
                self.logger.info(f"[StatArb] {self.symbol} price={mid_price:.8f} samples={samples}/{self.min_samples} z-score={z_str} | position={pos_str} | session P&L=${self.session_pnl_usd:+.4f}")
                self._last_print_time = current_time

            if self._halted:
                continue

            if self.position_qty > 0 and self.position_cost_usd > 0:
                quote_usd = await self._get_quote_usd_price()
                if quote_usd is None:
                    self.logger.error(f"[StatArb] Cannot price {self._quote_currency()} in USD -- stop-loss check skipped this tick.")
                else:
                    current_value_usd = mid_price * self.position_qty * quote_usd
                    unrealized_pct = (current_value_usd - self.position_cost_usd) / self.position_cost_usd * 100
                    if unrealized_pct <= -self.stop_loss_pct:
                        # Le stop-loss ne doit jamais attendre la fin du cooldown
                        # post-entrée : retarder une sortie qui limite les pertes
                        # pour éviter un whipsaw serait une fausse économie -- la
                        # position est déjà ouverte et expose du capital réel.
                        await self._exit_position(reason="stop-loss")
                        continue

            if not self._is_trading_enabled:
                continue
            if self.history.count(self.platform, self.symbol) < self.min_samples or zscore is None:
                continue

            if self.position_qty > 0:
                if zscore >= -self.exit_zscore:
                    await self._exit_position(reason="mean reversion")
            elif zscore <= -self.entry_zscore:
                await self._enter_position(zscore)

    async def _enter_position(self, zscore):
        self.logger.warning(f"[StatArb] Entry signal on {self.symbol}: z-score={zscore:.2f} <= -{self.entry_zscore}. Buying.")
        self._is_trading_enabled = False
        quote_usd = await self._get_quote_usd_price()
        if quote_usd is None:
            self.logger.error(f"[StatArb] Could not price {self._quote_currency()} in USD -- aborting entry rather than risking a mis-sized order.")
            asyncio.create_task(self._reenable_after_cooldown())
            return
        cost_in_quote = self.trade_size_usd / quote_usd
        order = await self._order_manager.create_market_order(self.platform, self.symbol, 'buy', cost=cost_in_quote)
        if order and order.get('filled'):
            self.position_qty = order['filled']
            quote_spent = order.get('cost') or (order['filled'] * (order.get('average') or 0))
            self.position_cost_usd = quote_spent * quote_usd
            self.logger.info(f"[StatArb] Entered position: {self.position_qty:.6f} {self.base_asset} @ cost ${self.position_cost_usd:.4f}")
            await self.notifier.send_message(f"📉 *Stat-Arb Entry* 📉\n{self.symbol}: bought {self.position_qty:.6f} {self.base_asset} (z-score {zscore:.2f})")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='STATARB_ENTRY', platform_buy=self.platform, symbol=self.symbol, volume=self.position_qty, buy_price=self.position_cost_usd/self.position_qty, details=f"z_score={zscore:.2f}, order_id={order['id']}")
        else:
            self.logger.error(f"[StatArb] Entry order failed on {self.platform}.")
        asyncio.create_task(self._reenable_after_cooldown())

    async def _exit_position(self, reason):
        qty = self.position_qty
        self.logger.warning(f"[StatArb] Exit signal on {self.symbol} ({reason}). Selling {qty:.6f} {self.base_asset}.")
        self._is_trading_enabled = False
        order = await self._order_manager.create_market_order(self.platform, self.symbol, 'sell', amount=qty)
        if order and order.get('filled'):
            quote_proceeds = order.get('cost') or (order['filled'] * (order.get('average') or 0))
            # Prix frais (pas le cache) pour ce calcul : il alimente le P&L
            # réalisé et donc le coupe-circuit, contrairement au sizing à
            # l'entrée où un prix vieux de quelques secondes est sans enjeu.
            quote_usd = await self._get_quote_usd_price(force=True)
            if quote_usd is None:
                self.logger.error(f"[StatArb] Could not price {self._quote_currency()} in USD at exit -- realized P&L below may be inaccurate.")
                quote_usd = self._quote_usd_price or 1.0  # dernier prix connu plutôt que planter, mieux que rien
            proceeds = quote_proceeds * quote_usd
            realized_pnl = proceeds - self.position_cost_usd
            self.session_pnl_usd += realized_pnl
            self.position_qty = 0.0
            self.position_cost_usd = 0.0
            self.logger.info(f"[StatArb] Position closed ({reason}). Realized P&L: ${realized_pnl:+.4f}")
            await self.notifier.send_message(f"📈 *Stat-Arb Exit ({reason})* 📈\n{self.symbol}: realized P&L ${realized_pnl:+.4f}")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='STATARB_EXIT', platform_sell=self.platform, symbol=self.symbol, volume=qty, sell_price=proceeds/qty if qty else 0, profit_usd=realized_pnl, details=f"reason={reason}, order_id={order['id']}")
            if not self._halted and self.session_pnl_usd <= -self.max_session_loss_usd:
                self._halted = True
                self.logger.critical(f"[StatArb] CIRCUIT BREAKER TRIGGERED: session P&L ${self.session_pnl_usd:.4f}. Trading halted.")
                await self.notifier.send_message(f"🛑 *CIRCUIT BREAKER (Stat-Arb)* 🛑\nSession P&L: ${self.session_pnl_usd:.4f}\nTrading halted. Restart the bot after review.")
        else:
            self.logger.error(f"[StatArb] UNHEDGED POSITION: exit order failed on {self.platform}. Manual intervention required.")
            await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Stat-Arb)* 🔥\nFailed to sell {qty:.6f} {self.base_asset} on {self.platform}. Manual intervention required.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='STATARB_UNHEDGED', platform_sell=self.platform, symbol=self.symbol, volume=qty, details="Exit order failed")
        asyncio.create_task(self._reenable_after_cooldown())

    async def _reenable_after_cooldown(self, cooldown=5):
        await asyncio.sleep(cooldown)
        if not self._halted:
            self._is_trading_enabled = True
