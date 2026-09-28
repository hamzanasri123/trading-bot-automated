# connectors/okx_connector.py
import asyncio, json, logging, websockets
from config import  PAPER_TRADING_MODE

class OkxConnector:
    def __init__(self, data_engine):
        self.name = "OKX"
        self.symbol_unified = "BTC/USDC"
        self.symbol_ws = self.symbol_unified.replace('/', '-')
        
        # --- CORRECTION : URL DYNAMIQUE ---
        if PAPER_TRADING_MODE:
            # URL du Demo Trading OKX (confirmée par l'utilisateur pour son compte)
            self.ws_url = "wss://wseeapap.okx.com:8443/ws/v5/public"
            self.mode_log = "(Paper Trading)"
        else:
            # URL de Production (Réelle) de OKX
            self.ws_url = "wss://ws.okx.com:8443/ws/v5/public"
            self.mode_log = "(Live)"

        self.logger = logging.getLogger(self.__class__.__name__)
        self.data_engine = data_engine

    async def run(self):
        self.logger.info(f"Connecting to {self.name} data stream {self.mode_log}: {self.ws_url}")
        subscribe_msg = { "op": "subscribe", "args": [{"channel": "books", "instId": self.symbol_ws}] }
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    confirmation = await ws.recv()
                    if '"event":"subscribe"' in confirmation:
                        self.logger.info(f"Subscribed to order book for {self.symbol_ws} on {self.name}.")
                    while True:
                        data = await ws.recv()
                        if 'data' in data:
                            payload = json.loads(data)['data'][0]
                            update_data = {"platform": self.name, "symbol": self.symbol_unified, "data": payload}
                            self.data_engine.process_update(update_data)
            except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
                self.logger.error(f"Connection lost to {self.name} (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"An unexpected error occurred with {self.name} connector: {e}", exc_info=True)
                await asyncio.sleep(5)
