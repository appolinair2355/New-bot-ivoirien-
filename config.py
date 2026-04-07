import os

def parse_channel_id(value: str) -> int:
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        raise ValueError(f"ID de canal invalide : {value}")

ADMIN_ID = int(os.getenv("ADMIN_ID", "1190237801"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")

PORT = int(os.getenv("PORT", "10000"))
API_POLL_INTERVAL = int(os.getenv("API_POLL_INTERVAL", "5"))

# Canal silencieux C1
C1_SILENT_CHANNEL_ID = parse_channel_id(os.getenv("C1_SILENT_CHANNEL_ID", "-1003651435888"))

# Canal principal (escalade après 1 perte silencieuse)
PREDICTION_CHANNEL_ID = parse_channel_id(os.getenv("PREDICTION_CHANNEL_ID", "-1003336559159"))

ALL_SUITS = ["♠", "♥", "♦", "♣"]

SUIT_DISPLAY = {
    "♠": "♠️",
    "♥": "❤️",
    "♦": "♦️",
    "♣": "♣️"
}
