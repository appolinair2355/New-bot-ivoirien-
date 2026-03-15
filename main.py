import os
import asyncio
import re
import logging
import sys
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    ALL_SUITS, SUIT_DISPLAY, SUIT_INVERSE,
    COMPTEUR2_ACTIVE, COMPTEUR2_B
)

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
prediction_channel_ok = False
current_game_number = 0
last_prediction_time: Optional[datetime] = None

# Prédictions en attente de vérification {game_number: {...}}
pending_predictions: Dict[int, dict] = {}

# Compteur2 - absences consécutives par couleur
compteur2_active = COMPTEUR2_ACTIVE
compteur2_b = COMPTEUR2_B
compteur2_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur2_last_game = 0

# Mode Attente - attend PERDU avant de prédire à nouveau
attente_mode = False
attente_locked = False   # True après une prédiction, attend PERDU pour déverrouiller

# Historique des prédictions
prediction_history: List[Dict] = []
MAX_HISTORY_SIZE = 100

# ============================================================================
# UTILITAIRES - Canaux
# ============================================================================

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
# UTILITAIRES - Parsing messages
# ============================================================================

def is_message_finalized(message: str) -> bool:
    if '⏰' in message or '⏳' in message:
        return False
    lower_msg = message.lower()
    if any(w in lower_msg for w in ['en cours', 'attente', 'pending', 'wait', 'waiting']):
        return False
    return '✅' in message or '🔰' in message

def extract_parentheses_groups(message: str) -> List[str]:
    scored = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored:
        return [f"{s}:{c}" if s else c for s, c in scored]
    return re.findall(r"\(([^)]*)\)", message)

def get_suits_in_group(group_str: str) -> List[str]:
    if ':' in group_str:
        group_str = group_str.split(':', 1)[1]
    normalized = group_str
    for old, new in [('❤️', '♥'), ('❤', '♥'), ('♥️', '♥'),
                     ('♠️', '♠'), ('♦️', '♦'), ('♣️', '♣')]:
        normalized = normalized.replace(old, new)
    return [suit for suit in ALL_SUITS if suit in normalized]

# ============================================================================
# HISTORIQUE DES PRÉDICTIONS
# ============================================================================

def add_prediction_to_history(game_number: int, suit: str, triggered_by_suit: str):
    global prediction_history
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit': suit,
        'triggered_by': triggered_by_suit,
        'predicted_at': datetime.now(),
        'status': 'en_cours',
        'result_game': None,
        'silent': attente_mode,
    })
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_history_status(game_number: int, suit: str, status: str, result_game: int):
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['status'] = status
            pred['result_game'] = result_game
            break

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS
# ============================================================================

async def send_prediction(game_number: int, suit: str, triggered_by_suit: str) -> Optional[int]:
    """Envoie une prédiction au canal."""
    global last_prediction_time, attente_locked

    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID non configuré")
        return None

    prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if not prediction_entity:
        logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
        return None

    suit_display = SUIT_DISPLAY.get(suit, suit)
    msg = (
        f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        f"Game {game_number}  :{suit_display}\n\n"
        f"En cours de vérification"
    )

    try:
        sent = await client.send_message(prediction_entity, msg)
        last_prediction_time = datetime.now()

        pending_predictions[game_number] = {
            'suit': suit,
            'triggered_by': triggered_by_suit,
            'message_id': sent.id,
            'status': 'en_cours',
            'awaiting_rattrapage': 0,
            'sent_time': datetime.now(),
        }

        add_prediction_to_history(game_number, suit, triggered_by_suit)

        # Si mode attente, verrouiller jusqu'à voir PERDU
        if attente_mode:
            attente_locked = True

        logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} (déclenché par absence {triggered_by_suit})")
        return sent.id

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
        return None
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
        return None
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    """Met à jour le message de prédiction avec le résultat."""
    global attente_locked

    if game_number not in pending_predictions:
        return

    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    suit_display = SUIT_DISPLAY.get(suit, suit)

    if status == '✅0️⃣':
        result_line = "Rattrapage :✅0️⃣"
    elif status == '✅1️⃣':
        result_line = "Rattrapage :✅1️⃣"
    elif status == '✅2️⃣':
        result_line = "Rattrapage :✅2️⃣"
    else:
        result_line = "Rattrapage : ❌PERDU"

    new_msg = (
        f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        f"Game {game_number}  :{suit_display}\n\n"
        f"{result_line}"
    )

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal prédiction inaccessible pour mise à jour")
            return

        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status

        status_key = 'gagne' if trouve else 'perdu'
        update_prediction_history_status(game_number, suit, status_key, game_number)

        if trouve:
            logger.info(f"✅ Gagné: #{game_number} {suit} ({status})")
        else:
            logger.info(f"❌ Perdu: #{game_number} {suit}")
            # Si mode attente → déverrouiller maintenant
            if attente_mode:
                attente_locked = False
                logger.info("🔓 Mode Attente: PERDU détecté → prêt pour prochaine prédiction")

        del pending_predictions[game_number]

    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# VÉRIFICATION DES RÉSULTATS
# ============================================================================

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    """Vérifie si un jeu correspond à une prédiction en attente."""
    suits_in_result = get_suits_in_group(first_group)

    # Vérification directe
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('awaiting_rattrapage', 0) == 0:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif #{game_number}: {target_suit} dans {suits_in_result}")

            if target_suit in suits_in_result:
                await update_prediction_message(game_number, '✅0️⃣', True)
                return True
            else:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"❌ #{game_number} échoué → attente rattrapage #{game_number + 1}")
                return False

    # Vérification rattrapages
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting > 0 and game_number == original_game + awaiting:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif R{awaiting} #{game_number}: {target_suit}")

            if target_suit in suits_in_result:
                status = f'✅{awaiting}️⃣'
                await update_prediction_message(original_game, status, True, awaiting)
                return True
            else:
                if awaiting < 2:
                    pred['awaiting_rattrapage'] = awaiting + 1
                    logger.info(f"❌ R{awaiting} échoué → attente R{awaiting+1} #{original_game + awaiting + 1}")
                    return False
                else:
                    logger.info(f"❌ R2 échoué → prédiction perdue")
                    await update_prediction_message(original_game, '❌', False)
                    return False

    return False

# ============================================================================
# COMPTEUR2 - Logique principale
# ============================================================================

def get_compteur2_status_text() -> str:
    status = "✅ ON" if compteur2_active else "❌ OFF"
    last_game_str = f"#{compteur2_last_game}" if compteur2_last_game else "Aucun"

    lines = [
        f"📊 Compteur2: {status} | B={compteur2_b}",
        f"🎮 Dernier jeu reçu: {last_game_str}",
        "",
        "Progression (absences):",
    ]

    for suit in ALL_SUITS:
        count = compteur2_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, compteur2_b - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        lines.append(f"{display} : {bar} {count}/{compteur2_b}")

    if attente_mode:
        attente_status = "🔒 Verrouillé (attend PERDU)" if attente_locked else "🔓 Prêt"
        lines.append(f"\n🕐 Mode Attente: ✅ ON | {attente_status}")
    else:
        lines.append(f"\n🕐 Mode Attente: ❌ OFF")

    return "\n".join(lines)

async def process_compteur2(game_number: int, suits_in_game: List[str]):
    """Traite le Compteur2 pour un jeu reçu."""
    global compteur2_absences, compteur2_last_game

    if not compteur2_active:
        return

    compteur2_last_game = game_number

    for suit in ALL_SUITS:
        if suit in suits_in_game:
            if compteur2_absences[suit] > 0:
                logger.info(f"📊 Compteur2 {suit}: reset (trouvé au jeu #{game_number})")
            compteur2_absences[suit] = 0
        else:
            compteur2_absences[suit] += 1
            count = compteur2_absences[suit]
            logger.info(f"📊 Compteur2 {suit}: absence {count}/{compteur2_b} au jeu #{game_number}")

            if count >= compteur2_b:
                # Calculer l'inverse
                inverse_suit = SUIT_INVERSE.get(suit, suit)
                pred_game = game_number + 1

                # Si mode attente et verrouillé → ne pas prédire
                if attente_mode and attente_locked:
                    logger.info(
                        f"🔒 Mode Attente verrouillé: B={compteur2_b} atteint pour {suit} "
                        f"→ prédiction {inverse_suit} ignorée (attend PERDU)"
                    )
                    compteur2_absences[suit] = 0
                    continue

                logger.info(
                    f"🔮 Compteur2: {suit} absent {compteur2_b}x → prédiction inverse "
                    f"{inverse_suit} pour #{pred_game}"
                )
                await send_prediction(pred_game, inverse_suit, suit)
                compteur2_absences[suit] = 0

# ============================================================================
# TRAITEMENT DES MESSAGES SOURCE
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    """Traite un résultat de jeu finalisé reçu du canal source."""
    global current_game_number

    current_game_number = game_number

    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupe trouvé dans #{game_number}")
        return

    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)

    logger.info(f"📊 Jeu #{game_number}: couleurs={suits_in_first}")

    # Vérifier les prédictions en attente
    await check_prediction_result(game_number, first_group)

    # Compteur2
    await process_compteur2(game_number, suits_in_first)

async def handle_message(event, is_edit: bool = False):
    """Gère les messages entrants et édités du canal source."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id

        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        normalized_source = normalize_channel_id(SOURCE_CHANNEL_ID)
        if chat_id != normalized_source:
            return

        message_text = event.message.message
        edit_info = " [ÉDITÉ]" if is_edit else ""
        logger.info(f"📨{edit_info} Msg {event.message.id}: {message_text[:60]}...")

        if not is_message_finalized(message_text):
            logger.info("⏳ Non finalisé ignoré")
            return

        match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:^|[^\d])(\d{3,4})(?:[^\d]|$)", message_text)

        if not match:
            logger.warning("⚠️ Numéro non trouvé dans le message")
            return

        game_number = int(match.group(1))
        await process_game_result(game_number, message_text)

    except Exception as e:
        logger.error(f"❌ Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def handle_new_message(event):
    await handle_message(event, False)

async def handle_edited_message(event):
    await handle_message(event, True)

# ============================================================================
# RESET AUTOMATIQUE
# ============================================================================

async def auto_reset_system():
    while True:
        try:
            now = datetime.now()
            if now.hour == 1 and now.minute == 0:
                logger.info("🕐 Reset automatique 1h00")
                await perform_full_reset("Reset automatique 1h00")
                await asyncio.sleep(60)
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time
    global compteur2_absences, compteur2_last_game, attente_locked

    stats = len(pending_predictions)
    pending_predictions.clear()
    last_prediction_time = None
    compteur2_absences = {suit: 0 for suit in ALL_SUITS}
    compteur2_last_game = 0
    attente_locked = False

    logger.info(f"🔄 {reason} - {stats} prédictions cleared")

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"🔄 **RESET SYSTÈME**\n\n{reason}\n\n"
                f"✅ Compteurs remis à zéro\n"
                f"✅ {stats} prédictions cleared\n\n"
                f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲"
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_compteur2(event):
    """Gère le Compteur2 - /compteur2 [on/off/b <val>/reset/status]."""
    global compteur2_active, compteur2_b, compteur2_absences, compteur2_last_game

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_compteur2_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur2_active = True
        compteur2_absences = {suit: 0 for suit in ALL_SUITS}
        await event.respond(
            f"✅ Compteur2 ACTIVÉ | B={compteur2_b}\n\n" + get_compteur2_status_text()
        )
        logger.info("Admin active Compteur2")

    elif arg == 'off':
        compteur2_active = False
        await event.respond("❌ Compteur2 DÉSACTIVÉ")
        logger.info("Admin désactive Compteur2")

    elif arg == 'reset':
        compteur2_absences = {suit: 0 for suit in ALL_SUITS}
        compteur2_last_game = 0
        await event.respond("🔄 Compteur2 remis à zéro\n\n" + get_compteur2_status_text())
        logger.info("Admin reset Compteur2")

    elif arg == 'b':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur2 b <valeur>` (ex: `/compteur2 b 4`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 20:
                await event.respond("❌ B doit être entre 1 et 20")
                return
            old_b = compteur2_b
            compteur2_b = val
            compteur2_absences = {suit: 0 for suit in ALL_SUITS}
            await event.respond(
                f"✅ Compteur2 B: {old_b} → {compteur2_b} | Compteurs remis à zéro\n\n"
                + get_compteur2_status_text()
            )
            logger.info(f"Admin change Compteur2 B={val}")
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur2 b 4`")
    else:
        await event.respond(
            "📊 **COMPTEUR2 - Aide**\n\n"
            "`/compteur2` — Afficher l'état\n"
            "`/compteur2 on` — Activer\n"
            "`/compteur2 off` — Désactiver\n"
            "`/compteur2 b <val>` — Changer le seuil B\n"
            "`/compteur2 reset` — Remettre les compteurs à zéro"
        )

async def cmd_attente(event):
    """Mode Attente - attend PERDU avant de prédire à nouveau.
    /attente [on/off/status]
    """
    global attente_mode, attente_locked

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        mode_str = "✅ ON" if attente_mode else "❌ OFF"
        lock_str = "🔒 Verrouillé (attend PERDU)" if (attente_mode and attente_locked) else "🔓 Prêt"
        await event.respond(
            f"🕐 **MODE ATTENTE**\n\n"
            f"Statut: {mode_str}\n"
            f"État: {lock_str}\n\n"
            f"**Fonctionnement:**\n"
            f"• Prédit l'inverse quand B absences atteint\n"
            f"• Après une prédiction, se verrouille\n"
            f"• Se déverrouille uniquement quand il voit ❌PERDU\n"
            f"• Sans PERDU → pas de nouvelle prédiction même si B atteint\n\n"
            f"`/attente on` — Activer\n"
            f"`/attente off` — Désactiver\n"
            f"`/attente reset` — Déverrouiller manuellement"
        )
        return

    arg = parts[1].lower()

    if arg == 'on':
        attente_mode = True
        attente_locked = False
        await event.respond(
            "✅ **Mode Attente ACTIVÉ**\n\n"
            "Le bot prédira une fois, puis attendra de voir ❌PERDU avant de prédire à nouveau.\n"
            "État actuel: 🔓 Prêt pour la prochaine prédiction."
        )
        logger.info("Admin active mode Attente")

    elif arg == 'off':
        attente_mode = False
        attente_locked = False
        await event.respond(
            "❌ **Mode Attente DÉSACTIVÉ**\n\n"
            "Le bot prédira normalement à chaque fois que B absences est atteint."
        )
        logger.info("Admin désactive mode Attente")

    elif arg == 'reset':
        attente_locked = False
        status = "✅ ON" if attente_mode else "❌ OFF"
        await event.respond(
            f"🔓 **Mode Attente déverrouillé manuellement**\n\n"
            f"Mode Attente: {status}\n"
            f"Le bot est prêt pour la prochaine prédiction."
        )
        logger.info("Admin déverrouille mode Attente")

    else:
        await event.respond(
            "🕐 **MODE ATTENTE - Aide**\n\n"
            "`/attente` — Voir le statut\n"
            "`/attente on` — Activer\n"
            "`/attente off` — Désactiver\n"
            "`/attente reset` — Déverrouiller manuellement"
        )

async def cmd_history(event):
    """Affiche l'historique de toutes les prédictions."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    if not prediction_history:
        await event.respond("📜 Aucune prédiction dans l'historique.")
        return

    lines = [
        "📜 **HISTORIQUE DES PRÉDICTIONS**",
        "═══════════════════════════════════════",
        ""
    ]

    for i, pred in enumerate(prediction_history[:20], 1):
        pred_game = pred['predicted_game']
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
        time_str = pred['predicted_at'].strftime('%H:%M:%S')
        silent_tag = " [Attente]" if pred.get('silent') else ""

        status = pred['status']
        if status == 'en_cours':
            status_str = "⏳ En cours..."
        elif status == 'gagne':
            status_str = "✅ GAGNÉ"
        elif status == 'perdu':
            status_str = "❌ PERDU"
        else:
            status_str = f"❓ {status}"

        lines.append(
            f"{i}. 🕐 `{time_str}` | **Game #{pred_game}** {suit}{silent_tag}\n"
            f"   📉 Déclenché par: {trig} absent {compteur2_b}x\n"
            f"   📊 Résultat: {status_str}"
        )
        lines.append("")

    # Prédictions actives
    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            ar = pred.get('awaiting_rattrapage', 0)
            if ar > 0:
                st = f"Attente R{ar} (#{num + ar})"
            else:
                st = "Vérification directe"
            lines.append(f"• Game #{num} {suit}: {st}")
        lines.append("")

    lines.append("═══════════════════════════════════════")
    await event.respond("\n".join(lines))

async def cmd_channels(event):
    """Vérifie la configuration et l'accès aux canaux."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    src_status = "❌"
    src_name = "Inaccessible"
    pred_status = "❌"
    pred_name = "Inaccessible"

    try:
        if SOURCE_CHANNEL_ID:
            src_entity = await resolve_channel(SOURCE_CHANNEL_ID)
            if src_entity:
                src_status = "✅"
                src_name = getattr(src_entity, 'title', 'Sans titre')
    except Exception as e:
        src_status = f"❌ ({str(e)[:30]})"

    try:
        if PREDICTION_CHANNEL_ID:
            pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
            if pred_entity:
                pred_status = "✅"
                pred_name = getattr(pred_entity, 'title', 'Sans titre')
    except Exception as e:
        pred_status = f"❌ ({str(e)[:30]})"

    await event.respond(
        f"📡 **CONFIGURATION DES CANAUX**\n\n"
        f"**Canal Source:**\n"
        f"ID: `{SOURCE_CHANNEL_ID}`\n"
        f"Status: {src_status}\n"
        f"Nom: {src_name}\n\n"
        f"**Canal Prédiction:**\n"
        f"ID: `{PREDICTION_CHANNEL_ID}`\n"
        f"Status: {pred_status}\n"
        f"Nom: {pred_name}\n\n"
        f"⚠️ Le bot doit être **administrateur** du canal prédiction!\n\n"
        f"**Paramètres:**\n"
        f"Compteur2 B={compteur2_b} | Actif: {'✅' if compteur2_active else '❌'}\n"
        f"Mode Attente: {'✅ ON' if attente_mode else '❌ OFF'}\n"
        f"Admin ID: `{ADMIN_ID}`"
    )

async def cmd_test(event):
    """Test d'envoi au canal de prédiction."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test de connexion au canal de prédiction...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré")
            return

        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(
                f"❌ **Canal inaccessible** `{PREDICTION_CHANNEL_ID}`\n\n"
                f"Vérifiez:\n"
                f"1. L'ID est correct (format: -100xxxxxxxxxx)\n"
                f"2. Le bot est administrateur du canal\n"
                f"3. Le bot a les permissions d'envoi"
            )
            return

        test_msg = (
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲 [TEST]\n"
            f"Game 9999  :♠️\n\n"
            f"En cours de vérification\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        sent = await client.send_message(prediction_entity, test_msg)
        await asyncio.sleep(2)

        await client.edit_message(
            prediction_entity, sent.id,
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲 [TEST]\n"
            f"Game 9999  :♠️\n\n"
            f"Rattrapage :✅0️⃣\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        await asyncio.sleep(2)
        await client.delete_messages(prediction_entity, [sent.id])

        pred_name_display = getattr(prediction_entity, 'title', str(prediction_entity.id))
        await event.respond(
            f"✅ **TEST RÉUSSI!**\n\n"
            f"Canal: `{pred_name_display}` (ID: {prediction_entity.id})\n"
            f"Envoi, modification et suppression: OK\n\n"
            f"Les prédictions fonctionneront correctement."
        )

    except ChatWriteForbiddenError:
        await event.respond(
            "❌ **Permission refusée**\n\n"
            "Le bot ne peut pas écrire dans le canal.\n"
            "→ Ajoutez le bot comme **administrateur** avec droit d'envoi."
        )
    except Exception as e:
        logger.error(f"Erreur test: {e}")
        await event.respond(f"❌ Échec du test: {e}")

async def cmd_reset(event):
    """Reset manuel complet."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteurs remis à zéro.")

async def cmd_status(event):
    """Affiche l'état complet du bot."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        get_compteur2_status_text(),
        "",
        f"🔮 Prédictions actives: {len(pending_predictions)}",
    ]

    if pending_predictions:
        lines.append("")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
            ar = pred.get('awaiting_rattrapage', 0)
            st = f"R{ar} en attente" if ar > 0 else "Vérification directe"
            lines.append(f"• Game #{num} {suit} (inverse de {trig}): {st}")

    await event.respond("\n".join(lines))

async def cmd_announce(event):
    """Annonce personnalisée dans le canal prédiction."""
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
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
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
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲"
        )
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_help(event):
    """Affiche l'aide complète."""
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "📖 **BACCARAT PREMIUM+2 - AIDE**\n\n"
        "**🎮 Système de prédiction (Compteur2):**\n"
        "• Compte les absences consécutives de chaque couleur\n"
        "• Quand une couleur atteint B absences → prédit l'**inverse**\n"
        "• ♠️↔♦️ | ❤️↔♣️\n\n"
        "**🕐 Mode Attente:**\n"
        "• Prédit une fois, puis attend de voir ❌PERDU\n"
        "• Tant que PERDU non vu → pas de nouvelle prédiction\n\n"
        "**🔧 Commandes Admin:**\n"
        "`/compteur2` — État et gestion du Compteur2\n"
        "`/compteur2 on/off` — Activer/désactiver\n"
        "`/compteur2 b <val>` — Changer le seuil B\n"
        "`/attente` — État du mode Attente\n"
        "`/attente on/off` — Activer/désactiver le mode Attente\n"
        "`/attente reset` — Déverrouiller manuellement\n"
        "`/status` — État complet du bot\n"
        "`/history` — Historique de toutes les prédictions\n"
        "`/channels` — Vérifier les canaux\n"
        "`/test` — Tester l'envoi au canal prédiction\n"
        "`/reset` — Reset complet\n"
        "`/announce <msg>` — Envoyer une annonce\n"
        "`/help` — Cette aide\n\n"
        "**📨 Format des prédictions:**\n"
        "```\n"
        "🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        "Game 4  :♠️\n\n"
        "En cours de vérification\n"
        "```\n"
        "→ Mis à jour avec: Rattrapage :✅0️⃣ / ✅1️⃣ / ✅2️⃣ / ❌PERDU"
    )

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_attente, events.NewMessage(pattern=r'^/attente'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))
    client.add_event_handler(handle_new_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    session = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal: {e}")

        logger.info(f"🤖 Bot démarré | Compteur2 B={compteur2_b} | Attente={'ON' if attente_mode else 'OFF'}")
        return True

    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        asyncio.create_task(auto_reset_system())
        logger.info("🔄 Auto-reset démarré")

        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT PREMIUM+2 🎲 Running"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()

        logger.info(f"🌐 Serveur web démarré sur port {PORT}")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
