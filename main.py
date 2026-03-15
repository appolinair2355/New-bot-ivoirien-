#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baccarat AI Bot - Version Minimaliste
Compteur2 + Perdu Silencieux uniquement
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from telethon import TelegramClient, events
from telethon.sessions import MemorySession
import re
import json

# Import configuration
from config import (
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, ADMIN_ID,
    API_ID, API_HASH, BOT_TOKEN, PORT,
    CONSECUTIVE_FAILURES_NEEDED, NUMBERS_PER_TOUR,
    ALL_SUITS, SUIT_DISPLAY
)

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONSTANTES
# ============================================================================

SUIT_INVERSES = {
    '♠': '♦',
    '♦': '♠',
    '♥': '♣',
    '♣': '♥',
}

PREDICTION_HEADER = "🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
PREDICTION_FOOTER_PENDING = "\n\n🔍 En cours de vérification..."
PREDICTION_FOOTER_R0 = "\n\nRattrapage : ✅0️⃣"
PREDICTION_FOOTER_R1 = "\n\nRattrapage : ✅1️⃣"
PREDICTION_FOOTER_R2 = "\n\nRattrapage : ✅2️⃣"
PREDICTION_FOOTER_PERDU = "\n\nRattrapage : ❌PERDU"

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client: Optional[TelegramClient] = None
current_game_number = 0
last_source_game_number = 0
last_prediction_number_sent = 0

pending_predictions: Dict[int, dict] = {}
prediction_history: List[dict] = []
prediction_queue: List[Tuple[int, str, str]] = []
waiting_finalization: set = set()

suit_block_until: Dict[str, datetime] = {}

# Compteur1
class Compteur1Tracker:
    def __init__(self, suit: str):
        self.suit = suit
        self.counter = 0
        self.start_game = 0
        self.last_game = 0
    
    def get_display_name(self) -> str:
        return SUIT_DISPLAY.get(self.suit, self.suit)

compteur1_trackers: Dict[str, Compteur1Tracker] = {s: Compteur1Tracker(s) for s in ALL_SUITS}
compteur1_history: List[dict] = []

# Compteur2
class Compteur2Tracker:
    def __init__(self, suit: str):
        self.suit = suit
        self.counter = 0
        self.last_increment_game = 0
    
    def reset(self, game_number: int):
        self.counter = 0
        self.last_increment_game = game_number
    
    def get_display_name(self) -> str:
        return SUIT_DISPLAY.get(self.suit, self.suit)

compteur2_trackers: Dict[str, Compteur2Tracker] = {s: Compteur2Tracker(s) for s in ALL_SUITS}
compteur2_active = True
compteur2_seuil_B = CONSECUTIVE_FAILURES_NEEDED

# Perdu Silencieux
perdu_silencieux_active = False
prediction_was_sent = False
last_prediction_result: Optional[str] = None

game_history: List[dict] = []

# ============================================================================
# FONCTIONS UTILITAIRES
# ============================================================================

def get_inverse_suit(suit: str) -> str:
    return SUIT_INVERSES.get(suit, suit)

def can_predict_new() -> bool:
    if not perdu_silencieux_active:
        return True
    if not prediction_was_sent:
        return True
    if last_prediction_result == 'perdu':
        return True
    return False

def reset_prediction_state():
    global prediction_was_sent, last_prediction_result
    prediction_was_sent = False
    last_prediction_result = None

def mark_prediction_sent():
    global prediction_was_sent
    prediction_was_sent = True

def set_prediction_result(result: str):
    global last_prediction_result
    last_prediction_result = 'perdu' if 'perdu' in result else 'gagne'

def is_suit_blocked(suit: str) -> bool:
    if suit in suit_block_until:
        if datetime.now() < suit_block_until[suit]:
            return True
        del suit_block_until[suit]
    return False

def block_suit(suit: str, games: int):
    suit_block_until[suit] = datetime.now() + timedelta(minutes=games * 2)
    logger.info(f"🚫 {suit} bloqué {games} jeux")

def extract_parentheses_groups(text: str) -> List[str]:
    return re.findall(r'\(([^)]+)\)', text)

def get_suits_in_group(group: str) -> List[str]:
    return [s for s in ALL_SUITS if s in group]

def format_prediction_message(game_number: int, suit: str, status: str = 'pending', rattrapage: int = 0) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    msg = f"{PREDICTION_HEADER}Game {game_number}  :{suit_display}"
    
    footers = {
        'pending': PREDICTION_FOOTER_PENDING,
        'en_cours': PREDICTION_FOOTER_PENDING,
        'gagne_r0': PREDICTION_FOOTER_R0,
        'gagne_r1': PREDICTION_FOOTER_R1,
        'gagne_r2': PREDICTION_FOOTER_R2,
        'perdu': PREDICTION_FOOTER_PERDU
    }
    return msg + footers.get(status, PREDICTION_FOOTER_PENDING)

# ============================================================================
# HISTORIQUE
# ============================================================================

def add_to_history(game_number: int, raw_text: str, first_group: str, suits: List[str]):
    game_history.append({
        'game_number': game_number,
        'raw_text': raw_text[:100],
        'first_group': first_group,
        'suits': suits,
        'timestamp': datetime.now()
    })
    if len(game_history) > 500:
        game_history.pop(0)

def save_compteur1_series(suit: str, count: int, start_game: int, end_game: int):
    compteur1_history.append({
        'suit': suit, 'count': count,
        'start_game': start_game, 'end_game': end_game,
        'timestamp': datetime.now()
    })

# ============================================================================
# COMPTeur1
# ============================================================================

def update_compteur1(game_number: int, first_group: str):
    suits_present = set(get_suits_in_group(first_group))
    
    for suit in ALL_SUITS:
        tracker = compteur1_trackers[suit]
        if suit in suits_present:
            if tracker.counter == 0:
                tracker.start_game = game_number
            tracker.counter += 1
            tracker.last_game = game_number
        else:
            if tracker.counter >= 3:
                save_compteur1_series(suit, tracker.counter, tracker.start_game, tracker.last_game)
            tracker.counter = 0
            tracker.start_game = 0
            tracker.last_game = 0

# ============================================================================
# COMPTeur2
# ============================================================================

def update_compteur2(game_number: int, first_group: str):
    suits_present = set(get_suits_in_group(first_group))
    
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        if suit not in suits_present and tracker.last_increment_game != game_number:
            tracker.counter += 1
            tracker.last_increment_game = game_number

def get_compteur2_ready_predictions(current_game: int) -> List[tuple]:
    ready = []
    
    if not can_predict_new():
        logger.info("🔕 Mode silencieux: attente résultat")
        return ready
    
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        if tracker.counter >= compteur2_seuil_B:
            inverse = get_inverse_suit(suit)
            pred_num = current_game + 1
            
            if is_suit_blocked(inverse):
                logger.info(f"🚫 {inverse} bloqué")
                tracker.reset(current_game)
                continue
            
            ready.append((inverse, pred_num, suit))
            logger.info(f"🎯 {suit} atteint B={compteur2_seuil_B} → prédit {inverse} #{pred_num}")
            tracker.reset(current_game)
    
    return ready

# ============================================================================
# FILE D'ATTENTE
# ============================================================================

def add_to_prediction_queue(game_number: int, suit: str, pred_type: str = 'compteur2') -> bool:
    global last_prediction_number_sent
    
    if last_prediction_number_sent > 0:
        gap = game_number - last_prediction_number_sent
        if gap < NUMBERS_PER_TOUR:
            logger.info(f"⏭️ Écart {gap} < {NUMBERS_PER_TOUR}")
    
    for gn, s, t in prediction_queue:
        if gn == game_number and s == suit:
            return False
    
    prediction_queue.append((game_number, suit, pred_type))
    logger.info(f"📥 File: #{game_number} {suit}")
    return True

async def process_prediction_queue(current_game: int):
    global prediction_queue, last_prediction_number_sent
    
    if not prediction_queue:
        return
    
    to_remove = []
    for i, (game_number, suit, pred_type) in enumerate(prediction_queue):
        if last_prediction_number_sent > 0:
            gap = game_number - last_prediction_number_sent
            if gap < NUMBERS_PER_TOUR:
                continue
        
        success = await send_prediction(game_number, suit, PREDICTION_CHANNEL_ID, pred_type)
        if success:
            last_prediction_number_sent = game_number
            to_remove.append(i)
    
    for i in reversed(to_remove):
        prediction_queue.pop(i)

# ============================================================================
# ENVOI & MISE À JOUR PRÉDICTIONS
# ============================================================================

async def send_prediction(game_number: int, suit: str, channel_id: int, pred_type: str = 'compteur2') -> bool:
    try:
        text = format_prediction_message(game_number, suit, 'pending')
        sent = await client.send_message(channel_id, text, parse_mode='html')
        
        pred_data = {
            'predicted_game': game_number, 'suit': suit, 'type': pred_type,
            'status': 'en_cours', 'message_id': sent.id,
            'chat_id': channel_id, 'predicted_at': datetime.now(),
            'verified_by': [], 'final_status': None
        }
        
        pending_predictions[game_number] = pred_data
        prediction_history.append(pred_data)
        mark_prediction_sent()
        
        logger.info(f"📤 Envoyé: #{game_number} {suit}")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur envoi #{game_number}: {e}")
        return False

async def update_prediction_message(game_number: int, status: str, rattrapage: int = 0):
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    
    set_prediction_result(status)
    new_text = format_prediction_message(game_number, suit, status, rattrapage)
    
    if 'gagne' in status:
        logger.info(f"✅ Gagné #{game_number} R{rattrapage}")
    elif 'perdu' in status:
        logger.info(f"❌ Perdu #{game_number}")
        block_suit(suit, 5)
    
    try:
        await client.edit_message(pred['chat_id'], pred['message_id'], new_text, parse_mode='html')
    except Exception as e:
        logger.error(f"❌ Erreur édition #{game_number}: {e}")
    
    if 'gagne' in status or 'perdu' in status:
        pred['final_status'] = status
        pred['completed_at'] = datetime.now()

# ============================================================================
# VÉRIFICATION RÉSULTATS
# ============================================================================

async def check_prediction_result(game_number: int, first_group: str):
    pred = pending_predictions.get(game_number)
    if not pred:
        return
    
    predicted_suit = pred['suit']
    suits_in_result = get_suits_in_group(first_group)
    
    if predicted_suit in suits_in_result:
        await update_prediction_message(game_number, 'gagne_r0', 0)
    else:
        waiting_finalization.add(game_number)
        logger.info(f"⏳ #{game_number} attente rattrapage")
    
    await check_rattrapages(game_number, first_group)

async def check_rattrapages(current_game: int, first_group: str):
    suits_in_result = get_suits_in_group(first_group)
    
    for game_num in list(waiting_finalization):
        if game_num >= current_game:
            continue
        
        pred = pending_predictions.get(game_num)
        if not pred:
            waiting_finalization.discard(game_num)
            continue
        
        rattrapage = current_game - game_num
        if rattrapage > 2:
            await update_prediction_message(game_num, 'perdu', rattrapage)
            waiting_finalization.discard(game_num)
        elif pred['suit'] in suits_in_result:
            await update_prediction_message(game_num, f'gagne_r{rattrapage}', rattrapage)
            waiting_finalization.discard(game_num)

# ============================================================================
# TRAITEMENT JEU
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    global current_game_number, last_source_game_number
    
    current_game_number = game_number
    last_source_game_number = game_number
    
    # Reset #1440
    if game_number >= 1440:
        await perform_full_reset("Reset #1440")
        return
    
    groups = extract_parentheses_groups(message_text)
    if not groups:
        return
    
    first_group = groups[0]
    suits = get_suits_in_group(first_group)
    
    logger.info(f"📊 #{game_number}: {suits}")
    add_to_history(game_number, message_text, first_group, suits)
    update_compteur1(game_number, first_group)
    await check_prediction_result(game_number, first_group)
    await process_prediction_queue(game_number)
    
    # Compteur2
    if compteur2_active:
        update_compteur2(game_number, first_group)
        for inverse_suit, pred_num, orig in get_compteur2_ready_predictions(game_number):
            add_to_prediction_queue(pred_num, inverse_suit, 'compteur2')

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_perdusilencieux(event):
    global perdu_silencieux_active
    
    if event.sender_id != ADMIN_ID:
        return
    
    parts = event.message.message.split()
    if len(parts) == 1:
        status = "🟢 ON" if perdu_silencieux_active else "🔴 OFF"
        attente = "⏳ OUI" if prediction_was_sent else "✅ NON"
        await event.respond(
            f"🔕 Mode Silencieux: {status}\n"
            f"En attente: {attente}\n\n"
            f"Logique: 1 prédiction → attend résultat\n"
            f"PERDU = peut reprendre | GAGNÉ = attend nouveau PERDU\n"
            f"Usage: `/perdusilencieux on/off`"
        )
        return
    
    arg = parts[1].lower()
    if arg == 'on':
        perdu_silencieux_active = True
        reset_prediction_state()
        await event.respond("🔕 Mode silencieux ACTIVÉ")
        logger.info("Admin: silencieux ON")
    elif arg == 'off':
        perdu_silencieux_active = False
        reset_prediction_state()
        await event.respond("🔔 Mode silencieux DÉSACTIVÉ")
        logger.info("Admin: silencieux OFF")

async def cmd_compteur2(event):
    global compteur2_active, compteur2_seuil_B
    
    if event.sender_id != ADMIN_ID:
        return
    
    parts = event.message.message.split()
    if len(parts) == 1:
        status = "✅ ON" if compteur2_active else "❌ OFF"
        lines = [
            f"📊 Compteur2: {status} | B={compteur2_seuil_B}",
            "", "Inverses: ♠️↔♦️  ❤️↔♣️", "", "Progression:"
        ]
        for suit in ALL_SUITS:
            t = compteur2_trackers[suit]
            bar = f"[{'█'*min(t.counter,compteur2_seuil_B)}{'░'*(compteur2_seuil_B-min(t.counter,compteur2_seuil_B))}]"
            status_txt = f"🔮 PRÊT→{get_inverse_suit(suit)}" if t.counter >= compteur2_seuil_B else f"{t.counter}/{compteur2_seuil_B}"
            lines.append(f"{t.get_display_name()}: {bar} {status_txt}")
        lines.append(f"\nUsage: `/compteur2 [B/on/off/reset]`")
        await event.respond("\n".join(lines))
        return
    
    arg = parts[1].lower()
    if arg == 'on':
        compteur2_active = True
        await event.respond("✅ Compteur2 ON")
    elif arg == 'off':
        compteur2_active = False
        await event.respond("❌ Compteur2 OFF")
    elif arg == 'reset':
        for t in compteur2_trackers.values():
            t.counter = 0
        await event.respond("🔄 Reset")
    else:
        try:
            b = int(arg)
            if 2 <= b <= 10:
                compteur2_seuil_B = b
                await event.respond(f"✅ B={b}")
            else:
                await event.respond("❌ B entre 2-10")
        except ValueError:
            await event.respond("❌ Invalide")

async def cmd_queue(event):
    if event.sender_id != ADMIN_ID:
        return
    
    if not prediction_queue:
        await event.respond("📭 Vide")
        return
    
    lines = ["📥 File d'attente:"]
    for i, (gn, suit, _) in enumerate(prediction_queue[:10], 1):
        lines.append(f"{i}. #{gn} {SUIT_DISPLAY.get(suit, suit)}")
    if len(prediction_queue) > 10:
        lines.append(f"...+{len(prediction_queue)-10}")
    await event.respond("\n".join(lines))

async def cmd_pending(event):
    if event.sender_id != ADMIN_ID:
        return
    
    if not pending_predictions:
        await event.respond("📭 Aucune")
        return
    
    lines = ["🎰 En cours:"]
    for game, pred in list(pending_predictions.items())[-10:]:
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        lines.append(f"#{game} {suit} | {pred.get('final_status', pred['status'])}")
    await event.respond("\n".join(lines))

async def cmd_history(event):
    if event.sender_id != ADMIN_ID:
        return
    
    if not prediction_history:
        await event.respond("📭 Vide")
        return
    
    lines = [f"📜 Historique ({len(prediction_history)}):"]
    for i, pred in enumerate(prediction_history[-15:], 1):
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        status = pred.get('final_status', pred['status'])
        emoji = '🏆' if 'gagne' in status else '💔' if 'perdu' in status else '🎰'
        lines.append(f"{i}. {emoji} #{pred['predicted_game']} {suit} {status}")
    await event.respond("\n".join(lines))

async def cmd_stats(event):
    if event.sender_id != ADMIN_ID:
        return
    
    total = len(prediction_history)
    gagne = sum(1 for p in prediction_history if 'gagne' in str(p.get('final_status', '')))
    perdu = sum(1 for p in prediction_history if 'perdu' in str(p.get('final_status', '')))
    encours = total - gagne - perdu
    
    taux = (gagne / max(gagne + perdu, 1)) * 100
    await event.respond(
        f"📊 Stats:\nTotal: {total}\n🏆 Gagnés: {gagne}\n💔 Perdus: {perdu}\n🎰 En cours: {encours}\n"
        f"📈 Taux: {taux:.1f}%"
    )

async def cmd_status(event):
    if event.sender_id != ADMIN_ID:
        return
    
    await event.respond(
        f"🤖 Status:\n"
        f"Jeu: #{current_game_number}\n"
        f"Compteur2: {'ON' if compteur2_active else 'OFF'} (B={compteur2_seuil_B})\n"
        f"Silencieux: {'ON' if perdu_silencieux_active else 'OFF'}\n"
        f"File: {len(prediction_queue)} | En cours: {len(pending_predictions)}"
    )

async def cmd_reset(event):
    if event.sender_id != ADMIN_ID:
        return
    await perform_full_reset("Manuel")
    await event.respond("🔄 Reset effectué")

async def cmd_help(event):
    await event.respond(
        "📖 Commandes:\n"
        "`/perdusilencieux on/off` - Mode attente PERDU\n"
        "`/compteur2 [B/on/off/reset]` - Config Compteur2\n"
        "`/queue` - File d'attente\n"
        "`/pending` - En cours\n"
        "`/history` - Historique\n"
        "`/stats` - Statistiques\n"
        "`/status` - Status\n"
        "`/reset` - Reset"
    )

# ============================================================================
# RESET
# ============================================================================

async def perform_full_reset(reason: str):
    global pending_predictions, prediction_queue, waiting_finalization
    global last_prediction_number_sent, prediction_was_sent, last_prediction_result
    
    logger.info(f"🔄 RESET: {reason}")
    
    for t in compteur2_trackers.values():
        t.counter = 0
        t.last_increment_game = 0
    
    for t in compteur1_trackers.values():
        t.counter = 0
        t.start_game = 0
        t.last_game = 0
    
    prediction_was_sent = False
    last_prediction_result = None
    pending_predictions.clear()
    waiting_finalization.clear()
    prediction_queue.clear()
    suit_block_until.clear()
    
    last_prediction_number_sent = 0
    
    try:
        await client.send_message(ADMIN_ID, f"🔄 Reset: {reason}")
    except Exception as e:
        logger.error(f"Erreur notif reset: {e}")

# ============================================================================
# HANDLERS
# ============================================================================

async def handle_new_message(event):
    if not event.chat_id or event.chat_id != SOURCE_CHANNEL_ID:
        return
    
    me = await client.get_me()
    if event.sender_id == me.id:
        return
    
    text = event.message.message
    match = re.search(r'#(\d+)', text)
    if not match:
        return
    
    game_number = int(match.group(1))
    if game_number <= last_source_game_number and last_source_game_number > 0:
        return
    
    await process_game_result(game_number, text)

async def handle_edited_message(event):
    await handle_new_message(event)

def setup_handlers():
    handlers = [
        (cmd_perdusilencieux, r'^/perdusilencieux'),
        (cmd_compteur2, r'^/compteur2'),
        (cmd_queue, r'^/queue$'),
        (cmd_pending, r'^/pending$'),
        (cmd_history, r'^/history$'),
        (cmd_stats, r'^/stats$'),
        (cmd_status, r'^/status$'),
        (cmd_reset, r'^/reset$'),
        (cmd_help, r'^/help$'),
    ]
    
    for handler, pattern in handlers:
        client.add_event_handler(handler, events.NewMessage(pattern=pattern))
    
    client.add_event_handler(handle_new_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())

# ============================================================================
# SERVEUR WEB
# ============================================================================

from aiohttp import web

async def health_check(request):
    return web.Response(text=json.dumps({
        "status": "alive",
        "bot": "Baccarat AI",
        "game": current_game_number,
        "predictions": len(pending_predictions),
        "queue": len(prediction_queue),
        "compteur2": compteur2_active,
        "silencieux": perdu_silencieux_active
    }), content_type='application/json')

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Web server port {PORT}")

# ============================================================================
# MAIN
# ============================================================================

async def main():
    global client
    
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.error("❌ API_ID, API_HASH et BOT_TOKEN requis!")
        return
    
    client = TelegramClient(MemorySession(), API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    me = await client.get_me()
    logger.info(f"✅ Bot démarré: @{me.username}")
    logger.info(f"📡 Source: {SOURCE_CHANNEL_ID}")
    logger.info(f"📡 Prédictions: {PREDICTION_CHANNEL_ID}")
    
    setup_handlers()
    await client.run_until_disconnected()

async def run_with_web():
    await asyncio.gather(main(), start_web_server())

if __name__ == '__main__':
    try:
        asyncio.run(run_with_web())
    except KeyboardInterrupt:
        logger.info("🛑 Arrêt")
    except Exception as e:
        logger.error(f"💥 Fatal: {e}")
        raise
