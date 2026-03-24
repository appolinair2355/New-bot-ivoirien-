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
ADMIN_ID = int(os.getenv('ADMIN_ID') or '1190237801')
API_ID = int(os.getenv('API_ID') or '29177661')
API_HASH = os.getenv('API_HASH') or 'a8639172fa8d35dbfd8ea46286d349ab'
BOT_TOKEN = os.getenv('BOT_TOKEN') or '8678647348:AAEJ10XquGFuSqViWiQFfXvK-iJHYPfbM2o'
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION') or '1BJWap1wBu0g_phkaACyeK4R57GPHlcjVwwS3mQzXXyagAq1qBHiWShyY2pDHYScRds6c_7Ug6xZnH6DjwQ19dttKvZ7FXhQTfIagaDV2p1sH_UWOlhiBIeVMuuH9PuaqhVRj073jrQZwIvalHgysDFptv2XXEYfXb7ipc-I3TgnIY_IZe0ZJA8cCm6kD7SjZviFQpCVzH6vt1oKj_Qrh2nF2JirC2qj2qAenYCzZmB7qwuP6tWaT6pgbEGdpnxvfwYiLf9q_ml9l18eUMt1BrH3CSYyeMa4JBqvDcz3rSngBAsAUjif2xfkuw-RB4WSkkgo0E8xp8h7gqwcwUbUhXDKTGng7VE0='

# Serveur (Render.com utilise le port 10000 par défaut)
PORT = int(os.getenv('PORT') or '10000')

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
