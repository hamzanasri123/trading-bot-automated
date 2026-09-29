# engine/triangular_engine.py
import asyncio, logging, time
from config import MAX_TRADE_SIZE_USD, MIN_PROFIT_PCT_TRIANGULAR

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
        # Sizing symétrique : sur un cycle gagnant, on ajoute ce pourcentage du
        # gain à la taille du prochain trade (compounding progressif) ; sur un
        # cycle perdant, on retire ce même pourcentage de la perte -- sans quoi
        # le compounding grossirait sur les séries gagnantes sans jamais se
        # réduire sur les séries perdantes qui suivraient.
        self.reinvest_pct = 0.5
        self.min_trade_size_usdc = MAX_TRADE_SIZE_USD
        self.max_trade_size_usdc = MAX_TRADE_SIZE_USD * 10
        # Marge de sécurité au-delà des 3 frais taker, pour absorber le slippage
        # et l'imprécision du calcul en top-of-book. Réglable via config.py.
        self.min_profit_pct = MIN_PROFIT_PCT_TRIANGULAR

        # Coupe-circuit : si le P&L réel cumulé de la session (calculé à partir
        # des montants effectivement exécutés, pas des estimations) descend
        # sous ce seuil, le trading s'arrête et ne reprend plus tout seul.
        self.session_pnl_usd = 0.0
        self.max_session_loss_usd = MAX_TRADE_SIZE_USD * 10
        self._halted = False

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
            if self._halted or not self._is_trading_enabled:
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
            recovered_usdc = await self._unwind(executed)
            if recovered_usdc is not None:
                real_profit_usd = recovered_usdc - self.trade_size_usdc
                real_profit_pct = (real_profit_usd / self.trade_size_usdc) * 100
                self.logger.info(f"[Triangular] Cycle unwound. Real P&L: ${real_profit_usd:.4f} ({real_profit_pct:.4f}%)")
                if self.trade_logger:
                    self.trade_logger.log_trade(
                        event_type='TRIANGULAR_UNWOUND', platform_buy=self.platform, platform_sell=self.platform,
                        symbol=f"{self.pair_bridge}|{self.pair_leg}|{self.pair_quote}", volume=self.trade_size_usdc,
                        profit_usd=real_profit_usd, profit_pct=real_profit_pct,
                        details=f"direction={direction}, partial legs=" + ",".join(f"{l['symbol']}:{l['order']['id']}" for l in executed)
                    )
                await self._apply_sizing_and_breaker(real_profit_usd)
            # Si recovered_usdc est None, le dénouement a lui-même échoué : déjà
            # alerté comme TRIANGULAR_UNHEDGED dans _unwind, aucun P&L fiable à
            # calculer ici (position réellement inconnue, nécessite une vérif manuelle).
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
            await self._apply_sizing_and_breaker(real_profit_usd)

        asyncio.create_task(self.cooldown_trading())

    async def _apply_sizing_and_breaker(self, real_profit_usd):
        # Sizing symétrique : la taille du prochain trade bouge de reinvest_pct
        # du P&L réel du cycle qui vient de se terminer, gain ou perte, dans les
        # bornes [min_trade_size_usdc, max_trade_size_usdc].
        self.session_pnl_usd += real_profit_usd
        old_size = self.trade_size_usdc
        new_size = old_size + real_profit_usd * self.reinvest_pct
        new_size = max(self.min_trade_size_usdc, min(new_size, self.max_trade_size_usdc))
        self.trade_size_usdc = new_size
        if abs(new_size - old_size) > 1e-9:
            self.logger.info(f"[Triangular] Sizing adjusted: ${old_size:.4f} -> ${new_size:.4f} (cycle P&L ${real_profit_usd:+.4f}, session P&L ${self.session_pnl_usd:+.4f})")

        if not self._halted and self.session_pnl_usd <= -self.max_session_loss_usd:
            self._halted = True
            self.logger.critical(f"[Triangular] CIRCUIT BREAKER TRIGGERED: session P&L ${self.session_pnl_usd:.4f} <= -${self.max_session_loss_usd:.4f}. Trading halted.")
            await self.notifier.send_message(f"🛑 *CIRCUIT BREAKER* 🛑\nSession P&L: ${self.session_pnl_usd:.4f}\nTrading has been halted and will NOT resume automatically. Restart the bot after review.")

    async def _unwind(self, executed_legs):
        # Si le cycle s'arrête en cours de route, on détient une devise intermédiaire
        # sans couverture : on la revend/rachète immédiatement dans le sens inverse,
        # à partir du montant RÉELLEMENT reçu (pas d'une estimation), pour revenir
        # vers l'USDC plutôt que de laisser une position non voulue.
        # Retourne le montant USDC effectivement récupéré, ou None si le
        # dénouement lui-même a échoué (position dans un état inconnu).
        if not executed_legs:
            return None
        self.logger.warning(f"[Triangular] UNWINDING {len(executed_legs)} executed leg(s) after a partial cycle failure.")
        recovered_usdc = None
        reversed_legs = list(reversed(executed_legs))
        for idx, leg in enumerate(reversed_legs):
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
                return None

            # Le dernier ordre de dénouement (inversion de la toute première jambe)
            # est celui qui revient effectivement en USDC.
            if idx == len(reversed_legs) - 1:
                recovered_usdc = unwind_result.get('cost')
                if recovered_usdc is None:
                    recovered_usdc = unwind_result.get('filled', 0) * (unwind_result.get('average') or 0)

        return recovered_usdc

    async def cooldown_trading(self):
        await asyncio.sleep(self._cooldown)
        if self._halted:
            self.logger.info("[Triangular] Trading remains halted (circuit breaker).")
            return
        self.logger.info(f"[Triangular] Trading re-enabled after {self._cooldown}s cooldown.")
        self._is_trading_enabled = True
