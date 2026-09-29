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
    # Testnet futures (testnet.binancefuture.com) est un environnement
    # SÉPARÉ du testnet spot (testnet.binance.vision) -- les clés spot
    # ci-dessus ne fonctionnent pas ici. Nécessite une inscription à part.
    'BinanceFutures': {
        'apiKey': os.environ.get('BINANCE_FUTURES_API_KEY', ''),
        'secret': os.environ.get('BINANCE_FUTURES_API_SECRET', ''),
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

# --- MARKET MAKING (stratégie différente : risque d'inventaire, pas d'exécution) ---
# Demi-spread (en %) appliqué de chaque côté du prix médian pour les ordres
# post-only. Total du spread coté = 2x cette valeur. Doit couvrir 2x les frais
# maker (aller-retour) plus une marge de profit.
MM_HALF_SPREAD_PCT = 0.20
# Exposition maximale en USD que la stratégie peut accumuler (achats non encore
# revendus). Au-delà, elle arrête de coter à l'achat jusqu'à ce que l'inventaire
# redescende.
MM_MAX_INVENTORY_USD = MAX_TRADE_SIZE_USD * 3
# Coupe-circuit dédié : si la perte latente (mark-to-market) sur l'inventaire
# détenu dépasse ce % de l'exposition maximale, on liquide immédiatement au
# marché plutôt que d'attendre un retour à l'équilibre qui n'arrive pas.
MM_STOP_LOSS_PCT = 20.0

# --- ARBITRAGE STATISTIQUE (retour à la moyenne, spot uniquement) ---
STAT_ARB_SYMBOL = "ETH/BTC"
STAT_ARB_PLATFORM = "Binance"
# Nombre minimum d'échantillons de prix avant de calculer une moyenne/écart-
# type fiable. En dessous, la stratégie reste inactive (pas assez d'historique).
STAT_ARB_MIN_SAMPLES = 60
# Écart-type au-delà duquel on considère le prix anormalement dévié (déclenche
# un achat, en pariant sur un retour vers la moyenne).
STAT_ARB_ENTRY_ZSCORE = 2.0
# Écart-type en dessous duquel on considère que le retour à la moyenne a eu
# lieu (déclenche la revente pour réaliser le profit).
STAT_ARB_EXIT_ZSCORE = 0.3
STAT_ARB_TRADE_SIZE_USD = MAX_TRADE_SIZE_USD
STAT_ARB_STOP_LOSS_PCT = 15.0

# --- SUIVI DE TENDANCE (directionnel, spot uniquement, long-only) ---
TREND_SYMBOL = "BTC/USDC"
TREND_PLATFORM = "Binance"
TREND_FAST_WINDOW = 20
TREND_SLOW_WINDOW = 80
TREND_TRADE_SIZE_USD = MAX_TRADE_SIZE_USD
TREND_STOP_LOSS_PCT = 10.0

# --- FUNDING RATE (futures perpétuels -- MONITORING SEULEMENT pour l'instant) ---
# Pas d'exécution automatique de positions à effet de levier tant que ce n'a
# pas été testé et validé étape par étape comme le reste du bot, avec de
# vraies clés testnet futures. Se contente pour l'instant de surveiller et
# d'alerter quand le taux dépasse le seuil.
FUNDING_RATE_SYMBOL = "BTC/USDT:USDT"
FUNDING_RATE_ALERT_APR_PCT = 15.0
