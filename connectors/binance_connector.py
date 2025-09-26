# connectors/binance_connector.py
import asyncio, json, logging
import websockets

class BinanceConnector:
    def __init__(self, data_engine):
        self.name = "Binance"
        self.symbol = "btcusdc"
        self.ws_url = f"wss://stream.binance.com:9443/ws/{self.symbol}@depth@100ms"
        self.logger = logging.getLogger(self.__class__.__name__)
        self.data_engine = data_engine

    async def run(self):
        self.logger.info(f"Connecting to {self.name} data stream: {self.ws_url}")
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.logger.info(f"Successfully connected to BTC/USDC on {self.name}.")
                    while True:
                        data = await ws.recv()
                        # --- CORRECTION APPLIQUÉE ICI ---
                        # On passe un seul dictionnaire, comme attendu par DataEngine
                        update_data = {
                            "platform": self.name,
                            "symbol": "BTC/USDC",
                            "data": json.loads(data)
                        }
                        self.data_engine.process_update(update_data)
            except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
                self.logger.error(f"Connection lost to {self.name} (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"An unexpected error occurred with {self.name} connector: {e}", exc_info=True)
                await asyncio.sleep(5)
