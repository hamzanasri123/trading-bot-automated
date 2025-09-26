# config.py

# --- EXCHANGE API KEYS ---
# IMPORTANT: Replace with your REAL production keys for live trading.
# For Paper Trading, use the keys from the exchange's Testnet website.
API_KEYS = {
    'Binance': {
        'apiKey': 'FaTbl8MDJC1QvBw8oMphtqeReo3IUP5bZ2QKZoVYilAGvbu8m9fOqShLE52vWzfr', # Use Testnet keys for paper trading
        'secret': 'QRF3tIkMFP2ez5OY5xiWNTzTirGJT3HkoAeoBSXuyniTqYRtNIcinXAgSs6eABVK',
    },
    'OKX': {
        'apiKey': '72daa7b0-d4f5-4455-8ccc-cf9822cbf726', # Use Testnet keys for paper trading
        'secret': '47902A8B6064A13700DE61305DCA5BF2',
        'password': 'Souadwanna1@',
    },
}

# --- TRADING MODE ---
# Set to True to run in testnet/paper trading mode.
# Set to False to run in live mode with real funds.
PAPER_TRADING_MODE = True # Set to False for cloud deployment

# --- TELEGRAM NOTIFICATIONS ---
# Get these from @BotFather and @userinfobot on Telegram. Set to '' to disable.
TELEGRAM_TOKEN = '8333658619:AAHtpa0YxjWdVwMSCK8kbnNePvCXzTg9djI'
TELEGRAM_CHAT_ID = '5763218219'

# --- SAFETY & RISK MANAGEMENT ---
# Maximum size in USD for a single arbitrage trade. This is your most important risk control.
MAX_TRADE_SIZE_USD = 15.0