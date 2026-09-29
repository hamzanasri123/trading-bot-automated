# engine/trend_following_engine.py
import asyncio, logging, time
from config import (
    TREND_SYMBOL, TREND_PLATFORM, TREND_FAST_WINDOW, TREND_SLOW_WINDOW,
    TREND_TRADE_SIZE_USD, TREND_STOP_LOSS_PCT, MAX_TRADE_SIZE_USD
)

class TrendFollowingEngine:
    """
    Suivi de tendance (momentum), spot uniquement, long-only. Le plus
    différent des autres moteurs : ce n'est PAS une stratégie neutre au
    marché. On prend un pari directionnel explicite -- on peut perdre de
    l'argent même si l'exécution se passe parfaitement, simplement parce
    que la tendance s'inverse après l'entrée. C'est un risque de marché
    assumé, pas un bug ni un problème d'exécution.

    Principe classique : croisement de moyennes mobiles. Achète quand la
    moyenne mobile courte croise au-dessus de la longue (signal haussier),
    revend quand elle croise en dessous (signal baissier) ou sur stop-loss.
    Ne vend jamais à découvert.
    """
    def __init__(self, data_engine, order_manager, notifier, trade_logger=None, symbol=None, platform=None):
        self._data_engine = data_engine
        self._order_books = data_engine.order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.platform = platform or TREND_PLATFORM
        self.symbol = symbol or TREND_SYMBOL
        self.base_asset = self.symbol.split('/')[0]
        self.logger = logging.getLogger(self.__class__.__name__)

        from engine.price_history import PriceHistory
        self.history = PriceHistory(max_samples=max(500, TREND_SLOW_WINDOW * 2))

        self.fast_window = TREND_FAST_WINDOW
        self.slow_window = TREND_SLOW_WINDOW
        self.trade_size_usd = TREND_TRADE_SIZE_USD
        self.stop_loss_pct = TREND_STOP_LOSS_PCT

        self.position_qty = 0.0
        self.position_cost_usd = 0.0
        self.session_pnl_usd = 0.0
        self.max_session_loss_usd = MAX_TRADE_SIZE_USD * 10
        self._halted = False
        self._is_trading_enabled = True
        self._last_signal = None  # 'bullish' | 'bearish' | None

        self._last_print_time = 0
        self._print_interval = 5
        self._new_data_event = asyncio.Event()
        data_engine.add_listener(self._on_book_update)

    def _on_book_update(self, platform, symbol):
        # Voir StatArbEngine._on_book_update : enregistrement synchrone ici,
        # pas dans run(), pour ne perdre aucun échantillon entre deux réveils.
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

    async def run(self):
        self.logger.info(f"Trend-Following Engine is running on {self.platform} {self.symbol} (SMA {self.fast_window}/{self.slow_window}).")
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
            fast_sma = self.history.sma(self.platform, self.symbol, self.fast_window)
            slow_sma = self.history.sma(self.platform, self.symbol, self.slow_window)

            if current_time - self._last_print_time > self._print_interval:
                samples = self.history.count(self.platform, self.symbol)
                fast_str = f"{fast_sma:.4f}" if fast_sma is not None else "n/a"
                slow_str = f"{slow_sma:.4f}" if slow_sma is not None else "n/a"
                pos_str = f"{self.position_qty:.6f} {self.base_asset}" if self.position_qty > 0 else "none"
                self.logger.info(f"[Trend] {self.symbol} price={mid_price:.4f} samples={samples}/{self.slow_window} SMA{self.fast_window}={fast_str} SMA{self.slow_window}={slow_str} | position={pos_str} | session P&L=${self.session_pnl_usd:+.4f}")
                self._last_print_time = current_time

            if self._halted:
                continue
            if fast_sma is None or slow_sma is None:
                continue

            if self.position_qty > 0 and self.position_cost_usd > 0:
                unrealized_pct = ((mid_price * self.position_qty) - self.position_cost_usd) / self.position_cost_usd * 100
                if unrealized_pct <= -self.stop_loss_pct:
                    # Le stop-loss ne doit jamais attendre la fin du cooldown
                    # post-entrée : retarder une sortie qui limite les pertes
                    # pour éviter un whipsaw d'entrée serait une fausse
                    # économie -- la position est déjà ouverte et expose du
                    # capital réel.
                    await self._exit_position(reason="stop-loss")
                    continue

            if not self._is_trading_enabled:
                continue

            signal = 'bullish' if fast_sma > slow_sma else 'bearish'
            crossed = signal != self._last_signal
            self._last_signal = signal

            if self.position_qty > 0:
                if crossed and signal == 'bearish':
                    await self._exit_position(reason="bearish crossover")
            elif crossed and signal == 'bullish':
                await self._enter_position()

    async def _enter_position(self):
        self.logger.warning(f"[Trend] Bullish crossover on {self.symbol}. Buying.")
        self._is_trading_enabled = False
        order = await self._order_manager.create_market_order(self.platform, self.symbol, 'buy', cost=self.trade_size_usd)
        if order and order.get('filled'):
            self.position_qty = order['filled']
            self.position_cost_usd = order.get('cost') or (order['filled'] * (order.get('average') or 0))
            self.logger.info(f"[Trend] Entered position: {self.position_qty:.6f} {self.base_asset} @ cost ${self.position_cost_usd:.4f}")
            await self.notifier.send_message(f"📈 *Trend Entry* 📈\n{self.symbol}: bought {self.position_qty:.6f} {self.base_asset} (bullish crossover)")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='TREND_ENTRY', platform_buy=self.platform, symbol=self.symbol, volume=self.position_qty, buy_price=self.position_cost_usd/self.position_qty, details=f"order_id={order['id']}")
        else:
            self.logger.error(f"[Trend] Entry order failed on {self.platform}.")
        asyncio.create_task(self._reenable_after_cooldown())

    async def _exit_position(self, reason):
        qty = self.position_qty
        self.logger.warning(f"[Trend] Exit signal on {self.symbol} ({reason}). Selling {qty:.6f} {self.base_asset}.")
        self._is_trading_enabled = False
        order = await self._order_manager.create_market_order(self.platform, self.symbol, 'sell', amount=qty)
        if order and order.get('filled'):
            proceeds = order.get('cost') or (order['filled'] * (order.get('average') or 0))
            realized_pnl = proceeds - self.position_cost_usd
            self.session_pnl_usd += realized_pnl
            self.position_qty = 0.0
            self.position_cost_usd = 0.0
            self.logger.info(f"[Trend] Position closed ({reason}). Realized P&L: ${realized_pnl:+.4f}")
            await self.notifier.send_message(f"📉 *Trend Exit ({reason})* 📉\n{self.symbol}: realized P&L ${realized_pnl:+.4f}")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='TREND_EXIT', platform_sell=self.platform, symbol=self.symbol, volume=qty, sell_price=proceeds/qty if qty else 0, profit_usd=realized_pnl, details=f"reason={reason}, order_id={order['id']}")
            if not self._halted and self.session_pnl_usd <= -self.max_session_loss_usd:
                self._halted = True
                self.logger.critical(f"[Trend] CIRCUIT BREAKER TRIGGERED: session P&L ${self.session_pnl_usd:.4f}. Trading halted.")
                await self.notifier.send_message(f"🛑 *CIRCUIT BREAKER (Trend)* 🛑\nSession P&L: ${self.session_pnl_usd:.4f}\nTrading halted. Restart the bot after review.")
        else:
            self.logger.error(f"[Trend] UNHEDGED POSITION: exit order failed on {self.platform}. Manual intervention required.")
            await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Trend)* 🔥\nFailed to sell {qty:.6f} {self.base_asset} on {self.platform}. Manual intervention required.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='TREND_UNHEDGED', platform_sell=self.platform, symbol=self.symbol, volume=qty, details="Exit order failed")
        asyncio.create_task(self._reenable_after_cooldown())

    async def _reenable_after_cooldown(self, cooldown=5):
        await asyncio.sleep(cooldown)
        if not self._halted:
            self._is_trading_enabled = True
