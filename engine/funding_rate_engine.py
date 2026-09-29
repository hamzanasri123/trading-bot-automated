# engine/funding_rate_engine.py
import asyncio, logging
import ccxt.async_support as ccxt
from config import API_KEYS, PAPER_TRADING_MODE, FUNDING_RATE_SYMBOL, FUNDING_RATE_ALERT_APR_PCT

class FundingRateEngine:
    """
    Surveillance du taux de financement (funding rate) sur les futures
    perpétuels -- MONITORING SEULEMENT pour l'instant, aucune exécution de
    position à effet de levier.

    Pourquoi s'arrêter au monitoring : le reste du bot n'a atteint sa forme
    actuelle qu'après plusieurs cycles de "construire -> tester en vrai ->
    découvrir un bug réel -> corriger" (filtres de notional, hypothèses de
    remplissage, frais...). Faire la même chose à l'aveugle sur des
    positions à effet de levier (risque de liquidation, pas juste un ordre
    rejeté) serait irresponsable sans pouvoir d'abord valider le
    comportement avec de vraies clés testnet futures.

    Nécessite BINANCE_FUTURES_API_KEY/SECRET dans .env -- des clés
    SÉPARÉES du testnet spot (testnet.binancefuture.com est un
    environnement différent de testnet.binance.vision). Sans ces clés,
    le moteur se contente de le signaler une fois et reste inactif,
    comme OKX quand il n'est pas configuré.

    Principe (si/quand l'exécution sera ajoutée) : quand le funding rate
    est fortement positif, les positions longues payent les positions
    courtes -- une position spot longue + perpétuel court (delta-neutre
    sur le prix) encaisserait ce paiement. L'inverse (funding négatif)
    n'est pas exploitable ici puisqu'on ne peut pas vendre le spot à
    découvert.
    """
    def __init__(self, notifier, trade_logger=None, symbol=None, alert_apr_pct=None):
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.symbol = symbol or FUNDING_RATE_SYMBOL
        self.alert_apr_pct = alert_apr_pct or FUNDING_RATE_ALERT_APR_PCT
        self.logger = logging.getLogger(self.__class__.__name__)
        self.poll_interval = 300  # le funding ne change que toutes les 8h, pas besoin de sonder plus vite
        self.exchange = None
        self._last_alerted_apr = None

    def _configured(self) -> bool:
        keys = API_KEYS.get('BinanceFutures', {})
        return bool(keys.get('apiKey')) and 'YOUR' not in keys.get('apiKey', '')

    async def run(self):
        if not self._configured():
            self.logger.warning(
                "BinanceFutures API keys not configured (BINANCE_FUTURES_API_KEY/SECRET). "
                "Funding rate monitoring is inactive. Register separately at testnet.binancefuture.com "
                "if you want this engine to run -- your spot testnet keys will not work here."
            )
            return

        keys = API_KEYS['BinanceFutures']
        self.exchange = ccxt.binanceusdm({'apiKey': keys['apiKey'], 'secret': keys['secret'], 'enableRateLimit': True})
        try:
            if PAPER_TRADING_MODE and self.exchange.has.get('sandbox', False):
                self.exchange.set_sandbox_mode(True)
                self.logger.info(f"Funding Rate Engine (Paper Trading) is running on {self.symbol}.")
            else:
                self.logger.info(f"Funding Rate Engine (LIVE) is running on {self.symbol}.")

            await self.exchange.load_markets()
            while True:
                await self._check_funding_rate()
                await asyncio.sleep(self.poll_interval)
        except Exception as e:
            self.logger.error(f"Funding Rate Engine failed to initialize or run: {e}", exc_info=True)
        finally:
            if self.exchange:
                await self.exchange.close()

    async def _check_funding_rate(self):
        try:
            info = await self.exchange.fetch_funding_rate(self.symbol)
        except Exception as e:
            self.logger.error(f"Failed to fetch funding rate for {self.symbol}: {e}")
            return

        rate = info.get('fundingRate')
        if rate is None:
            self.logger.warning(f"No fundingRate field in response for {self.symbol}.")
            return

        # Binance paie le funding toutes les 8h, soit 3x/jour.
        apr_pct = rate * 3 * 365 * 100
        self.logger.info(f"[FundingRate] {self.symbol}: rate={rate*100:.4f}% per 8h -> ~{apr_pct:+.2f}% APR annualisé")

        if abs(apr_pct) >= self.alert_apr_pct and self._last_alerted_apr != round(apr_pct, 1):
            direction = "longs payent les shorts (spot long + perp short serait rémunéré)" if apr_pct > 0 else "shorts payent les longs (non exploitable sans short sur le spot)"
            self.logger.warning(f"[FundingRate] Opportunité potentielle sur {self.symbol}: ~{apr_pct:+.2f}% APR -- {direction}")
            await self.notifier.send_message(f"💰 *Funding Rate Alert* 💰\n{self.symbol}: ~{apr_pct:+.2f}% APR\n{direction}\n(Monitoring seulement -- pas d'exécution automatique)")
            self._last_alerted_apr = round(apr_pct, 1)
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='FUNDING_RATE_ALERT', symbol=self.symbol, details=f"apr_pct={apr_pct:.2f}, raw_rate={rate}")
