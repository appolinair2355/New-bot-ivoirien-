# Baccarat Prediction Bot

A Telegram bot that monitors live Baccarat game results from the 1xBet API and sends betting predictions to a Telegram channel based on the "Compteur1" tracking algorithm.

## Tech Stack

- **Language:** Python 3.11/3.12
- **Telegram Framework:** Telethon (async Telegram client)
- **HTTP:** aiohttp (keep-alive web server), requests (API polling)
- **Package Manager:** pip

## Project Structure

- `main.py` — Core bot logic, polling loop, Compteur1 strategy, Telegram commands
- `config.py` — Configuration via environment variables
- `utils.py` — 1xBet API helpers (fetch live results, parse card data)
- `requirements.txt` — Python dependencies
- `Procfile` / `render.yaml` — For Render/Heroku deployment

## Required Secrets (set in Replit Secrets tab)

| Secret | Description |
|--------|-------------|
| `API_ID` | Telegram API ID (from my.telegram.org) |
| `API_HASH` | Telegram API Hash |
| `BOT_TOKEN` | Telegram bot token (from @BotFather) |
| `TELEGRAM_SESSION` | Telethon StringSession string |
| `ADMIN_ID` | Telegram user ID of the admin |
| `PREDICTION_CHANNEL_ID` | Main prediction channel ID (default: -1003336559159) |

## Prediction Logic (Compteur1)

- B = 3 absences consécutives d'un costume → prédiction = ce costume lui-même
- **Silencieux (R max 1)** : prédiction interne visible via `/compteur1` uniquement
- **Canal (R max 2)** : envoyée dans le canal de prédiction après 1 perte silencieuse

## Running

The bot runs as a console workflow: `python main.py`
