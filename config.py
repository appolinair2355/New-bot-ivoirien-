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

# Canal de prédiction uniquement
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003336559159')

# Authentification
ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')
API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION', '')

# Serveur
PORT = int(os.getenv('PORT') or '5000')

# Polling API (secondes entre chaque appel)
API_POLL_INTERVAL = int(os.getenv('API_POLL_INTERVAL') or '5')

# Compteur2 - compteur d'absences consécutives
COMPTEUR2_ACTIVE = os.getenv('COMPTEUR2_ACTIVE', 'true').lower() == 'true'
COMPTEUR2_B = int(os.getenv('COMPTEUR2_B') or '4')

# Couleurs (costumes du joueur)
ALL_SUITS = ['♠', '♥', '♦', '♣']

SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Inverse des couleurs (pour les prédictions Compteur2)
SUIT_INVERSE = {
    '♠': '♦',
    '♦': '♠',
    '♥': '♣',
    '♣': '♥',
}
