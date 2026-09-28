# connectors/binance_connector.py
import asyncio, json, logging, websockets
from config import PAPER_TRADING_MODE

class BinanceConnector:
    def __init__(self, data_engine):
        self.name = "Binance"
        self.symbol_unified = "BTC/USDC"
        self.symbol_ws = self.symbol_unified.replace('/', '').lower()
        
        # --- CORRECTION : URL DYNAMIQUE ---
        if PAPER_TRADING_MODE:
            # URL du Testnet de Binance (les données de marché testnet diffèrent
            # de la prod — utiliser le flux prod en paper trading donnerait des
            # prix qui ne correspondent pas aux ordres réellement testés)
            base_url = "wss://stream.testnet.binance.vision:9443/ws"
        else:
            # URL de Production de Binance
            base_url = "wss://stream.binance.com:9443/ws"
        
        self.ws_url = f"{base_url}/{self.symbol_ws}@depth@100ms"
        self.logger = logging.getLogger(self.__class__.__name__)
        self.data_engine = data_engine

    async def run(self):
        # ... (le reste du fichier ne change pas)
        self.logger.info(f"Connecting to {self.name} data stream: {self.ws_url}")
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.logger.info(f"Successfully connected to {self.symbol_unified} on {self.name}.")
                    while True:
                        data = await ws.recv()
                        update_data = {"platform": self.name, "symbol": self.symbol_unified, "data": json.loads(data)}
                        self.data_engine.process_update(update_data)
            except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
                self.logger.error(f"Connection lost to {self.name} (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"An unexpected error occurred with {self.name} connector: {e}", exc_info=True)
                await asyncio.sleep(5)
