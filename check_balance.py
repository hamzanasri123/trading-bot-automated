# check_balance.py
# Script autonome pour vérifier le solde du compte (testnet ou live) sans
# lancer la boucle de trading. Utile pour un diagnostic rapide.
import asyncio
import ccxt.async_support as ccxt
from config import API_KEYS, PAPER_TRADING_MODE

async def main():
    keys = API_KEYS['Binance']
    if not keys['apiKey']:
        print("Aucune clé API Binance configurée dans .env")
        return

    exchange = ccxt.binance({'apiKey': keys['apiKey'], 'secret': keys['secret'], 'enableRateLimit': True})
    if PAPER_TRADING_MODE and exchange.has.get('sandbox', False):
        exchange.set_sandbox_mode(True)
        print("Mode: PAPER TRADING (testnet)")
    else:
        print("Mode: LIVE")

    try:
        balance = await exchange.fetch_free_balance()
        for currency in ['USDC', 'BTC', 'ETH']:
            print(f"{currency}: {balance.get(currency, 0.0)}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
