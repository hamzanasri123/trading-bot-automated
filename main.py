# main.py
import asyncio
import logging
from config import PAPER_TRADING_MODE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from utils.notifier import Notifier
from execution.live_order_manager import LiveOrderManager
from connectors.binance_connector import BinanceConnector
from connectors.okx_connector import OkxConnector
from engine.data_engine import DataEngine
from engine.strategy_engine import StrategyEngine

async def main_bot( ):
    logger = logging.getLogger()
    notifier = Notifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)
    notifier.set_logger(logging.getLogger("Notifier"))
    await notifier.send_message("🤖 *Arbitrage Bot Starting Up* 🤖")

    symbol_to_trade = "BTC/USDT"
    data_engine = DataEngine()
    
    if PAPER_TRADING_MODE: logger.info("Trading Mode: PAPER TRADING (Testnet)")
    else: logger.info("Trading Mode: LIVE TRADING (Production)")
    
    order_manager = LiveOrderManager(notifier)
    strategy_engine = StrategyEngine(data_engine.order_books, order_manager, notifier)
    
    binance_connector = BinanceConnector(data_engine)
    okx_connector = OkxConnector(data_engine)

    await order_manager.initialize()
    logger.info("--- Initial Balance Check ---")
    try:
        if 'Binance' in order_manager.exchanges:
            usdt_balance = await order_manager.get_balance('Binance', 'USDT')
            btc_balance = await order_manager.get_balance('Binance', 'BTC')
            logger.info(f"[Binance] Available Balance: {usdt_balance or 0.0:.4f} USDT, {btc_balance or 0.0:.8f} BTC")
        if 'OKX' in order_manager.exchanges:
            usdt_balance_okx = await order_manager.get_balance('OKX', 'USDT')
            btc_balance_okx = await order_manager.get_balance('OKX', 'BTC')
            logger.info(f"[OKX] Available Balance: {usdt_balance_okx or 0.0:.4f} USDT, {btc_balance_okx or 0.0:.8f} BTC")
    except Exception as e: logger.error(f"Could not retrieve initial balances: {e}")
    logger.info("-----------------------------")

    logger.info("Starting all arbitrage bot tasks...")
    tasks = [
        asyncio.create_task(binance_connector.connect(symbol_to_trade)),
        asyncio.create_task(okx_connector.connect(symbol_to_trade)),
        asyncio.create_task(data_engine.run()),
        asyncio.create_task(strategy_engine.run()),
    ]
    
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try: task.result()
            except Exception as e:
                logger.critical(f"A critical task failed: {e}. Shutting down.", exc_info=True)
                await notifier.send_message(f"🔥 *CRITICAL FAILURE* 🔥\nA core task failed: `{e}`. The bot is shutting down.")
    finally:
        logger.info("Shutdown procedure initiated...")
        await notifier.send_message("🛑 *Bot Shutting Down* 🛑")
        for task in tasks: task.cancel()
        await order_manager.close_all()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All tasks cancelled. Bot has stopped.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)-25s - %(levelname)-8s - %(message)s')
    try: asyncio.run(main_bot())
    except KeyboardInterrupt: logging.info("\nShutdown requested by user (Ctrl+C).")
