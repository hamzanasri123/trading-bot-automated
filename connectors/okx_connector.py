# connectors/okx_connector.py
import asyncio, json, logging, websockets
from config import PAPER_TRADING_MODE

class OkxConnector:
    def __init__(self, data_engine):
        self.data_engine = data_engine
        self.logger = logging.getLogger(self.__class__.__name__)
        if PAPER_TRADING_MODE:
            self._base_url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999" # Paper Trading URL
        else:
            self._base_url = "wss://ws.okx.com:8443/ws/v5/public" # Production URL

    async def connect(self, symbol: str):
        okx_symbol = symbol.replace('/', '-')
        subscription_message = {"op": "subscribe", "args": [{"channel": "books", "instId": okx_symbol}]}
        self.logger.info(f"Connecting to OKX data stream: {self._base_url}")
        while True:
            try:
                async with websockets.connect(self._base_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps(subscription_message))
                    self.logger.info(f"Subscribed to order book for {okx_symbol} on OKX.")
                    while True:
                        message = await ws.recv()
                        data = json.loads(message)
                        if data.get('event') == 'subscribe': continue
                        if data.get('action') in ['snapshot', 'update']:
                            bids = data['data'][0]['bids']
                            asks = data['data'][0]['asks']
                            formatted_data = {'b': bids, 'a': asks}
                            packaged_data = {"platform": "OKX", "symbol": symbol, "data": formatted_data}
                            self.data_engine.process_update(packaged_data)
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError) as e:
                self.logger.error(f"Connection lost to OKX (type: {type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"Unexpected error on OKX connector: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
