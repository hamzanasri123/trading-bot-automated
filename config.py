# config.py
import os
from dotenv import load_dotenv

load_dotenv()


def _env_str(name: str, default: str = '') -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return float(raw)
    except ValueError:
        return raw  # deliberately invalid: validate_config() will catch and report it


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return int(raw)
    except ValueError:
        return raw  # deliberately invalid: validate_config() will catch and report it


# --- EXCHANGE API KEYS ---
# Real values live in a local .env file (never committed — see .gitignore).
# Copy .env.example to .env and fill it in.
API_KEYS = {
    'Binance': {
        'apiKey': _env_str('BINANCE_API_KEY'),
        'secret': _env_str('BINANCE_API_SECRET'),
    },
    'OKX': {
        'apiKey': _env_str('OKX_API_KEY'),
        'secret': _env_str('OKX_API_SECRET'),
        'password': _env_str('OKX_API_PASSPHRASE'),
    },
}

# --- TRADING MODE ---
# true = paper/testnet trading (safe). false = LIVE trading with real funds.
# Defaults to True (paper trading) when unset, so a missing .env fails safe.
PAPER_TRADING_MODE = _env_bool('PAPER_TRADING_MODE', default=True)

# --- TELEGRAM NOTIFICATIONS ---
# Get these from @BotFather and @userinfobot on Telegram. Leave unset to disable.
TELEGRAM_TOKEN = _env_str('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = _env_str('TELEGRAM_CHAT_ID')

# --- SAFETY & RISK MANAGEMENT ---
# Maximum size in USD for a single arbitrage trade. This is your most important risk control.
MAX_TRADE_SIZE_USD = _env_float('MAX_TRADE_SIZE_USD', 15.0)

# Kill switch: if the estimated cumulative PnL for the day drops to or below
# -MAX_DAILY_LOSS_USD, trading is halted until the bot is manually restarted.
MAX_DAILY_LOSS_USD = _env_float('MAX_DAILY_LOSS_USD', 50.0)

# Kill switch: if this many "leg risk" events (one leg of an arbitrage trade
# failed to place while the other went through) happen back to back, trading
# is halted. This usually signals a bug, an exchange outage, or bad market
# conditions rather than normal slippage.
MAX_CONSECUTIVE_LEG_RISK_EVENTS = _env_int('MAX_CONSECUTIVE_LEG_RISK_EVENTS', 2)

# The bot does NOT rebalance inventory between exchanges automatically —
# that's a treasury decision, not something it should do on its own. These
# only make it proactively warn (via Telegram) when a balance drops low
# enough that trades will start silently failing the pre-trade balance
# check, instead of the operator finding out only when trading quietly stops.
MIN_BASE_CURRENCY_BALANCE = _env_float('MIN_BASE_CURRENCY_BALANCE', 0.0005)  # e.g. BTC on a BTC/USDC pair — tune per symbol
LOW_BALANCE_WARNING_COOLDOWN_S = _env_int('LOW_BALANCE_WARNING_COOLDOWN_S', 6 * 3600)  # don't re-alert more than once per 6h


def validate_config():
    """Sanity-check the risk/trading configuration. Returns a list of human
    readable error strings; an empty list means the config is safe to start
    with. Called at startup so a bad config (including a missing/broken
    .env) fails fast and loud instead of the bot limping along half-broken."""
    errors = []

    if not isinstance(MAX_TRADE_SIZE_USD, (int, float)) or MAX_TRADE_SIZE_USD <= 0:
        errors.append("MAX_TRADE_SIZE_USD must be a positive number (check .env).")
    if not isinstance(MAX_DAILY_LOSS_USD, (int, float)) or MAX_DAILY_LOSS_USD <= 0:
        errors.append("MAX_DAILY_LOSS_USD must be a positive number (check .env).")
    if not isinstance(MAX_CONSECUTIVE_LEG_RISK_EVENTS, int) or MAX_CONSECUTIVE_LEG_RISK_EVENTS < 1:
        errors.append("MAX_CONSECUTIVE_LEG_RISK_EVENTS must be an integer >= 1 (check .env).")
    if not isinstance(PAPER_TRADING_MODE, bool):
        errors.append("PAPER_TRADING_MODE must resolve to true/false (check .env).")

    configured_exchanges = [
        name for name, keys in API_KEYS.items()
        if keys.get('apiKey') and 'YOUR' not in keys['apiKey']
    ]
    if not configured_exchanges:
        errors.append("No exchange has API keys configured — copy .env.example to .env and fill it in.")
    elif len(configured_exchanges) < 2:
        errors.append(f"Only {len(configured_exchanges)} exchange(s) configured ({configured_exchanges}) — arbitrage requires at least 2.")

    if 'OKX' in API_KEYS and API_KEYS['OKX'].get('apiKey') and not API_KEYS['OKX'].get('password'):
        errors.append("OKX_API_PASSPHRASE is missing in .env (required alongside OKX_API_KEY/OKX_API_SECRET).")

    return errors
