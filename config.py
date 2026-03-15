# config.py
"""
Configuration BACCARAT AI 🤖
"""

import os

def parse_channel_id(env_var: str, default: str) -> int:
    """Parse l'ID de canal Telegram."""
    value = os.getenv(env_var) or default
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        return int(default)

# ============================================================================
# AUTHENTIFICATION TELEGRAM (VOS IDENTIFIANTS)
# ============================================================================

API_ID = 29177661
API_HASH = 'a8639172fa8d35dbfd8ea46286d349ab'
BOT_TOKEN = '8678647348:AAEJ10XquGFuSqViWiQFfXvK-iJHYPfbM2o'
ADMIN_ID = 1190237801

# Session string (optionnel si BOT_TOKEN utilisé)
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION', '')

# ============================================================================
# CANAUX TELEGRAM (À CONFIGURER SELON VOS BESOINS)
# ============================================================================

# Canal source où arrivent les résultats de jeux
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')

# Canal où envoyer les prédictions
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003336559159')

# ============================================================================
# SERVEUR WEB (RENDER.COM)
# ============================================================================

PORT = int(os.getenv('PORT') or '10000')

# ============================================================================
# PARAMÈTRES SYSTÈME PRÉDICTION
# ============================================================================

# Nombre d'échecs consécutifs avant prédiction (seuil B)
CONSECUTIVE_FAILURES_NEEDED = int(os.getenv('FAILURES_NEEDED', '2'))

# Nombre de numéros vérifiés par tour
NUMBERS_PER_TOUR = 3

# ============================================================================
# CYCLES DES COULEURS (SYSTÈME AVANCÉ)
# ============================================================================

SUIT_CYCLES = {
    '♠': {'start': 1, 'interval': 5},   # Pique: 1, 6, 11, 16...
    '♥': {'start': 1, 'interval': 6},   # Cœur: 1, 7, 13, 19...
    '♦': {'start': 1, 'interval': 6},   # Carreau: 1, 7, 13, 19...
    '♣': {'start': 1, 'interval': 7},   # Trèfle: 1, 8, 15, 22...
}

ALL_SUITS = ['♠', '♥', '♦', '♣']

SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}
