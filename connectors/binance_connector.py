# connectors/binance_connector.py
import asyncio, json, logging, websockets

class BinanceConnector:
    def __init__(self, data_engine):
        self._base_url = "wss://stream.binance.com:9443/ws"
        self.data_engine = data_engine
        self.logger = logging.getLogger(self.__class__.__name__)

    async def connect(self, symbol: str):
        stream_name = f"{symbol.lower().replace('/', '')}@depth@100ms"
        url = f"{self._base_url}/{stream_name}"
        self.logger.info(f"Connecting to Binance data stream: {url}")
        while True:
            try:
                async with websockets.connect(url) as ws:
                    self.logger.info(f"Successfully connected to {symbol} on Binance.")
                    while True:
                        message = await ws.recv()
                        data = json.loads(message)
                        packaged_data = {"platform": "Binance", "symbol": symbol, "data": data}
                        self.data_engine.process_update(packaged_data)
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError) as e:
                self.logger.error(f"Connection lost to Binance (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"Unexpected error on Binance connector: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
