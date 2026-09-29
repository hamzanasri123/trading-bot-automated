# engine/price_history.py
import time
from collections import deque

class PriceHistory:
    """
    Historique glissant de prix médians par (platform, symbol), utilisé par
    les stratégies qui ont besoin de statistiques dans le temps (moyenne,
    écart-type, moyennes mobiles) -- contrairement aux carnets d'ordres qui
    ne donnent que l'instant présent.
    """
    def __init__(self, max_samples=500):
        self.max_samples = max_samples
        self._samples = {}  # (platform, symbol) -> deque[(timestamp, mid_price)]

    def record(self, platform: str, symbol: str, mid_price: float):
        key = (platform, symbol)
        if key not in self._samples:
            self._samples[key] = deque(maxlen=self.max_samples)
        self._samples[key].append((time.time(), mid_price))

    def prices(self, platform: str, symbol: str):
        return [p for _, p in self._samples.get((platform, symbol), [])]

    def latest(self, platform: str, symbol: str):
        series = self._samples.get((platform, symbol))
        return series[-1][1] if series else None

    def mean_std(self, platform: str, symbol: str):
        prices = self.prices(platform, symbol)
        if len(prices) < 2:
            return None, None
        n = len(prices)
        mean = sum(prices) / n
        variance = sum((p - mean) ** 2 for p in prices) / n
        return mean, variance ** 0.5

    def sma(self, platform: str, symbol: str, window: int):
        prices = self.prices(platform, symbol)
        if len(prices) < window:
            return None
        return sum(prices[-window:]) / window

    def count(self, platform: str, symbol: str) -> int:
        return len(self._samples.get((platform, symbol), []))
