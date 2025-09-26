# connectors/okx_connector.py
import asyncio, json, logging
import websockets

class OkxConnector:
    def __init__(self, data_engine):
        self.name = "OKX"
        self.symbol = "BTC-USDT"
        self.ws_url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999" # Paper Trading URL
        self.logger = logging.getLogger(self.__class__.__name__)
        self.data_engine = data_engine

    async def run(self):
        self.logger.info(f"Connecting to {self.name} data stream (Paper Trading): {self.ws_url}")
        subscribe_msg = { "op": "subscribe", "args": [{"channel": "books", "instId": self.symbol}] }
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    
                    # Attendre la confirmation d'abonnement
                    confirmation = await ws.recv()
                    if '"event":"subscribe"' in confirmation:
                        self.logger.info(f"Subscribed to order book for {self.symbol} on {self.name}.")
                    
                    while True:
                        data = await ws.recv()
                        if 'data' in data:
                            payload = json.loads(data)['data'][0]
                            self.data_engine.process_update(self.name, "BTC/USDT", payload)
            except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
                self.logger.error(f"Connection lost to {self.name} (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"An unexpected error occurred with {self.name} connector: {e}", exc_info=True)
                await asyncio.sleep(5)
