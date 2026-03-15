#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baccarat AI Bot - Compteur2 avec Inverses + Perdu Silencieux
Basé sur le système du fichier uploadé
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

# Inverses des cartes
SUIT_INVERSES = {
    '♠': '♦',  # Pique → Carreau
    '♦': '♠',  # Carreau → Pique
    '♥': '♣',  # Cœur → Trèfle
    '♣': '♥',  # Trèfle → Cœur
}

# Format messages prédiction
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
last_received_message = ""
last_received_game = 0

pending_predictions: Dict[int, dict] = {}
prediction_history: List[dict] = []
prediction_queue: List[Tuple[int, str, str]] = []
waiting_finalization: set = set()

suit_block_until: Dict[str, datetime] = {}

# Compteur2 - Compte les ABSENCES (costumes manquants)
class Compteur2Tracker:
    def __init__(self, suit: str):
        self.suit = suit
        self.counter = 0  # Nombre d'absences consécutives
        self.last_present_game = 0  # Dernier jeu où le costume était présent
    
    def reset(self):
        """Reset quand le costume est présent."""
        self.counter = 0
    
    def increment(self):
        """Incrémente quand le costume est absent."""
        self.counter += 1
    
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
    """Mode silencieux: attendre PERDU avant nouvelle prédiction."""
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
# COMPTeur2 - CORRIGÉ: Compte les ABSENCES (manques)
# ============================================================================

def update_compteur2(game_number: int, first_group: str):
    """
    Met à jour Compteur2: compte les absences consécutives.
    - Si costume dans premier groupe → reset (présent)
    - Si costume absent → incrémente (manquant)
    """
    suits_in_first = set(get_suits_in_group(first_group))
    
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        
        if suit in suits_in_first:
            # PRÉSENT: reset le compteur d'absences
            if tracker.counter > 0:
                logger.info(f"🔄 {tracker.get_display_name()} présent au #{game_number} → reset (était absent {tracker.counter}x)")
            tracker.reset()
            tracker.last_present_game = game_number
        else:
            # ABSENT: incrémente le compteur d'absences
            tracker.increment()
            logger.info(f"📊 {tracker.get_display_name()} ABSENT au #{game_number} → compteur: {tracker.counter}/{compteur2_seuil_B}")

def get_compteur2_ready_predictions(current_game: int) -> List[tuple]:
    """
    Retourne les prédictions prêtes quand un compteur atteint B.
    Prédit l'INVERSE du costume qui manque.
    """
    ready = []
    
    if not can_predict_new():
        logger.info("🔕 Mode silencieux: attente résultat prédiction précédente")
        return ready
    
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        
        # Quand le compteur d'ABSENCES atteint B
        if tracker.counter >= compteur2_seuil_B:
            # Prédire l'INVERSE du costume qui manque
            inverse_suit = get_inverse_suit(suit)
            pred_number = current_game + 1
            
            if is_suit_blocked(inverse_suit):
                logger.info(f"🚫 {inverse_suit} bloqué, reset compteur {suit}")
                tracker.reset()
                continue
            
            ready.append((inverse_suit, pred_number, suit))
            logger.info(f"🎯 {suit} a manqué {compteur2_seuil_B} fois → prédit INVERSE {inverse_suit} au #{pred_number}")
            tracker.reset()  # Reset après prédiction
    
    return ready

# ============================================================================
# HISTORIQUE & FILE
# ============================================================================

def add_to_history(game_number: int, raw_text: str, first_group: str, suits: List[str]):
    global last_received_game, last_received_message
    last_received_game = game_number
    last_received_message = raw_text[:100]
    
    game_history.append({
        'game_number': game_number,
        'raw_text': raw_text[:100],
        'first_group': first_group,
        'suits': suits,
        'timestamp': datetime.now()
    })
    if len(game_history) > 500:
        game_history.pop(0)

def add_to_prediction_queue(game_number: int, suit: str, pred_type: str = 'compteur2') -> bool:
    global last_prediction_number_sent
    
    # Vérifier écart minimum
    if last_prediction_number_sent > 0:
        gap = game_number - last_prediction_number_sent
        if gap < NUMBERS_PER_TOUR:
            logger.info(f"⏭️ Écart {gap} < {NUMBERS_PER_TOUR}")
    
    # Éviter doublons
    for gn, s, t in prediction_queue:
        if gn == game_number and s == suit:
            return False
    
    prediction_queue.append((game_number, suit, pred_type))
    logger.info(f"📥 File: #{game_number} {suit} (inverse de {pred_type})")
    return True

async def process_prediction_queue(current_game: int):
    global prediction_queue, last_prediction_number_sent
    
    if not prediction_queue:
        return
    
    to_remove = []
    for i, (game_number, suit, pred_type) in enumerate(prediction_queue):
        # Vérifier écart
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
# ENVOI & VÉRIFICATION
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
        
        logger.info(f"📤 Envoyé: #{game_number} {suit} (type: {pred_type})")
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
    
    logger.info(f"🎮 TRAITEMENT JEU #{game_number}")
    
    if game_number >= 1440:
        await perform_full_reset("Reset #1440")
        return
    
    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupes dans #{game_number}")
        return
    
    first_group = groups[0]
    suits = get_suits_in_group(first_group)
    
    logger.info(f"📊 #{game_number}: Premier groupe={first_group} | Costumes présents={suits}")
    
    add_to_history(game_number, message_text, first_group, suits)
    
    # Vérifier résultats des prédictions en cours
    await check_prediction_result(game_number, first_group)
    await process_prediction_queue(game_number)
    
    # Compteur2: met à jour les absences et prédit si B atteint
    if compteur2_active:
        update_compteur2(game_number, first_group)
        for inverse_suit, pred_num, original_suit in get_compteur2_ready_predictions(game_number):
            add_to_prediction_queue(pred_num, inverse_suit, f'inv_{original_suit}')

# ============================================================================
# HANDLERS
# ============================================================================

async def handle_new_message(event):
    chat_id = event.chat_id
    if chat_id != SOURCE_CHANNEL_ID:
        return
    
    try:
        me = await client.get_me()
        if event.sender_id == me.id:
            return
    except:
        pass
    
    text = event.message.message
    if not text:
        return
    
    match = re.search(r'#(\d+)', text)
    if not match:
        return
    
    game_number = int(match.group(1))
    logger.info(f"🔢 Message reçu: #{game_number} | Chat: {chat_id}")
    
    if game_number <= last_source_game_number and last_source_game_number > 0:
        return
    
    await process_game_result(game_number, text)

async def handle_edited_message(event):
    await handle_new_message(event)

# ============================================================================
# COMMANDES
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
    elif arg == 'off':
        perdu_silencieux_active = False
        reset_prediction_state()
        await event.respond("🔔 Mode silencieux DÉSACTIVÉ")

async def cmd_compteur2(event):
    global compteur2_active, compteur2_seuil_B
    
    if event.sender_id != ADMIN_ID:
        return
    
    parts = event.message.message.split()
    if len(parts) == 1:
        status = "✅ ON" if compteur2_active else "❌ OFF"
        
        lines = [
            f"📊 Compteur2: {status} | B={compteur2_seuil_B}",
            f"🎮 Dernier jeu: #{last_received_game}",
            f"📝 Message: {last_received_message[:40]}...",
            "",
            "Inverses: ♠️↔♦️  ❤️↔♣️",
            "",
            "Progression (absences consécutives → prédit l'inverse):"
        ]
        
        for suit in ALL_SUITS:
            t = compteur2_trackers[suit]
            bar = f"[{'█'*min(t.counter,compteur2_seuil_B)}{'░'*(compteur2_seuil_B-min(t.counter,compteur2_seuil_B))}]"
            
            if t.counter >= compteur2_seuil_B:
                status_txt = f"🔮 PRÊT → prédit {get_inverse_suit(suit)}"
            else:
                status_txt = f"{t.counter}/{compteur2_seuil_B}"
            
            last_seen = f"(présent #{t.last_present_game})" if t.last_present_game > 0 else "(jamais vu)"
            lines.append(f"{t.get_display_name()} {last_seen}: {bar} {status_txt}")
        
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
            t.last_present_game = 0
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

async def cmd_status(event):
    if event.sender_id != ADMIN_ID:
        return
    
    await event.respond(
        f"🤖 Status:\n"
        f"🎮 Dernier jeu: #{last_received_game}\n"
        f"📝 Message: {last_received_message[:40] if last_received_message else 'Aucun'}...\n"
        f"📊 Compteur2: {'ON' if compteur2_active else 'OFF'} (B={compteur2_seuil_B})\n"
        f"🔕 Silencieux: {'ON' if perdu_silencieux_active else 'OFF'}\n"
        f"📥 File: {len(prediction_queue)} | 🎰 En cours: {len(pending_predictions)}"
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
        "`/compteur2` - Voir progression (absences)\n"
        "`/status` - Status\n"
        "`/reset` - Reset"
    )

async def perform_full_reset(reason: str):
    global pending_predictions, prediction_queue, waiting_finalization
    global last_prediction_number_sent, prediction_was_sent, last_prediction_result
    global last_received_game, last_received_message
    
    logger.info(f"🔄 RESET: {reason}")
    
    for t in compteur2_trackers.values():
        t.counter = 0
        t.last_present_game = 0
    
    prediction_was_sent = False
    last_prediction_result = None
    pending_predictions.clear()
    waiting_finalization.clear()
    prediction_queue.clear()
    suit_block_until.clear()
    
    last_prediction_number_sent = 0
    last_received_game = 0
    last_received_message = ""

def setup_handlers():
    handlers = [
        (cmd_perdusilencieux, r'^/perdusilencieux'),
        (cmd_compteur2, r'^/compteur2'),
        (cmd_status, r'^/status$'),
        (cmd_reset, r'^/reset$'),
        (cmd_help, r'^/help$'),
    ]
    
    for handler, pattern in handlers:
        client.add_event_handler(handler, events.NewMessage(pattern=pattern))
    
    client.add_event_handler(handle_new_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())

# ============================================================================
# WEB & MAIN
# ============================================================================

from aiohttp import web

async def health_check(request):
    return web.Response(text=json.dumps({
        "status": "alive",
        "bot": "Baccarat AI",
        "last_game": last_received_game,
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

async def main():
    global client
    
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.error("❌ Config incomplète!")
        return
    
    client = TelegramClient(MemorySession(), API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    me = await client.get_me()
    logger.info(f"✅ Bot: @{me.username}")
    logger.info(f"📡 Source: {SOURCE_CHANNEL_ID}")
    logger.info(f"📡 Pred: {PREDICTION_CHANNEL_ID}")
    
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
