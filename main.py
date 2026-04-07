import re
import asyncio
import logging
import sys
import traceback
from typing import List, Optional, Dict
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PREDICTION_CHANNEL_ID, PORT, API_POLL_INTERVAL,
    ALL_SUITS, SUIT_DISPLAY, TELEGRAM_SESSION,
    C1_SILENT_CHANNEL_ID
)
from utils import get_latest_results

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client = None
current_game_number = 0

silent_history: List[Dict] = []
MAX_SILENT_HISTORY = 150

api_results_cache: Dict[int, dict] = {}
player_processed_games: set = set()
reset_done_for_cycle: bool = False

# ============================================================================
# COMPTEUR1
# B=3 | silencieux (rattrapage 1) → canal après 1 perte silencieuse (rattrapage 2)
# Mapping: ♣→♦, ♦→♣, ♠→♥, ♥→♠
# ============================================================================

C1_B = 3
C1_SUIT_MAP = {'♣': '♦', '♦': '♣', '♠': '♥', '♥': '♠'}

c1_active: bool = True
c1_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c1_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c1_processed_games: set = set()
c1_consec_losses: int = 0        # pertes silencieuses consécutives
c1_pending_silent: Dict[int, dict] = {}   # prédictions silencieuses en attente
c1_pending_canal: Dict[int, dict] = {}    # prédictions canal en attente

# ============================================================================
# INTERVALLES HORAIRES
# ============================================================================

BENIN_TZ = timezone(timedelta(hours=1))
prediction_intervals: List[Dict[str, int]] = []
intervals_enabled: bool = False

def is_prediction_allowed_now() -> bool:
    if not intervals_enabled or not prediction_intervals:
        return True
    now_benin = datetime.now(BENIN_TZ)
    current_total = now_benin.hour * 60 + now_benin.minute
    for interval in prediction_intervals:
        start_total = interval["start"] * 60
        end_total = interval["end"] * 60
        if start_total <= end_total:
            if start_total <= current_total < end_total:
                return True
        else:
            if current_total >= start_total or current_total < end_total:
                return True
    return False

def get_intervals_status_text() -> str:
    now_benin = datetime.now(BENIN_TZ)
    status = "✅ ON" if intervals_enabled else "❌ OFF"
    allowed = "✅ OUI" if is_prediction_allowed_now() else "🚫 NON"
    lines = [
        f"⏰ **Intervalles de prédiction**",
        f"Mode restriction: {status}",
        f"Heure Bénin actuelle: {now_benin.strftime('%H:%M')}",
        f"Prédiction autorisée: {allowed}",
        "",
    ]
    if prediction_intervals:
        lines.append("Intervalles configurés:")
        for i, iv in enumerate(prediction_intervals, 1):
            lines.append(f"  {i}. {iv['start']:02d}h00 → {iv['end']:02d}h00")
    else:
        lines.append("Aucun intervalle défini (toujours autorisé si mode OFF)")
    return "\n".join(lines)

# ============================================================================
# UTILITAIRES
# ============================================================================

def normalize_suit(suit_emoji: str) -> str:
    return suit_emoji.replace('\ufe0f', '').replace('❤', '♥')

def player_suits_from_cards(player_cards: list) -> List[str]:
    suits = set()
    for card in player_cards:
        raw = card.get('S', '')
        normalized = normalize_suit(raw)
        if normalized in ALL_SUITS:
            suits.add(normalized)
    return list(suits)

def normalize_channel_id(channel_id) -> Optional[int]:
    if not channel_id:
        return None
    s = str(channel_id)
    if s.startswith('-100'):
        return int(s)
    if s.startswith('-'):
        return int(s)
    return int(f"-100{s}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        normalized = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized)
        return entity
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# HISTORIQUE SILENCIEUX
# ============================================================================

def add_silent_entry(pred_game: int, pred_suit: str, triggered_by: str,
                     consec_losses: int = 0, mode: str = "silent"):
    global silent_history
    silent_history.insert(0, {
        'pred_game': pred_game,
        'pred_suit': pred_suit,
        'triggered_by': triggered_by,
        'created_at': datetime.now(),
        'status': 'en_attente',
        'rattrapage': 0,
        'consec_losses_at_trigger': consec_losses,
        'mode': mode,
    })
    if len(silent_history) > MAX_SILENT_HISTORY:
        silent_history = silent_history[:MAX_SILENT_HISTORY]

def update_silent_entry_status(pred_game: int, status: str, rattrapage: int = 0):
    for entry in silent_history:
        if entry['pred_game'] == pred_game and entry['status'] == 'en_attente':
            entry['status'] = status
            entry['rattrapage'] = rattrapage
            entry['resolved_at'] = datetime.now()
            break

# ============================================================================
# FORMAT DES MESSAGES
# ============================================================================

def build_prediction_msg(game_number: int, suit: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        f"Game {game_number} :{suit_display}\n\n"
        f"En cours de vérification.⌛"
    )

def build_result_msg(game_number: int, suit: str, trouve: bool, rattrapage: int) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    rattrapage_icons = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
    result_icon = rattrapage_icons.get(rattrapage, f"✅{rattrapage}️⃣") if trouve else "❌"
    return (
        f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        f"Game {game_number} :{suit_display}\n\n"
        f"{result_icon}"
    )

# ============================================================================
# ENVOI PRÉDICTIONS SILENCIEUSES (rattrapage max 1)
# ============================================================================

async def send_silent_prediction(game_number: int, suit: str, triggered_by: str) -> dict:
    result = {'msg_id': None}
    entity = await resolve_channel(C1_SILENT_CHANNEL_ID)
    if entity:
        try:
            sent = await client.send_message(entity, build_prediction_msg(game_number, suit))
            result['msg_id'] = sent.id
            logger.info(f"🔕 [SILENT] #{game_number} {SUIT_DISPLAY.get(suit, suit)} → canal silencieux")
        except Exception as e:
            logger.error(f"❌ Erreur canal silencieux: {e}")
    return result

async def update_silent_message(pred: dict, game_number: int, suit: str, trouve: bool, rattrapage: int):
    if pred.get('msg_id') and C1_SILENT_CHANNEL_ID:
        entity = await resolve_channel(C1_SILENT_CHANNEL_ID)
        if entity:
            try:
                await client.edit_message(entity, pred['msg_id'], build_result_msg(game_number, suit, trouve, rattrapage))
            except Exception as e:
                logger.error(f"❌ Erreur update silencieux: {e}")

# ============================================================================
# ENVOI PRÉDICTIONS CANAL (rattrapage max 2)
# ============================================================================

async def send_canal_prediction(game_number: int, suit: str, triggered_by: str) -> dict:
    result = {'msg_id': None}
    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID non configuré")
        return result
    entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if entity:
        try:
            sent = await client.send_message(entity, build_prediction_msg(game_number, suit))
            result['msg_id'] = sent.id
            logger.info(f"📢 [CANAL] #{game_number} {SUIT_DISPLAY.get(suit, suit)} → canal principal")
        except ChatWriteForbiddenError:
            logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
        except Exception as e:
            logger.error(f"❌ Erreur canal principal: {e}")
    return result

async def update_canal_message(pred: dict, game_number: int, suit: str, trouve: bool, rattrapage: int):
    if pred.get('msg_id') and PREDICTION_CHANNEL_ID:
        entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if entity:
            try:
                await client.edit_message(entity, pred['msg_id'], build_result_msg(game_number, suit, trouve, rattrapage))
            except Exception as e:
                logger.error(f"❌ Erreur update canal: {e}")

# ============================================================================
# VÉRIFICATION - Prédictions silencieuses (rattrapage max 1)
# ============================================================================

async def check_silent_result_c1(game_number: int, player_suits: List[str], is_finished: bool):
    global c1_consec_losses, c1_pending_silent

    to_delete = []

    for pred_game, pred in list(c1_pending_silent.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        target_game = pred_game + awaiting

        if game_number != target_game:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            logger.info(f"🔕 SILENT #{pred_game} R{awaiting}: {target_suit} ✅ → consec_losses=0")
            c1_consec_losses = 0
            await update_silent_message(pred, pred_game, target_suit, True, awaiting)
            update_silent_entry_status(pred_game, "gagné", awaiting)
            to_delete.append(pred_game)

        elif is_finished:
            if awaiting < 1:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔕 SILENT #{pred_game}: {target_suit} ❌ → R1 (jeu #{pred_game + 1})")
            else:
                c1_consec_losses += 1
                logger.info(f"🔕 SILENT #{pred_game}: ❌ PERDU → consec_losses={c1_consec_losses} → escalade canal")
                await update_silent_message(pred, pred_game, target_suit, False, awaiting)
                update_silent_entry_status(pred_game, "perdu", awaiting)
                to_delete.append(pred_game)

    for k in to_delete:
        del c1_pending_silent[k]

# ============================================================================
# VÉRIFICATION - Prédictions canal (rattrapage max 2)
# ============================================================================

async def check_canal_result_c1(game_number: int, player_suits: List[str], is_finished: bool):
    global c1_consec_losses, c1_pending_canal

    to_delete = []

    for pred_game, pred in list(c1_pending_canal.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        target_game = pred_game + awaiting

        if game_number != target_game:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            logger.info(f"📢 CANAL #{pred_game} R{awaiting}: {target_suit} ✅ → consec_losses=0")
            c1_consec_losses = 0
            await update_canal_message(pred, pred_game, target_suit, True, awaiting)
            update_silent_entry_status(pred_game, "gagné", awaiting)
            to_delete.append(pred_game)

        elif is_finished:
            if awaiting < 2:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"📢 CANAL #{pred_game}: {target_suit} ❌ → R{awaiting+1} (jeu #{pred_game + awaiting + 1})")
            else:
                logger.info(f"📢 CANAL #{pred_game}: ❌ PERDU final R2")
                await update_canal_message(pred, pred_game, target_suit, False, awaiting)
                update_silent_entry_status(pred_game, "perdu", awaiting)
                to_delete.append(pred_game)

    for k in to_delete:
        del c1_pending_canal[k]

# ============================================================================
# COMPTEUR1 - Logique principale
# ============================================================================

def get_c1_status_text() -> str:
    status = "✅ ON" if c1_active else "❌ OFF"
    mode_canal = c1_consec_losses >= 1
    lines = [
        f"📊 Compteur1: {status} | B={C1_B}",
        f"🔕 Pertes silencieuses: {c1_consec_losses} → Mode: {'📢 CANAL (R2)' if mode_canal else '🔕 SILENT (R1)'}",
        "",
        "Progression des absences (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = c1_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, C1_B - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        pred_display = SUIT_DISPLAY.get(C1_SUIT_MAP.get(suit, suit), suit)
        lines.append(f"{display} → {pred_display} : {bar} {count}/{C1_B}")
    if c1_pending_silent:
        lines.append(f"\n🔕 Silencieux actifs: {len(c1_pending_silent)}")
        for g, p in sorted(c1_pending_silent.items()):
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(f"  • #{g} {sd} (R{ar})")
    if c1_pending_canal:
        lines.append(f"\n📢 Canal actifs: {len(c1_pending_canal)}")
        for g, p in sorted(c1_pending_canal.items()):
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(f"  • #{g} {sd} (R{ar})")
    return "\n".join(lines)

async def process_compteur1(game_number: int, player_suits: List[str]):
    global c1_absences, c1_last_seen, c1_processed_games
    global c1_consec_losses, c1_pending_silent, c1_pending_canal

    if not c1_active:
        return
    if game_number in c1_processed_games:
        return

    c1_processed_games.add(game_number)
    if len(c1_processed_games) > 200:
        c1_processed_games.discard(min(c1_processed_games))

    for suit in ALL_SUITS:
        if suit in player_suits:
            if c1_absences[suit] > 0:
                logger.info(f"📊 C1 {suit}: trouvé #{game_number} → reset (était {c1_absences[suit]})")
            c1_absences[suit] = 0
            c1_last_seen[suit] = game_number
        else:
            last_seen = c1_last_seen.get(suit, 0)
            if last_seen == 0 or game_number == last_seen + 1:
                c1_absences[suit] += 1
            else:
                c1_absences[suit] = 1
            c1_last_seen[suit] = game_number
            count = c1_absences[suit]
            logger.info(f"📊 C1 {suit}: absence {count}/{C1_B} (jeu #{game_number})")

            if count >= C1_B:
                pred_suit = C1_SUIT_MAP.get(suit, suit)
                pred_game = game_number + 1
                c1_absences[suit] = 0

                # Déjà une prédiction en attente pour ce jeu
                if pred_game in c1_pending_silent or pred_game in c1_pending_canal:
                    continue

                if c1_consec_losses >= 1:
                    # Escalade vers le canal (rattrapage 2)
                    losses_snap = c1_consec_losses
                    c1_consec_losses = 0
                    logger.info(
                        f"📢 C1 CANAL: {suit} absent {C1_B}x, {losses_snap} perte(s) silencieuse(s) "
                        f"→ #{pred_game} {pred_suit} [R max 2]"
                    )
                    add_silent_entry(pred_game, pred_suit, suit, losses_snap, mode="canal")
                    msg_ids = await send_canal_prediction(pred_game, pred_suit, suit)
                    c1_pending_canal[pred_game] = {
                        'suit': pred_suit,
                        'triggered_by': suit,
                        'awaiting_rattrapage': 0,
                        'msg_id': msg_ids['msg_id'],
                    }
                else:
                    # Mode silencieux (rattrapage 1)
                    logger.info(
                        f"🔕 C1 SILENT: {suit} absent {C1_B}x "
                        f"→ #{pred_game} {pred_suit} [R max 1]"
                    )
                    add_silent_entry(pred_game, pred_suit, suit, 0, mode="silent")
                    msg_ids = await send_silent_prediction(pred_game, pred_suit, suit)
                    c1_pending_silent[pred_game] = {
                        'suit': pred_suit,
                        'triggered_by': suit,
                        'awaiting_rattrapage': 0,
                        'msg_id': msg_ids['msg_id'],
                    }

# ============================================================================
# BOUCLE DE POLLING API
# ============================================================================

async def api_polling_loop():
    global current_game_number, api_results_cache, player_processed_games
    global reset_done_for_cycle

    loop = asyncio.get_event_loop()
    logger.info(f"🔄 Polling API démarré (intervalle: {API_POLL_INTERVAL}s)")

    while True:
        try:
            results = await loop.run_in_executor(None, get_latest_results)

            if results:
                for result in results:
                    game_number = result["game_number"]
                    is_finished = result["is_finished"]
                    player_cards = result.get("player_cards", [])
                    phase = result.get("phase")

                    api_results_cache[game_number] = result

                    player_suits = player_suits_from_cards(player_cards)

                    if len(player_cards) < 2:
                        continue

                    PLAYER_DONE_PHASES = ("DealerMove", "Win1", "Win2", "Tie")
                    player_done = phase in PLAYER_DONE_PHASES or is_finished

                    if not player_done:
                        continue

                    current_game_number = game_number
                    p_display = " ".join(SUIT_DISPLAY.get(s, s) for s in player_suits) or "—"

                    # Vérification des prédictions silencieuses (R max 1)
                    await check_silent_result_c1(game_number, player_suits, is_finished)

                    # Vérification des prédictions canal (R max 2)
                    await check_canal_result_c1(game_number, player_suits, is_finished)

                    # Traitement compteur C1
                    if game_number not in player_processed_games and player_done:
                        player_processed_games.add(game_number)
                        if len(player_processed_games) > 500:
                            player_processed_games.discard(min(player_processed_games))

                        logger.info(f"🃏 Jeu #{game_number} | Joueur: {p_display}")
                        await process_compteur1(game_number, player_suits)

                    # Reset automatique sur partie #1440
                    if game_number == 1440 and is_finished and not reset_done_for_cycle:
                        reset_done_for_cycle = True
                        logger.info("🔄 Reset automatique: partie #1440 terminée")
                        await perform_full_reset("Reset automatique (partie #1440)")

                    if game_number < 100 and reset_done_for_cycle:
                        reset_done_for_cycle = False

                if len(api_results_cache) > 300:
                    oldest = min(api_results_cache.keys())
                    del api_results_cache[oldest]

        except Exception as e:
            logger.error(f"❌ Erreur polling API: {e}")
            logger.error(traceback.format_exc())

        await asyncio.sleep(API_POLL_INTERVAL)

# ============================================================================
# RESET COMPLET
# ============================================================================

async def perform_full_reset(reason: str):
    global player_processed_games, api_results_cache, reset_done_for_cycle
    global c1_absences, c1_last_seen, c1_processed_games
    global c1_consec_losses, c1_pending_silent, c1_pending_canal

    player_processed_games = set()
    api_results_cache = {}

    c1_absences = {suit: 0 for suit in ALL_SUITS}
    c1_last_seen = {suit: 0 for suit in ALL_SUITS}
    c1_processed_games = set()
    c1_consec_losses = 0
    c1_pending_silent = {}
    c1_pending_canal = {}

    global silent_history
    silent_history = []

    logger.info(f"🔄 {reason} - Reset effectué")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_compteur1(event):
    global c1_active, c1_absences, c1_last_seen, c1_processed_games
    global c1_consec_losses, c1_pending_silent, c1_pending_canal

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_c1_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        c1_active = True
        c1_absences = {suit: 0 for suit in ALL_SUITS}
        c1_last_seen = {suit: 0 for suit in ALL_SUITS}
        c1_processed_games = set()
        c1_consec_losses = 0
        c1_pending_silent = {}
        c1_pending_canal = {}
        await event.respond(f"✅ Compteur1 ACTIVÉ | B={C1_B}\n\n" + get_c1_status_text())

    elif arg == 'off':
        c1_active = False
        await event.respond("❌ Compteur1 DÉSACTIVÉ")

    elif arg == 'reset':
        c1_absences = {suit: 0 for suit in ALL_SUITS}
        c1_last_seen = {suit: 0 for suit in ALL_SUITS}
        c1_processed_games = set()
        c1_consec_losses = 0
        c1_pending_silent = {}
        c1_pending_canal = {}
        await event.respond("🔄 Compteur1 remis à zéro\n\n" + get_c1_status_text())

    else:
        await event.respond(
            "📊 **COMPTEUR1 - Aide**\n\n"
            f"B={C1_B} | Silencieux R1 → Canal R2 après 1 perte\n\n"
            "Mapping: ♣️→♦️ | ♦️→♣️ | ♠️→❤️ | ❤️→♠️\n\n"
            "`/compteur1` — Afficher l'état\n"
            "`/compteur1 on` — Activer\n"
            "`/compteur1 off` — Désactiver\n"
            "`/compteur1 reset` — Remettre à zéro"
        )


async def cmd_silencieux(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()
    show_all = len(parts) > 1 and parts[1].lower() == 'all'
    max_show = 50 if show_all else 20

    lines = [
        "🔕 **PRÉDICTIONS C1**",
        "═══════════════════════════════════════",
        ""
    ]

    actives_silent = [(g, p) for g, p in sorted(c1_pending_silent.items())]
    actives_canal = [(g, p) for g, p in sorted(c1_pending_canal.items())]

    if actives_silent or actives_canal:
        lines.append("**⏳ EN COURS :**")
        for g, p in actives_silent:
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            td = SUIT_DISPLAY.get(p['triggered_by'], p['triggered_by'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(
                f"  🔕 Game #N{g} R{ar} | {td} → {sd} | [SILENT R max 1]"
            )
        for g, p in actives_canal:
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            td = SUIT_DISPLAY.get(p['triggered_by'], p['triggered_by'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(
                f"  📢 Game #N{g} R{ar} | {td} → {sd} | [CANAL R max 2]"
            )
        lines.append("")

    if not silent_history:
        if not actives_silent and not actives_canal:
            lines.append("Aucune prédiction enregistrée.")
    else:
        lines.append(f"**📜 HISTORIQUE** (dernières {min(len(silent_history), max_show)}) :")
        lines.append("")

        for i, entry in enumerate(silent_history[:max_show], 1):
            g = entry['pred_game']
            sd = SUIT_DISPLAY.get(entry['pred_suit'], entry['pred_suit'])
            td = SUIT_DISPLAY.get(entry['triggered_by'], entry['triggered_by'])
            t = entry['created_at'].strftime('%H:%M:%S')
            status = entry['status']
            ratt = entry.get('rattrapage', 0)
            mode = entry.get('mode', 'silent')
            mode_icon = "📢" if mode == "canal" else "🔕"

            if status == 'en_attente':
                status_icon, status_str = "⏳", "En cours..."
            elif status == 'gagné':
                r_str = f" (R{ratt})" if ratt > 0 else ""
                status_icon, status_str = "✅", f"GAGNÉ{r_str}"
            else:
                status_icon, status_str = "❌", "PERDU"

            lines.append(
                f"{i}. 🕐 `{t}` | {mode_icon} Game #N{g} | {status_icon} {status_str}\n"
                f"   🃏 {td} absent → prédit {sd}"
            )
            lines.append("")

    if len(silent_history) > max_show and not show_all:
        lines.append(f"_... {len(silent_history) - max_show} entrées. Tapez `/silencieux all` pour tout voir._")

    lines.append("═══════════════════════════════════════")
    mode_actuel = "📢 CANAL (R max 2)" if c1_consec_losses >= 1 else "🔕 SILENT (R max 1)"
    lines.append(
        f"\n📊 **Résumé C1:**\n"
        f"Pertes silencieuses: **{c1_consec_losses}** → Mode actuel: {mode_actuel}"
    )

    full_text = "\n".join(lines)
    if len(full_text) > 4000:
        chunks = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
        for chunk in chunks:
            await event.respond(chunk)
    else:
        await event.respond(full_text)


async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        get_c1_status_text(),
        "",
        f"📡 Source: API 1xBet (polling {API_POLL_INTERVAL}s)",
        f"📦 Jeux en cache: {len(api_results_cache)}",
        f"🔄 Reset automatique: partie #1440",
    ]
    await event.respond("\n".join(lines))


async def check_channel_access(channel_id) -> dict:
    result = {'id': channel_id, 'status': '❌', 'name': 'Inaccessible', 'can_write': False, 'error': ''}
    if not channel_id:
        result['error'] = 'ID non configuré'
        return result
    try:
        entity = await resolve_channel(channel_id)
        if not entity:
            result['error'] = 'Canal introuvable'
            return result
        result['name'] = getattr(entity, 'title', 'Sans titre')
        try:
            from telethon.tl.functions.channels import GetParticipantRequest
            from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
            me = await client.get_me()
            participant = await client(GetParticipantRequest(entity, me))
            p = participant.participant
            if isinstance(p, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                result['status'] = '✅ Admin'
                result['can_write'] = True
            else:
                result['status'] = '⚠️ Membre'
                result['can_write'] = False
                result['error'] = 'Bot non admin'
        except Exception:
            result['status'] = '⚠️ Accessible'
            result['error'] = 'Permissions non vérifiées'
    except Exception as e:
        err = str(e)
        if 'Could not find' in err or 'PeerChannel' in err:
            result['error'] = 'Bot non membre du canal'
        else:
            result['error'] = err[:50]
    return result


async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔍 Vérification des canaux en cours...")

    canaux = [
        ("🔕 C1 Silencieux (R1)", C1_SILENT_CHANNEL_ID),
        ("📢 Canal Principal (R2)", PREDICTION_CHANNEL_ID),
    ]

    lines = ["📡 **ÉTAT DES CANAUX**", "═══════════════════════════════════════", ""]
    all_ok = True

    for label, cid in canaux:
        info = await check_channel_access(cid)
        if not info['can_write']:
            all_ok = False
        name_str = f"**{info['name']}**" if info['name'] != 'Inaccessible' else "_Inaccessible_"
        err_str = f"\n     ⚠️ {info['error']}" if info['error'] else ""
        lines.append(
            f"{label}\n"
            f"     {info['status']} | ID: `{cid}`\n"
            f"     {name_str}{err_str}"
        )
        lines.append("")

    lines.append("═══════════════════════════════════════")
    if all_ok:
        lines.append("✅ **Tous les canaux sont OK.**")
    else:
        lines.append("❌ **Certains canaux nécessitent une action.**")
        lines.append("Ajoutez le bot comme administrateur avec permission de publier.")

    lines.append(f"\n📊 **Config:** API poll={API_POLL_INTERVAL}s | Jeu actuel: #{current_game_number}")
    await event.respond("\n".join(lines))


async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test des canaux...")

    for label, cid in [("Silencieux (R1)", C1_SILENT_CHANNEL_ID), ("Principal (R2)", PREDICTION_CHANNEL_ID)]:
        if not cid:
            await event.respond(f"❌ {label}: ID non configuré")
            continue
        try:
            entity = await resolve_channel(cid)
            if not entity:
                await event.respond(f"❌ {label}: Canal inaccessible")
                continue
            sent = await client.send_message(entity, build_prediction_msg(9999, '♠'))
            await asyncio.sleep(1)
            await client.edit_message(entity, sent.id, build_result_msg(9999, '♠', True, 0))
            await asyncio.sleep(1)
            await client.delete_messages(entity, [sent.id])
            name = getattr(entity, 'title', str(cid))
            await event.respond(f"✅ {label}: `{name}` — OK")
        except ChatWriteForbiddenError:
            await event.respond(f"❌ {label}: Permission refusée — bot non admin")
        except Exception as e:
            await event.respond(f"❌ {label}: {e}")


async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteur1 remis à zéro.")


async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return

    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not entity:
            await event.respond("❌ Canal principal non accessible")
            return
        now = datetime.now()
        msg = (
            f"╔══════════════════════════════════════╗\n"
            f"║     📢 ANNONCE OFFICIELLE 📢          ║\n"
            f"╠══════════════════════════════════════╣\n\n"
            f"{text}\n\n"
            f"╠══════════════════════════════════════╣\n"
            f"║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨"
        )
        sent = await client.send_message(entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")


async def cmd_predi(event):
    global prediction_intervals, intervals_enabled

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    raw = event.message.message.strip()
    add_match = re.match(r'^/predi\+(\d{1,2})-(\d{1,2})$', raw)
    if add_match:
        start_h = int(add_match.group(1))
        end_h = int(add_match.group(2))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            await event.respond("❌ Heures invalides (0-23).")
            return
        if start_h == end_h:
            await event.respond("❌ Début et fin identiques.")
            return
        for iv in prediction_intervals:
            if iv["start"] == start_h and iv["end"] == end_h:
                await event.respond(f"⚠️ L'intervalle existe déjà.")
                return
        prediction_intervals.append({"start": start_h, "end": end_h})
        await event.respond(f"✅ Intervalle ajouté: {start_h:02d}h00 → {end_h:02d}h00\n\n" + get_intervals_status_text())
        return

    parts = raw.split()
    if len(parts) == 1:
        await event.respond(
            get_intervals_status_text() + "\n\n"
            "**Commandes:**\n"
            "`/predi+HH-HH` — Ajouter un intervalle\n"
            "`/predi del <N>` — Supprimer\n"
            "`/predi clear` — Tout supprimer\n"
            "`/predi on` — Activer\n"
            "`/predi off` — Désactiver"
        )
        return

    arg = parts[1].lower()
    if arg == "on":
        intervals_enabled = True
        await event.respond("✅ Restriction horaire ACTIVÉE\n\n" + get_intervals_status_text())
    elif arg == "off":
        intervals_enabled = False
        await event.respond("❌ Restriction horaire DÉSACTIVÉE\n\n" + get_intervals_status_text())
    elif arg == "clear":
        prediction_intervals = []
        await event.respond("🗑️ Intervalles supprimés.\n\n" + get_intervals_status_text())
    elif arg == "del":
        if len(parts) < 3:
            await event.respond("Usage: `/predi del <N>`")
            return
        try:
            idx = int(parts[2]) - 1
            if not (0 <= idx < len(prediction_intervals)):
                await event.respond(f"❌ Index invalide.")
                return
            removed = prediction_intervals.pop(idx)
            await event.respond(f"🗑️ Intervalle {removed['start']:02d}h→{removed['end']:02d}h supprimé.\n\n" + get_intervals_status_text())
        except ValueError:
            await event.respond("❌ Numéro invalide.")
    else:
        await event.respond("Usage: `/predi`, `/predi+HH-HH`, `/predi on/off/clear/del`")


async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(
        "🎲 **BACCARAT PREMIUM+2 ✨**\n\n"
        "Bot de prédiction Baccarat intelligent.\n\n"
        "📊 **Compteur1** (B=5) :\n"
        "• Détecte le costume absent 5x\n"
        "• Envoie une prédiction **silencieuse** (rattrapage 1)\n"
        "• Après 1 perte silencieuse → envoie au **canal principal** (rattrapage 2)\n\n"
        "Mapping: ♣️→♦️ | ♦️→♣️ | ♠️→❤️ | ❤️→♠️\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 Tapez /help pour les commandes.\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )


async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(
        "📖 **BACCARAT PREMIUM+2 ✨ - AIDE**\n\n"
        "**📊 Compteur1 (B=5) :**\n"
        "• Absent 5x → prédiction **silencieuse** (R max 1)\n"
        "• Après 1 perte silencieuse → prédiction **canal** (R max 2)\n"
        "• Victoire → retour en mode silencieux\n"
        "• ♣️→♦️ | ♦️→♣️ | ♠️→❤️ | ❤️→♠️\n\n"
        "**🔧 Commandes Admin:**\n"
        "`/compteur1` — État du Compteur1\n"
        "`/compteur1 on/off` — Activer/désactiver\n"
        "`/compteur1 reset` — Remettre à zéro\n"
        "`/silencieux` — Historique des prédictions\n"
        "`/silencieux all` — Tout l'historique\n"
        "`/status` — État complet\n"
        "`/channels` — Vérifier les canaux\n"
        "`/test` — Tester les canaux\n"
        "`/predi` — Gérer les intervalles horaires\n"
        "`/reset` — Reset complet\n"
        "`/announce <msg>` — Annonce\n"
        "`/help` — Cette aide"
    )

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_compteur1, events.NewMessage(pattern=r'^/compteur1'))
    client.add_event_handler(cmd_silencieux, events.NewMessage(pattern=r'^/silencieux'))
    client.add_event_handler(cmd_predi, events.NewMessage(pattern=r'^/predi'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_start, events.NewMessage(pattern=r'^/start$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client

    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()
        logger.info(f"🤖 BACCARAT PREMIUM+2 ✨ démarré | C1 B={C1_B} | Silent R1 → Canal R2")
        logger.info(f"🔄 Reset automatique: fin de la partie #1440")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False


async def main():
    try:
        if not await start_bot():
            return

        asyncio.create_task(api_polling_loop())
        logger.info("🔄 Polling API démarré")

        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT PREMIUM+2 ✨ Running"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"🌐 Health check sur port {PORT}")

        await client.run_until_disconnected()

    except KeyboardInterrupt:
        logger.info("🛑 Arrêt demandé")
    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
        logger.error(traceback.format_exc())


if __name__ == '__main__':
    asyncio.run(main())
