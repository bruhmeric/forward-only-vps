"""Admin commands: /setgroup, /refresh, /addtopic, /help, /status."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Telegram clients (mobile, desktop) aggressively auto-correct hyphens to
# "nicer-looking" Unicode variants when the user types a leading minus sign.
# This breaks int() parsing silently from the user's perspective because the
# ValueError handler used to just say "group_id must be an integer" — and if
# the message delivery failed or Render was asleep, the user saw nothing.
#
# Map all common hyphen/dash variants back to ASCII '-' so int() works.
_HYPHEN_REPLACEMENTS = {
    "\u2010": "-",  # HYPHEN
    "\u2011": "-",  # NON-BREAKING HYPHEN
    "\u2012": "-",  # FIGURE DASH
    "\u2013": "-",  # EN DASH
    "\u2014": "-",  # EM DASH
    "\u2015": "-",  # HORIZONTAL BAR
    "\u2212": "-",  # MINUS SIGN (math)
    "\uFE63": "-",  # SMALL HYPHEN-MINUS
    "\uFF0D": "-",  # FULL-WIDTH HYPHEN-MINUS
}


def _normalize_int_str(s: str) -> str:
    """Normalize a string that should represent an integer — strips whitespace,
    replaces Unicode hyphen variants with ASCII '-', and removes any
    thousands separators (commas, spaces between digits)."""
    if not s:
        return s
    # Replace Unicode hyphens
    for bad, good in _HYPHEN_REPLACEMENTS.items():
        s = s.replace(bad, good)
    # Strip whitespace
    s = s.strip()
    # Remove thousand separators ONLY between digits (e.g. "-100 123 456" -> "-100123456")
    s = re.sub(r"(?<=\d)[\s,](?=\d)", "", s)
    return s


def _safe_int(value: str) -> int | None:
    """Try to parse `value` as an int, normalizing Unicode hyphens first.
    Returns None if parsing fails."""
    if value is None:
        return None
    try:
        return int(_normalize_int_str(value))
    except (ValueError, TypeError):
        return None


def _is_authorized(cfg, user_id: int) -> bool:
    """Check if user_id is in the admin whitelist. If the whitelist is empty,
    everyone is allowed (single-user self-hosted bot)."""
    return cfg.is_admin(user_id)


async def _deny_silent(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        reason: str) -> None:
    """Log a denied command attempt and (optionally) notify the user.

    For commands like /setgroup that the user EXPECTS to work, we want to
    give them feedback so they don't think the bot is broken. But for
    security, we don't want to leak "you are not authorized" to random
    users probing the bot — we just stay silent.

    Compromise: if ADMIN_IDS is set AND the user is NOT in it, log the
    attempt but don't reply. This is what the original code did.
    If ADMIN_IDS is empty (everyone allowed), this function is never called.
    """
    user = update.effective_user
    logger.warning("Unauthorized /%s attempt by user_id=%s username=%s: %s",
                    reason, user.id if user else '?',
                    getattr(user, 'username', None) if user else '?', reason)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hi! I forward whatever you send me to a topic in your group.\n\n"
        "Setup:\n"
        "1. /setgroup <group_id>  — set the destination group\n"
        "2. /refresh              — discover forum topics (needs Telethon session)\n"
        "   or /addtopic <title> <topic_id>  — add a topic manually\n"
        "\nThen just send me anything — I'll show you a topic picker."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "*Commands*\n"
        "/setgroup <id>  — set destination group/channel (forum OR non-forum)\n"
        "/info           — show destination chat info (is it a forum? title?)\n"
        "/refresh        — re-fetch forum topics via Telethon (forum only)\n"
        "/topics         — list currently-known topics (forum only)\n"
        "/addtopic <title> <id>  — add a topic manually (forum only)\n"
        "/deltopic <id>  — remove a manually-added topic\n"
        "/status         — show bot status (Telethon, group, topics)\n"
        "/whoami         — show your Telegram user ID + admin status\n"
        "/test_link <url>  — diagnostic: test fetching a t.me link\n"
        "/saved <url>    — 🚀 FAST: send t.me link content to Saved Messages\n"
        "/scrape <url> [flags]  — 🤖 AUTO: scrape ALL media from a channel\n"
        "/scrapeid <url> [start] [end] [saved] [keep]  — 🚀 FAST: forward by ID (flood-adaptive)\n"
        "/stop_scrape    — 🛑 stop the active scrape\n"
        "/scrape_status  — 📊 check scrape progress\n"
        "/caption <text>  — 📝 set a custom caption (replaces original)\n"
        "/caption strip   — 📝 strip ALL captions from forwarded media\n"
        "/caption clear   — 📝 restore original captions\n"
        "/cancel         — cancel the latest pending forward\n"
        "/reconnect     — retry Telethon connection (if session failed at boot)\n"
        "\n*Sending content*\n"
        "• Send me a photo / video / text / file -> I show topics (if forum) "
        "or a single Forward button (if not) -> tap to forward\n"
        "• Send me a t.me/c/<id>/<msg> link -> I fetch via your Telethon "
        "session and let you pick a destination\n"
        "• `/saved <url>` -> skip the picker, send directly to Saved Messages "
        "(fastest path)\n"
        "• `/scrape <url>` -> scrape the entire channel and auto-send all "
        "media to your destination\n"
        "\n*Scrape flags*\n"
        "  `old` — oldest first (chronological)\n"
        "  `saved` — send to Saved Messages (default: destination group)\n"
        "  `photo` / `video` / `doc` / `audio` / `voice` / `animation` — filter by media type\n"
        "  `parallel=N` — set parallel sends (default 3, max 10)\n"
        "  Example: `/scrape https://t.me/c/123 saved old videos parallel=5`\n"
        "\n*Captions*\n"
        "  `/caption <text>` — set a custom caption applied to all forwards\n"
        "  `/caption strip` — strip ALL captions (forward media without any text)\n"
        "  `/caption clear` — restore original caption behavior\n"
        "  `/caption` (no args) — show current setting\n"
        "\n*Destination types*\n"
        "• Forum groups: pick a topic from the picker\n"
        "• Regular groups/channels: single Forward button (no topic picker)\n"
        "• Saved Messages: use /saved or /scrape saved",
        parse_mode="Markdown",
    )


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        # User is not in ADMIN_IDS whitelist — log and silently return.
        # This is the "nothing happens" symptom: bot received /setgroup but
        # ignored it because the user is not authorized.
        user = update.effective_user
        logger.warning("/setgroup DENIED — user_id=%s username=%s not in ADMIN_IDS=%s",
                       user.id if user else '?',
                       getattr(user, 'username', None) if user else '?',
                       cfg.admin_ids)
        return
    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Usage: /setgroup <group_id>\n"
            "Example: /setgroup -1001234567890\n\n"
            "To find the group ID, add @RawDataBot to the group, read its "
            "reply, then remove it.\n\n"
            "Tip: if the bot says 'group_id must be an integer', your "
            "Telegram client may have auto-corrected the minus sign to a "
            "Unicode dash. Try copying the ID directly from @RawDataBot's "
            "message (long-press -> Copy)."
        )
        return

    raw_arg = context.args[0]
    gid = _safe_int(raw_arg)
    if gid is None:
        # Show the user EXACTLY what we received — this is the #1 diagnostic
        # for the "nothing happens" bug. Telegram auto-corrects hyphens.
        await update.effective_message.reply_text(
            f"Could not parse '{raw_arg}' as an integer.\n\n"
            f"Most likely cause: Telegram auto-corrected your '-' to a "
            f"Unicode dash character. Try this instead:\n"
            f"  1. Long-press the group ID from @RawDataBot's message\n"
            f"  2. Paste it directly (don't retype the '-')\n\n"
            f"Alternatively, send:\n"
            f"  /setgroup {raw_arg!r}\n...and I'll show what I received."
        )
        return

    await context.bot_data["db"].set_runtime("destination_group_id", str(gid))
    # Also stash on the config object so handlers don't need to re-read each time
    cfg.destination_group_id = gid

    # Invalidate the cached "is_forum" check so it gets re-evaluated for the
    # new destination on next use
    context.bot_data.pop("destination_is_forum", None)
    context.bot_data.pop("destination_is_forum_group_id", None)
    context.bot_data.pop("destination_chat_title", None)

    # Try to fetch chat info immediately so we can tell the user whether
    # it's a forum or not (and update the cache at the same time)
    chat_type_info = ""
    try:
        chat = await context.bot.get_chat(chat_id=gid)
        is_forum = bool(getattr(chat, "is_forum", False))
        title = getattr(chat, "title", None) or "(no title)"
        # Cache it
        context.bot_data["destination_is_forum"] = is_forum
        context.bot_data["destination_is_forum_group_id"] = gid
        context.bot_data["destination_chat_title"] = title
        chat_type_info = (
            f"\n\n📊 Chat info:\n"
            f"  • Title: {title}\n"
            f"  • Type: {getattr(chat, 'type', '?')}\n"
            f"  • Is forum: {'✅ YES (use /refresh to discover topics)' if is_forum else '❌ NO (single Forward button)'}"
        )
    except Exception as e:
        chat_type_info = (
            f"\n\n⚠️ Couldn't fetch chat info: {type(e).__name__}: {e}\n"
            f"Make sure the bot is a member of this chat."
        )

    await update.effective_message.reply_text(
        f"✅ Destination group set to {gid}.{chat_type_info}"
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return
    topics_mgr = context.bot_data["topics"]
    if not topics_mgr.user_session:
        await update.effective_message.reply_text(
            "Telethon user session failed to start at boot. "
            "Send /reconnect to retry, or check logs: docker-compose logs forwarder-bot"
        )
        return
    # Auto-reconnect if the Telethon connection has dropped.
    if not await topics_mgr.user_session._ensure_connected():
        await update.effective_message.reply_text(
            "❌ Telethon user session is disconnected and could not be reconnected.\n"
            "Try restarting the container: docker-compose restart forwarder-bot"
        )
        return
    await update.effective_message.reply_text("Refreshing topics...")
    topics = await topics_mgr.refresh(group_id)
    if not topics:
        await update.effective_message.reply_text(
            "No topics found. Make sure your user account is a member of the "
            "destination group and that the group is a forum (topics enabled)."
        )
        return
    listing = "\n".join(f"• `{t['id']}` — {t['title']}" for t in topics)
    await update.effective_message.reply_text(
        f"Found {len(topics)} topic(s):\n{listing}", parse_mode="Markdown"
    )


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id>.")
        return
    topics = await context.bot_data["topics"].get_topics(group_id)
    if not topics:
        await update.effective_message.reply_text("No topics known. /refresh or /addtopic first.")
        return
    listing = "\n".join(f"• `{t['id']}` — {t['title']}" for t in topics)
    await update.effective_message.reply_text(f"Known topics:\n{listing}", parse_mode="Markdown")


async def cmd_addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/addtopic DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /addtopic <title> <topic_id>\nExample: /addtopic Videos 12"
        )
        return
    topic_id_str = context.args[-1]
    title = " ".join(context.args[:-1])
    topic_id = _safe_int(topic_id_str)
    if topic_id is None:
        await update.effective_message.reply_text(
            f"Could not parse '{topic_id_str}' as an integer (topic_id)."
        )
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return
    await db.add_topic_override(group_id, topic_id, title)
    await update.effective_message.reply_text(
        f"Added topic override: {title} -> {topic_id} for group {group_id}"
    )


async def cmd_deltopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/deltopic DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /deltopic <topic_id>")
        return
    topic_id = _safe_int(context.args[0])
    if topic_id is None:
        await update.effective_message.reply_text(
            f"Could not parse '{context.args[0]}' as an integer (topic_id)."
        )
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        return
    await db._conn.execute(
        "DELETE FROM topic_overrides WHERE group_id = ? AND topic_id = ?",
        (group_id, topic_id),
    )
    await db._conn.commit()
    await update.effective_message.reply_text(f"Removed topic override {topic_id}.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/status DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    telethon_status = "n/a"
    if user_session:
        telethon_status = "connected" if user_session.available else "disconnected"
    elif not cfg.has_user_session:
        telethon_status = "not configured"

    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = v if v else None

    topics_count = 0
    if group_id is not None:
        topics_count = len(await context.bot_data["topics"].get_topics(int(group_id)))

    msg = (
        f"Bot status:\n"
        f"• Telethon user session: {telethon_status}\n"
        f"• Destination group: {group_id or '(not set)'}\n"
        f"• Known topics: {topics_count}\n"
        f"• Admin whitelist: {len(cfg.admin_ids)} user(s)"
    )
    await update.effective_message.reply_text(msg)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — shows the user their Telegram user ID and whether
    they are in the admin whitelist. Useful for debugging 'nothing happens'
    when ADMIN_IDS is misconfigured."""
    cfg = context.bot_data["config"]
    user = update.effective_user
    if not user:
        return
    is_admin = cfg.is_admin(user.id)
    admin_list = cfg.admin_ids if cfg.admin_ids else "(empty — everyone allowed)"
    await update.effective_message.reply_text(
        f"Your Telegram user ID: `{user.id}`\n"
        f"Your username: @{getattr(user, 'username', None)}\n"
        f"Your name: {getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}\n\n"
        f"Admin whitelist (ADMIN_IDS env var): {admin_list}\n"
        f"You are{' ' if is_admin else ' NOT '}authorized to use admin commands.",
        parse_mode="Markdown",
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show destination chat info — is it a forum? what's the title?
    Useful for debugging "is my destination a forum or not?"""
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/info DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return

    status_msg = await update.effective_message.reply_text(f"Fetching info for chat `{group_id}`...", parse_mode="Markdown")

    try:
        chat = await context.bot.get_chat(chat_id=group_id)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Failed to fetch chat info: `{type(e).__name__}: {e}`\n\n"
            f"Make sure the bot is a member of the chat with permission to view chat info.",
            parse_mode="Markdown",
        )
        return

    is_forum = bool(getattr(chat, "is_forum", False))
    title = getattr(chat, "title", None) or "(no title)"
    chat_type = getattr(chat, "type", "?")
    username = getattr(chat, "username", None)
    member_count = "?"
    try:
        member_count = await context.bot.get_chat_member_count(chat_id=group_id)
    except Exception:
        pass

    # If it's a forum, list known topics
    topics_info = ""
    if is_forum:
        topics_mgr = context.bot_data.get("topics")
        if topics_mgr:
            topics = await topics_mgr.get_topics(group_id)
            if topics:
                topics_info = f"\n\n📋 Known topics ({len(topics)}):"
                for t in topics[:20]:  # show up to 20
                    topics_info += f"\n  • `{t['id']}` — {t['title']}"
                if len(topics) > 20:
                    topics_info += f"\n  ... and {len(topics) - 20} more"
            else:
                topics_info = "\n\n📋 No topics cached. Run /refresh to fetch them."
        else:
            topics_info = "\n\n📋 Topics manager not initialized."

    # Update the cache
    context.bot_data["destination_is_forum"] = is_forum
    context.bot_data["destination_is_forum_group_id"] = group_id
    context.bot_data["destination_chat_title"] = title

    await status_msg.edit_text(
        f"📊 Destination chat info:\n\n"
        f"• ID: `{group_id}`\n"
        f"• Title: {title}\n"
        f"• Type: {chat_type}\n"
        f"• Username: @{username}\n"
        f"• Is forum (has topics): {'✅ YES' if is_forum else '❌ NO'}\n"
        f"• Member count: {member_count}"
        f"{topics_info}",
        parse_mode="Markdown",
    )


async def cmd_test_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — try to fetch a t.me link via Telethon and show
    the user a detailed report. Useful for debugging "forwarding from locked
    private channels doesn't work".

    Usage: /test_link https://t.me/c/1234567890/42
    """
    from user_session import parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/test_link DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session:
        await update.effective_message.reply_text(
            "❌ Telethon session failed to start at boot.\n\n"
            "Send /reconnect to retry.\n"
            "If that fails, check logs: docker-compose logs forwarder-bot\n"
            "Common fix: re-run `python login.py --string` locally and update SESSION_STRING in .env"
        )
        return
    # Auto-reconnect if the Telethon connection has dropped.
    if not await user_session._ensure_connected():
        await update.effective_message.reply_text(
            "❌ Telethon user session is disconnected and could not be reconnected.\n"
            "Try restarting the container: docker-compose restart forwarder-bot"
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/test_link <t.me URL>`\n\n"
            "Example: `/test_link https://t.me/c/1234567890/42`\n"
            "         `/test_link https://t.me/somechannel/42`",
            parse_mode="Markdown",
        )
        return

    url = " ".join(context.args)
    parsed = parse_telegram_link(url)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel post)\n"
            f"  • `https://t.me/channelname/42`   (public channel post)",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.effective_message.reply_text(
        f"🔍 Testing link...\n\n"
        f"Parsed:\n"
        f"  kind: `{parsed.kind}`\n"
        f"  chat_ref: `{parsed.chat_ref}`\n"
        f"  message_id: `{parsed.message_id}`\n\n"
        f"Fetching via Telethon...",
        parse_mode="Markdown",
    )

    try:
        result = await user_session.test_link(parsed)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Exception: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Build report
    steps_text = "\n".join(result.get("steps", []))
    if len(steps_text) > 3000:
        steps_text = steps_text[:3000] + "\n... (truncated)"

    report = (
        f"📋 Test link report:\n\n"
        f"Parsed:\n"
        f"  kind: `{result['parsed']['kind']}`\n"
        f"  chat_ref: `{result['parsed']['chat_ref']}`\n"
        f"  message_id: `{result['parsed']['message_id']}`\n\n"
        f"Result:\n"
        f"  success: {'✅ YES' if result['success'] else '❌ NO'}\n"
        f"  has_media: {'✅' if result['has_media'] else '❌'}\n"
        f"  media_type: `{result['media_type']}`\n"
        f"  chat_title: {result['chat_title'] or '(unknown)'}\n"
        f"  error: {result['error'] or '(none)'}\n\n"
        f"Steps:\n```\n{steps_text}\n```"
    )

    await status_msg.edit_text(report, parse_mode="Markdown")


async def cmd_saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a t.me link directly to Saved Messages via Telethon — FAST PATH.

    This bypasses the topic picker entirely. The user account sends the
    message to its own Saved Messages ("me" in Telethon), which is the
    fastest destination because:
      1. The user account is ALWAYS a member of its own Saved Messages
      2. forward_messages or send_message works directly (no topic thread)
      3. No need to wait for the user to tap a button

    Usage: /saved https://t.me/c/1234567890/42
    """
    from user_session import parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/saved DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session:
        await update.effective_message.reply_text(
            "❌ Telethon session failed to start at boot.\n\n"
            "Send /reconnect to retry.\n"
            "If that fails, check logs: docker-compose logs forwarder-bot\n"
            "Common fix: re-run `python login.py --string` locally and update SESSION_STRING in .env"
        )
        return
    # Auto-reconnect if the Telethon connection has dropped.
    if not await user_session._ensure_connected():
        await update.effective_message.reply_text(
            "❌ Telethon user session is disconnected and could not be reconnected.\n"
            "Try restarting the container: docker-compose restart forwarder-bot"
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/saved <t.me URL>`\n\n"
            "Sends the content directly to your Saved Messages (fastest path — "
            "no topic picker needed).\n\n"
            "Example: `/saved https://t.me/c/1234567890/42`",
            parse_mode="Markdown",
        )
        return

    url = " ".join(context.args)
    parsed = parse_telegram_link(url)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel post)\n"
            f"  • `https://t.me/channelname/42`   (public channel post)",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.effective_message.reply_text(
        f"📩 Sending to Saved Messages...\n\n"
        f"Source: `{parsed.chat_ref}` / msg `{parsed.message_id}`",
        parse_mode="Markdown",
    )

    # Build a progress callback that updates the status message
    import time as _time
    last_update = {"time": 0.0, "text": "", "first": True}

    async def progress_cb(sent_bytes: int, total_bytes: int, label: str):
        now = _time.time()
        if total_bytes > 0:
            pct = (sent_bytes / total_bytes) * 100
            sent_mb = sent_bytes / (1024 * 1024)
            total_mb = total_bytes / (1024 * 1024)
            text = (f"📡 {label}\n\n"
                    f"Progress: {pct:.1f}%\n"
                    f"{sent_mb:.2f} / {total_mb:.2f} MB")
        else:
            text = f"📡 {label}..."

        if text == last_update["text"]:
            return

        if not last_update["first"]:
            if now - last_update["time"] < 0.5:
                return
        last_update["first"] = False
        last_update["time"] = now
        last_update["text"] = text
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        # Send to "me" — Telethon's special entity for Saved Messages.
        # This is a direct call to send_to_destination with dest_chat_id="me"
        # which Telethon resolves to the user's own Saved Messages chat.
        custom_caption = await _get_custom_caption(context)
        success, diag = await user_session.send_to_destination(
            source_chat_id=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
            source_message_ids=[parsed.message_id],
            dest_chat_id="me",  # Saved Messages
            topic_id=None,
            progress_callback=progress_cb,
            custom_caption=custom_caption,
        )
    except Exception as e:
        logger.exception("/saved failed")
        await status_msg.edit_text(
            f"❌ Exception: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    if success:
        # Update cumulative stats
        db = context.bot_data.get("db")
        if db:
            try:
                await db.increment_stat("total_saved_forwards", 1)
            except Exception:
                pass
        await status_msg.edit_text("✅ Sent to Saved Messages!")
    else:
        err_lines = "\n".join(diag[-7:])
        await status_msg.edit_text(
            f"❌ Failed to send to Saved Messages.\n\n"
            f"Last diagnostic steps:\n{err_lines}",
        )


# ── Shared live-status machinery (used by BOTH /scrape and /scrapeid) ──
# The status message used to freeze during long flood waits / recovery
# breaks: the counts didn't change, so the 2s ticker rebuilt an identical
# text and Telegram answered "message is not modified". To a user that is
# indistinguishable from a hang. These helpers render the ACTIVE WAIT PHASE
# (published by user_session via the shared phase_state dict) as a live
# countdown — the text now changes every tick while we wait, so the message
# visibly counts down and "Last progress: Xs ago" shows liveness.

def _new_phase_state() -> dict:
    """Fresh shared-by-reference wait-phase dict (see user_session._set_phase)."""
    return {"key": None, "until": 0.0, "note": "", "updated": 0.0}


def _fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def _scrape_phase_line(context) -> str:
    """Live one-line status of the current wait phase ('' while working)."""
    ph = context.bot_data.get("scrape_phase") or {}
    key = ph.get("key")
    if not key:
        return ""
    remaining = float(ph.get("until", 0.0)) - time.time()
    if remaining <= 0:
        return f"[{str(key).upper()}] wrapping up..."
    resume = time.strftime("%H:%M", time.localtime(time.time() + remaining))
    label = {"break": "RECOVERY BREAK", "flood": "FLOOD WAIT"}.get(key, str(key).upper())
    return (f"[{label}] {_fmt_countdown(remaining)} remaining "
            f"(resumes ~{resume}) — /stop_scrape works")


def _build_scrape_live_text(context, started_at: float) -> str:
    """Build the live status text from bot_data: counts + active wait phase
    + liveness. Shared by the /scrape and /scrapeid status tickers."""
    st = context.bot_data.get("scrape_status", {})
    sent = st.get("sent_count", 0)
    failed = st.get("failed_count", 0)
    skipped = st.get("skipped_count", 0)
    total_seen = st.get("total_seen", 0)
    in_flight = st.get("in_flight", 0)
    flood_waits = st.get("flood_waits", 0)
    last_msg_id = st.get("last_message_id", 0)

    elapsed = time.time() - started_at
    if elapsed < 1:
        elapsed = 1
    throughput = sent / (elapsed / 60) if elapsed > 0 else 0

    # Simple progress bar using ASCII characters
    if total_seen > 0:
        progress = min(1.0, sent / total_seen)
        bar_len = 10
        filled = int(progress * bar_len)
        bar = "=" * filled + "-" * (bar_len - filled)
        pct = progress * 100
    else:
        bar = "-" * 10
        pct = 0

    # Activity: the live wait phase (if any) is the single source of truth
    # for "currently waiting" — flood_waits is a HISTORICAL counter and must
    # not claim the bot is flood-waiting right now (that misled users into
    # thinking a working scrape was stuck).
    phase = _scrape_phase_line(context)
    if phase:
        activity = phase
    elif in_flight > 0:
        activity = f"[SENDING] {in_flight} item(s) in flight"
    else:
        activity = "[SCANNING]"

    # Liveness indicator: how long since the last REAL progress update?
    idle_str = ""
    last_upd = st.get("last_update", 0)
    if last_upd:
        idle = time.time() - last_upd
        if idle > 30:
            idle_str = f"\nLast progress: {_fmt_countdown(idle)} ago"

    # ETA if we have throughput data
    eta_str = ""
    if throughput > 0 and total_seen > sent:
        remaining = total_seen - sent
        eta_sec = remaining / (throughput / 60)
        if eta_sec > 60:
            eta_str = f"\nETA:       {eta_sec/60:.0f}m {eta_sec%60:.0f}s"
        else:
            eta_str = f"\nETA:       {eta_sec:.0f}s"

    return (
        f">>> SCRAPE IN PROGRESS <<<\n"
        f"{activity}\n\n"
        f"Progress: [{bar}] {pct:.0f}%\n"
        f"----------------------------------------\n"
        f"Sent:      {sent}\n"
        f"In-flight: {in_flight}\n"
        f"Failed:    {failed}\n"
        f"Skipped:   {skipped}\n"
        f"Seen:      {total_seen}\n"
        f"----------------------------------------\n"
        f"Elapsed:   {elapsed:.0f}s\n"
        f"Speed:     {throughput:.1f} items/min\n"
        f"Last ID:   {last_msg_id}"
        f"{idle_str}"
        f"{eta_str}"
    )


async def _run_scrape_status_ticker(context, edit_fn, started_at: float,
                                    interval: float = 2.0):
    """Background loop refreshing the status message every `interval` seconds.

    While a wait phase (flood/break) is active the text contains a live
    countdown, so it CHANGES every tick — the edit goes through and the
    user sees the countdown ticking. When nothing changed, Telegram replies
    "message is not modified", which the edit helper silently ignores.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            task = context.bot_data.get("scrape_task")
            if task and task.done():
                # Scrape finished — one final update, then exit
                try:
                    await edit_fn(_build_scrape_live_text(context, started_at),
                                  force=True)
                except Exception:
                    pass
                break
            try:
                await edit_fn(_build_scrape_live_text(context, started_at),
                              force=False)
            except Exception:
                pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("status ticker error: %s", e)


async def cmd_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scrape a channel — send all media (photos/videos) to destination.

    Usage:
      /scrape <channel_url> [flags]

    Flags (any combination, space-separated):
      old           — oldest first (chronological order)
      saved         — send to Saved Messages (default: destination group)
      photo         — only photos
      video         — only videos
      doc           — only documents
      audio         — only audio
      voice         — only voice messages
      animation     — only animations (GIFs)
      photos        — only photos (alias)
      videos        — only videos (alias)
      docs          — only documents (alias)
      parallel=N    — set parallel send count (default 3, max 10)

    Examples:
      /scrape https://t.me/publicchannel
      /scrape https://t.me/c/1234567890 saved old
      /scrape https://t.me/c/1234567890 photo video   — only photos and videos
      /scrape https://t.me/c/1234567890 saved old videos parallel=5

    Notes:
      - If no media type filter is given, ALL media is forwarded
      - Text-only messages are always skipped (they have no media)
      - Rate limit: 0.3 sec delay between sends (per parallel slot)
      - On FloodWait, the bot sleeps and retries automatically
      - Protected (noforwards) channels use the same three-tier fallback
        as /saved — forward → send_message(file=) → download+send_file
    """
    import asyncio
    from user_session import parse_channel_link, parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/scrape DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session:
        await update.effective_message.reply_text(
            "❌ Telethon session failed to start at boot.\n\n"
            "Send /reconnect to retry.\n"
            "If that fails, check logs: docker-compose logs forwarder-bot\n"
            "Common fix: re-run `python login.py --string` locally and update SESSION_STRING in .env"
        )
        return
    # Auto-reconnect if the Telethon connection has dropped (Telegram idle
    # disconnect, network blip, etc.). This makes /scrape resilient to
    # long idle periods between scrape commands.
    if not await user_session._ensure_connected():
        await update.effective_message.reply_text(
            "❌ Telethon user session is disconnected and could not be reconnected.\n"
            "Check the bot logs — the SESSION_STRING may be invalid or revoked.\n"
            "Try restarting the container: docker-compose restart forwarder-bot"
        )
        return

    # Check if there's already an active scrape
    if context.bot_data.get("scrape_task") and not context.bot_data["scrape_task"].done():
        await update.effective_message.reply_text(
            "⚠️ A scrape is already running. Use /stop_scrape to stop it first, "
            "or /scrape_status to check progress."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/scrape <channel_url> [flags]`\n\n"
            "Flags:\n"
            "  `old` — oldest first (chronological)\n"
            "  `saved` — send to Saved Messages (default: destination group)\n"
            "  `resume` — continue from where the last scrape of this channel stopped\n"
            "  `photo` / `photos` — only photos\n"
            "  `video` / `videos` — only videos\n"
            "  `doc` / `docs` — only documents\n"
            "  `audio` — only audio\n"
            "  `voice` — only voice messages\n"
            "  `animation` — only animations (GIFs)\n"
            "  `parallel=N` — set parallel sends (default 3, max 10)\n\n"
            "Examples:\n"
            "  `/scrape https://t.me/publicchannel`\n"
            "  `/scrape https://t.me/c/1234567890 saved old`\n"
            "  `/scrape https://t.me/c/1234567890 photo video`\n"
            "  `/scrape https://t.me/c/123 saved old videos parallel=5`\n"
            "  `/scrape https://t.me/c/123 resume` — continue after a crash/flood",
            parse_mode="Markdown",
        )
        return

    # Parse args: first arg is URL, rest are flags
    url = context.args[0]
    raw_flags = [a.lower() for a in context.args[1:]]
    send_to_saved = "saved" in raw_flags
    oldest_first = "old" in raw_flags or "oldest" in raw_flags
    do_resume = "resume" in raw_flags

    # Parse media type filters
    valid_media_types = {"photo", "video", "animation", "document", "audio", "voice"}
    # Aliases: photos -> photo, videos -> video, docs -> document
    alias_map = {"photos": "photo", "videos": "video", "docs": "document"}
    media_types: list[str] = []
    parallel = cfg.default_parallel  # default from PARALLEL env var (was hardcoded 3)
    for flag in raw_flags:
        # Resolve aliases
        actual = alias_map.get(flag, flag)
        if actual in valid_media_types:
            if actual not in media_types:
                media_types.append(actual)
        elif flag.startswith("parallel="):
            try:
                p = int(flag.split("=", 1)[1])
                parallel = max(1, min(p, 10))  # clamp 1..10
            except ValueError:
                pass
    # If no media types specified, set to None (all media)
    if not media_types:
        media_types = None

    # Try parsing as a channel-only link first, then fall back to a post link
    parsed = parse_channel_link(url)
    if not parsed:
        # Maybe they passed a post link (t.me/c/123/42) — extract the channel part
        parsed_post = parse_telegram_link(url)
        if parsed_post:
            # Use the same chat_ref but message_id=0 (whole channel)
            parsed = type(parsed_post)(kind=parsed_post.kind,
                                        chat_ref=parsed_post.chat_ref,
                                        message_id=0)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890`  (private channel)\n"
            f"  • `https://t.me/channelname`   (public channel)\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel + start msg)\n"
            f"  • `https://t.me/channelname/42`   (public channel + start msg)",
            parse_mode="Markdown",
        )
        return

    # Determine destination
    if send_to_saved:
        dest_chat_id = "me"
        dest_label = "Saved Messages"
    else:
        db = context.bot_data["db"]
        dest_chat_id = cfg.destination_group_id
        if dest_chat_id is None:
            v = await db.get_runtime("destination_group_id")
            dest_chat_id = int(v) if v else None
        if dest_chat_id is None:
            await update.effective_message.reply_text(
                "No destination group set. Either:\n"
                "  • /setgroup <group_id> first, OR\n"
                "  • Use /scrape <url> saved — to send to Saved Messages"
            )
            return
        dest_label = f"chat {dest_chat_id}"

    # Build the filter description for the status message
    filter_desc = "ALL media"
    if media_types:
        filter_desc = "only: " + ", ".join(media_types)

    # ── Resume from checkpoint (state saving) ────────────────────────────
    # The last scrape's progress (last_message_id) is persisted to SQLite
    # after every batch. `resume` picks it up so a crash / flood abort /
    # manual stop never re-sends thousands of messages:
    #   - newest-first: continue BELOW the checkpoint → max_id = ckpt - 1
    #   - oldest-first: continue ABOVE the checkpoint → min_id = ckpt
    resume_min_id = 0
    resume_max_id = 0
    resume_note = ""
    if do_resume:
        db = context.bot_data.get("db")
        ckpt_raw = await db.get_runtime(f"scrape_checkpoint:{parsed.chat_ref}") if db else None
        if ckpt_raw:
            try:
                ckpt = int(ckpt_raw)
            except (TypeError, ValueError):
                ckpt = 0
            if ckpt > 0:
                if oldest_first:
                    resume_min_id = ckpt
                else:
                    resume_max_id = ckpt - 1
                resume_note = (f"\n♻️ Resuming from checkpoint: msg {ckpt} "
                               f"(already-sent messages are skipped)")
                if not oldest_first and resume_max_id < 1:
                    await update.effective_message.reply_text(
                        "✅ Checkpoint is at the very top of the channel — nothing older to resume."
                    )
                    return
        else:
            await update.effective_message.reply_text(
                "ℹ️ No checkpoint found for this channel — starting from the beginning.\n"
                "(Checkpoints are saved automatically during every scrape.)"
            )

    # Initial status message
    status_msg = await update.effective_message.reply_text(
        f"🔍 Starting scrape...\n\n"
        f"Source: `{parsed.chat_ref}`\n"
        f"Destination: {dest_label}\n"
        f"Order: {'oldest first' if oldest_first else 'newest first'}\n"
        f"Filter: {filter_desc}\n"
        f"Parallel: {parallel} sends{resume_note}\n\n"
        f"_Use /stop_scrape to cancel, /scrape_status to check progress._",
        parse_mode="Markdown",
    )

    # Set up cancellation event and status storage
    cancel_event = asyncio.Event()
    context.bot_data["scrape_cancel"] = cancel_event
    context.bot_data["scrape_status"] = {
        "sent_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "total_seen": 0,
        "last_message_id": 0,
        "started_at": time.time(),
        "source_ref": parsed.chat_ref,
        "dest_label": dest_label,
        "order": "oldest" if oldest_first else "newest",
        "filter": filter_desc,
        "parallel": parallel,
    }
    # Shared wait-phase dict — scrape_channel publishes flood waits and
    # recovery breaks here; the ticker renders them as LIVE countdowns so
    # a long wait never looks like a hang again.
    phase_state = _new_phase_state()
    context.bot_data["scrape_phase"] = phase_state

    # ── Real-time status update system ─────────────────────────────────────
    # Design:
    #   - A dedicated background ticker task updates the Telegram message
    #     every 2 seconds with the latest counts from bot_data.
    #   - stats_callback only updates bot_data (no Telegram API calls) —
    #     this avoids race conditions from concurrent edit_text calls.
    #   - status_callback is for milestone events (start, FloodWait, cancel,
    #     complete) and uses an asyncio.Lock to serialize edits.
    #
    # This design fixes the "bugs out" issue caused by multiple parallel
    # send tasks triggering concurrent edit_text calls, which caused
    # "Message is not modified" errors and rate-limit conflicts.
    import asyncio as _asyncio
    status_lock = _asyncio.Lock()
    last_edit_time = {"time": 0.0}
    last_displayed = {"sent": -1, "in_flight": -1, "total_seen": -1}
    ticker_task_ref = {"task": None}
    started_at = time.time()

    async def _edit_telegram_message_safe(text: str, force: bool = False):
        """Edit the Telegram status message with a lock to prevent
        concurrent edit calls. Returns True if edited."""
        async with status_lock:
            now = time.time()
            if not force and now - last_edit_time["time"] < 1.5:
                return False
            last_edit_time["time"] = now
            try:
                await status_msg.edit_text(text)
                return True
            except Exception as e:
                err_str = str(e).lower()
                if "not modified" in err_str:
                    pass
                elif "too many requests" in err_str or "retry after" in err_str:
                    logger.warning("status: rate-limited edit; backing off 3s")
                    last_edit_time["time"] = now + 3.0
                else:
                    logger.warning("status: edit_text failed: %s: %s", type(e).__name__, e)
                return False

    def _build_live_status_text() -> str:
        # Shared builder (module level, above): counts + active wait phase
        # with LIVE countdown + "Last progress: Xs ago" liveness line.
        return _build_scrape_live_text(context, started_at)

    async def _status_ticker():
        """Background task that updates the Telegram message every 2 seconds.
        This is the ONLY thing that edits the message during a scrape —
        stats_callback just updates bot_data, avoiding race conditions.
        Delegates to the shared _run_scrape_status_ticker."""
        await _run_scrape_status_ticker(context, _edit_telegram_message_safe,
                                        started_at)

    # Start the background ticker
    ticker_task_ref["task"] = _asyncio.create_task(_status_ticker())

    async def status_callback(text: str):
        """Called by scrape_channel for milestone events (start, FloodWait,
        cancel, complete). Force-edits the Telegram message."""
        now = time.time()
        context.bot_data["scrape_status"].update({
            "last_update": now,
        })
        await _edit_telegram_message_safe(text, force=True)

    async def stats_callback(result_dict: dict):
        """Called on EVERY send completion and every message iteration.
        ONLY updates bot_data — does NOT edit the Telegram message.
        The background _status_ticker handles message edits every 2s.
        This prevents race conditions from concurrent send completions.
        Also persists the per-channel checkpoint (last_message_id) to
        SQLite after every batch, so `resume` can pick it up after a
        crash, a flood abort, or a manual /stop_scrape."""
        context.bot_data["scrape_status"].update({
            "sent_count": result_dict.get("sent_count", 0),
            "failed_count": result_dict.get("failed_count", 0),
            "skipped_count": result_dict.get("skipped_count", 0),
            "total_seen": result_dict.get("total_seen", 0),
            "last_message_id": result_dict.get("last_message_id", 0),
            "flood_waits": result_dict.get("flood_waits", 0),
            "cancelled": result_dict.get("cancelled", False),
            "in_flight": result_dict.get("in_flight", 0),
            "last_update": time.time(),
        })
        # Checkpoint (state saving) — cheap SQLite write, never fatal
        ckpt_id = result_dict.get("last_message_id", 0)
        if ckpt_id:
            _db = context.bot_data.get("db")
            if _db:
                try:
                    await _db.set_runtime(
                        f"scrape_checkpoint:{parsed.chat_ref}", str(ckpt_id)
                    )
                except Exception:
                    pass

    # Run the scrape as a background task
    async def scrape_task():
        scrape_result = None
        try:
            # Load the custom_caption setting (None=original, ""=strip, "<text>"=custom)
            custom_caption = await _get_custom_caption(context)
            scrape_result = await user_session.scrape_channel(
                source_chat_ref=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
                dest_chat_id=dest_chat_id,
                topic_id=None,  # scraper doesn't support topics yet (use /saved for that)
                reverse=oldest_first,
                min_id=resume_min_id,
                max_id=resume_max_id,
                cancel_event=cancel_event,
                status_callback=status_callback,
                stats_callback=stats_callback,
                media_types=media_types,
                parallel=parallel,
                custom_caption=custom_caption,
                flood_break_every=cfg.flood_break_every,
                flood_break_seconds=cfg.flood_break_seconds,
                phase_state=phase_state,
            )
            # ── Update cumulative stats (persisted to SQLite) ───────────────
            # These survive restarts and power the dashboard's "All-Time" stats.
            db = context.bot_data.get("db")
            if db and isinstance(scrape_result, dict):
                try:
                    await db.increment_stat("total_scrapes", 1)
                    await db.increment_stat("total_sent", scrape_result.get("sent_count", 0))
                    await db.increment_stat("total_failed", max(0, scrape_result.get("failed_count", 0)))
                    await db.increment_stat("total_skipped", scrape_result.get("skipped_count", 0))
                    await db.increment_stat("total_flood_waits", scrape_result.get("flood_waits", 0))
                    logger.info("Cumulative stats updated: scrape complete "
                                "(sent=%d, failed=%d, skipped=%d)",
                                scrape_result.get("sent_count", 0),
                                scrape_result.get("failed_count", 0),
                                scrape_result.get("skipped_count", 0))
                except Exception as e:
                    logger.warning("Failed to update cumulative stats: %s", e)
        except Exception as e:
            logger.exception("/scrape task failed")
            try:
                await status_msg.edit_text(f"❌ Scrape crashed: {type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            # Cancel the status ticker task (it will do one final update
            # before exiting, showing the final counts)
            t = ticker_task_ref.get("task")
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass
            # ── Show final completion message ───────────────────────────────
            # Build a clear "DONE" message showing the final results, so the
            # user knows the scrape finished successfully (or was cancelled).
            try:
                elapsed = time.time() - started_at
                if scrape_result and isinstance(scrape_result, dict):
                    sent = scrape_result.get("sent_count", 0)
                    failed = scrape_result.get("failed_count", 0)
                    skipped = scrape_result.get("skipped_count", 0)
                    total_seen = scrape_result.get("total_seen", 0)
                    flood_waits = scrape_result.get("flood_waits", 0)
                    last_id = scrape_result.get("last_message_id", 0)
                    cancelled = scrape_result.get("cancelled", False)

                    if cancelled:
                        status_line = ">>> SCRAPE CANCELLED <<<"
                    elif failed > 0 and sent == 0:
                        status_line = ">>> SCRAPE COMPLETED (with errors) <<<"
                    else:
                        status_line = ">>> SCRAPE COMPLETE <<<"

                    throughput = sent / (elapsed / 60) if elapsed > 60 else 0

                    # Resume hint — a checkpoint was persisted for this
                    # channel, so a cancelled/aborted run can continue
                    # exactly where it stopped without re-sending anything.
                    resume_hint = ""
                    if last_id and (cancelled or failed == -1):
                        resume_hint = (
                            f"\n----------------------------------------\n"
                            f"♻️ Continue: /scrape {url}{' old' if oldest_first else ''} resume"
                        )

                    final_text = (
                        f"{status_line}\n\n"
                        f"Source:      {parsed.chat_ref}\n"
                        f"Destination: {dest_label}\n"
                        f"Duration:    {elapsed:.0f}s\n"
                        f"----------------------------------------\n"
                        f"Sent:        {sent}\n"
                        f"Failed:      {failed}\n"
                        f"Skipped:     {skipped}\n"
                        f"Total seen:  {total_seen}\n"
                        f"Flood waits: {flood_waits}\n"
                        f"Last msg ID: {last_id}\n"
                        f"----------------------------------------\n"
                        f"Speed:       {throughput:.1f} items/min"
                        f"{resume_hint}"
                    )
                else:
                    final_text = (
                        f">>> SCRAPE ENDED <<<\n\n"
                        f"Duration: {elapsed:.0f}s\n"
                        f"(No result data available — check logs for details)"
                    )
                await _edit_telegram_message_safe(final_text, force=True)
            except Exception:
                pass
            # Clean up the scrape state when done
            context.bot_data.pop("scrape_task", None)
            context.bot_data.pop("scrape_cancel", None)
            context.bot_data.pop("scrape_phase", None)

    context.bot_data["scrape_task"] = asyncio.create_task(scrape_task())
    logger.info("Scrape started for %s -> %s", parsed.chat_ref, dest_label)


async def _get_custom_caption(context) -> str | None:
    """Load the custom_caption setting from DB / bot_data cache.
    Returns:
      None — use original captions (legacy behavior)
      "" — strip all captions
      "<text>" — use this custom caption
    """
    # Check in-memory cache first (set by /caption command)
    if "custom_caption" in context.bot_data:
        return context.bot_data["custom_caption"]
    # Fall back to DB
    db = context.bot_data["db"]
    raw = await db.get_runtime("custom_caption", None)
    if raw is None or raw == "__none__":
        return None  # use original
    return raw  # "" means strip, anything else is custom text


async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or clear a custom caption applied to all forwarded media.

    Usage:
      /caption <text>      — set a custom caption (applied to all forwards)
      /caption clear       — clear the custom caption (use original captions)
      /caption strip       — always strip captions (no caption at all)
      /caption             — show current setting

    When a custom caption is set:
      - /scrape sends media with your custom caption (no original captions)
      - /saved sends media with your custom caption (no original captions)
      - Direct forwards (links) send with your custom caption (no original)

    Use `/caption clear` to restore original-caption behavior.
    Use `/caption strip` to remove ALL captions (forward media without any text).
    """
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/caption DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return

    if not context.args:
        # Show current setting
        current = await _get_custom_caption(context)
        if current is None:
            current_str = "(not set — using original captions)"
        elif current == "":
            current_str = "(strip mode — all captions removed)"
        else:
            preview = current[:200] + ("..." if len(current) > 200 else "")
            current_str = f"`{preview}`"
        await update.effective_message.reply_text(
            f"📝 Current caption setting:\n\n{current_str}\n\n"
            f"Usage:\n"
            f"  `/caption <text>` — set custom caption\n"
            f"  `/caption clear` — restore original captions\n"
            f"  `/caption strip` — remove all captions\n",
            parse_mode="Markdown",
        )
        return

    arg = " ".join(context.args)
    if arg.lower() == "clear":
        await db.set_runtime("custom_caption", "__none__")  # sentinel for "use original"
        # Also clear from in-memory config cache if present
        context.bot_data.pop("custom_caption", None)
        await update.effective_message.reply_text(
            "✅ Custom caption cleared. Forwards will use original captions."
        )
    elif arg.lower() == "strip":
        await db.set_runtime("custom_caption", "")  # empty string = strip all
        context.bot_data["custom_caption"] = ""
        await update.effective_message.reply_text(
            "✅ Caption mode: STRIP. All forwarded media will have no caption."
        )
    else:
        # Set custom caption (truncate to Telegram's 1024-char caption limit)
        if len(arg) > 1024:
            arg = arg[:1024]
            await update.effective_message.reply_text(
                f"⚠️ Caption truncated to 1024 chars (Telegram's limit)."
            )
        await db.set_runtime("custom_caption", arg)
        context.bot_data["custom_caption"] = arg
        preview = arg[:200] + ("..." if len(arg) > 200 else "")
        await update.effective_message.reply_text(
            f"✅ Custom caption set:\n\n`{preview}`\n\n"
            f"All forwarded media will use this caption instead of the original.",
            parse_mode="Markdown",
        )


async def cmd_stop_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the currently running scrape.

    Uses a three-tier approach:
    1. Set the cancel_event (graceful — scrape stops at next check point)
    2. If the scrape doesn't stop within 10 seconds, forcefully cancel the task
    3. If that doesn't work either, disconnect the Telethon client (tears down
       the blocked transport, unblocking any stuck socket reads)

    This ensures /stop_scrape ALWAYS works, even when the scrape is stuck
    in a blocking I/O call that doesn't respond to task.cancel().
    """
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return
    cancel_event = context.bot_data.get("scrape_cancel")
    task = context.bot_data.get("scrape_task")
    user_session = context.bot_data.get("user_session")
    if not task or task.done():
        await update.effective_message.reply_text("No active scrape to stop.")
        return

    # Tier 1: Set the cancel event (graceful stop)
    if cancel_event:
        cancel_event.set()

    status_msg = await update.effective_message.reply_text(
        "🛑 Stop signal sent. Waiting for graceful stop (up to 10s)..."
    )

    # Wait up to 10 seconds for graceful stop
    import asyncio as _asyncio
    stopped_gracefully = False
    for _ in range(10):
        await _asyncio.sleep(1)
        if task.done():
            stopped_gracefully = True
            break

    if not stopped_gracefully:
        # Tier 2: Force cancel the task
        try:
            task.cancel()
            await _asyncio.sleep(3)
            if task.done():
                await status_msg.edit_text(
                    "🛑 Scrape force-stopped via task.cancel().\n"
                    "Use /scrape_status to see final stats."
                )
                return
        except Exception:
            pass

        # Tier 3: Disconnect the Telethon client (last resort)
        # This tears down the blocked transport, unblocking any stuck
        # socket reads in get_messages or send_file.
        if user_session:
            try:
                await status_msg.edit_text(
                    "🛑 Scrape didn't respond to cancel. Disconnecting Telethon client..."
                )
                await user_session.client.disconnect()
                await _asyncio.sleep(2)
                # Reconnect for future use
                await user_session._ensure_connected()
                await status_msg.edit_text(
                    "🛑 Scrape force-stopped via client disconnect.\n"
                    "Telethon reconnected. Use /scrape_status to see final stats."
                )
            except Exception as e:
                await status_msg.edit_text(
                    f"🛑 Force-stop attempted.\n"
                    f"Error: {type(e).__name__}: {e}\n"
                    f"Try: docker-compose restart forwarder-bot"
                )
    else:
        await status_msg.edit_text(
            "✅ Scrape stopped gracefully.\n"
            "Use /scrape_status to see final stats."
        )


async def cmd_scrape_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current scrape status."""
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return
    status = context.bot_data.get("scrape_status")
    task = context.bot_data.get("scrape_task")
    if not status:
        await update.effective_message.reply_text("No scrape has been started yet.")
        return
    running = task and not task.done()
    elapsed = time.time() - status.get("started_at", 0) if status.get("started_at") else 0
    state_str = "🟢 running" if running else "🔴 finished"

    # Live wait phase (flood wait / recovery break) with countdown, plus a
    # liveness line — so "waiting 20 min for the rate budget" is clearly
    # distinguishable from "hung".
    phase_str = ""
    if running:
        pl = _scrape_phase_line(context)
        if pl:
            phase_str = f"\n⏳ Currently: {pl}\n"
    liveness_str = ""
    last_upd = status.get("last_update", 0)
    if running and last_upd:
        idle = time.time() - last_upd
        liveness_str = f"\nLast progress: {_fmt_countdown(idle)} ago"

    await update.effective_message.reply_text(
        f"📊 Scrape status: {state_str}\n\n"
        f"Source: `{status.get('source_ref', '?')}`\n"
        f"Destination: {status.get('dest_label', '?')}\n"
        f"Order: {status.get('order', '?')}\n"
        f"Filter: {status.get('filter', 'ALL media')}\n"
        f"Parallel: {status.get('parallel', 3)}\n"
        f"Elapsed: {elapsed:.0f} sec\n"
        f"{phase_str}"
        f"{liveness_str}\n\n"
        f"Total seen: {status.get('total_seen', 0)}\n"
        f"Sent: {status.get('sent_count', 0)}\n"
        f"Failed: {status.get('failed_count', 0)}\n"
        f"Skipped (filtered/no media): {status.get('skipped_count', 0)}\n"
        f"Flood waits: {status.get('flood_waits', 0)}\n"
        f"Last msg ID: {status.get('last_message_id', 0)}",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    # Remove all pending forwards for this user
    async with db._conn.execute(
        "DELETE FROM pending_forwards WHERE user_id = ?",
        (update.effective_user.id,),
    ) as cur:
        await db._conn.commit()
        n = cur.rowcount or 0
    # Cancel pending batch tasks (time-based batch window)
    # Look for both old and new batch keys for backward compatibility
    batches = context.chat_data.pop("msg_batches", None)
    if batches:
        for batch in batches.values():
            t = batch.get("task")
            if t and not t.done():
                t.cancel()
    # Legacy key (old album_batches)
    legacy_batches = context.chat_data.pop("album_batches", None)
    if legacy_batches:
        for batch in legacy_batches.values():
            t = batch.get("task")
            if t and not t.done():
                t.cancel()
    await update.effective_message.reply_text(f"Cancelled {n} pending forward(s).")


async def cmd_reconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reconnect the Telethon user session without restarting the bot.

    Useful if the session failed to start at boot (e.g. temporary network
    issue) or if the connection dropped and auto-reconnect failed.
    """
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return

    status_msg = await update.effective_message.reply_text("🔄 Reconnecting Telethon session...")

    # Check if credentials are present
    if not cfg.api_id or not cfg.api_hash:
        await status_msg.edit_text(
            "❌ Cannot reconnect: API_ID or API_HASH is missing in .env\n"
            "Edit telegram-forwarder-bot/.env and restart: docker-compose restart forwarder-bot"
        )
        return
    if not cfg.session_string:
        await status_msg.edit_text(
            "❌ Cannot reconnect: SESSION_STRING is missing in .env\n"
            "Run `python login.py --string` locally, then update .env and restart."
        )
        return

    # Stop existing session if any
    old_session = context.bot_data.get("user_session")
    if old_session:
        try:
            await old_session.stop()
        except Exception:
            pass

    # Create and start a new session
    from user_session import UserSession
    new_session = UserSession(
        cfg.session_name, cfg.api_id, cfg.api_hash,
        session_string=cfg.session_string,
        flood_sleep_threshold=cfg.flood_sleep_threshold,
    )
    ok = await new_session.start()
    if ok:
        context.bot_data["user_session"] = new_session
        # Re-init the TopicManager with the new session
        from topics import TopicManager
        context.bot_data["topics"] = TopicManager(context.bot_data["db"], new_session)
        await status_msg.edit_text(
            "✅ Telethon session reconnected successfully!\n"
            "Locked-channel forwarding and topic discovery are now available."
        )
    else:
        context.bot_data["user_session"] = None
        await status_msg.edit_text(
            "❌ Telethon session failed to reconnect.\n\n"
            "Possible causes:\n"
            "  1. SESSION_STRING is invalid or revoked\n"
            "  2. Network issue connecting to Telegram\n"
            "  3. API_ID/API_HASH mismatch\n\n"
            "Fix: Re-run `python login.py --string` locally,\n"
            "update SESSION_STRING in .env, then:\n"
            "docker-compose restart forwarder-bot"
        )


async def cmd_scrapeid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward messages by ID range — NO getHistory, avoids rate limits.

    Usage:
      /scrapeid <url>                    — forward ALL messages (auto-detect range)
      /scrapeid <url> 1 5000             — forward IDs 1 to 5000
      /scrapeid <url> 1000 2000 saved    — forward IDs 1000-2000 to Saved Messages
      /scrapeid <url> 1 5000 keep        — keep "Forwarded from" header
      /scrapeid <url> 1 5000 strip       — strip ALL captions from media
      /scrapeid <url> 1 5000 keep strip  — keep header AND strip captions

    This is the RECOMMENDED method for large public channels because:
    - Uses forward_messages(ids, from_peer) — no getHistory API calls
    - Uses the SEND rate-limit bucket (not getHistory)
    - 100 messages per API call
    - No ~1800-message FloodWait cliff
    - Works for channels with 50k+ messages

    Flags:
      saved  — send to Saved Messages instead of destination group
      keep   — keep "Forwarded from" header (default: strip)
      strip  — strip ALL captions from media (default: keep captions)

    For protected channels (content protection enabled), use /scrape instead.
    """
    import asyncio
    import time as _time
    from user_session import parse_channel_link, parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/scrapeid DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session:
        await update.effective_message.reply_text(
            "❌ Telethon session failed to start at boot.\n\n"
            "Send /reconnect to retry."
        )
        return
    if not await user_session._ensure_connected():
        await update.effective_message.reply_text(
            "❌ Telethon session disconnected. Try /reconnect or restart."
        )
        return

    # Check if there's already an active scrape
    if context.bot_data.get("scrape_task") and not context.bot_data["scrape_task"].done():
        await update.effective_message.reply_text(
            "⚠️ A scrape is already running. Use /stop_scrape to stop it first."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/scrapeid <url> [start_id] [end_id] [saved] [keep] [resume]`\n\n"
            "Examples:\n"
            "  `/scrapeid https://t.me/channelname`\n"
            "  `/scrapeid https://t.me/channelname 1 5000`\n"
            "  `/scrapeid https://t.me/c/1234567890 1000 2000 saved`\n"
            "  `/scrapeid https://t.me/channelname 1 10000 keep`\n"
            "  `/scrapeid https://t.me/channelname resume` — continue after a stop\n\n"
            "Flags:\n"
            "  `saved` — send to Saved Messages instead of destination group\n"
            "  `keep`  — keep 'Forwarded from' header (default: strip)\n"
            "  `strip` — strip all media captions\n"
            "  `resume` — continue from the last checkpoint of this channel\n\n"
            "This method uses forward_messages by ID — NO getHistory rate limits.\n"
            "Recommended for large public channels (10k+ messages).",
            parse_mode="Markdown",
        )
        return

    # Parse args
    url = context.args[0]
    raw_flags = [a.lower() for a in context.args[1:]]
    send_to_saved = "saved" in raw_flags
    keep_author = "keep" in raw_flags
    strip_captions = "strip" in raw_flags
    do_resume = "resume" in raw_flags

    # Parse start_id and end_id
    start_id = 1
    end_id = 0  # 0 = auto-detect
    id_args = [a for a in raw_flags if a not in ("saved", "keep", "strip", "resume")]
    if len(id_args) >= 1:
        try:
            start_id = int(id_args[0])
        except ValueError:
            await update.effective_message.reply_text(f"❌ Invalid start_id: {id_args[0]}")
            return
    if len(id_args) >= 2:
        try:
            end_id = int(id_args[1])
        except ValueError:
            await update.effective_message.reply_text(f"❌ Invalid end_id: {id_args[1]}")
            return

    # Parse URL
    parsed = parse_channel_link(url)
    if not parsed:
        parsed_post = parse_telegram_link(url)
        if parsed_post:
            parsed = type(parsed_post)(kind=parsed_post.kind,
                                        chat_ref=parsed_post.chat_ref,
                                        message_id=0)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported:\n"
            f"  • `https://t.me/c/1234567890` (private)\n"
            f"  • `https://t.me/channelname` (public)",
            parse_mode="Markdown",
        )
        return

    # Determine destination
    if send_to_saved:
        dest_chat_id = "me"
        dest_label = "Saved Messages"
    else:
        db = context.bot_data["db"]
        dest_chat_id = cfg.destination_group_id
        if dest_chat_id is None:
            v = await db.get_runtime("destination_group_id")
            dest_chat_id = int(v) if v else None
        if dest_chat_id is None:
            await update.effective_message.reply_text(
                "No destination group set. Either:\n"
                "  • /setgroup <group_id> first, OR\n"
                "  • Use /scrapeid <url> ... saved"
            )
            return
        dest_label = f"chat {dest_chat_id}"

    # ── Resume from checkpoint (state saving) ────────────────────────────
    # /scrapeid walks IDs ascending, so resume = start at ckpt + 1.
    resume_note = ""
    if do_resume:
        _db = context.bot_data.get("db")
        ckpt_raw = await _db.get_runtime(f"scrape_checkpoint:{parsed.chat_ref}") if _db else None
        if ckpt_raw:
            try:
                ckpt = int(ckpt_raw)
            except (TypeError, ValueError):
                ckpt = 0
            if ckpt > 0:
                start_id = ckpt + 1
                resume_note = (f"\n♻️ Resuming from checkpoint: msg {ckpt} "
                               f"(starting at ID {start_id})")
        else:
            await update.effective_message.reply_text(
                "ℹ️ No checkpoint found for this channel — starting from ID 1."
            )

    # Initial status message
    range_str = f"{start_id} to {'auto' if end_id == 0 else end_id}"
    status_msg = await update.effective_message.reply_text(
        f"🚀 Starting ID-based forward...\n\n"
        f"Source: `{parsed.chat_ref}`\n"
        f"Destination: {dest_label}\n"
        f"ID range: {range_str}\n"
        f"Keep author: {keep_author}\n"
        f"Strip captions: {strip_captions}{resume_note}\n\n"
        f"_Uses forward_messages by ID — no getHistory rate limits._\n"
        f"_Send /stop_scrape to cancel._",
        parse_mode="Markdown",
    )

    # Set up cancellation and status
    cancel_event = asyncio.Event()
    context.bot_data["scrape_cancel"] = cancel_event
    context.bot_data["scrape_status"] = {
        "sent_count": 0, "failed_count": 0, "skipped_count": 0,
        "total_seen": 0, "last_message_id": 0, "started_at": _time.time(),
        "source_ref": parsed.chat_ref, "dest_label": dest_label,
        "order": "id-ascending", "filter": "ALL (forward by ID)",
        "parallel": 100, "in_flight": 0,
    }
    # Shared wait-phase dict — scrape_channel_by_ids publishes flood waits
    # and recovery breaks here; the ticker below renders them as LIVE
    # countdowns. /scrapeid previously had NO ticker at all, so its status
    # message went completely static during breaks/flood waits — exactly
    # the "stuck, nothing updates" symptom.
    phase_state = _new_phase_state()
    context.bot_data["scrape_phase"] = phase_state

    # Status callbacks — same locked-edit + ticker design as /scrape:
    # stats_callback only touches bot_data; the 2s ticker is the ONLY thing
    # that edits the message; milestone notices force-edit but the ticker's
    # live text (which includes the wait phase) follows 2s later.
    status_lock = asyncio.Lock()
    last_edit_time = {"time": 0.0}
    ticker_task_ref = {"task": None}
    started_at = _time.time()

    async def _edit_status(text, force=False):
        """Locked, rate-limit-aware edit of the status message."""
        async with status_lock:
            now = _time.time()
            if not force and now - last_edit_time["time"] < 1.5:
                return
            last_edit_time["time"] = now
            try:
                await status_msg.edit_text(text)
            except Exception as e:
                err_str = str(e).lower()
                if "too many requests" in err_str or "retry after" in err_str:
                    # Back off — editing too fast; ticker will catch up
                    last_edit_time["time"] = now + 3.0
                # "not modified" and everything else: ignore, never fatal

    # Live status ticker (shared with /scrape): renders counts PLUS the
    # active wait phase with a ticking countdown + liveness line.
    ticker_task_ref["task"] = asyncio.create_task(
        _run_scrape_status_ticker(context, _edit_status, started_at)
    )

    async def status_callback(text):
        context.bot_data["scrape_status"]["last_update"] = _time.time()
        await _edit_status(text, force=True)

    async def stats_callback(result_dict):
        context.bot_data["scrape_status"].update({
            "sent_count": result_dict.get("sent_count", 0),
            "failed_count": result_dict.get("failed_count", 0),
            "total_seen": result_dict.get("total_seen", 0),
            "last_message_id": result_dict.get("last_message_id", 0),
            "flood_waits": result_dict.get("flood_waits", 0),
            "in_flight": result_dict.get("in_flight", 0),
            "last_update": _time.time(),
        })
        # Checkpoint (state saving) — pick up exactly where we stopped
        ckpt_id = result_dict.get("last_message_id", 0)
        if ckpt_id:
            _db = context.bot_data.get("db")
            if _db:
                try:
                    await _db.set_runtime(
                        f"scrape_checkpoint:{parsed.chat_ref}", str(ckpt_id)
                    )
                except Exception:
                    pass

    # Run as background task
    async def scrape_task():
        scrape_result = None
        try:
            scrape_result = await user_session.scrape_channel_by_ids(
                source_chat_ref=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
                dest_chat_id=dest_chat_id,
                start_id=start_id,
                end_id=end_id,
                cancel_event=cancel_event,
                status_callback=status_callback,
                stats_callback=stats_callback,
                drop_author=not keep_author,
                drop_media_captions=strip_captions,
                flood_break_every=cfg.flood_break_every,
                flood_break_seconds=cfg.flood_break_seconds,
                phase_state=phase_state,
            )
            # Update cumulative stats
            db = context.bot_data.get("db")
            if db and isinstance(scrape_result, dict):
                try:
                    await db.increment_stat("total_scrapes", 1)
                    await db.increment_stat("total_sent", scrape_result.get("sent_count", 0))
                    await db.increment_stat("total_failed", max(0, scrape_result.get("failed_count", 0)))
                except Exception:
                    pass
        except Exception as e:
            logger.exception("/scrapeid task failed")
            try:
                await status_msg.edit_text(f"❌ Scrape crashed: {type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            # Show final completion message
            try:
                elapsed = _time.time() - started_at
                if scrape_result and isinstance(scrape_result, dict):
                    sent = scrape_result.get("sent_count", 0)
                    failed = scrape_result.get("failed_count", 0)
                    total = scrape_result.get("total_seen", 0)
                    flood = scrape_result.get("flood_waits", 0)
                    last_id = scrape_result.get("last_message_id", 0)
                    cancelled = scrape_result.get("cancelled", False)

                    status_line = ">>> SCRAPE CANCELLED <<<" if cancelled else ">>> SCRAPE COMPLETE <<<"
                    final_text = (
                        f"{status_line}\n\n"
                        f"Source:      {parsed.chat_ref}\n"
                        f"Destination: {dest_label}\n"
                        f"Duration:    {elapsed:.0f}s\n"
                        f"----------------------------------------\n"
                        f"Sent:        {sent}\n"
                        f"Failed:      {failed}\n"
                        f"Total IDs:   {total}\n"
                        f"Flood waits: {flood}\n"
                        f"Last ID:     {last_id}"
                    )
                else:
                    final_text = f">>> SCRAPE ENDED <<<\nDuration: {elapsed:.0f}s"
                await _edit_status(final_text, force=True)
            except Exception:
                pass
            # Stop the status ticker (it already did its final edit when
            # it noticed the task finish, but cancel it defensively)
            t = ticker_task_ref.get("task")
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass
            context.bot_data.pop("scrape_task", None)
            context.bot_data.pop("scrape_cancel", None)
            context.bot_data.pop("scrape_phase", None)

    context.bot_data["scrape_task"] = asyncio.create_task(scrape_task())
    logger.info("ID-based scrape started for %s -> %s", parsed.chat_ref, dest_label)


def register_admin_handlers(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("addtopic", cmd_addtopic))
    app.add_handler(CommandHandler("deltopic", cmd_deltopic))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("test_link", cmd_test_link))
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CommandHandler("scrape", cmd_scrape))
    app.add_handler(CommandHandler("scrapeid", cmd_scrapeid))
    app.add_handler(CommandHandler("stop_scrape", cmd_stop_scrape))
    app.add_handler(CommandHandler("scrape_status", cmd_scrape_status))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("reconnect", cmd_reconnect))
