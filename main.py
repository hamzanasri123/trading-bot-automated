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

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s')

async def main_bot():
    shutdown_event = asyncio.Event()
    
    # Initialisation des composants
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
            if balance is not None:
                logging.info(f"[{platform}] Available balance: {balance:.4f} {currency}")
    logging.info("-----------------------------")

    data_engine = DataEngine()
    strategy_engine = StrategyEngine(data_engine.order_books, order_manager, notifier)

    # --- MODIFICATION : Création des connecteurs corrigée ---
    # On passe la fonction de callback au moment de l'appel à `run`, pas à l'initialisation.
    binance_connector = BinanceConnector()
    okx_connector = OkxConnector()

    # Lancement des tâches
    logging.info("Starting all arbitrage bot tasks...")
    tasks = [
        # --- MODIFICATION : On passe la fonction ici ---
        asyncio.create_task(binance_connector.run(data_engine.process_update)),
        asyncio.create_task(okx_connector.run(data_engine.process_update)),
        asyncio.create_task(data_engine.run()),
        asyncio.create_task(strategy_engine.run()),
        asyncio.create_task(notifier.run())
    ]

    # Gestion de l'arrêt propre
    loop = asyncio.get_running_loop()
    def handle_shutdown_signal():
        logging.warning("\nShutdown signal received. Initiating graceful shutdown...")
        shutdown_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, handle_shutdown_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown_signal)
    except NotImplementedError:
        pass

    try:
        await shutdown_event.wait()
    finally:
        logging.info("Initiating shutdown procedure...")
        strategy_engine.process_pool.shutdown(wait=True)
        logging.info("Process pool shut down.")
        
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await order_manager.close_all()
        trade_logger.close() # Fermer le logger de trade
        logging.info("All tasks have been cancelled and connections closed.")

if __name__ == "__main__":
    try:
        asyncio.run(main_bot())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    finally:
        logging.info("Bot has been shut down.")
