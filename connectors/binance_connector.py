# connectors/binance_connector.py
import asyncio, json, logging, websockets

class BinanceConnector:
    def __init__(self, data_engine, symbols=None):
        self.name = "Binance"
        self.symbols_unified = symbols or ["BTC/USDC"]
        self.stream_names = [s.replace('/', '').lower() + '@depth@100ms' for s in self.symbols_unified]
        self.ws_symbol_map = {name.split('@')[0]: sym for name, sym in zip(self.stream_names, self.symbols_unified)}

        base_url = "wss://stream.binance.com:9443"
        if len(self.stream_names) == 1:
            self.ws_url = f"{base_url}/ws/{self.stream_names[0]}"
            self.combined = False
        else:
            self.ws_url = f"{base_url}/stream?streams=" + "/".join(self.stream_names)
            self.combined = True

        self.logger = logging.getLogger(self.__class__.__name__)
        self.data_engine = data_engine

    async def run(self):
        self.logger.info(f"Connecting to {self.name} data stream: {self.ws_url}")
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.logger.info(f"Successfully connected to {', '.join(self.symbols_unified)} on {self.name}.")
                    while True:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        if self.combined:
                            stream_name = msg.get('stream', '')
                            payload = msg.get('data', {})
                            ws_symbol = stream_name.split('@')[0]
                        else:
                            payload = msg
                            ws_symbol = self.stream_names[0].split('@')[0]
                        symbol_unified = self.ws_symbol_map.get(ws_symbol)
                        if not symbol_unified:
                            continue
                        update_data = {"platform": self.name, "symbol": symbol_unified, "data": payload}
                        self.data_engine.process_update(update_data)
            except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
                self.logger.error(f"Connection lost to {self.name} (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"An unexpected error occurred with {self.name} connector: {e}", exc_info=True)
                await asyncio.sleep(5)
