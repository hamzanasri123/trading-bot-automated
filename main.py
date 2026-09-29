# main.py
import asyncio, logging, signal
from config import PAPER_TRADING_MODE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, API_KEYS
from execution.live_order_manager import LiveOrderManager
from engine.data_engine import DataEngine
from engine.triangular_engine import TriangularEngine
from engine.cross_exchange_engine import CrossExchangeEngine
from connectors.binance_connector import BinanceConnector
from connectors.okx_connector import OkxConnector
from utils.notifier import Notifier
from utils.trade_logger import TradeLogger

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s')

async def main_bot():
    shutdown_event = asyncio.Event()
    notifier = Notifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)
    trade_logger = TradeLogger()

    if PAPER_TRADING_MODE:
        logging.info("Trading Mode: PAPER TRADING (Testnet)")
    else:
        logging.info("Trading Mode: LIVE TRADING")
    order_manager = LiveOrderManager(notifier, trade_logger)

    await order_manager.initialize()

    # Note : ce sont toujours des paires Binance -- toujours surveillées par
    # les mêmes bots professionnels que ETH/XRP/SOL/NEAR. Ça élargit le nombre
    # d'opportunités surveillées, mais ça ne garantit pas une vraie inefficacité
    # de marché (contrairement à un exchange différent ou des paires vraiment
    # peu tradées, qu'on n'a pas les moyens de vérifier depuis cet environnement).
    triangular_legs = ["ETH", "XRP", "SOL", "NEAR", "DOGE", "ADA", "LINK", "DOT", "LTC", "AVAX"]

    logging.info("--- Initial Balance Check ---")
    for platform in order_manager.exchanges.keys():
        for currency in ['USDC', 'BTC'] + triangular_legs:
            balance = await order_manager.get_balance(platform, currency)
            if balance is not None: logging.info(f"[{platform}] Available balance: {balance:.6f} {currency}")
    logging.info("-----------------------------")

    data_engine = DataEngine()
    triangular_engine = TriangularEngine(data_engine, order_manager, notifier, trade_logger, platform='Binance', legs=triangular_legs)

    triangular_symbols = ["BTC/USDC"] + [f"{leg}/BTC" for leg in triangular_legs] + [f"{leg}/USDC" for leg in triangular_legs]
    binance_connector = BinanceConnector(data_engine, symbols=triangular_symbols)

    tasks = [asyncio.create_task(binance_connector.run()), asyncio.create_task(triangular_engine.run())]

    # Arbitrage inter-exchange (Binance <-> OKX) en parallèle du triangulaire,
    # uniquement si OKX est bien configuré et connecté.
    if 'OKX' in order_manager.exchanges:
        okx_connector = OkxConnector(data_engine)
        cross_engine = CrossExchangeEngine(data_engine.order_books, order_manager, notifier, trade_logger, symbol="BTC/USDC", platform_a="Binance", platform_b="OKX")
        cross_engine.register_listeners(data_engine)
        tasks += [asyncio.create_task(okx_connector.run()), asyncio.create_task(cross_engine.run())]
        logging.info("Starting triangular AND cross-exchange (Binance/OKX) arbitrage tasks...")
    else:
        logging.warning("OKX not configured/connected -- running triangular arbitrage only.")

    # --- CORRECTION : La tâche du Notifier est supprimée ---
    # asyncio.create_task(notifier.run())

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
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await order_manager.close_all()
        trade_logger.close()
        logging.info("All tasks have been cancelled and connections closed.")

if __name__ == "__main__":
    try: asyncio.run(main_bot())
    except KeyboardInterrupt: logging.info("Bot stopped by user.")
    finally: logging.info("Bot has been shut down.")
