# engine/strategy_engine.py
import asyncio, logging, time, json
from config import MAX_TRADE_SIZE_USD

class StrategyEngine:
    def __init__(self, order_books: dict, order_manager, notifier):
        self._order_books = order_books
        self._order_manager = order_manager
        self.notifier = notifier
        self.logger = logging.getLogger(self.__class__.__name__)
        self.taker_profit_threshold_pct = 0.05
        self.maker_spread_threshold_pct = 0.0
        self._is_trading_enabled = True
        self._cooldown = 5
        self._last_print_time = 0
        self._print_interval = 10
        self.active_maker_trade = None

    # ... (la méthode _print_order_books ne change pas) ...
    def _print_order_books(self):
        print("\n" + "="*80 + f"\n--- ORDER BOOK SNAPSHOT ({time.strftime('%H:%M:%S')}) ---")
        order_books_copy = dict(self._order_books)
        if not order_books_copy: print("No order books available.")
        for (platform, symbol), book in order_books_copy.items():
            print(f"\n--- {platform} ({symbol}) ---")
            asks, bids = book.get_asks(3), book.get_bids(3)
            if not asks or not bids: print("Order book is empty or incomplete."); continue
            print("ASKS (Sell)                  | BIDS (Buy)")
            print("Price (USDC)   | Qty (BTC)     | Price (USDC)   | Qty (BTC)")
            print("--------------|---------------|----------------|---------------")
            for i in range(3):
                ask_price, ask_qty = asks[i] if i < len(asks) else ('-', '-')
                bid_price, bid_qty = bids[i] if i < len(bids) else ('-', '-')
                print(f"{str(ask_price):<14} | {str(ask_qty):<13} | {str(bid_price):<14} | {str(bid_qty):<13}")
        print("="*80 + "\n")

    async def run(self):
        self.logger.info("Strategy Engine is running.")
        while True:
            # --- MODIFICATION : La vérification se fait maintenant dans une tâche de fond ---
            await asyncio.sleep(0.1) # Petite pause pour ne pas surcharger le CPU
            
            current_time = time.time()
            if current_time - self._last_print_time > self._print_interval:
                self._print_order_books()
                self._last_print_time = current_time

            if not self._is_trading_enabled or self.active_maker_trade:
                continue

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

    # ... (evaluate_market_pair et execute_taker_strategy ne changent pas) ...
    def evaluate_market_pair(self, book_buy_on, book_sell_on, buy_platform_name, sell_platform_name, symbol):
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
            asyncio.create_task(self.execute_maker_strategy(book_buy_on, book_sell_on, buy_platform_name, sell_platform_name, symbol))

    def execute_taker_strategy(self, book_buy, book_sell, platform_buy_name, platform_sell_name, symbol):
        asks, bids = book_buy.get_asks(10), book_sell.get_bids(10)
        if not asks or not bids: return
        taker_fee_buy = self._order_manager.get_fees(platform_buy_name)['taker']
        taker_fee_sell = self._order_manager.get_fees(platform_sell_name)['taker']
        result = self.calculate_real_profit(asks, bids, taker_fee_buy, taker_fee_sell, MAX_TRADE_SIZE_USD)
        if result and result['net_profit_pct'] > self.taker_profit_threshold_pct:
            self.logger.info(f"--- Triggering TAKER order for {result['net_profit_pct']:.4f}% profit. ---")
            self._is_trading_enabled = False
            asyncio.create_task(self.notifier.send_message(f"🚀 *Taker Opportunity Found* 🚀\nProfit: *{result['net_profit_pct']:.4f}%*\nBuy on {platform_buy_name}, Sell on {platform_sell_name}."))
            asyncio.create_task(self._order_manager.execute_arbitrage(volume=result['volume'], platform_buy=platform_buy_name, platform_sell=platform_sell_name, max_buy_price=float(asks[0][0]), min_sell_price=float(bids[0][0]), symbol=symbol))
            asyncio.create_task(self.cooldown_trading())

    async def execute_maker_strategy(self, book_buy_on, book_sell_on, buy_platform, sell_platform, symbol):
        if self.active_maker_trade: return
        buy_bids = book_buy_on.get_bids(1)
        sell_asks = book_sell_on.get_asks(1)
        if not buy_bids or not sell_asks: return
        our_buy_price = float(buy_bids[0][0]) + 0.01
        our_sell_price = float(sell_asks[0][0]) - 0.01
        if our_buy_price >= our_sell_price:
            self.logger.info(f"Maker prices crossed or invalid. Buy: {our_buy_price}, Sell: {our_sell_price}. Aborting.")
            return
        self.logger.info("--- Triggering MAKER orders (Post-Only) ---")
        self._is_trading_enabled = False # Désactive la recherche de nouvelles opportunités
        volume = MAX_TRADE_SIZE_USD / our_buy_price
        buy_order_task = asyncio.create_task(self._order_manager.create_limit_order(buy_platform, symbol, 'buy', volume, our_buy_price, post_only=True))
        sell_order_task = asyncio.create_task(self._order_manager.create_limit_order(sell_platform, symbol, 'sell', volume, our_sell_price, post_only=True))
        buy_result, sell_result = await asyncio.gather(buy_order_task, sell_order_task)
        if buy_result and buy_result.get('id') and sell_result and sell_result.get('id'):
            self.active_maker_trade = {
                "buy_leg": buy_result, "sell_leg": sell_result, 
                "status": "active", "creation_time": time.time(),
                "buy_platform": buy_platform, "sell_platform": sell_platform, "symbol": symbol
            }
            self.logger.info(f"Active Maker trade created. Buy ID: {buy_result['id']}, Sell ID: {sell_result['id']}")
            # Lance la tâche de surveillance en fond
            asyncio.create_task(self.maker_trade_monitoring_loop())
        else:
            self.logger.error("Failed to place one or both Maker (Post-Only) orders. Cleaning up.")
            if buy_result and buy_result.get('id'): await self._order_manager.cancel_order(buy_platform, buy_result['id'], symbol)
            if sell_result and sell_result.get('id'): await self._order_manager.cancel_order(sell_platform, sell_result['id'], symbol)
            self._is_trading_enabled = True # Réactive la recherche

    async def maker_trade_monitoring_loop(self):
        """
        Une boucle dédiée qui surveille un trade Maker actif.
        """
        self.logger.info("Starting Maker trade monitoring loop...")
        while self.active_maker_trade:
            await self.check_maker_trade_status()
            await asyncio.sleep(1) # Vérifie le statut toutes les secondes
        self.logger.info("Exiting Maker trade monitoring loop.")
        # Une fois la boucle terminée (trade complété ou annulé), on réactive le trading
        await self.cooldown_trading()

    async def check_maker_trade_status(self):
        # --- LOGIQUE DE "QUEUE JUMPING" AJOUTÉE ICI ---
        if not self.active_maker_trade: return
        
        trade_info = self.active_maker_trade
        buy_leg, sell_leg = trade_info['buy_leg'], trade_info['sell_leg']
        buy_platform, sell_platform, symbol = trade_info['buy_platform'], trade_info['sell_platform'], trade_info['symbol']

        buy_book = self._order_books.get((buy_platform, symbol))
        sell_book = self._order_books.get((sell_platform, symbol))

        if not buy_book or not sell_book: return

        # Vérifie si quelqu'un a placé un meilleur ordre que nous
        current_best_bid_buy_platform = float(buy_book.get_bids(1)[0][0]) if buy_book.get_bids(1) else 0
        current_best_ask_sell_platform = float(sell_book.get_asks(1)[0][0]) if sell_book.get_asks(1) else float('inf')

        # Si un meilleur acheteur est apparu, notre ordre d'achat n'est plus le premier
        if current_best_bid_buy_platform > buy_leg['price']:
            self.logger.info("Queue Jump: Market moved against our Buy Maker order. Repositioning...")
            await self.cancel_and_reset_maker_trade()
            return

        # Si un meilleur vendeur est apparu, notre ordre de vente n'est plus le premier
        if current_best_ask_sell_platform < sell_leg['price']:
            self.logger.info("Queue Jump: Market moved against our Sell Maker order. Repositioning...")
            await self.cancel_and_reset_maker_trade()
            return

        # --- Le reste de la logique (vérification de l'exécution, timeout) reste le même ---
        buy_status_task = self._order_manager.fetch_order_status(buy_platform, buy_leg['id'], symbol)
        sell_status_task = self._order_manager.fetch_order_status(sell_platform, sell_leg['id'], symbol)
        buy_order, sell_order = await asyncio.gather(buy_status_task, sell_status_task)

        if buy_order and buy_order['status'] == 'closed' and sell_order and sell_order['status'] == 'closed':
            self.logger.info("SUCCESS: Both Maker legs filled! Profit captured.")
            await self.notifier.send_message("✅ *Maker Arbitrage Success* ✅\nBoth passive orders were filled.")
            self.active_maker_trade = None
            return

        # ... (logique de "chasing" si un seul ordre est rempli) ...

        if time.time() - trade_info['creation_time'] > 30: # Timeout de 30 secondes
            self.logger.info("Maker orders timed out. Cancelling and resetting.")
            await self.cancel_and_reset_maker_trade()
            return

    async def cancel_and_reset_maker_trade(self):
        if not self.active_maker_trade: return
        self.logger.info("Cancelling active maker orders to reposition.")
        buy_leg, sell_leg = self.active_maker_trade['buy_leg'], self.active_maker_trade['sell_leg']
        await asyncio.gather(
            self._order_manager.cancel_order(buy_leg['info']['platform'], buy_leg['id'], buy_leg['symbol']),
            self._order_manager.cancel_order(sell_leg['info']['platform'], sell_leg['id'], sell_leg['symbol'])
        )
        self.active_maker_trade = None
        self.logger.info("Maker trade reset. Resuming general strategy evaluation.")

    # ... (cooldown_trading et calculate_real_profit ne changent pas) ...
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
