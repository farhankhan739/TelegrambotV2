#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
bot.py

A Telegram deep-link gateway bot built with python-telegram-bot v22+.

Run locally or on Railway with:
    python bot.py

Environment variables required:
    BOT_TOKEN - your bot token from @BotFather

Configuration:
    config.json (same directory as this file) - maps deep-link parameters
    to a target channel + destination link. Each campaign can choose ONE
    of two verification modes:

    Mode "membership" (Config Style A is NOT this - see below, this is
    the default / backward-compatible mode):
        The bot calls get_chat_member and only unlocks the destination
        link once the user is an actual MEMBER/ADMIN/OWNER of the
        channel. This is the original behaviour of this bot.

    Mode "join_request":
        The bot listens for Telegram's native "join request" events
        (used when a channel's invite link has "Approve new members"
        turned on). As soon as a user taps Join and their request comes
        in, the bot records it - the user does NOT need to wait for
        anyone to approve them, and does NOT need to already show up
        as a member. This is useful for private channels / campaigns
        where you want a fast, request-based unlock instead of a full
        membership check.

    Example config.json:

    {
      "jjk": {
        "channel_username": "@AnimeStreet_backup",
        "destination_link": "https://t.me/+3RnRB0avwhk1OTBl",
        "verification_mode": "membership"
      },
      "ddd": {
        "channel_username": "@AnimeStreet_backup",
        "destination_link": "https://t.me/+Dy-rWieJUOI3Y2E9",
        "verification_mode": "join_request",
        "auto_approve": true
      }
    }

    Field reference:
        channel_username   Required. Public @username of the channel, OR
                            the username to match against for join
                            requests. Include the "@".
        channel_id         Optional. Numeric chat id of the channel.
                            Recommended (and required for PRIVATE
                            channels with no public username) since
                            get_chat_member / approve_chat_join_request
                            need a real chat id or public username to
                            work reliably.
        channel_url        Optional. Overrides the URL used for the
                            "Join Channel" button. If omitted, it's
                            derived from channel_username
                            (https://t.me/<username>). Set this
                            explicitly for private channels where you
                            need to share the actual invite link.
        destination_link   Required. The link revealed after
                            verification succeeds.
        verification_mode  Optional. "membership" (default) or
                            "join_request".
        auto_approve       Optional, only used when verification_mode
                            is "join_request". Defaults to true - the
                            bot immediately approves the join request
                            (via approve_chat_join_request) so the user
                            also lands in the channel. Set to false if
                            you'd rather approve requests manually and
                            just use the request itself as the unlock
                            signal.

    IMPORTANT for "join_request" mode: the bot must be an ADMINISTRATOR
    of the channel with the "Add New Admins"-adjacent permission that
    lets it manage/approve join requests (in Telegram this is bundled
    under the channel admin's "Invite Users via Link" permission).
"""

import json
import logging
import os
import sys
from typing import Any, Dict, Optional, Set, Union

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
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
    # Exit immediately - there is no point starting the bot without a token.
    sys.exit(1)

# ---------------------------------------------------------------------------
# Path to the campaign configuration file (same directory as this script)
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Verification mode constants
MODE_MEMBERSHIP = "membership"
MODE_JOIN_REQUEST = "join_request"
VALID_MODES = {MODE_MEMBERSHIP, MODE_JOIN_REQUEST}


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

    # Validate each campaign entry; skip (and warn about) malformed ones
    # instead of letting one bad entry break the whole config.
    validated: Dict[str, Any] = {}
    for key, value in data.items():
        if not (
            isinstance(value, dict)
            and value.get("channel_username")
            and value.get("destination_link")
        ):
            logger.warning(
                "Skipping campaign '%s': missing 'channel_username' or "
                "'destination_link'.",
                key,
            )
            continue

        mode = value.get("verification_mode", MODE_MEMBERSHIP)
        if mode not in VALID_MODES:
            logger.warning(
                "Campaign '%s' has invalid verification_mode '%s'. "
                "Falling back to '%s'.",
                key,
                mode,
                MODE_MEMBERSHIP,
            )
            mode = MODE_MEMBERSHIP
        value["verification_mode"] = mode

        # auto_approve only matters for join_request mode; default True.
        value["auto_approve"] = bool(value.get("auto_approve", True))

        # channel_id, if present, must be an int (Telegram chat ids).
        if "channel_id" in value and value["channel_id"] is not None:
            try:
                value["channel_id"] = int(value["channel_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "Campaign '%s' has a non-numeric channel_id (%r). Ignoring it.",
                    key,
                    value["channel_id"],
                )
                value["channel_id"] = None

        validated[key] = value

    logger.info("Loaded %d campaign(s) from config.json.", len(validated))
    return validated


# Config is loaded once at startup and kept in memory for the lifetime
# of the process. Restart the bot to pick up changes to config.json.
CAMPAIGNS: Dict[str, Any] = load_config(CONFIG_PATH)


# ---------------------------------------------------------------------------
# In-memory store of join requests we've seen, for campaigns running in
# "join_request" mode. NOTE: this resets whenever the process restarts
# (e.g. on a Railway redeploy). That's fine for most gateway use-cases
# since users can just tap Join again, but if you need it to survive
# restarts you'd want to persist this to a file or small database.
#
# Keyed by a normalized "channel key" (see channel_keys_for_campaign)
# -> set of user_ids who have an active join request recorded.
# ---------------------------------------------------------------------------
JOIN_REQUESTS: Dict[str, Set[int]] = {}


def channel_keys_for_campaign(campaign: Dict[str, Any]) -> Set[str]:
    """
    Returns the set of normalized keys that identify a campaign's channel,
    used to look up / record entries in JOIN_REQUESTS. We track both the
    numeric channel_id (if configured) and the @username so a join
    request event matches regardless of which one Telegram gives us.
    """
    keys: Set[str] = set()
    channel_id = campaign.get("channel_id")
    if channel_id is not None:
        keys.add(f"id:{channel_id}")
    channel_username = campaign.get("channel_username")
    if channel_username:
        keys.add(f"username:{channel_username.lstrip('@').lower()}")
    return keys


def channel_ref_for_campaign(campaign: Dict[str, Any]) -> Union[int, str]:
    """
    Returns whatever should be passed as chat_id to Telegram API calls
    (get_chat_member, approve_chat_join_request): prefer the numeric
    channel_id if configured (works for private channels too), else
    fall back to the @username.
    """
    channel_id = campaign.get("channel_id")
    if channel_id is not None:
        return channel_id
    return campaign["channel_username"]


# Chat member statuses that count as "is a member" for our purposes.
# Excludes LEFT and BANNED/KICKED.
MEMBER_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}


async def check_membership(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_ref: Union[int, str]
) -> bool:
    """
    Checks whether a user is currently a member of chat_ref (a channel
    username like "@Foo" or a numeric channel id).

    IMPORTANT: the bot must be an ADMINISTRATOR of the target channel for
    get_chat_member to succeed. If it isn't (or the channel is invalid),
    Telegram raises an error, which we catch and treat as "not a member"
    so the gateway fails safe instead of crashing.
    """
    try:
        member = await context.bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
        return member.status in MEMBER_STATUSES
    except Forbidden as exc:
        logger.error(
            "Forbidden checking membership in %s: %s. "
            "Make sure the bot is an ADMIN of this channel.",
            chat_ref,
            exc,
        )
        return False
    except BadRequest as exc:
        logger.error("BadRequest checking membership in %s: %s", chat_ref, exc)
        return False
    except TelegramError as exc:
        logger.error("Telegram error checking membership in %s: %s", chat_ref, exc)
        return False


def has_pending_join_request(user_id: int, campaign: Dict[str, Any]) -> bool:
    """
    Mode "join_request" check: True if we've recorded a join-request
    event from this user for this campaign's channel.
    """
    for key in channel_keys_for_campaign(campaign):
        if user_id in JOIN_REQUESTS.get(key, set()):
            return True
    return False


async def is_verified(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, campaign: Dict[str, Any]
) -> bool:
    """
    Single entry point used by the "Continue" button to decide whether a
    user has satisfied a campaign's requirement. Dispatches based on the
    campaign's configured verification_mode:

        "join_request" -> has the user sent a join request we recorded?
        "membership"   -> (default) is the user currently a channel member?
    """
    mode = campaign.get("verification_mode", MODE_MEMBERSHIP)
    if mode == MODE_JOIN_REQUEST:
        return has_pending_join_request(user_id, campaign)

    chat_ref = channel_ref_for_campaign(campaign)
    return await check_membership(context, user_id, chat_ref)


def build_gate_keyboard(channel_url: str, campaign_key: str) -> InlineKeyboardMarkup:
    """
    Builds the two-button keyboard shown on /start:
        1. Join Channel (url button)
        2. Continue (callback button - triggers a membership check)
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="📢 Join Channel", url=channel_url)],
            [InlineKeyboardButton(text="➡️ Continue", callback_data=f"verify:{campaign_key}")],
        ]
    )


def channel_url_for_campaign(campaign: Dict[str, Any]) -> str:
    """
    Returns the URL to use for the "Join Channel" button: an explicit
    channel_url override if configured, otherwise derived from
    channel_username.
    """
    if campaign.get("channel_url"):
        return campaign["channel_url"]
    return f"https://t.me/{campaign['channel_username'].lstrip('@')}"


WELCOME_TEXT = (
    "👋 Welcome!\n\n"
    "Please join our channel first, then tap *Continue* to proceed."
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

    # context.args contains whatever follows "/start ", split by spaces.
    # e.g. "/start jjk" -> context.args == ["jjk"]
    args = context.args
    param = args[0].strip().lower() if args else None

    # No parameter, or parameter not found in our loaded config.
    if not param or param not in CAMPAIGNS:
        logger.info("Invalid or missing deep-link parameter: %r", param)
        await message.reply_text("Invalid or expired link.")
        return

    campaign = CAMPAIGNS[param]
    channel_url = channel_url_for_campaign(campaign)
    keyboard = build_gate_keyboard(channel_url, param)

    await message.reply_text(
        text=WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles taps on the "➡️ Continue" button.

    callback_data is "verify:<campaign_key>". Depending on the
    campaign's verification_mode, we either check current channel
    membership or check whether we've recorded a join request:
        - If verified -> delete the gate message (it "vanishes") and
          send a NEW message with the destination link as a button.
        - If not -> re-send (forward again, as a new message, not an
          edit) the same welcome message with the same two buttons so
          they can try again.
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
    channel_url = channel_url_for_campaign(campaign)

    verified = await is_verified(context, user.id, campaign)

    if verified:
        # User has joined (or requested to join) - delete the gate
        # message so it vanishes from the chat, then send a new message
        # with the destination link.
        try:
            await query.message.delete()
        except (BadRequest, Forbidden) as exc:
            # Not fatal - e.g. message already deleted, or too old to
            # delete. Log it and continue to deliver the destination link.
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
        # User hasn't joined / requested to join yet - forward (re-send)
        # the same welcome message with the same two buttons, as a
        # brand-new message.
        keyboard = build_gate_keyboard(channel_url, campaign_key)
        await context.bot.send_message(
            chat_id=user.id,
            text=WELCOME_TEXT,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


async def chat_join_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires whenever someone taps Join on a channel invite link that has
    "Approve new members" turned on, for ANY channel the bot administers.

    We record the request (keyed by both the channel's numeric id and
    its @username, so it matches campaigns configured either way), then
    - if at least one "join_request"-mode campaign for that channel has
    auto_approve enabled - immediately approve it so the user actually
    lands in the channel too.
    """
    request = update.chat_join_request
    if request is None:
        return

    user_id = request.from_user.id
    chat = request.chat

    id_key = f"id:{chat.id}"
    username_key = f"username:{chat.username.lower()}" if chat.username else None

    JOIN_REQUESTS.setdefault(id_key, set()).add(user_id)
    if username_key:
        JOIN_REQUESTS.setdefault(username_key, set()).add(user_id)

    logger.info("Recorded join request from user %s for chat %s", user_id, chat.id)

    # Auto-approve if any campaign pointing at this channel wants it.
    should_auto_approve = any(
        campaign.get("verification_mode") == MODE_JOIN_REQUEST
        and campaign.get("auto_approve", True)
        and (id_key in channel_keys_for_campaign(campaign) or username_key in channel_keys_for_campaign(campaign))
        for campaign in CAMPAIGNS.values()
    )

    if should_auto_approve:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=user_id)
            logger.info("Auto-approved join request for user %s in chat %s", user_id, chat.id)
        except Forbidden as exc:
            logger.error(
                "Forbidden approving join request in %s: %s. "
                "Make sure the bot is an ADMIN with permission to approve join requests.",
                chat.id,
                exc,
            )
        except TelegramError as exc:
            logger.error("Telegram error approving join request in %s: %s", chat.id, exc)


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


def main() -> None:
    """
    Builds the Application, registers all handlers, and starts polling.
    """
    logger.info("Starting bot...")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start (with or without a deep-link parameter)
    application.add_handler(CommandHandler("start", start_handler))

    # "Continue" button -> callback_data starts with "verify:"
    application.add_handler(CallbackQueryHandler(continue_callback, pattern=r"^verify:"))

    # Join-request events (only needed for campaigns using
    # verification_mode "join_request", but harmless to register always).
    application.add_handler(ChatJoinRequestHandler(chat_join_request_callback))

    # Fallback for any other command the bot doesn't explicitly handle.
    # filters.COMMAND matches any message starting with "/".
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))

    # Global error handler for unhandled exceptions in any handler above.
    application.add_error_handler(error_handler)

    logger.info("Bot is running. Press Ctrl+C to stop.")

    # Long polling - no webhook server, no open port required. This is
    # exactly what's needed for a Railway "Worker" style deployment.
    application.run_polling(
        allowed_updates=["message", "callback_query", "chat_join_request"]
    )


if __name__ == "__main__":
    main()
