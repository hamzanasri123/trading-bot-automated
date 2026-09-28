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
PAPER_TRADING_MODE = False # Set to False for cloud deployment

# --- TELEGRAM NOTIFICATIONS ---
# Get these from @BotFather and @userinfobot on Telegram. Set to '' to disable.
TELEGRAM_TOKEN = '8333658619:AAHtpa0YxjWdVwMSCK8kbnNePvCXzTg9djI'
TELEGRAM_CHAT_ID = '5763218219'

# --- SAFETY & RISK MANAGEMENT ---
# Maximum size in USD for a single arbitrage trade. This is your most important risk control.
MAX_TRADE_SIZE_USD = 15.0

# Kill switch: if the estimated cumulative PnL for the day drops to or below
# -MAX_DAILY_LOSS_USD, trading is halted until the bot is manually restarted.
MAX_DAILY_LOSS_USD = 50.0

# Kill switch: if this many "leg risk" events (one leg of an arbitrage trade
# failed to place while the other went through) happen back to back, trading
# is halted. This usually signals a bug, an exchange outage, or bad market
# conditions rather than normal slippage.
MAX_CONSECUTIVE_LEG_RISK_EVENTS = 2


def validate_config():
    """Sanity-check the risk/trading configuration. Returns a list of human
    readable error strings; an empty list means the config is safe to start
    with. Called at startup so a bad config fails fast and loud instead of
    the bot limping along in a half-broken state."""
    errors = []

    if not isinstance(MAX_TRADE_SIZE_USD, (int, float)) or MAX_TRADE_SIZE_USD <= 0:
        errors.append("MAX_TRADE_SIZE_USD must be a positive number.")
    if not isinstance(MAX_DAILY_LOSS_USD, (int, float)) or MAX_DAILY_LOSS_USD <= 0:
        errors.append("MAX_DAILY_LOSS_USD must be a positive number.")
    if not isinstance(MAX_CONSECUTIVE_LEG_RISK_EVENTS, int) or MAX_CONSECUTIVE_LEG_RISK_EVENTS < 1:
        errors.append("MAX_CONSECUTIVE_LEG_RISK_EVENTS must be an integer >= 1.")
    if not isinstance(PAPER_TRADING_MODE, bool):
        errors.append("PAPER_TRADING_MODE must be True or False.")

    configured_exchanges = [
        name for name, keys in API_KEYS.items()
        if keys.get('apiKey') and 'YOUR' not in keys['apiKey']
    ]
    if not configured_exchanges:
        errors.append("No exchange has valid API keys configured in API_KEYS — there would be nothing to trade on.")
    elif len(configured_exchanges) < 2:
        errors.append(f"Only {len(configured_exchanges)} exchange(s) configured ({configured_exchanges}) — arbitrage requires at least 2.")

    if 'OKX' in API_KEYS and not API_KEYS['OKX'].get('password'):
        errors.append("API_KEYS['OKX'] is missing its 'password' (API passphrase).")

    return errors