import os

def parse_channel_id(value: str) -> int:
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        raise ValueError(f"ID de canal invalide : {value}")

# === TELEGRAM ===
ADMIN_IDS = [
    7719356239,
    1190237801,
]
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")

# === SERVEUR ===
PORT = int(os.getenv("PORT", "10000"))
API_POLL_INTERVAL = int(os.getenv("API_POLL_INTERVAL", "5"))

# === CANAL ===
PREDICTION_CHANNEL_ID = parse_channel_id(os.getenv("PREDICTION_CHANNEL_ID", "-1003336559159"))

# === COMPTEUR1 ===
C1_B = int(os.getenv("C1_B", "3"))
C1_SILENT_MAX_RATTRAPAGE = int(os.getenv("C1_SILENT_MAX_RATTRAPAGE", "1"))
C1_CANAL_MAX_RATTRAPAGE = int(os.getenv("C1_CANAL_MAX_RATTRAPAGE", "2"))
MAX_SILENT_HISTORY = int(os.getenv("MAX_SILENT_HISTORY", "150"))

# === CONSTANTES ===
ALL_SUITS = ["♠", "♥", "♦", "♣"]

SUIT_DISPLAY = {
    "♠": "♠️",
    "♥": "❤️",
    "♦": "♦️",
    "♣": "♣️"
}
