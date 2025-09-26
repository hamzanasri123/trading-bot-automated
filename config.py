# config.py

# --- EXCHANGE API KEYS ---
# IMPORTANT: Replace with your REAL production keys for live trading.
# For Paper Trading, use the keys from the exchange's Testnet website.
API_KEYS = {
    'Binance': {
        'apiKey': 'YOUR_BINANCE_API_KEY', # Use Testnet keys for paper trading
        'secret': 'YOUR_BINANCE_SECRET_KEY',
    },
    'OKX': {
        'apiKey': 'YOUR_OKX_API_KEY', # Use Testnet keys for paper trading
        'secret': 'YOUR_OKX_SECRET_KEY',
        'password': 'YOUR_OKX_API_PASSPHRASE',
    },
}

# --- TRADING MODE ---
# Set to True to run in testnet/paper trading mode.
# Set to False to run in live mode with real funds.
PAPER_TRADING_MODE = True # Set to False for cloud deployment

# --- TELEGRAM NOTIFICATIONS ---
# Get these from @BotFather and @userinfobot on Telegram. Set to '' to disable.
TELEGRAM_TOKEN = '8333658619:AAHtpa0YxjWdVwMSCK8kbnNePvCXzTg9djI'
TELEGRAM_CHAT_ID = '8333658619'

# --- SAFETY & RISK MANAGEMENT ---
# Maximum size in USD for a single arbitrage trade. This is your most important risk control.
MAX_TRADE_SIZE_USD = 15.0