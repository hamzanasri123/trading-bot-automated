# engine/data_engine.py
import asyncio, logging
from sortedcontainers import SortedDict

class OrderBook:
    def __init__(self):
        self.bids = SortedDict()  # Clés = prix (float), Valeurs = quantité (float)
        self.asks = SortedDict()  # Clés = prix (float), Valeurs = quantité (float)

    def update(self, bids, asks):
        # Gère les deux formats de données (Binance: 2 valeurs, OKX: 4 valeurs)
        for item in bids:
            price, qty = float(item[0]), float(item[1])
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        
        for item in asks:
            price, qty = float(item[0]), float(item[1])
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty

    def get_bids(self, n: int):
        """
        Retourne les N meilleurs bids (les plus hauts prix).
        Convertit le dictionnaire en liste de paires, prend les N derniers, et les inverse.
        """
        # --- CORRECTION DÉFINITIVE ET STANDARD ---
        all_bids = list(self.bids.items())
        # Prend les N derniers éléments (les plus hauts prix)
        top_bids = all_bids[-n:]
        # Les inverse pour que le meilleur prix soit en premier
        return top_bids[::-1]

    def get_asks(self, n: int):
        """
        Retourne les N meilleurs asks (les plus bas prix).
        Convertit le dictionnaire en liste de paires et prend les N premiers.
        """
        # --- CORRECTION DÉFINITIVE ET STANDARD ---
        all_asks = list(self.asks.items())
        # Prend les N premiers éléments (les plus bas prix)
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
            
            self.order_books[book_key].update(data['b'], data['a'])
        except Exception as e:
            self.logger.error(f"Error processing direct update in DataEngine: {e}", exc_info=True)

    async def run(self):
        self.logger.info("Data Engine is running (in direct-call mode).")
        while True:
            await asyncio.sleep(3600)
