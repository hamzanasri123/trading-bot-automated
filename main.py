# main.py
import asyncio, logging, signal
from config import PAPER_TRADING_MODE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, API_KEYS
from execution.live_order_manager import LiveOrderManager
from engine.data_engine import DataEngine
from engine.strategy_engine import StrategyEngine
from connectors.binance_connector import BinanceConnector
from connectors.okx_connector import OkxConnector
from utils.notifier import Notifier
from analysis.trade_logger import TradeLogger

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s')

async def main_bot():
    shutdown_event = asyncio.Event()
    notifier = Notifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)
    trade_logger = TradeLogger()
    
    if PAPER_TRADING_MODE:
        logging.info("Trading Mode: PAPER TRADING (Testnet)")
        order_manager = LiveOrderManager(notifier, trade_logger)
    else:
        logging.info("Trading Mode: LIVE TRADING")
        order_manager = LiveOrderManager(notifier, trade_logger)
    
    await order_manager.initialize()

    logging.info("--- Initial Balance Check ---")
    for platform in order_manager.exchanges.keys():
        for currency in ['USDT', 'BTC']:
            balance = await order_manager.get_balance(platform, currency)
            if balance is not None: logging.info(f"[{platform}] Available balance: {balance:.4f} {currency}")
    logging.info("-----------------------------")

    data_engine = DataEngine()
    strategy_engine = StrategyEngine(data_engine.order_books, order_manager, notifier)

    # --- CORRECTION DÉFINITIVE APPLIQUÉE ICI ---
    # On passe le data_engine à l'initialisation, comme les connecteurs l'attendent.
    binance_connector = BinanceConnector(data_engine)
    okx_connector = OkxConnector(data_engine)

    logging.info("Starting all arbitrage bot tasks...")
    tasks = [
        # On appelle simplement run() car le data_engine est déjà connu.
        asyncio.create_task(binance_connector.run()),
        asyncio.create_task(okx_connector.run()),
        asyncio.create_task(strategy_engine.run()),
        asyncio.create_task(notifier.run())
    ]

    loop = asyncio.get_running_loop()
    def handle_shutdown_signal():
        logging.warning("\nShutdown signal received. Initiating graceful shutdown...")
        shutdown_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, handle_shutdown_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown_signal)
    except NotImplementedError: pass

    try:
        await shutdown_event.wait()
    finally:
        logging.info("Initiating shutdown procedure...")
        if hasattr(strategy_engine, 'process_pool'): strategy_engine.process_pool.shutdown(wait=True); logging.info("Process pool shut down.")
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await order_manager.close_all()
        trade_logger.close()
        logging.info("All tasks have been cancelled and connections closed.")

if __name__ == "__main__":
    try: asyncio.run(main_bot())
    except KeyboardInterrupt: logging.info("Bot stopped by user.")
    finally: logging.info("Bot has been shut down.")
