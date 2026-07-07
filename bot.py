#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
bot.py

A Telegram deep-link gateway bot built with python-telegram-bot v22+.

This version verifies users via the "chat_join_request" update instead of
get_chat_member. It works with channels that have "Approve New Members"
(join requests) enabled: the user only needs to SUBMIT a join request,
not be approved as a full member, to pass verification.

Run locally or on Railway with:
    python bot.py

Environment variables required:
    BOT_TOKEN - your bot token from @BotFather

Configuration:
    config.json (same directory as this file) - maps deep-link parameters
    to a target channel + destination link. Two campaign shapes are
    supported:

    1) Public channel (has a @username) - the bot builds the "join" URL
       itself from channel_username:

    {
      "jjk": {
        "channel_username": "@AnimeStreet_backup",
        "destination_link": "https://t.me/+3RnRB0avwhk1OTBl"
      }
    }

    2) Private channel (no @username) - since there's no username to build
       a link from, you must supply the invite_link yourself, plus the
       numeric chat_id (used to match incoming chat_join_request updates).
       You can get the chat_id by adding the bot as admin and checking the
       logs after any user submits a join request there once, or via
       @userinfobot / @RawDataBot style utilities:

    {
      "nrt": {
        "chat_id": -1001234567890,
        "invite_link": "https://t.me/+abcDEFghiJKLmno",
        "destination_link": "https://t.me/+XXXXXXXXXXXX"
      }
    }

    3) Multiple channels behind one parameter - use "channels": [...]
       instead of putting channel fields directly on the campaign. The
       user must submit a join request to EVERY channel listed before
       Verify passes. Mixing public and private channels is fine, and an
       optional "label" controls the button text ("Channel N" if omitted):

    {
      "multi": {
        "channels": [
          { "chat_id": -1001111111111, "invite_link": "https://t.me/+aaa", "label": "Main Channel" },
          { "channel_username": "@PublicBackup", "label": "Public Backup" }
        ],
        "destination_link": "https://t.me/+XXXXXXXXXXXX"
      }
    }
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiosqlite
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load environment variables (.env locally; Railway injects real env vars
# directly, so load_dotenv() is a harmless no-op there if no .env exists)
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Read and validate BOT_TOKEN
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical(
        "BOT_TOKEN environment variable is not set. "
        "Set it locally in a .env file or in Railway's Variables tab."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths (same directory as this script)
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# The photo shown above the Join/Verify buttons on the gate message.
# This must be a Telegram file_id (a string Telegram gives back once a photo
# has been uploaded to it at least once) - see get_file_id_handler below for
# the easiest way to obtain one. Leave empty ("") to send text-only, no photo.
GATE_PHOTO_FILE_ID = os.environ.get("GATE_PHOTO_FILE_ID", "")

# SQLite file used to persist join requests across restarts.
# NOTE: on some hosts (see "Limitations" below) the filesystem is wiped on
# every redeploy, so this is not a substitute for a real external DB if you
# need requests to survive redeploys, not just process restarts/crashes.
DB_PATH = os.path.join(BASE_DIR, "join_requests.db")


def _validate_channel(channel: Any) -> Optional[Dict[str, Any]]:
    """
    Validates a single channel entry (one item of a campaign's "channels"
    list, or the legacy single-channel fields living directly on the
    campaign). Returns a normalized dict, or None if invalid.
    """
    if not isinstance(channel, dict):
        return None

    if channel.get("channel_username"):
        return {
            "channel_username": channel["channel_username"],
            "label": channel.get("label"),
        }

    if channel.get("chat_id") and channel.get("invite_link"):
        if not isinstance(channel["chat_id"], int):
            logger.warning(
                "'chat_id' must be a JSON integer (e.g. -1001234567890), not a string."
            )
            return None
        return {
            "chat_id": channel["chat_id"],
            "invite_link": channel["invite_link"],
            "label": channel.get("label"),
        }

    return None


def load_config(path: str) -> Dict[str, Any]:
    """
    Loads the campaign configuration from config.json exactly once at
    startup. Returns an empty dict (with a warning logged) if the file
    is missing or malformed, so the bot can still start and simply
    report "Invalid or expired link." for every deep link rather than
    crashing.

    Every campaign is normalized internally to:
        {"channels": [ {...channel...}, ... ], "destination_link": "..."}
    regardless of whether config.json used the legacy single-channel shape
    (channel_username / chat_id+invite_link directly on the campaign) or the
    newer "channels": [...] list for gating behind multiple channels at once.
    """
    if not os.path.exists(path):
        logger.warning(
            "config.json not found at '%s'. The bot will start, but every "
            "deep link will be treated as invalid until the file is added.",
            path,
        )
        return {}

    try:
        with open(path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except json.JSONDecodeError as exc:
        logger.error("config.json contains invalid JSON: %s", exc)
        return {}
    except OSError as exc:
        logger.error("Failed to read config.json: %s", exc)
        return {}

    if not isinstance(data, dict):
        logger.error("config.json must contain a top-level JSON object. Ignoring file.")
        return {}

    validated: Dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(value, dict) or not value.get("destination_link"):
            logger.warning(
                "Skipping campaign '%s': missing 'destination_link'.", key
            )
            continue

        # Accept either the new "channels": [...] list, or fall back to
        # treating the campaign's own top-level fields as a single channel
        # (legacy shape, still fully supported).
        raw_channels = value.get("channels", [value])
        if not isinstance(raw_channels, list) or not raw_channels:
            logger.warning(
                "Skipping campaign '%s': 'channels' must be a non-empty list.", key
            )
            continue

        channels = []
        campaign_invalid = False
        for i, raw_channel in enumerate(raw_channels, start=1):
            validated_channel = _validate_channel(raw_channel)
            if validated_channel is None:
                logger.warning(
                    "Skipping campaign '%s': channel #%d is invalid - each "
                    "channel needs either 'channel_username' (public) or "
                    "both 'chat_id' (int) and 'invite_link' (private).",
                    key,
                    i,
                )
                campaign_invalid = True
                break
            validated_channel.setdefault("label", f"Channel {i}")
            channels.append(validated_channel)

        if campaign_invalid:
            continue

        validated[key] = {"channels": channels, "destination_link": value["destination_link"]}

    logger.info("Loaded %d campaign(s) from config.json.", len(validated))
    return validated


# Config is loaded once at startup and kept in memory for the lifetime
# of the process. Restart the bot to pick up changes to config.json.
CAMPAIGNS: Dict[str, Any] = load_config(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Persistence layer: pending join requests
# ---------------------------------------------------------------------------
# We store (user_id, chat_id) pairs the moment Telegram tells us a user has
# submitted a join request. A single shared aiosqlite connection + lock is
# kept on `application.bot_data` for the life of the process, so every
# handler call reuses the same connection instead of opening a new file
# handle per request (this also avoids "database is locked" errors under
# concurrent writes from multiple users).

async def init_db(application: Application) -> None:
    db = await aiosqlite.connect(DB_PATH)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS join_requests (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            requested_at TEXT NOT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
        """
    )
    await db.commit()
    application.bot_data["db"] = db
    application.bot_data["db_lock"] = asyncio.Lock()
    application.bot_data["channel_id_cache"] = {}
    logger.info("Join-request database ready at %s", DB_PATH)


async def close_db(application: Application) -> None:
    db = application.bot_data.get("db")
    if db is not None:
        await db.close()


async def record_join_request(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> None:
    db: aiosqlite.Connection = context.application.bot_data["db"]
    lock: asyncio.Lock = context.application.bot_data["db_lock"]
    async with lock:
        await db.execute(
            "INSERT OR REPLACE INTO join_requests (user_id, chat_id, requested_at) VALUES (?, ?, ?)",
            (user_id, chat_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def has_join_request(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> bool:
    db: aiosqlite.Connection = context.application.bot_data["db"]
    lock: asyncio.Lock = context.application.bot_data["db_lock"]
    async with lock:
        cursor = await db.execute(
            "SELECT 1 FROM join_requests WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
    return row is not None


async def resolve_channel_chat_id(
    context: ContextTypes.DEFAULT_TYPE, channel_username: str
) -> Optional[int]:
    """
    Resolves a config channel_username (e.g. "@AnimeStreet_backup") to its
    numeric chat_id, using get_chat, and caches the result for the life of
    the process. We need the numeric chat_id because that's what arrives on
    the chat_join_request update (update.chat_join_request.chat.id) - matching
    on numeric ID is more robust than string-matching usernames.

    Returns None if the channel can't be resolved (e.g. bot isn't an admin,
    or the username is wrong) - callers should fail safe in that case.
    """
    cache: Dict[str, int] = context.application.bot_data["channel_id_cache"]
    key = channel_username.lower()
    if key in cache:
        return cache[key]

    try:
        chat = await context.bot.get_chat(chat_id=channel_username)
        cache[key] = chat.id
        return chat.id
    except (BadRequest, Forbidden) as exc:
        logger.error(
            "Could not resolve channel '%s' to a chat_id: %s. "
            "Make sure the bot is an ADMIN of this channel and the "
            "username in config.json is correct.",
            channel_username,
            exc,
        )
        return None
    except TelegramError as exc:
        logger.error("Telegram error resolving channel '%s': %s", channel_username, exc)
        return None


async def get_channel_chat_id(
    context: ContextTypes.DEFAULT_TYPE, channel: Dict[str, Any]
) -> Optional[int]:
    """
    Returns the numeric chat_id to match join requests against, for a
    single channel entry:
      - Public (channel_username): resolved via get_chat (and cached).
      - Private (chat_id given directly in config.json): returned as-is,
        no API call needed since there's no username to look up.
    """
    if channel.get("channel_username"):
        return await resolve_channel_chat_id(context, channel["channel_username"])
    # Private-channel shape already has the numeric chat_id in config.
    return channel.get("chat_id")


def get_channel_join_url(channel: Dict[str, Any]) -> str:
    """
    Returns the URL the "Request to Join" button should open, for a single
    channel entry:
      - Public (channel_username): built as https://t.me/<username>.
      - Private (invite_link given directly in config.json): used as-is,
        since a private channel has no username to build a link from.
    """
    if channel.get("channel_username"):
        return f"https://t.me/{channel['channel_username'].lstrip('@')}"
    return channel["invite_link"]


async def check_campaign_verified(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, campaign: Dict[str, Any]
) -> bool:
    """
    A campaign is verified only if the user has a recorded join request for
    EVERY channel in campaign["channels"]. Short-circuits (and fails safe)
    on the first channel that's missing a request or can't be resolved.
    """
    for channel in campaign["channels"]:
        chat_id = await get_channel_chat_id(context, channel)
        if chat_id is None:
            logger.error(
                "Skipping verification for user %s - channel '%s' could not "
                "be resolved to a chat_id.",
                user_id,
                channel.get("channel_username") or channel.get("chat_id"),
            )
            return False
        if not await has_join_request(context, user_id, chat_id):
            return False
    return True


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def build_gate_keyboard(
    channels: list, campaign_key: str
) -> InlineKeyboardMarkup:
    """
    Builds the gate keyboard shown on /start: every "Join <label>" button
    plus the Verify button, packed together 2-per-row (in that order, so
    Verify slots in next to the last join button if the channel count is
    odd, rather than always getting its own row).
    """
    join_buttons = [
        InlineKeyboardButton(
            text=f"📢 Join {channel.get('label', 'Channel')}",
            url=get_channel_join_url(channel),
        )
        for channel in channels
    ]
    verify_button = InlineKeyboardButton(
        text="✅ Verify", callback_data=f"verify:{campaign_key}"
    )
    all_buttons = join_buttons + [verify_button]
    rows = [all_buttons[i:i + 2] for i in range(0, len(all_buttons), 2)]
    return InlineKeyboardMarkup(rows)


WELCOME_TEXT = (
    "<blockquote>Join the following channels to continue</blockquote>\n\n"
)


async def send_gate_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, keyboard: InlineKeyboardMarkup
):
    """
    Sends the gate message: the configured photo (via GATE_PHOTO_FILE_ID)
    with WELCOME_TEXT as its caption, plus the Join/Verify keyboard.
    Falls back to a plain text message if no photo is configured, or if
    Telegram rejects the file_id (e.g. it was typed wrong or has expired).

    Returns the sent Message object so callers can track its message_id
    for later cleanup (see _track_gate_message / _cleanup_gate_messages).
    """
    if GATE_PHOTO_FILE_ID:
        try:
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=GATE_PHOTO_FILE_ID,
                caption=WELCOME_TEXT,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except (BadRequest, Forbidden) as exc:
            logger.error(
                "Could not send gate photo (file_id=%s): %s. Falling back "
                "to text-only.",
                GATE_PHOTO_FILE_ID,
                exc,
            )

    return await context.bot.send_message(
        chat_id=chat_id,
        text=WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _track_gate_message(
    context: ContextTypes.DEFAULT_TYPE, campaign_key: str, message_id: int
) -> None:
    """
    Remembers a message_id (gate message, failed-verification notice, or
    the user's own /start command) so it can be deleted in bulk once the
    user successfully verifies for this campaign.
    """
    pending: Dict[str, list] = context.user_data.setdefault("pending_gate_messages", {})
    pending.setdefault(campaign_key, []).append(message_id)


async def _cleanup_gate_messages(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, campaign_key: str
) -> None:
    """
    Deletes every message tracked for this campaign (the user's /start,
    every gate message shown, and every failed-verification notice sent),
    then clears the tracking list. Each delete is attempted independently
    so one failure (e.g. a message older than 48h) doesn't block the rest.
    """
    pending: Dict[str, list] = context.user_data.get("pending_gate_messages", {})
    message_ids = pending.pop(campaign_key, [])
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except (BadRequest, Forbidden) as exc:
            logger.warning(
                "Could not delete message %s in chat %s: %s", message_id, chat_id, exc
            )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the /start command, including deep-link parameters such as
    /start jjk (sent by Telegram when a user opens
    https://t.me/YourBot?start=jjk).
    """
    message = update.message
    if message is None:
        return

    args = context.args
    param = args[0].strip().lower() if args else None

    if not param or param not in CAMPAIGNS:
        logger.info("Invalid or missing deep-link parameter: %r", param)
        await message.reply_text("Invalid or expired link.")
        return

    campaign = CAMPAIGNS[param]
    keyboard = build_gate_keyboard(campaign["channels"], param)

    # Track the user's own /start message so it can be deleted along with
    # the gate message(s) once they successfully verify.
    _track_gate_message(context, param, message.message_id)

    gate_message = await send_gate_message(context, chat_id=message.chat_id, keyboard=keyboard)
    _track_gate_message(context, param, gate_message.message_id)


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires on every "chat_join_request" update Telegram sends us - i.e. the
    instant a user submits a join request in a channel/group where the bot
    is an admin. We record (user_id, chat_id) immediately; we do NOT
    approve or decline the request here, so the user remains "pending" in
    the channel exactly as before - we're only using this event as a signal.
    """
    jr = update.chat_join_request
    if jr is None:
        return

    user_id = jr.from_user.id
    chat_id = jr.chat.id

    await record_join_request(context, user_id, chat_id)
    logger.info(
        "Recorded join request: user_id=%s chat_id=%s (@%s)",
        user_id,
        chat_id,
        jr.chat.username,
    )


async def continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles taps on the "✅ Verify" button.

    callback_data is "verify:<campaign_key>". Instead of calling
    get_chat_member, we look up whether we've already recorded a
    chat_join_request event for this user + the campaign's channel:
        - If found -> treat as verified, delete the gate message, and
          send the destination link.
        - If not found -> tell the user to submit a join request first
          and re-show the gate message.
    """
    query = update.callback_query
    user = update.effective_user

    # Acknowledge immediately so Telegram doesn't show a loading spinner.
    await query.answer()

    try:
        _, campaign_key = query.data.split(":", maxsplit=1)
    except (ValueError, AttributeError):
        logger.error("Malformed callback_data: %r", query.data)
        await query.answer(text="Something went wrong. Please use the link again.", show_alert=True)
        return

    campaign = CAMPAIGNS.get(campaign_key)
    if campaign is None:
        await context.bot.send_message(chat_id=user.id, text="Invalid or expired link.")
        return

    destination_link = campaign["destination_link"]
    verified = await check_campaign_verified(context, user.id, campaign)

    if verified:
        # Deletes the user's original /start message, every gate message
        # shown so far, and every failed-verification notice sent for this
        # campaign - all in one go.
        await _cleanup_gate_messages(context, chat_id=user.id, campaign_key=campaign_key)

        destination_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="Download", url=destination_link)]]
        )
        await context.bot.send_message(
            chat_id=user.id,
            text="✅ Verification successful! Tap below to continue:",
            reply_markup=destination_keyboard,
        )
    else:
        keyboard = build_gate_keyboard(campaign["channels"], campaign_key)

        failure_message = await context.bot.send_message(
            chat_id=user.id,
            text="We prefer you joining all the channels to get the link",
        )
        _track_gate_message(context, campaign_key, failure_message.message_id)

        gate_message = await send_gate_message(context, chat_id=user.id, keyboard=keyboard)
        _track_gate_message(context, campaign_key, gate_message.message_id)


async def get_file_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Utility handler: send any photo directly to the bot in a private chat,
    and it replies with that photo's file_id - the value to put in the
    GATE_PHOTO_FILE_ID environment variable.

    Telegram stores several sizes per photo; update.message.photo[-1] is
    always the largest/highest-resolution one, which is what you want here.
    """
    message = update.message
    if message is None or not message.photo:
        return
    file_id = message.photo[-1].file_id
    await message.reply_text(
        f"file_id:\n<code>{file_id}</code>\n\n"
        "Set this as GATE_PHOTO_FILE_ID in your environment variables.",
        parse_mode="HTML",
    )


async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches any message that isn't recognized as a known command
    (registered as a fallback MessageHandler with filters.COMMAND).
    """
    message = update.message
    if message is None:
        return
    await message.reply_text("Unknown command. Please use a valid deep link to get started.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler. Logs unhandled exceptions with traceback so
    they're visible in Railway's log viewer, without crashing the bot.
    """
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


async def post_init(application: Application) -> None:
    await init_db(application)


async def post_shutdown(application: Application) -> None:
    await close_db(application)


def main() -> None:
    """
    Builds the Application, registers all handlers, and starts polling.
    """
    logger.info("Starting bot...")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /start (with or without a deep-link parameter)
    application.add_handler(CommandHandler("start", start_handler))

    # Utility: send any photo directly to the bot to get back its file_id,
    # for use as GATE_PHOTO_FILE_ID. Safe to remove once you have the ID(s)
    # you need, though harmless to leave in.
    application.add_handler(MessageHandler(filters.PHOTO, get_file_id_handler))

    # Fires the instant a user submits a join request in any chat where the
    # bot is an admin. This MUST also be included in allowed_updates below,
    # or Telegram will silently withhold these updates.
    application.add_handler(ChatJoinRequestHandler(join_request_handler))

    # "Verify" button -> callback_data starts with "verify:"
    application.add_handler(CallbackQueryHandler(continue_callback, pattern=r"^verify:"))

    # Fallback for any other command the bot doesn't explicitly handle.
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))

    # Global error handler for unhandled exceptions in any handler above.
    application.add_error_handler(error_handler)

    logger.info("Bot is running. Press Ctrl+C to stop.")

    # "chat_join_request" must be explicitly listed - it is NOT included in
    # Telegram's default update set, so omitting it here means the handler
    # above would simply never fire.
    application.run_polling(
        allowed_updates=["message", "callback_query", "chat_join_request"]
    )


if __name__ == "__main__":
    main()
