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
    to a target channel + destination link. Example:

    {
      "jjk": {
        "channel_username": "@AnimeStreet_backup",
        "destination_link": "https://t.me/+3RnRB0avwhk1OTBl"
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

# SQLite file used to persist join requests across restarts.
# NOTE: on some hosts (see "Limitations" below) the filesystem is wiped on
# every redeploy, so this is not a substitute for a real external DB if you
# need requests to survive redeploys, not just process restarts/crashes.
DB_PATH = os.path.join(BASE_DIR, "join_requests.db")


def load_config(path: str) -> Dict[str, Any]:
    """
    Loads the campaign configuration from config.json exactly once at
    startup. Returns an empty dict (with a warning logged) if the file
    is missing or malformed, so the bot can still start and simply
    report "Invalid or expired link." for every deep link rather than
    crashing.
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
        if (
            isinstance(value, dict)
            and value.get("channel_username")
            and value.get("destination_link")
        ):
            validated[key] = value
        else:
            logger.warning(
                "Skipping campaign '%s': missing 'channel_username' or "
                "'destination_link'.",
                key,
            )

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


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def build_gate_keyboard(channel_url: str, campaign_key: str) -> InlineKeyboardMarkup:
    """
    Builds the two-button keyboard shown on /start:
        1. Join Channel (url button) - opens the channel so the user can
           submit a join request.
        2. Verify (callback button) - checks whether we've received a
           chat_join_request event for this user + channel.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="📢 Request to Join", url=channel_url)],
            [InlineKeyboardButton(text="✅ Verify", callback_data=f"verify:{campaign_key}")],
        ]
    )


WELCOME_TEXT = (
    "👋 Welcome!\n\n"
    "Tap *Request to Join* below and submit a join request in the channel, "
    "then come back and tap *Verify*.\n\n"
    "_You don't need to wait for your request to be approved - submitting "
    "the request is enough to verify here._"
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
    channel_username = campaign["channel_username"]
    channel_url = f"https://t.me/{channel_username.lstrip('@')}"

    keyboard = build_gate_keyboard(channel_url, param)

    await message.reply_text(
        text=WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


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

    channel_username = campaign["channel_username"]
    destination_link = campaign["destination_link"]
    channel_url = f"https://t.me/{channel_username.lstrip('@')}"

    channel_chat_id = await resolve_channel_chat_id(context, channel_username)

    verified = False
    if channel_chat_id is not None:
        verified = await has_join_request(context, user.id, channel_chat_id)
    else:
        # Fail safe: if we can't even resolve the channel, we can't verify.
        logger.error(
            "Skipping verification for user %s - channel '%s' could not be resolved.",
            user.id,
            channel_username,
        )

    if verified:
        try:
            await query.message.delete()
        except (BadRequest, Forbidden) as exc:
            logger.warning("Could not delete gate message for user %s: %s", user.id, exc)

        destination_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="Download", url=destination_link)]]
        )
        await context.bot.send_message(
            chat_id=user.id,
            text="✅ Verification successful! Tap below to continue:",
            reply_markup=destination_keyboard,
        )
    else:
        keyboard = build_gate_keyboard(channel_url, campaign_key)
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "❌ We haven't received a join request from you yet.\n\n"
                "Please tap *Request to Join*, submit the request in the "
                "channel, then come back and tap *Verify* again."
            ),
            parse_mode="Markdown",
            reply_markup=keyboard,
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
