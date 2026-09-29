# engine/triangular_engine.py
import asyncio, logging, time
from config import MAX_TRADE_SIZE_USD

class TriangularEngine:
    """
    Arbitrage triangulaire sur une seule plateforme (par défaut Binance) :
    exploite les écarts de prix entre 3 paires corrélées (BTC/USDC, ETH/BTC,
    ETH/USDC) sans jamais toucher un autre exchange.

    IMPORTANT (v2) : chaque jambe est un vrai ordre MARKET, exécuté et
    vérifié (montant réellement rempli) avant de passer à la jambe
    suivante. La v1 utilisait des ordres LIMIT "agressifs" qui pouvaient
    rester posés sur le carnet réel sans se remplir immédiatement -- le
    bot croyait alors le cycle terminé alors que les ordres traînaient sur
    le carnet et s'exécutaient plus tard, à des prix déconnectés du calcul.
    Résultat observé en test : ~1 ETH de solde vidé en quelques heures
    alors que le journal de trades affichait un profit cumulé fictif.

    Limitations connues :
    - Le calcul de rentabilité utilise seulement le meilleur bid/ask (pas de
      "walk" du carnet en profondeur), donc le slippage réel sur un ordre
      plus gros que le haut du carnet n'est pas modélisé -- ça reste un
      filtre pour décider s'il vaut la peine de tenter un cycle, pas une
      garantie de profit exact.
    - Les frais taker sont approximés avec ceux de BTC/USDC pour les 3
      paires (Binance applique en général un taux plat par compte, mais ça
      reste une approximation).
    """
    def __init__(self, order_books: dict, order_manager, notifier, trade_logger=None, platform='Binance'):
        self._order_books = order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.platform = platform
        self.logger = logging.getLogger(self.__class__.__name__)

        # Le triangle : USDC <-> BTC <-> ETH <-> USDC
        self.pair_bridge = "BTC/USDC"   # USDC <-> BTC
        self.pair_leg = "ETH/BTC"       # BTC <-> ETH
        self.pair_quote = "ETH/USDC"    # USDC <-> ETH

        self.trade_size_usdc = MAX_TRADE_SIZE_USD
        # Marge de sécurité au-delà des 3 frais taker, pour absorber le slippage
        # et l'imprécision du calcul en top-of-book.
        self.min_profit_pct = 0.15

        self._is_trading_enabled = True
        self._cooldown = 5
        self._last_print_time = 0
        self._print_interval = 10

    def _book(self, symbol):
        return self._order_books.get((self.platform, symbol))

    def _taker_fee_pct(self):
        return self._order_manager.get_fees(self.platform).get('taker', 0.1)

    async def run(self):
        self.logger.info(f"Triangular Engine is running on {self.platform} ({self.pair_bridge} / {self.pair_leg} / {self.pair_quote}).")
        while True:
            await asyncio.sleep(0.2)
            current_time = time.time()
            if current_time - self._last_print_time > self._print_interval:
                self._print_status()
                self._last_print_time = current_time
            if not self._is_trading_enabled:
                continue

            book_bridge, book_leg, book_quote = self._book(self.pair_bridge), self._book(self.pair_leg), self._book(self.pair_quote)
            if not book_bridge or not book_leg or not book_quote:
                continue

            forward_pct = self._estimate_profit_pct('forward', book_bridge, book_leg, book_quote)
            if forward_pct is not None and forward_pct > self.min_profit_pct:
                await self._execute_cycle('forward', forward_pct)
                continue

            reverse_pct = self._estimate_profit_pct('reverse', book_bridge, book_leg, book_quote)
            if reverse_pct is not None and reverse_pct > self.min_profit_pct:
                await self._execute_cycle('reverse', reverse_pct)

    def _print_status(self):
        book_bridge, book_leg, book_quote = self._book(self.pair_bridge), self._book(self.pair_leg), self._book(self.pair_quote)
        if not book_bridge or not book_leg or not book_quote:
            self.logger.info("[Triangular] Waiting for order books...")
            return
        f_pct = self._estimate_profit_pct('forward', book_bridge, book_leg, book_quote)
        r_pct = self._estimate_profit_pct('reverse', book_bridge, book_leg, book_quote)
        f_str = f"{f_pct:.4f}%" if f_pct is not None else "n/a"
        r_str = f"{r_pct:.4f}%" if r_pct is not None else "n/a"
        self.logger.info(f"[Triangular] Best cycle right now -- forward: {f_str}, reverse: {r_str} (threshold: {self.min_profit_pct}%)")

    def _estimate_profit_pct(self, direction, book_bridge, book_leg, book_quote):
        # Estimation en haut du carnet uniquement : sert à décider si un cycle
        # vaut la peine d'être tenté, pas à calculer le profit réel (celui-ci
        # est recalculé après coup à partir des montants effectivement remplis).
        fee = self._taker_fee_pct() / 100
        if direction == 'forward':
            asks_bridge, asks_leg, bids_quote = book_bridge.get_asks(1), book_leg.get_asks(1), book_quote.get_bids(1)
            if not asks_bridge or not asks_leg or not bids_quote:
                return None
            price_bridge, price_leg, price_quote = float(asks_bridge[0][0]), float(asks_leg[0][0]), float(bids_quote[0][0])
            btc = (self.trade_size_usdc / price_bridge) * (1 - fee)
            eth = (btc / price_leg) * (1 - fee)
            usdc_final = (eth * price_quote) * (1 - fee)
        else:
            asks_quote, bids_leg, bids_bridge = book_quote.get_asks(1), book_leg.get_bids(1), book_bridge.get_bids(1)
            if not asks_quote or not bids_leg or not bids_bridge:
                return None
            price_quote, price_leg, price_bridge = float(asks_quote[0][0]), float(bids_leg[0][0]), float(bids_bridge[0][0])
            eth = (self.trade_size_usdc / price_quote) * (1 - fee)
            btc = (eth * price_leg) * (1 - fee)
            usdc_final = (btc * price_bridge) * (1 - fee)

        profit_usd = usdc_final - self.trade_size_usdc
        return (profit_usd / self.trade_size_usdc) * 100

    async def _execute_cycle(self, direction, estimated_pct):
        self.logger.warning(f"[Triangular] Opportunity found ({direction}): estimated profit {estimated_pct:.4f}%. Executing with real market orders...")
        self._is_trading_enabled = False
        await self.notifier.send_message(f"🔺 *Triangular Opportunity* 🔺\nDirection: {direction}\nEstimated profit: {estimated_pct:.4f}%")

        if direction == 'forward':
            legs_plan = [
                (self.pair_bridge, 'buy'),   # USDC -> BTC
                (self.pair_leg, 'buy'),      # BTC -> ETH
                (self.pair_quote, 'sell'),   # ETH -> USDC
            ]
        else:
            legs_plan = [
                (self.pair_quote, 'buy'),    # USDC -> ETH
                (self.pair_leg, 'sell'),     # ETH -> BTC
                (self.pair_bridge, 'sell'),  # BTC -> USDC
            ]

        executed = []
        held_amount = self.trade_size_usdc  # montant à dépenser sur la jambe 1 (USDC), puis quantité reçue à chaque étape
        aborted = False

        for i, (symbol, side) in enumerate(legs_plan):
            if side == 'buy':
                order = await self._order_manager.create_market_order(self.platform, symbol, 'buy', cost=held_amount)
            else:
                order = await self._order_manager.create_market_order(self.platform, symbol, 'sell', amount=held_amount)

            filled = order.get('filled') if order else None
            if not order or not order.get('id') or not filled:
                self.logger.error(f"[Triangular] Leg {i+1}/3 failed or did not fill on {symbol} ({side}). Aborting cycle.")
                aborted = True
                break

            executed.append({"symbol": symbol, "side": side, "order": order})
            if side == 'buy':
                held_amount = filled  # devise de base reçue, utilisée par la jambe suivante
            else:
                received_cost = order.get('cost')
                held_amount = received_cost if received_cost else filled * (order.get('average') or 0)

        if aborted:
            await self._unwind(executed)
        else:
            final_usdc = held_amount
            real_profit_usd = final_usdc - self.trade_size_usdc
            real_profit_pct = (real_profit_usd / self.trade_size_usdc) * 100
            self.logger.info(f"[Triangular] All 3 legs filled. Real profit: ${real_profit_usd:.4f} ({real_profit_pct:.4f}%)")
            await self.notifier.send_message(f"✅ *Triangular Cycle Complete* ✅\nReal profit: ${real_profit_usd:.4f} ({real_profit_pct:.4f}%)")
            if self.trade_logger:
                self.trade_logger.log_trade(
                    event_type='TRIANGULAR_FILLED', platform_buy=self.platform, platform_sell=self.platform,
                    symbol=f"{self.pair_bridge}|{self.pair_leg}|{self.pair_quote}", volume=self.trade_size_usdc,
                    profit_usd=real_profit_usd, profit_pct=real_profit_pct,
                    details=f"direction={direction}, legs=" + ",".join(f"{l['symbol']}:{l['order']['id']}" for l in executed)
                )

        asyncio.create_task(self.cooldown_trading())

    async def _unwind(self, executed_legs):
        # Si le cycle s'arrête en cours de route, on détient une devise intermédiaire
        # sans couverture : on la revend/rachète immédiatement dans le sens inverse,
        # à partir du montant RÉELLEMENT reçu (pas d'une estimation), pour revenir
        # vers l'USDC plutôt que de laisser une position non voulue.
        if not executed_legs:
            return
        self.logger.warning(f"[Triangular] UNWINDING {len(executed_legs)} executed leg(s) after a partial cycle failure.")
        for leg in reversed(executed_legs):
            symbol, side, order = leg['symbol'], leg['side'], leg['order']
            filled = order.get('filled') or 0
            if side == 'buy':
                unwind_result = await self._order_manager.create_market_order(self.platform, symbol, 'sell', amount=filled)
            else:
                cost = order.get('cost') or (filled * (order.get('average') or 0))
                unwind_result = await self._order_manager.create_market_order(self.platform, symbol, 'buy', cost=cost)

            if not unwind_result or not unwind_result.get('id') or not unwind_result.get('filled'):
                self.logger.error(f"[Triangular] UNHEDGED POSITION: failed to unwind {symbol}. Manual intervention required.")
                await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Triangular)* 🔥\nFailed to unwind {symbol} after a partial cycle failure. Manual intervention required.")
                if self.trade_logger:
                    self.trade_logger.log_trade(event_type='TRIANGULAR_UNHEDGED', platform_buy=self.platform, platform_sell=self.platform, symbol=symbol, volume=filled, details="Failed to unwind after partial cycle failure")

    async def cooldown_trading(self):
        await asyncio.sleep(self._cooldown)
        self.logger.info(f"[Triangular] Trading re-enabled after {self._cooldown}s cooldown.")
        self._is_trading_enabled = True
