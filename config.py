# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# --- EXCHANGE API KEYS ---
# Keys are read from environment variables (.env locally, real env vars in
# deployment) so secrets are never committed to the repository.
# For Paper Trading, use the keys from the exchange's Testnet website.
API_KEYS = {
    'Binance': {
        'apiKey': os.environ.get('BINANCE_API_KEY', ''),
        'secret': os.environ.get('BINANCE_API_SECRET', ''),
    },
    'OKX': {
        'apiKey': os.environ.get('OKX_API_KEY', ''),
        'secret': os.environ.get('OKX_API_SECRET', ''),
        'password': os.environ.get('OKX_API_PASSWORD', ''),
        # Set to 'eea.okx.com' for OKX EEA-regulated accounts (their API keys
        # only work against that domain, not www.okx.com). Leave unset otherwise.
        'hostname': os.environ.get('OKX_HOSTNAME', ''),
    },
}

# --- TRADING MODE ---
# Set to True to run in testnet/paper trading mode.
# Set to False to run in live mode with real funds.
PAPER_TRADING_MODE = True

# --- TELEGRAM NOTIFICATIONS ---
# Get these from @BotFather and @userinfobot on Telegram. Leave unset to disable.
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# --- SAFETY & RISK MANAGEMENT ---
# Maximum size in USD for a single arbitrage trade. This is your most important risk control.
MAX_TRADE_SIZE_USD = 15.0

# Seuil de profit minimum (en %) au-delà des 3 frais taker déjà déduits dans le
# calcul, avant de déclencher un cycle triangulaire. Sert de marge de sécurité
# contre le slippage et l'imprécision du calcul en haut du carnet. Plus bas =
# plus de trades tentés, mais moins de marge d'erreur par trade.
MIN_PROFIT_PCT_TRIANGULAR = 0.10

# Même principe pour l'arbitrage inter-exchange (Binance <-> OKX) : seuil au-
# delà des 2 frais taker déjà déduits. Plus haut que le triangulaire car il y a
# un vrai délai réseau entre les deux jambes (contrairement au triangulaire, qui
# reste sur une seule plateforme) -- marge de sécurité plus large contre le
# risque que le marché bouge entre les deux ordres.
MIN_PROFIT_PCT_CROSS = 0.20
