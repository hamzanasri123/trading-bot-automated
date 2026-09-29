# engine/cross_exchange_engine.py
import asyncio, logging, time
from config import MAX_TRADE_SIZE_USD, MIN_PROFIT_PCT_CROSS

class CrossExchangeEngine:
    """
    Arbitrage spatial (inter-exchange) Binance <-> OKX sur une paire donnée
    (BTC/USDC par défaut). Reprend le même modèle de sécurité que
    TriangularEngine v2 : ordres MARKET vérifiés (remplissage réel confirmé
    avant de considérer une jambe comme faite), dénouement à partir des
    montants réellement exécutés si la 2e jambe échoue, sizing symétrique
    et coupe-circuit de session -- plutôt que la stratégie Maker (ordres
    limit qui restaient posés sur le carnet) qui a coûté ~3500 USD
    équivalent la première fois qu'on a testé ce type d'arbitrage.

    Limite structurelle NON résolue par le code, à connaître avant tout
    passage en argent réel : ce type d'arbitrage suppose de détenir déjà
    de l'inventaire (l'actif de base ET la devise de cotation) sur les
    DEUX exchanges, puisqu'un transfert on-chain entbetween exchanges
    prend des minutes -- impossible de le faire en temps réel entre les
    deux jambes d'un même cycle. Chaque cycle déséquilibre légèrement les
    soldes des deux côtés (plus de BTC/moins d'USDC sur l'un, l'inverse
    sur l'autre) ; sans rééquilibrage périodique MANUEL, l'inventaire d'un
    des deux côtés finira par s'épuiser et les cycles dans ce sens-là
    échoueront (faute de solde), pas parce que l'opportunité a disparu.

    Il reste aussi un vrai délai réseau entre les deux jambes (contrairement
    au triangulaire, qui reste sur une seule connexion) : le marché peut
    bouger entre l'exécution de la jambe 1 et celle de la jambe 2, d'où un
    seuil de sécurité plus large (MIN_PROFIT_PCT_CROSS) que pour le
    triangulaire.
    """
    def __init__(self, order_books: dict, order_manager, notifier, trade_logger=None, symbol="BTC/USDC", platform_a="Binance", platform_b="OKX"):
        self._order_books = order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.symbol = symbol
        self.platform_a = platform_a
        self.platform_b = platform_b
        self.logger = logging.getLogger(self.__class__.__name__)

        self.trade_size_usdc = MAX_TRADE_SIZE_USD
        self.reinvest_pct = 0.5
        self.min_trade_size_usdc = MAX_TRADE_SIZE_USD
        self.max_trade_size_usdc = MAX_TRADE_SIZE_USD * 10
        self.min_profit_pct = MIN_PROFIT_PCT_CROSS

        self.session_pnl_usd = 0.0
        self.max_session_loss_usd = MAX_TRADE_SIZE_USD * 10
        self._halted = False

        self._is_trading_enabled = True
        self._cooldown = 5
        self._last_print_time = 0
        self._print_interval = 2

        self._relevant = {(platform_a, symbol), (platform_b, symbol)}
        self._new_data_event = asyncio.Event()

    def register_listeners(self, *data_engines):
        # Les deux exchanges peuvent être alimentés par des DataEngine
        # différents (chacun n'a besoin de connaître que son propre flux) ;
        # on s'abonne à tous ceux fournis.
        for de in data_engines:
            de.add_listener(self._on_book_update)

    def _on_book_update(self, platform, symbol):
        if (platform, symbol) in self._relevant:
            self._new_data_event.set()

    def _book(self, platform):
        return self._order_books.get((platform, self.symbol))

    def _fee_pct(self, platform):
        return self._order_manager.get_fees(platform).get('taker', 0.1)

    async def run(self):
        self.logger.info(f"Cross-Exchange Engine is running on {self.symbol}: {self.platform_a} <-> {self.platform_b}.")
        while True:
            try:
                await asyncio.wait_for(self._new_data_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._new_data_event.clear()
            current_time = time.time()
            if current_time - self._last_print_time > self._print_interval:
                self._print_status()
                self._last_print_time = current_time
            if self._halted or not self._is_trading_enabled:
                continue

            book_a, book_b = self._book(self.platform_a), self._book(self.platform_b)
            if not book_a or not book_b:
                continue

            pct_a_to_b = self._estimate('buy_a_sell_b', book_a, book_b)
            if pct_a_to_b is not None and pct_a_to_b > self.min_profit_pct:
                await self._execute_cycle('buy_a_sell_b', pct_a_to_b)
                continue

            pct_b_to_a = self._estimate('buy_b_sell_a', book_a, book_b)
            if pct_b_to_a is not None and pct_b_to_a > self.min_profit_pct:
                await self._execute_cycle('buy_b_sell_a', pct_b_to_a)

    def _print_status(self):
        book_a, book_b = self._book(self.platform_a), self._book(self.platform_b)
        if not book_a or not book_b:
            self.logger.info("[Cross] Waiting for order books...")
            return
        pct_ab = self._estimate('buy_a_sell_b', book_a, book_b)
        pct_ba = self._estimate('buy_b_sell_a', book_a, book_b)
        ab_str = f"{pct_ab:.4f}%" if pct_ab is not None else "n/a"
        ba_str = f"{pct_ba:.4f}%" if pct_ba is not None else "n/a"
        self.logger.info(f"[Cross] {self.symbol} -- buy {self.platform_a}/sell {self.platform_b}: {ab_str} | buy {self.platform_b}/sell {self.platform_a}: {ba_str} (threshold {self.min_profit_pct}%)")

    def _estimate(self, direction, book_a, book_b):
        if direction == 'buy_a_sell_b':
            buy_book, sell_book, buy_platform, sell_platform = book_a, book_b, self.platform_a, self.platform_b
        else:
            buy_book, sell_book, buy_platform, sell_platform = book_b, book_a, self.platform_b, self.platform_a

        asks, bids = buy_book.get_asks(1), sell_book.get_bids(1)
        if not asks or not bids:
            return None
        buy_price, sell_price = float(asks[0][0]), float(bids[0][0])
        buy_fee, sell_fee = self._fee_pct(buy_platform) / 100, self._fee_pct(sell_platform) / 100

        amount = (self.trade_size_usdc / buy_price) * (1 - buy_fee)
        usdc_final = (amount * sell_price) * (1 - sell_fee)
        profit_usd = usdc_final - self.trade_size_usdc
        return (profit_usd / self.trade_size_usdc) * 100

    async def _execute_cycle(self, direction, estimated_pct):
        buy_platform, sell_platform = (self.platform_a, self.platform_b) if direction == 'buy_a_sell_b' else (self.platform_b, self.platform_a)
        self.logger.warning(f"[Cross] Opportunity found (buy {buy_platform} / sell {sell_platform}): estimated profit {estimated_pct:.4f}%. Executing with real market orders...")
        self._is_trading_enabled = False
        await self.notifier.send_message(f"🔀 *Cross-Exchange Opportunity* 🔀\nBuy: {buy_platform}\nSell: {sell_platform}\nEstimated profit: {estimated_pct:.4f}%")

        buy_order = await self._order_manager.create_market_order(buy_platform, self.symbol, 'buy', cost=self.trade_size_usdc)
        if not buy_order or not buy_order.get('id') or not buy_order.get('filled'):
            self.logger.error(f"[Cross] Buy leg failed or did not fill on {buy_platform}. Nothing to unwind, no position taken.")
            asyncio.create_task(self.cooldown_trading())
            return

        amount = buy_order['filled']
        sell_order = await self._order_manager.create_market_order(sell_platform, self.symbol, 'sell', amount=amount)

        if sell_order and sell_order.get('id') and sell_order.get('filled'):
            final_usdc = sell_order.get('cost') or (sell_order['filled'] * (sell_order.get('average') or 0))
            real_profit_usd = final_usdc - self.trade_size_usdc
            real_profit_pct = (real_profit_usd / self.trade_size_usdc) * 100
            self.logger.info(f"[Cross] Both legs filled. Real profit: ${real_profit_usd:.4f} ({real_profit_pct:.4f}%)")
            await self.notifier.send_message(f"✅ *Cross-Exchange Cycle Complete* ✅\nReal profit: ${real_profit_usd:.4f} ({real_profit_pct:.4f}%)")
            if self.trade_logger:
                self.trade_logger.log_trade(
                    event_type='CROSS_FILLED', platform_buy=buy_platform, platform_sell=sell_platform,
                    symbol=self.symbol, volume=self.trade_size_usdc,
                    profit_usd=real_profit_usd, profit_pct=real_profit_pct,
                    details=f"buy_id={buy_order['id']}, sell_id={sell_order['id']}"
                )
            await self._apply_sizing_and_breaker(real_profit_usd)
        else:
            # La jambe de vente sur l'autre exchange a échoué : impossible de
            # déplacer le BTC déjà acheté vers sell_platform en temps réel
            # (transfert on-chain = plusieurs minutes). On le revend
            # immédiatement sur buy_platform, là où il se trouve réellement.
            self.logger.warning(f"[Cross] Sell leg failed on {sell_platform}. Unwinding by selling back on {buy_platform} instead.")
            unwind_result = await self._order_manager.create_market_order(buy_platform, self.symbol, 'sell', amount=amount)
            if unwind_result and unwind_result.get('id') and unwind_result.get('filled'):
                recovered = unwind_result.get('cost') or (unwind_result['filled'] * (unwind_result.get('average') or 0))
                real_profit_usd = recovered - self.trade_size_usdc
                real_profit_pct = (real_profit_usd / self.trade_size_usdc) * 100
                self.logger.info(f"[Cross] Unwound on {buy_platform}. Real P&L: ${real_profit_usd:.4f} ({real_profit_pct:.4f}%)")
                if self.trade_logger:
                    self.trade_logger.log_trade(
                        event_type='CROSS_UNWOUND', platform_buy=buy_platform, platform_sell=buy_platform,
                        symbol=self.symbol, volume=self.trade_size_usdc,
                        profit_usd=real_profit_usd, profit_pct=real_profit_pct,
                        details=f"buy_id={buy_order['id']}, unwind_sell_id={unwind_result['id']}"
                    )
                await self._apply_sizing_and_breaker(real_profit_usd)
            else:
                self.logger.error(f"[Cross] UNHEDGED POSITION: failed to unwind on {buy_platform} after Sell leg failed on {sell_platform}. Manual intervention required.")
                await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Cross-Exchange)* 🔥\nBought on {buy_platform} but could not sell on {sell_platform} NOR unwind on {buy_platform}. Manual intervention required.")
                if self.trade_logger:
                    self.trade_logger.log_trade(event_type='CROSS_UNHEDGED', platform_buy=buy_platform, platform_sell=sell_platform, symbol=self.symbol, volume=amount, details=f"buy_id={buy_order['id']}, failed to sell or unwind")

        asyncio.create_task(self.cooldown_trading())

    async def _apply_sizing_and_breaker(self, real_profit_usd):
        self.session_pnl_usd += real_profit_usd
        old_size = self.trade_size_usdc
        new_size = max(self.min_trade_size_usdc, min(old_size + real_profit_usd * self.reinvest_pct, self.max_trade_size_usdc))
        self.trade_size_usdc = new_size
        if abs(new_size - old_size) > 1e-9:
            self.logger.info(f"[Cross] Sizing adjusted: ${old_size:.4f} -> ${new_size:.4f} (cycle P&L ${real_profit_usd:+.4f}, session P&L ${self.session_pnl_usd:+.4f})")

        if not self._halted and self.session_pnl_usd <= -self.max_session_loss_usd:
            self._halted = True
            self.logger.critical(f"[Cross] CIRCUIT BREAKER TRIGGERED: session P&L ${self.session_pnl_usd:.4f} <= -${self.max_session_loss_usd:.4f}. Trading halted.")
            await self.notifier.send_message(f"🛑 *CIRCUIT BREAKER (Cross-Exchange)* 🛑\nSession P&L: ${self.session_pnl_usd:.4f}\nTrading has been halted and will NOT resume automatically. Restart the bot after review.")

    async def cooldown_trading(self):
        await asyncio.sleep(self._cooldown)
        if self._halted:
            self.logger.info("[Cross] Trading remains halted (circuit breaker).")
            return
        self.logger.info(f"[Cross] Trading re-enabled after {self._cooldown}s cooldown.")
        self._is_trading_enabled = True
