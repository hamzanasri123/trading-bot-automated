# engine/triangular_engine.py
import asyncio, logging, time
from config import MAX_TRADE_SIZE_USD

class TriangularEngine:
    """
    Arbitrage triangulaire sur une seule plateforme (par défaut Binance) :
    exploite les écarts de prix entre 3 paires corrélées (BTC/USDC, ETH/BTC,
    ETH/USDC) sans jamais toucher un autre exchange. Contrairement à la
    stratégie Maker cross-exchange, il n'y a pas de délai réseau entre deux
    plateformes distinctes -- les 3 jambes s'exécutent sur le même carnet
    d'ordres/la même connexion.

    Limitations connues (v1) :
    - Le calcul de rentabilité utilise seulement le meilleur bid/ask (pas de
      "walk" du carnet en profondeur), donc le slippage réel sur un ordre
      plus gros que le haut du carnet n'est pas modélisé.
    - Le montant de chaque jambe est calculé à l'avance à partir du carnet,
      pas à partir du montant réellement rempli de la jambe précédente.
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
        # Prime appliquée au prix affiché pour garantir une exécution taker
        # immédiate (post_only=False), au lieu d'un vrai ordre market.
        self._aggressive_slippage_pct = 0.002

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

            forward = self._compute_forward(book_bridge, book_leg, book_quote)
            if forward and forward['profit_pct'] > self.min_profit_pct:
                await self._execute_cycle(forward)
                continue

            reverse = self._compute_reverse(book_bridge, book_leg, book_quote)
            if reverse and reverse['profit_pct'] > self.min_profit_pct:
                await self._execute_cycle(reverse)

    def _print_status(self):
        book_bridge, book_leg, book_quote = self._book(self.pair_bridge), self._book(self.pair_leg), self._book(self.pair_quote)
        if not book_bridge or not book_leg or not book_quote:
            self.logger.info("[Triangular] Waiting for order books...")
            return
        forward = self._compute_forward(book_bridge, book_leg, book_quote)
        reverse = self._compute_reverse(book_bridge, book_leg, book_quote)
        f_pct = f"{forward['profit_pct']:.4f}%" if forward else "n/a"
        r_pct = f"{reverse['profit_pct']:.4f}%" if reverse else "n/a"
        self.logger.info(f"[Triangular] Best cycle right now -- forward: {f_pct}, reverse: {r_pct} (threshold: {self.min_profit_pct}%)")

    def _compute_forward(self, book_bridge, book_leg, book_quote):
        # USDC -> BTC (achat sur pair_bridge) -> ETH (achat sur pair_leg) -> USDC (vente sur pair_quote)
        asks_bridge, asks_leg, bids_quote = book_bridge.get_asks(1), book_leg.get_asks(1), book_quote.get_bids(1)
        if not asks_bridge or not asks_leg or not bids_quote:
            return None
        price_bridge, price_leg, price_quote = float(asks_bridge[0][0]), float(asks_leg[0][0]), float(bids_quote[0][0])
        fee = self._taker_fee_pct() / 100

        btc_amount = self.trade_size_usdc / price_bridge
        btc_received = btc_amount * (1 - fee)
        eth_amount = btc_received / price_leg
        eth_received = eth_amount * (1 - fee)
        usdc_final = (eth_received * price_quote) * (1 - fee)

        profit_usd = usdc_final - self.trade_size_usdc
        profit_pct = (profit_usd / self.trade_size_usdc) * 100
        return {
            "direction": "forward", "profit_usd": profit_usd, "profit_pct": profit_pct,
            "legs": [
                {"symbol": self.pair_bridge, "side": "buy", "price": price_bridge, "amount": btc_amount},
                {"symbol": self.pair_leg, "side": "buy", "price": price_leg, "amount": eth_amount},
                {"symbol": self.pair_quote, "side": "sell", "price": price_quote, "amount": eth_received},
            ]
        }

    def _compute_reverse(self, book_bridge, book_leg, book_quote):
        # USDC -> ETH (achat sur pair_quote) -> BTC (vente sur pair_leg) -> USDC (vente sur pair_bridge)
        asks_quote, bids_leg, bids_bridge = book_quote.get_asks(1), book_leg.get_bids(1), book_bridge.get_bids(1)
        if not asks_quote or not bids_leg or not bids_bridge:
            return None
        price_quote, price_leg, price_bridge = float(asks_quote[0][0]), float(bids_leg[0][0]), float(bids_bridge[0][0])
        fee = self._taker_fee_pct() / 100

        eth_amount = self.trade_size_usdc / price_quote
        eth_received = eth_amount * (1 - fee)
        btc_amount = eth_received * price_leg
        btc_received = btc_amount * (1 - fee)
        usdc_final = (btc_received * price_bridge) * (1 - fee)

        profit_usd = usdc_final - self.trade_size_usdc
        profit_pct = (profit_usd / self.trade_size_usdc) * 100
        return {
            "direction": "reverse", "profit_usd": profit_usd, "profit_pct": profit_pct,
            "legs": [
                {"symbol": self.pair_quote, "side": "buy", "price": price_quote, "amount": eth_amount},
                {"symbol": self.pair_leg, "side": "sell", "price": price_leg, "amount": eth_received},
                {"symbol": self.pair_bridge, "side": "sell", "price": price_bridge, "amount": btc_received},
            ]
        }

    async def _execute_cycle(self, opportunity):
        self.logger.warning(f"[Triangular] Opportunity found ({opportunity['direction']}): est. profit {opportunity['profit_pct']:.4f}% (${opportunity['profit_usd']:.4f})")
        self._is_trading_enabled = False
        await self.notifier.send_message(f"🔺 *Triangular Opportunity* 🔺\nDirection: {opportunity['direction']}\nEst. profit: {opportunity['profit_pct']:.4f}% (${opportunity['profit_usd']:.4f})")

        executed_legs = []
        aborted = False
        for i, leg in enumerate(opportunity['legs']):
            aggressive_price = leg['price'] * (1 + self._aggressive_slippage_pct if leg['side'] == 'buy' else 1 - self._aggressive_slippage_pct)
            amount = self._order_manager.round_amount(self.platform, leg['symbol'], leg['amount'])
            result = await self._order_manager.create_limit_order(self.platform, leg['symbol'], leg['side'], amount, aggressive_price)
            if not result or not result.get('id'):
                self.logger.error(f"[Triangular] Leg {i+1}/3 failed on {leg['symbol']} ({leg['side']}). Aborting cycle.")
                aborted = True
                break
            executed_legs.append({**leg, "amount": amount, "order": result})

        if aborted:
            await self._unwind(executed_legs)
        else:
            self.logger.info("[Triangular] All 3 legs executed successfully.")
            await self.notifier.send_message("✅ *Triangular Cycle Complete* ✅\nAll 3 legs executed.")
            if self.trade_logger:
                self.trade_logger.log_trade(
                    event_type='TRIANGULAR_FILLED', platform_buy=self.platform, platform_sell=self.platform,
                    symbol=f"{self.pair_bridge}|{self.pair_leg}|{self.pair_quote}", volume=self.trade_size_usdc,
                    profit_usd=opportunity['profit_usd'], profit_pct=opportunity['profit_pct'],
                    details=f"direction={opportunity['direction']}, legs=" + ",".join(f"{l['symbol']}:{l['order']['id']}" for l in executed_legs)
                )

        asyncio.create_task(self.cooldown_trading())

    async def _unwind(self, executed_legs):
        # Si le cycle s'arrête en cours de route, on détient une devise intermédiaire
        # sans couverture : on la revend/rachète immédiatement dans le sens inverse
        # pour revenir vers l'USDC plutôt que de laisser une position non voulue.
        if not executed_legs:
            return
        self.logger.warning(f"[Triangular] UNWINDING {len(executed_legs)} executed leg(s) after a partial cycle failure.")
        for leg in reversed(executed_legs):
            reverse_side = 'sell' if leg['side'] == 'buy' else 'buy'
            filled_amount = leg['order'].get('filled') or leg['order'].get('amount') or leg['amount']
            aggressive_price = leg['price'] * (1 - self._aggressive_slippage_pct * 5 if reverse_side == 'sell' else 1 + self._aggressive_slippage_pct * 5)
            unwind_result = await self._order_manager.create_limit_order(self.platform, leg['symbol'], reverse_side, filled_amount, aggressive_price)
            if not unwind_result or not unwind_result.get('id'):
                self.logger.error(f"[Triangular] UNHEDGED POSITION: failed to unwind {leg['symbol']}. Manual intervention required.")
                await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Triangular)* 🔥\nFailed to unwind {leg['symbol']} after a partial cycle failure. Manual intervention required.")
                if self.trade_logger:
                    self.trade_logger.log_trade(event_type='TRIANGULAR_UNHEDGED', platform_buy=self.platform, platform_sell=self.platform, symbol=leg['symbol'], volume=filled_amount, details="Failed to unwind after partial cycle failure")

    async def cooldown_trading(self):
        await asyncio.sleep(self._cooldown)
        self.logger.info(f"[Triangular] Trading re-enabled after {self._cooldown}s cooldown.")
        self._is_trading_enabled = True
