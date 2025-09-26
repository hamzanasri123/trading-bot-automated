# engine/strategy_engine.py
import asyncio, logging, time
from config import MAX_TRADE_SIZE_USD

class StrategyEngine:
    def __init__(self, order_books: dict, order_manager, notifier):
        self._order_books = order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.logger = logging.getLogger(self.__class__.__name__)
        self.taker_profit_threshold_pct = 0.001 #0.05
        self.maker_spread_threshold_pct = 0.0
        self._is_trading_enabled = True
        self._cooldown = 5
        self._last_print_time = 0
        self._print_interval = 10
        self.active_maker_trade = None

    def _print_order_books(self):
        # ... (cette méthode ne change pas)
        print("\n" + "="*80 + f"\n--- ORDER BOOK SNAPSHOT ({time.strftime('%H:%M:%S')}) ---")
        order_books_copy = dict(self._order_books)
        if not order_books_copy: print("No order books available.")
        for (platform, symbol), book in order_books_copy.items():
            print(f"\n--- {platform} ({symbol}) ---")
            asks, bids = book.get_asks(3), book.get_bids(3)
            if not asks or not bids: print("Order book is empty or incomplete."); continue
            print("ASKS (Sell)                  | BIDS (Buy)")
            print("Price (USDT)   | Qty (BTC)     | Price (USDT)   | Qty (BTC)")
            print("--------------|---------------|----------------|---------------")
            for i in range(3):
                ask_price, ask_qty = asks[i] if i < len(asks) else ('-', '-')
                bid_price, bid_qty = bids[i] if i < len(bids) else ('-', '-')
                print(f"{str(ask_price):<14} | {str(ask_qty):<13} | {str(bid_price):<14} | {str(bid_qty):<13}")
        print("="*80 + "\n")

    async def run(self):
        # ... (cette méthode ne change pas)
        self.logger.info("Strategy Engine is running.")
        while True:
            if self.active_maker_trade:
                await self.check_maker_trade_status()
            await asyncio.sleep(0.1)
            current_time = time.time()
            if current_time - self._last_print_time > self._print_interval:
                self._print_order_books()
                self._last_print_time = current_time
            if not self._is_trading_enabled: continue
            try: order_books_copy = dict(self._order_books)
            except Exception: continue
            if len(order_books_copy) < 2: continue
            platforms = list(order_books_copy.keys())
            for i in range(len(platforms)):
                for j in range(i + 1, len(platforms)):
                    platform_A_key, platform_B_key = platforms[i], platforms[j]
                    if platform_A_key not in order_books_copy or platform_B_key not in order_books_copy: continue
                    book_A, book_B = order_books_copy[platform_A_key], order_books_copy[platform_B_key]
                    self.evaluate_market_pair(book_A, book_B, platform_A_key[0], platform_B_key[0], platform_A_key[1])
                    self.evaluate_market_pair(book_B, book_A, platform_B_key[0], platform_A_key[0], platform_B_key[1])

    def evaluate_market_pair(self, book_buy_on, book_sell_on, buy_platform_name, sell_platform_name, symbol):
        # ... (cette méthode ne change pas)
        asks, bids = book_buy_on.get_asks(1), book_sell_on.get_bids(1)
        if not asks or not bids: return
        best_ask_price, best_bid_price = float(asks[0][0]), float(bids[0][0])
        spread_pct = ((best_bid_price - best_ask_price) / best_ask_price) * 100
        taker_fee_buy = self._order_manager.get_fees(buy_platform_name)['taker']
        taker_fee_sell = self._order_manager.get_fees(sell_platform_name)['taker']
        taker_profit_pct = spread_pct - taker_fee_buy - taker_fee_sell
        if taker_profit_pct > self.taker_profit_threshold_pct:
            self.logger.warning(f"[TAKER STRATEGY] Opportunity found! Est. Net Profit: {taker_profit_pct:.4f}%. (Spread: {spread_pct:.4f}%, Fees: {taker_fee_buy + taker_fee_sell:.4f}%)")
            self.execute_taker_strategy(book_buy_on, book_sell_on, buy_platform_name, sell_platform_name, symbol)
        elif spread_pct > self.maker_spread_threshold_pct and not self.active_maker_trade:
            self.logger.info(f"[MAKER STRATEGY] Favorable spread ({spread_pct:.4f}%). Triggering Maker logic.")
            # On passe les deux carnets d'ordres à la stratégie Maker
            asyncio.create_task(self.execute_maker_strategy(book_buy_on, book_sell_on, buy_platform_name, sell_platform_name, symbol))

    def execute_taker_strategy(self, book_buy, book_sell, platform_buy_name, platform_sell_name, symbol):
        # ... (cette méthode ne change pas)
        asks, bids = book_buy.get_asks(10), book_sell.get_bids(10)
        if not asks or not bids: return
        taker_fee_buy = self._order_manager.get_fees(platform_buy_name)['taker']
        taker_fee_sell = self._order_manager.get_fees(platform_sell_name)['taker']
        result = self.calculate_real_profit(asks, bids, taker_fee_buy, taker_fee_sell, MAX_TRADE_SIZE_USD)
        if result and result['net_profit_pct'] > self.taker_profit_threshold_pct:
            self.logger.info(f"--- Triggering TAKER order for {result['net_profit_pct']:.4f}% profit. ---")
            self._is_trading_enabled = False
            #self.notifier.send_message(f"🚀 *Taker Opportunity Found* 🚀\nProfit: *{result['net_profit_pct']:.4f}%*\nBuy on {platform_buy_name}, Sell on {platform_sell_name}.")
            asyncio.create_task(self.notifier.send_message(f"🚀 *Taker Opportunity Found* 🚀\nProfit: *{result['net_profit_pct']:.4f}%*\nBuy on {platform_buy_name}, Sell on {platform_sell_name}."))
            asyncio.create_task(self._order_manager.execute_arbitrage(volume=result['volume'], platform_buy=platform_buy_name, platform_sell=platform_sell_name, max_buy_price=float(asks[0][0]), min_sell_price=float(bids[0][0]), symbol=symbol))
            asyncio.create_task(self.cooldown_trading())

    async def execute_maker_strategy(self, book_buy_on, book_sell_on, buy_platform, sell_platform, symbol):
        # --- CORRECTION DE LOGIQUE APPLIQUÉE ICI ---
        if self.active_maker_trade: return

        # On récupère les meilleurs prix des DEUX plateformes
        buy_asks = book_buy_on.get_asks(1)
        buy_bids = book_buy_on.get_bids(1)
        sell_asks = book_sell_on.get_asks(1)
        sell_bids = book_sell_on.get_bids(1)

        if not all([buy_asks, buy_bids, sell_asks, sell_bids]):
            self.logger.debug("Maker strategy aborted: incomplete order books.")
            return

        # Le meilleur prix pour acheter sur la plateforme d'achat
        best_ask_on_buy_platform = float(buy_asks[0][0])
        # Le meilleur prix pour vendre sur la plateforme de vente
        best_bid_on_sell_platform = float(sell_bids[0][0])

        # On se place juste à l'intérieur du spread pour devenir le meilleur prix
        # Notre prix d'achat est juste au-dessus du meilleur acheteur actuel sur la plateforme d'achat
        our_buy_price = float(buy_bids[0][0]) + 0.01
        # Notre prix de vente est juste en dessous du meilleur vendeur actuel sur la plateforme de vente
        our_sell_price = float(sell_asks[0][0]) - 0.01

        # Vérification de sécurité : nos prix ne doivent pas se croiser
        if our_buy_price >= our_sell_price:
            self.logger.info(f"Maker prices crossed or invalid. Buy: {our_buy_price}, Sell: {our_sell_price}. Aborting.")
            return
        
        # Vérification de sécurité : nos prix ne doivent pas être pires que le marché
        if our_buy_price >= best_ask_on_buy_platform or our_sell_price <= best_bid_on_sell_platform:
             self.logger.info(f"Maker prices would cross the live market spread. Aborting.")
             return

        self.logger.info("--- Triggering MAKER orders (Post-Only) ---")
        volume = MAX_TRADE_SIZE_USD / our_buy_price
        buy_order_task = asyncio.create_task(self._order_manager.create_limit_order(buy_platform, symbol, 'buy', volume, our_buy_price, post_only=True))
        sell_order_task = asyncio.create_task(self._order_manager.create_limit_order(sell_platform, symbol, 'sell', volume, our_sell_price, post_only=True))
        
        buy_result, sell_result = await asyncio.gather(buy_order_task, sell_order_task)
        
        if buy_result and buy_result.get('id') and sell_result and sell_result.get('id'):
            self.active_maker_trade = {"buy_leg": buy_result, "sell_leg": sell_result, "status": "active", "creation_time": time.time()}
            self.logger.info(f"Active Maker trade created. Buy ID: {buy_result['id']}, Sell ID: {sell_result['id']}")
            self._is_trading_enabled = False
        else:
            self.logger.error("Failed to place one or both Maker (Post-Only) orders. Cleaning up.")
            if buy_result and buy_result.get('id'): await self._order_manager.cancel_order(buy_platform, buy_result['id'], symbol)
            if sell_result and sell_result.get('id'): await self._order_manager.cancel_order(sell_platform, sell_result['id'], symbol)

    # ... (le reste du fichier ne change pas)
    async def check_maker_trade_status(self):
        if not self.active_maker_trade: return
        buy_leg, sell_leg = self.active_maker_trade['buy_leg'], self.active_maker_trade['sell_leg']
        buy_book = self._order_books.get((buy_leg['info']['platform'], buy_leg['symbol']))
        sell_book = self._order_books.get((sell_leg['info']['platform'], sell_leg['symbol']))
        if not buy_book or not sell_book: return
        current_best_bid_buy_platform = float(buy_book.get_bids(1)[0][0]) if buy_book.get_bids(1) else 0
        current_best_ask_sell_platform = float(sell_book.get_asks(1)[0][0]) if sell_book.get_asks(1) else float('inf')
        if current_best_bid_buy_platform > buy_leg['price']: self.logger.info("Market moved against our Buy Maker order. Repositioning."); await self.cancel_and_reset_maker_trade(); return
        if current_best_ask_sell_platform < sell_leg['price']: self.logger.info("Market moved against our Sell Maker order. Repositioning."); await self.cancel_and_reset_maker_trade(); return
        buy_status_task, sell_status_task = self._order_manager.fetch_order_status(buy_leg['info']['platform'], buy_leg['id'], buy_leg['symbol']), self._order_manager.fetch_order_status(sell_leg['info']['platform'], sell_leg['id'], sell_leg['symbol'])
        buy_order, sell_order = await asyncio.gather(buy_status_task, sell_status_task)
        if buy_order and buy_order['status'] == 'closed' and sell_order and sell_order['status'] == 'closed':
            self.logger.info("SUCCESS: Both Maker legs filled! Profit captured."); self.notifier.send_message("✅ *Maker Arbitrage Success* ✅\nBoth passive orders were filled."); self.active_maker_trade = None; self._is_trading_enabled = True; return
        if (buy_order and buy_order['status'] == 'closed') and (sell_order and sell_order['status'] == 'open'):
            self.logger.warning("Maker leg filled (Buy). Chasing the Sell leg."); self.notifier.send_message("🏃‍♂️ *Chasing Maker Leg* 🏃‍♂️\nBuy order filled. Converting Sell order to Taker to complete trade."); await self._order_manager.cancel_order(sell_leg['info']['platform'], sell_leg['id'], sell_leg['symbol']); await self._order_manager.create_limit_order(sell_leg['info']['platform'], sell_leg['symbol'], 'sell', sell_leg['amount'], sell_order['price'] * 0.99); self.active_maker_trade = None; self._is_trading_enabled = True; return
        if (sell_order and sell_order['status'] == 'closed') and (buy_order and buy_order['status'] == 'open'):
            self.logger.warning("Maker leg filled (Sell). Chasing the Buy leg."); self.notifier.send_message("🏃‍♂️ *Chasing Maker Leg* 🏃‍♂️\nSell order filled. Converting Buy order to Taker to complete trade."); await self._order_manager.cancel_order(buy_leg['info']['platform'], buy_leg['id'], buy_leg['symbol']); await self._order_manager.create_limit_order(buy_leg['info']['platform'], buy_leg['symbol'], 'buy', buy_leg['amount'], buy_order['price'] * 1.01); self.active_maker_trade = None; self._is_trading_enabled = True; return
        if time.time() - self.active_maker_trade['creation_time'] > 30:
            self.logger.info("Maker orders timed out. Cancelling and resetting."); await self.cancel_and_reset_maker_trade(); return

    async def cancel_and_reset_maker_trade(self):
        if not self.active_maker_trade: return
        self.logger.info("Cancelling active maker orders to reposition.")
        buy_leg, sell_leg = self.active_maker_trade['buy_leg'], self.active_maker_trade['sell_leg']
        await asyncio.gather(self._order_manager.cancel_order(buy_leg['info']['platform'], buy_leg['id'], buy_leg['symbol']), self._order_manager.cancel_order(sell_leg['info']['platform'], sell_leg['id'], sell_leg['symbol']))
        self.active_maker_trade = None; self._is_trading_enabled = True
        self.logger.info("Maker trade reset. Resuming general strategy evaluation.")

    async def cooldown_trading(self):
        await asyncio.sleep(self._cooldown)
        self.logger.info(f"Trading re-enabled after {self._cooldown}s cooldown.")
        self._is_trading_enabled = True

    def calculate_real_profit(self, asks_to_buy, bids_to_sell, buy_fee_pct: float, sell_fee_pct: float, max_size_usd: float):
        volume_traded, buy_cost, sell_revenue = 0, 0, 0
        buy_idx, sell_idx = 0, 0
        buy_qtys, sell_qtys = [float(q) for _, q in asks_to_buy], [float(q) for _, q in bids_to_sell]
        while buy_idx < len(asks_to_buy) and sell_idx < len(bids_to_sell):
            buy_price, sell_price = float(asks_to_buy[buy_idx][0]), float(bids_to_sell[sell_idx][0])
            if buy_price >= sell_price: break
            max_volume_allowed = max_size_usd / buy_price
            vol = min(buy_qtys[buy_idx], sell_qtys[sell_idx], max_volume_allowed - volume_traded)
            if vol <= 0: break
            cost, revenue = vol * buy_price, vol * sell_price
            fees = (cost * (buy_fee_pct / 100)) + (revenue * (sell_fee_pct / 100))
            if (revenue - cost - fees) <= 0: break
            volume_traded, buy_cost, sell_revenue = volume_traded + vol, buy_cost + cost, sell_revenue + revenue
            buy_qtys[buy_idx] -= vol; sell_qtys[sell_idx] -= vol
            if buy_qtys[buy_idx] <= 1e-9: buy_idx += 1
            if sell_qtys[sell_idx] <= 1e-9: sell_idx += 1
            if buy_cost >= max_size_usd: break
        if volume_traded > 0:
            total_fees = (buy_cost * (buy_fee_pct / 100)) + (sell_revenue * (sell_fee_pct / 100))
            net_profit_usd = sell_revenue - buy_cost - total_fees
            net_profit_pct = (net_profit_usd / buy_cost) * 100 if buy_cost > 0 else 0
            return {"volume": volume_traded, "buy_cost": buy_cost, "sell_revenue": sell_revenue, "net_profit_usd": net_profit_usd, "net_profit_pct": net_profit_pct}
        return None
