# engine/data_engine.py
import asyncio, logging
from sortedcontainers import SortedDict

class OrderBook:
    def __init__(self):
        self.bids = SortedDict()
        self.asks = SortedDict()

    def update(self, bids, asks):
        for item in bids:
            price, qty = float(item[0]), float(item[1])
            if qty == 0: self.bids.pop(price, None)
            else: self.bids[price] = qty
        
        for item in asks:
            price, qty = float(item[0]), float(item[1])
            if qty == 0: self.asks.pop(price, None)
            else: self.asks[price] = qty

    def get_bids(self, n: int):
        all_bids = list(self.bids.items())
        top_bids = all_bids[-n:]
        return top_bids[::-1]

    def get_asks(self, n: int):
        all_asks = list(self.asks.items())
        top_asks = all_asks[:n]
        return top_asks

class DataEngine:
    def __init__(self):
        self.order_books = {}
        self.logger = logging.getLogger(self.__class__.__name__)

    def process_update(self, packaged_data: dict):
        try:
            platform, symbol, data = packaged_data["platform"], packaged_data["symbol"], packaged_data["data"]
            book_key = (platform, symbol)
            if book_key not in self.order_books:
                self.order_books[book_key] = OrderBook()
                self.logger.info(f"Order book created for {platform}-{symbol}.")
            
            # --- CORRECTION DÉFINITIVE APPLIQUÉE ICI ---
            # Gère les deux formats de données :
            # Binance utilise 'b' (bids) et 'a' (asks)
            # OKX utilise 'bids' et 'asks'
            bids_data = data.get('bids', data.get('b'))
            asks_data = data.get('asks', data.get('a'))

            if bids_data is None or asks_data is None:
                self.logger.warning(f"Received malformed data from {platform}: missing bids or asks.")
                return

            self.order_books[book_key].update(bids_data, asks_data)
            
        except Exception as e:
            self.logger.error(f"Error processing direct update in DataEngine: {e}", exc_info=True)

    async def run(self):
        # Cette tâche ne fait plus rien d'actif, mais elle maintient le moteur "en vie"
        # pour la cohérence de l'architecture.
        self.logger.info("Data Engine is running (in direct-call mode).")
        while True:
            await asyncio.sleep(3600)
