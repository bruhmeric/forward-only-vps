"""Telethon user-session manager.

Used to:
  * Enumerate forum topics in the destination group (requires the user account
    to be a member of that group)
  * Fetch messages from locked / private channels the user account is a member
    of, including channels where forwarding is disabled by the admin

Login flow is handled by `login.py` separately so that bot.py never needs to
prompt interactively.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, FloodWaitError, InviteHashInvalidError
from telethon.tl import types as tl
from telethon.tl.functions.messages import GetForumTopicsRequest

logger = logging.getLogger(__name__)


# ---------- link parsing ----------

# Private channel link: https://t.me/c/1234567890/42
PRIVATE_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/c/(\d+)/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)
# Public channel link: https://t.me/channelname/42
PUBLIC_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)


@dataclass
class ParsedLink:
    kind: str  # 'private' | 'public' | 'invite'
    chat_ref: str  # '-1001234567890' or 'channelname' or invite hash
    message_id: int
    # For albums: a list of message ids, else None
    extra_message_ids: Optional[list[int]] = None


def parse_telegram_link(url: str) -> Optional[ParsedLink]:
    """Return a ParsedLink if `url` looks like a t.me deep link to a post."""
    url = url.strip()
    m = PRIVATE_LINK_RE.search(url)
    if m:
        # Telegram private channel link format: t.me/c/<raw_id>/<msg_id>
        # Bot API chat_id = -100 concatenated with raw_id, i.e. -1e12 - raw_id
        raw_id = int(m.group(1))
        chat_id = -1_000_000_000_000 - raw_id
        msg_id = int(m.group(2))
        return ParsedLink(kind="private", chat_ref=str(chat_id), message_id=msg_id)
    m = PUBLIC_LINK_RE.search(url)
    if m:
        # Skip if this is actually an invite link like t.me/+abc...; the regex
        # already requires the first char to be a letter, so 'abc' style invites
        # won't match — but we still need to reject '+' and 'joinchat' links.
        username = m.group(1)
        if username.lower() in ("joinchat", "share", "addstickers", "setlanguage"):
            return None
        msg_id = int(m.group(2))
        return ParsedLink(kind="public", chat_ref=username, message_id=msg_id)
    # t.me/+abc... (private invite, no message_id) — we don't auto-join, skip
    return None


# Regex for channel-only links (no message_id):
# https://t.me/c/1234567890  (private channel, no msg_id)
# https://t.me/channelname   (public channel, no msg_id)
PRIVATE_CHANNEL_ONLY_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/c/(\d+)/?$",
    re.IGNORECASE,
)
PUBLIC_CHANNEL_ONLY_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$",
    re.IGNORECASE,
)


def parse_channel_link(url: str) -> Optional[ParsedLink]:
    """Parse a channel-only link (no message_id) — used by /scrape.

    Returns a ParsedLink with message_id=0 (signaling "no specific message")
    or None if the URL doesn't match.
    """
    url = url.strip()
    # Strip any trailing query string or fragment
    url = url.split("?")[0].split("#")[0]

    m = PRIVATE_CHANNEL_ONLY_RE.match(url)
    if m:
        raw_id = int(m.group(1))
        chat_id = -1_000_000_000_000 - raw_id
        return ParsedLink(kind="private", chat_ref=str(chat_id), message_id=0)

    m = PUBLIC_CHANNEL_ONLY_RE.match(url)
    if m:
        username = m.group(1)
        if username.lower() in ("joinchat", "share", "addstickers", "setlanguage"):
            return None
        return ParsedLink(kind="public", chat_ref=username, message_id=0)

    return None


# ---------- flood-wait / rate-limit helpers ----------

async def _sleep_chunks(seconds: float, cancel_event=None, chunk: float = 1.0) -> bool:
    """Sleep for `seconds` in small, cancellable chunks.

    Returns True if the full delay elapsed, False if `cancel_event` was set
    mid-sleep (task cancellation still raises CancelledError as usual).

    Used everywhere instead of raw `asyncio.sleep` for long waits (FloodWait
    sleeps, inter-batch delays) so `/stop_scrape` stays responsive even
    during multi-minute server-requested waits. Unlike the ad-hoc loops it
    replaced, it also supports fractional delays.
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(chunk, remaining))


class AdaptivePacer:
    """Inter-request delay that backs off when Telegram complains.

    The scrapes used fixed delays chosen exactly at (or above) Telegram's
    rate budgets, so big channels always hit FloodWait at the same point
    (~2000 messages) and then re-offended at the same pace after every
    sleep — escalating waits until the retry budget ran out. The pacer
    instead *grows* the delay multiplicatively whenever a FloodWait is
    seen and slowly relaxes it back towards the base after a streak of
    quiet batches, converging on the fastest flood-free pace.
    """

    def __init__(self, base: float, maximum: float = 15.0, factor: float = 1.5,
                 recover_after: int = 8):
        self.base = base
        self.maximum = maximum
        self.factor = factor
        self.recover_after = recover_after
        self.current = base
        self._clean_streak = 0

    def on_flood(self, seconds: float = 0) -> float:
        """Grow the delay after a FloodWait. Returns the new delay."""
        # Grow at least 1.5x; also honour (a fraction of) the server's own
        # requested wait so repeated violations quickly become unlikely.
        target = self.current * self.factor + 0.5
        if seconds and seconds > 0:
            target = max(target, min(seconds, self.maximum) / 3.0)
        self.current = min(target, self.maximum)
        self._clean_streak = 0
        return self.current

    def on_success(self) -> None:
        """Count a clean batch; gently recover towards the base pace."""
        self._clean_streak += 1
        if self._clean_streak >= self.recover_after:
            self.current = max(self.base, self.current * 0.9)
            self._clean_streak = 0


_FORWARD_SUPPORTS_DROP_MEDIA_CAPTIONS: Optional[bool] = None


def _forward_kwargs(drop_author: bool, drop_media_captions: bool) -> dict:
    """Build kwargs for TelegramClient.forward_messages.

    `drop_media_captions` only exists on Telethon >= 1.40 — passing it to
    older versions raises TypeError on every call. Detect support once and
    degrade gracefully (with a logged warning) instead of crashing.
    """
    global _FORWARD_SUPPORTS_DROP_MEDIA_CAPTIONS
    if _FORWARD_SUPPORTS_DROP_MEDIA_CAPTIONS is None:
        try:
            params = inspect.signature(TelegramClient.forward_messages).parameters
            _FORWARD_SUPPORTS_DROP_MEDIA_CAPTIONS = "drop_media_captions" in params
        except (TypeError, ValueError):
            _FORWARD_SUPPORTS_DROP_MEDIA_CAPTIONS = False
    kwargs: dict = {"drop_author": drop_author}
    if _FORWARD_SUPPORTS_DROP_MEDIA_CAPTIONS:
        kwargs["drop_media_captions"] = drop_media_captions
    elif drop_media_captions:
        logger.warning(
            "Installed Telethon does not support drop_media_captions "
            "(needs >= 1.40) — captions will be kept. Upgrade Telethon."
        )
    return kwargs


# ---------- session manager ----------

class UserSession:
    def __init__(self, session_name: str, api_id: int, api_hash: str,
                 session_string: Optional[str] = None) -> None:
        """Create a Telethon client backed by either a file-based session
        (session_name) or a StringSession (session_string). StringSession is
        preferred for ephemeral filesystems like Render's free tier.

        Enables Telethon's built-in auto_reconnect and connection_retries
        to handle transient network drops silently — combined with our
        own _ensure_connected() for cases where auto_reconnect fails."""
        client_kwargs = dict(
            api_id=api_id,
            api_hash=api_hash,
            # Telethon's built-in auto-reconnect: if the connection drops,
            # it will try to reconnect automatically. This handles most
            # transient drops without us needing to intervene.
            connection_retries=5,    # retry 5 times on connect failure
            retry_delay=2,           # 2s between retries
            auto_reconnect=True,     # reconnect automatically on disconnect
            request_retries=3,       # retry failed requests 3 times
        )
        if session_string:
            from telethon.sessions import StringSession
            self.client = TelegramClient(StringSession(session_string), **client_kwargs)
            self._uses_string_session = True
        else:
            self.client = TelegramClient(session_name, **client_kwargs)
            self._uses_string_session = False
        self._started = False

    async def start(self) -> bool:
        """Connect using an existing session. Returns False if no session
        exists or the session is invalid. Callers should NOT call interactive
        login here — that's done by login.py."""
        if not self._uses_string_session and not os.path.exists(self.session_filename):
            logger.warning("Telethon session file missing — run python login.py first.")
            return False
        try:
            logger.info("Telethon: connecting to Telegram...")
            await self.client.connect()
            logger.info("Telethon: connected, checking authorization...")
            if not await self.client.is_user_authorized():
                logger.warning("Telethon: session exists but is NOT authorized. "
                               "The SESSION_STRING may be invalid or revoked.")
                await self.client.disconnect()
                return False
            self._started = True
            logger.info("Telethon user session connected as %s (mode=%s)",
                        await self._safe_get_me(),
                        "string" if self._uses_string_session else "file")
            return True
        except Exception as e:
            logger.exception("Failed to start Telethon client: %s: %s", type(e).__name__, e)
            return False

    async def _safe_get_me(self) -> str:
        try:
            me = await self.client.get_me()
            return f"@{me.username} ({me.id})" if me else "?"
        except Exception:
            return "?"

    @property
    def session_filename(self) -> str:
        if self._uses_string_session:
            return "<string-session>"
        # Telethon stores session in <session_name>.session
        try:
            return f"{self.client.session.filename}.session" if (
                hasattr(self.client, "session") and self.client.session
            ) else f"{self.client.api_id}.session"
        except Exception:
            return "user_session.session"

    async def stop(self) -> None:
        if self._started:
            await self.client.disconnect()
            self._started = False

    @property
    def available(self) -> bool:
        """Returns True only if the session was started AND the underlying
        Telethon client is still connected. Telethon connections can drop
        silently (network blips, Telegram idle disconnects, container
        restarts), so checking _started alone is not enough — we must also
        verify the live connection state."""
        if not self._started:
            return False
        try:
            return self.client.is_connected()
        except Exception:
            return False

    async def _ensure_connected(self) -> bool:
        """Ensure the Telethon client is connected. If it has dropped
        (network blip, Telegram idle disconnect, container restart),
        reconnect silently. Returns True if connected (either was already
        connected, or reconnection succeeded).

        This is called at the start of every operation that hits the
        Telegram API (scrape, /saved, /refresh, link fetch) so that
        long-idle bots don't fail with 'ConnectionError: Cannot send
        requests while disconnected'.
        """
        # Fast path: still connected
        try:
            if self.client.is_connected():
                return True
        except Exception:
            pass

        # Slow path: reconnect
        logger.info("Telethon client disconnected — attempting reconnect…")
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error("Telethon reconnected but session is not authorized. "
                             "The SESSION_STRING may have been revoked.")
                self._started = False
                return False
            self._started = True
            logger.info("Telethon user session reconnected as %s",
                        await self._safe_get_me())
            return True
        except Exception:
            logger.exception("Failed to reconnect Telethon client")
            self._started = False
            return False

    # ---------- entity resolution (with dialog cache refresh) ----------

    async def _resolve_entity(self, chat_ref, diag: list[str] | None = None):
        """Resolve a chat reference to a Telethon entity, refreshing the
        dialog cache if the entity isn't found.

        This fixes the common 'ValueError: Could not find the input entity
        for PeerChannel(...)' error that happens with StringSession on
        Render. StringSession keeps the entity cache in memory only — after
        a restart, the cache is empty and get_entity fails for channels
        that haven't been accessed since startup.

        Fix: call get_dialogs() to fetch the user's chat list from
        Telegram's API. This populates the session cache with access_hash
        for all channels/groups the user is a member of. Then retry.

        Also handles 'ConnectionError: Cannot send requests while disconnected'
        by auto-reconnecting before retrying — Telethon connections can drop
        silently after long idle periods (Telegram server-side disconnects
        after ~5-10 min of inactivity).

        Args:
          chat_ref: int chat_id (e.g. -1003617504074) or str username
          diag: optional diagnostic list to append steps to

        Returns:
          The resolved entity (Channel, User, Chat, etc.)

        Raises:
          ValueError if the entity can't be resolved even after refreshing
          ConnectionError if the client can't be reconnected
        """
        # Ensure the Telethon client is connected before hitting the API.
        # This handles the case where the connection has dropped silently
        # since startup (Telegram idle disconnect, network blip, etc.).
        if not await self._ensure_connected():
            err = "Telethon client is disconnected and could not be reconnected. "
            err += "Check the bot logs — the SESSION_STRING may be invalid or revoked."
            if diag is not None:
                diag.append(f"✗ {err}")
            raise ConnectionError(err)

        try:
            entity = await self.client.get_entity(chat_ref)
            return entity
        except ValueError:
            # Entity not in cache — refresh dialogs
            if diag is not None:
                diag.append(f"  → Entity not in cache. Refreshing dialogs...")
            logger.info("Entity %s not in cache — calling get_dialogs() to refresh", chat_ref)

            # Fetch dialogs (up to 200) to populate the session cache
            # with access_hash for all channels/groups the user is a member of
            try:
                async for _dialog in self.client.iter_dialogs(limit=200):
                    pass  # just iterating to populate the cache
                if diag is not None:
                    diag.append(f"  → Dialogs refreshed. Retrying entity resolution...")
            except ConnectionError as e:
                # Connection dropped mid-refresh — try one reconnect+retry
                logger.warning("Connection dropped during dialog refresh: %s", e)
                if diag is not None:
                    diag.append(f"  → Connection dropped. Reconnecting...")
                if await self._ensure_connected():
                    if diag is not None:
                        diag.append(f"  → Reconnected. Retrying dialog refresh...")
                    try:
                        async for _dialog in self.client.iter_dialogs(limit=200):
                            pass
                        if diag is not None:
                            diag.append(f"  → Dialogs refreshed after reconnect.")
                    except Exception as e2:
                        logger.warning("Dialog refresh after reconnect also failed: %s", e2)
                        if diag is not None:
                            diag.append(f"  → Dialog refresh after reconnect failed: {type(e2).__name__}: {e2}")
                else:
                    raise ConnectionError(
                        f"Telethon connection dropped during dialog refresh and "
                        f"could not be reconnected: {e}"
                    )
            except Exception as e:
                logger.warning("get_dialogs() failed: %s", e)
                if diag is not None:
                    diag.append(f"  → Dialog refresh failed: {type(e).__name__}: {e}")

            # Retry get_entity now that the cache should be populated
            try:
                entity = await self.client.get_entity(chat_ref)
                if diag is not None:
                    diag.append(f"  → ✓ Entity resolved after dialog refresh")
                return entity
            except ValueError:
                # Still not found — try a larger dialog fetch
                if diag is not None:
                    diag.append(f"  → Still not found. Trying larger dialog fetch (1000)...")
                logger.info("Entity still not in cache after first refresh — trying 1000 dialogs")
                try:
                    async for _dialog in self.client.iter_dialogs(limit=1000):
                        pass
                except Exception:
                    pass

                try:
                    entity = await self.client.get_entity(chat_ref)
                    if diag is not None:
                        diag.append(f"  → ✓ Entity resolved after larger dialog fetch")
                    return entity
                except ValueError:
                    raise ValueError(
                        f"Could not resolve entity for {chat_ref} even after "
                        f"refreshing dialogs. Make sure your user account is a "
                        f"member of this chat. If it is, try sending a message "
                        f"in that chat manually (via your Telegram app) to "
                        f"populate the cache, then retry."
                    )
        except ConnectionError:
            # Connection dropped on the initial get_entity — reconnect and retry once
            logger.warning("ConnectionError on get_entity(%s) — reconnecting and retrying", chat_ref)
            if diag is not None:
                diag.append(f"  → ConnectionError. Reconnecting and retrying...")
            if await self._ensure_connected():
                entity = await self.client.get_entity(chat_ref)
                if diag is not None:
                    diag.append(f"  → ✓ Entity resolved after reconnect")
                return entity
            raise

    # ---------- forum topic enumeration ----------

    async def list_forum_topics(self, chat_id: int) -> list[dict]:
        """Return list of {'id': int, 'title': str} for all forum topics except
        the General topic (id=1)."""
        try:
            peer = await self._resolve_entity(chat_id)
        except Exception:
            logger.exception("_resolve_entity failed for chat_id=%s", chat_id)
            return []

        topics: list[dict] = []
        offset_id = 0
        # Paginate up to 500 topics
        for _ in range(10):
            try:
                result = await self.client(GetForumTopicsRequest(
                    peer=peer,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=0,
                    limit=100,
                ))
            except Exception:
                logger.exception("GetForumTopicsRequest failed")
                break
            for t in result.topics:
                # Skip General topic (id=1)
                if getattr(t, "id", 0) == 1:
                    continue
                title = getattr(t, "title", None) or f"Topic {t.id}"
                topics.append({"id": t.id, "title": title})
            # GetForumTopics returns topics ordered by creation date; we use
            # offset_id = last topic's top_message id, but Telethon's wrapper
            # already handles pagination internally in most cases. For
            # simplicity, we break if fewer than 100 returned.
            if len(result.topics) < 100:
                break
            # advance offset — use the last topic's top_message id
            offset_id = getattr(result.topics[-1], "top_message", 0) or 0
        return topics

    # ---------- message fetching for locked channels ----------

    async def fetch_message(self, parsed: ParsedLink) -> tuple[Optional[dict], list[str]]:
        """Fetch a single message (and possibly its album siblings) from a
        private / public channel the user account is a member of.

        Returns a tuple of (result_or_none, diagnostics_log) where:
          - result_or_none is a dict with keys:
              chat_id: int (negative for channels)
              message: telethon Message object (single)
              album: list[Message] | None  (album siblings if any)
            ...or None if fetch failed
          - diagnostics_log is a list of human-readable strings describing
            each step that was attempted (for surfacing to the user when
            something fails)

        The diagnostics_log is what made the difference — previously the bot
        would say "Couldn't fetch that message" with no detail. Now the user
        gets the full step-by-step trace so they can pinpoint the failure
        (e.g. "Step 2 failed: ChannelPrivateError - you're not a member").
        """
        diag: list[str] = []
        diag.append(f"Step 1: Parsed link → kind={parsed.kind}, chat_ref={parsed.chat_ref}, msg_id={parsed.message_id}")

        # Step 2: resolve entity
        try:
            if parsed.kind == "private":
                chat_id = int(parsed.chat_ref)
                diag.append(f"Step 2: Resolving entity for chat_id={chat_id} via _resolve_entity()...")
                entity = await self._resolve_entity(chat_id, diag)
            else:
                diag.append(f"Step 2: Resolving entity for username={parsed.chat_ref} via _resolve_entity()...")
                entity = await self._resolve_entity(parsed.chat_ref, diag)
            diag.append(f"Step 2: ✓ Entity resolved → {type(entity).__name__} (id={getattr(entity, 'id', '?')})")
        except ChannelPrivateError as e:
            diag.append(f"Step 2: ✗ FAILED — ChannelPrivateError: {e}")
            diag.append("  → Your user account is NOT a member of this chat, or the chat doesn't exist.")
            return None, diag
        except Exception as e:
            diag.append(f"Step 2: ✗ FAILED — {type(e).__name__}: {e}")
            return None, diag

        # Step 3: fetch the message
        try:
            diag.append(f"Step 3: Fetching message id={parsed.message_id} via get_messages()...")
            messages = await self.client.get_messages(entity, ids=parsed.message_id)
        except Exception as e:
            diag.append(f"Step 3: ✗ FAILED — {type(e).__name__}: {e}")
            return None, diag

        if not messages:
            diag.append(f"Step 3: ✗ No message returned — message_id={parsed.message_id} may not exist in this chat.")
            return None, diag

        msg = messages[0] if isinstance(messages, list) else messages
        if not msg:
            diag.append("Step 3: ✗ Message object is empty.")
            return None, diag

        diag.append(f"Step 3: ✓ Message fetched (id={msg.id}, has_media={bool(getattr(msg, 'media', None))})")

        # Step 4: detect album
        album: list = []
        if getattr(msg, "grouped_id", None):
            try:
                diag.append(f"Step 4: Message is part of an album (grouped_id={msg.grouped_id}). Fetching siblings...")
                all_msgs = await self.client.get_messages(entity, limit=20)
                album = [m for m in all_msgs
                         if getattr(m, "grouped_id", None) == msg.grouped_id]
                album.sort(key=lambda m: m.id)
                diag.append(f"Step 4: ✓ Found {len(album)} sibling(s) in the album")
            except Exception as e:
                diag.append(f"Step 4: ✗ Album fetch failed (continuing with single message) — {type(e).__name__}: {e}")
                album = []
        else:
            diag.append("Step 4: Message is NOT part of an album (single message).")

        # Step 5: compute chat_id in Bot API format
        chat_real_id = msg.peer_id.channel_id if isinstance(msg.peer_id, tl.PeerChannel) else msg.chat_id
        bot_api_chat_id = -1_000_000_000_000 - chat_real_id
        diag.append(f"Step 5: ✓ Chat resolved to Bot API chat_id={bot_api_chat_id}")

        return {
            "chat_id": bot_api_chat_id,
            "message": msg,
            "album": album or None,
        }, diag


    async def test_link(self, parsed: ParsedLink) -> dict:
        """Diagnostic method — try to resolve and fetch the link, return
        a structured result with all the info. Used by /test_link command.

        Returns a dict with keys:
          parsed: dict — what was parsed from the URL
          steps: list[str] — step-by-step diagnostic log
          success: bool — whether fetch succeeded
          error: str | None — top-level error message if any
          has_media: bool — whether the message has downloadable media
          media_type: str | None — 'photo' | 'video' | 'document' | etc.
          chat_title: str | None — resolved chat title (if entity was resolved)
        """
        result = {
            "parsed": {
                "kind": parsed.kind,
                "chat_ref": parsed.chat_ref,
                "message_id": parsed.message_id,
            },
            "steps": [],
            "success": False,
            "error": None,
            "has_media": False,
            "media_type": None,
            "chat_title": None,
        }

        fetched, diag = await self.fetch_message(parsed)
        result["steps"] = diag

        if fetched is None:
            result["error"] = "Fetch failed — see steps above for the cause."
            return result

        msg = fetched["message"]
        result["success"] = True
        result["has_media"] = bool(getattr(msg, "media", None))

        # Determine media type
        from telethon.tl import types as tl_types
        if isinstance(msg.media, tl_types.MessageMediaPhoto):
            result["media_type"] = "photo"
        elif isinstance(msg.media, tl_types.MessageMediaDocument):
            doc = msg.media.document
            if doc and doc.mime_type:
                if doc.mime_type.startswith("video/"):
                    result["media_type"] = "video"
                elif doc.mime_type.startswith("audio/"):
                    result["media_type"] = "audio"
                else:
                    result["media_type"] = "document"
            else:
                result["media_type"] = "document"
        elif isinstance(msg.media, tl_types.MessageMediaWebPage):
            result["media_type"] = "web_page"
        else:
            result["media_type"] = type(msg.media).__name__ if msg.media else None

        # Try to get chat title (best effort)
        try:
            entity = await self._resolve_entity(int(fetched["chat_id"]), None)
            result["chat_title"] = getattr(entity, "title", None)
        except Exception:
            pass

        return result

    # ---------- direct send to destination (NEW — fast path) ----------

    async def send_to_destination(
        self,
        source_chat_id,
        source_message_ids: list[int],
        dest_chat_id,
        topic_id: int | None = None,
        progress_callback=None,
        custom_caption: str | None = None,
        source_messages: list | None = None,
    ) -> tuple[bool, list[str]]:
        """Send message(s) from a source chat to the destination chat using
        the user account (Telethon). This is the NEW fast path that avoids
        downloading media to disk and re-uploading via Bot API.

        Args:
          source_chat_id: source chat_id (int) or resolved entity (cached)
          source_message_ids: list of message IDs to send
          dest_chat_id: destination chat_id (int) or resolved entity (cached)
          topic_id: optional topic thread (for forum destinations)
          progress_callback: optional callable(sent_bytes, total_bytes) called
            during downloads and uploads.
          custom_caption: None=original, ""=strip, "<text>"=custom
          source_messages: OPTIONAL — list of pre-fetched Telethon Message
            objects. If provided, skips the get_messages() API call (major
            optimization for scrape_channel which already has the messages).

        Strategy:
          1. Try `user_client.forward_messages(dest, source_msg_ids, source_chat)`
          2. If that fails, fall back to `send_message(file=msg.media)` per msg
          3. If that also fails, download to disk + `send_file(file=path)`

        Requirements:
          - The user account must be a member of BOTH source and destination
          - For topics, pass topic_id (the topic's top message ID)

        Returns:
          (success: bool, diagnostics_log: list[str])
        """
        diag: list[str] = []
        diag.append(f"send_to_destination: {len(source_message_ids)} message(s) "
                    f"from {source_chat_id} to {dest_chat_id}"
                    f"{f' topic {topic_id}' if topic_id else ''}")

        # Helper: compute the effective caption based on custom_caption policy
        # - custom_caption=None: use original caption (msg.message)
        # - custom_caption="" (empty string): strip ALL captions
        # - custom_caption="<text>": use this string for the first item only
        #   (albums only show the first item's caption in Telegram)
        def _caption_for(msg, item_index: int = 0) -> str:
            """Return the caption to send for this message."""
            if custom_caption is None:
                # Use original caption (legacy behavior)
                return msg.message or ""
            if custom_caption == "":
                # Strip captions entirely
                return ""
            # Use custom caption, but only on the first item of an album
            if item_index == 0:
                return custom_caption
            return ""

        def _formatting_entities_for(msg, item_index: int = 0):
            """Return formatting entities to preserve. When using a custom
            caption, we strip entities (they wouldn't match the new text)."""
            if custom_caption is None:
                return msg.entities if item_index == 0 else None
            # Strip formatting entities when using custom caption
            return None

        # ── Use cached entities if provided (avoids _resolve_entity API calls) ──
        # scrape_channel passes already-resolved entities for massive speedup.
        # If source_chat_id/dest_chat_id are already entity objects, use them
        # directly instead of calling _resolve_entity.
        try:
            # Check if source_chat_id is already an entity object (not int/str)
            if hasattr(source_chat_id, 'id') or hasattr(source_chat_id, 'channel_id'):
                source_entity = source_chat_id
                diag.append(f"✓ Using cached source entity ({type(source_entity).__name__})")
            else:
                source_entity = await self._resolve_entity(source_chat_id, diag)
                diag.append(f"✓ Resolved source ({type(source_entity).__name__})")

            if hasattr(dest_chat_id, 'id') or hasattr(dest_chat_id, 'channel_id'):
                dest_entity = dest_chat_id
                diag.append(f"✓ Using cached dest entity ({type(dest_entity).__name__})")
            else:
                dest_entity = await self._resolve_entity(dest_chat_id, diag)
                diag.append(f"✓ Resolved dest ({type(dest_entity).__name__})")
        except Exception as e:
            diag.append(f"✗ Failed to resolve entities: {type(e).__name__}: {e}")
            return False, diag

        # ── Use pre-fetched messages if provided (avoids get_messages API call) ──
        # scrape_channel passes the msg objects directly — major optimization
        # for massive channels (saves 1 API call per message).
        if source_messages:
            messages = [m for m in source_messages if m is not None]
            if not messages:
                diag.append(f"✗ No valid messages in source_messages")
                return False, diag
            diag.append(f"✓ Using {len(messages)} pre-fetched message(s)")
        else:
            try:
                messages = await self.client.get_messages(source_entity, ids=source_message_ids)
                if isinstance(messages, list):
                    messages = [m for m in messages if m is not None]
                else:
                    messages = [messages] if messages else []
                if not messages:
                    diag.append(f"✗ No messages found with IDs {source_message_ids}")
                    return False, diag
                diag.append(f"✓ Fetched {len(messages)} message(s) from source")
            except Exception as e:
                diag.append(f"✗ Failed to fetch messages: {type(e).__name__}: {e}")
                return False, diag

        # ---- Step 1: try true forward (works for non-protected content) ----
        # Skip this step if a custom_caption is set — forward_messages doesn't
        # let us override the caption, so we need to use the send_message path
        # (which DOES let us set the caption) for caption control.
        # Also skip when sending into a forum TOPIC: forward_messages has no
        # reply_to/top_msg_id parameter, so a true forward would land in the
        # group's GENERAL chat instead of the requested topic (the old code
        # built a reply_to kwarg for it but never passed it — dead code that
        # silently sent topic scrapes to the wrong place).
        if custom_caption is None and not topic_id:
            # CRITICAL: a FloodWaitError here is NOT a "forward not allowed"
            # error. The old code caught it with a generic `except Exception`
            # and fell into the copy-and-re-upload fallback, which issues FAR
            # more API calls (one send_message per item, or a full download
            # + re-upload) and escalates the flood into a death spiral on big
            # scrapes. Now we sleep it off and retry the forward; only a
            # *persistent* flood is re-raised so the caller's own flood
            # handling (scrape's _send_one) can take over.
            forward_flood_tries = 0
            while True:
                try:
                    await self.client.forward_messages(
                        dest_entity,
                        [m.id for m in messages],
                        source_entity,
                    )
                    diag.append(f"✓ True forward succeeded — {len(messages)} message(s) "
                                f"forwarded to destination")
                    return True, diag
                except FloodWaitError as fw_err:
                    forward_flood_tries += 1
                    if forward_flood_tries > 2:
                        raise  # persistent flood — let the caller handle it
                    diag.append(
                        f"⏳ FloodWait {fw_err.seconds}s on true forward — sleeping and "
                        f"retrying ({forward_flood_tries}/2); NOT falling back to re-upload"
                    )
                    await _sleep_chunks(float(fw_err.seconds) + 2)
                except Exception as forward_err:
                    diag.append(f"⚠ True forward failed: {type(forward_err).__name__}: {forward_err}")
                    diag.append("  → Falling back to copy-and-resend (for protected content)")
                    break
        else:
            if custom_caption is not None:
                diag.append("ℹ Skipping true forward (custom_caption is set — using send_message)")
            else:
                diag.append("ℹ Skipping true forward (topic destination — "
                            "forward_messages cannot target a forum topic)")

        # ---- Step 2: fallback — re-upload via send_message(file=msg.media) ----
        # This bypasses the noforwards restriction by re-uploading from
        # Telegram's servers directly (no disk download).
        sent_count = 0
        last_error = None
        for i, msg in enumerate(messages):
            try:
                # ── Strip DocumentAttributeAnimated from the media ──────────
                # This ensures GIFs/animations are sent as regular videos,
                # not as looping GIF animations. We deep-copy the media
                # object and remove the Animated attribute from the document.
                media_to_send = msg.media
                try:
                    from telethon.tl import types as _tl_types
                    if isinstance(msg.media, _tl_types.MessageMediaDocument) and msg.media.document:
                        doc = msg.media.document
                        # Check if it has DocumentAttributeAnimated
                        has_animated = any(
                            isinstance(a, _tl_types.DocumentAttributeAnimated)
                            for a in (doc.attributes or [])
                        )
                        if has_animated:
                            # Rebuild the document without DocumentAttributeAnimated
                            new_attrs = [
                                a for a in (doc.attributes or [])
                                if not isinstance(a, _tl_types.DocumentAttributeAnimated)
                            ]
                            # Make sure there's still a DocumentAttributeVideo
                            # so Telegram knows it's a video
                            has_video_attr = any(
                                isinstance(a, _tl_types.DocumentAttributeVideo)
                                for a in new_attrs
                            )
                            if not has_video_attr:
                                new_attrs.append(
                                    _tl_types.DocumentAttributeVideo(
                                        duration=0, w=0, h=0,
                                        supports_streaming=True,
                                    )
                                )
                            # Create a new Document with the modified attributes
                            # (Documents are immutable in Telethon, so we make a copy)
                            new_doc = _tl_types.Document(
                                id=doc.id,
                                access_hash=doc.access_hash,
                                file_reference=doc.file_reference,
                                date=doc.date,
                                mime_type=doc.mime_type,
                                size=doc.size,
                                thumbs=doc.thumbs,
                                dc_id=doc.dc_id,
                                attributes=new_attrs,
                                version=getattr(doc, 'version', 0),
                            )
                            media_to_send = _tl_types.MessageMediaDocument(
                                document=new_doc,
                                ttl_seconds=getattr(msg.media, 'ttl_seconds', None),
                            )
                            diag.append(f"  • Stripped GIF/animation flag from msg {i+1} — sending as video")
                except Exception as e:
                    # If the deep-copy fails, fall back to the original media
                    # (it will be sent as a GIF, which is not ideal but works)
                    logger.debug("Failed to strip DocumentAttributeAnimated: %s", e)

                send_kwargs = dict(
                    message=_caption_for(msg, i),
                    file=media_to_send,
                    formatting_entities=_formatting_entities_for(msg, i),
                    link_preview=False,
                )
                if topic_id:
                    from telethon.tl.types import MessageReplyHeader
                    send_kwargs["reply_to"] = MessageReplyHeader(
                        reply_to_top_id=topic_id,
                        reply_to_msg_id=topic_id,
                    )
                # For multi-item albums, only the first item gets a caption
                # (already handled by _caption_for which returns "" for i>0
                # when custom_caption is set; for None it returns "" for i>0
                # since the original captions of album items >0 don't make
                # sense to repeat — only the first item's caption matters in
                # a Telegram album).
                if i > 0 and custom_caption is None:
                    send_kwargs["message"] = ""

                # Flood-aware send: sleep + retry instead of instantly
                # marking the message failed. The old behaviour counted the
                # message as failed while the loop kept hammering the API
                # with the remaining messages — escalating the flood.
                send_flood_tries = 0
                while True:
                    try:
                        await self.client.send_message(dest_entity, **send_kwargs)
                        sent_count += 1
                        diag.append(f"✓ Re-sent message {i+1}/{len(messages)} "
                                    f"(type: {type(msg.media).__name__ if msg.media else 'text'})")
                        break
                    except FloodWaitError as fw_err:
                        send_flood_tries += 1
                        if send_flood_tries > 2:
                            raise  # persistent — outer handler re-raises to caller
                        diag.append(
                            f"⏳ FloodWait {fw_err.seconds}s on send_message — sleeping and "
                            f"retrying ({send_flood_tries}/2)"
                        )
                        await _sleep_chunks(float(fw_err.seconds) + 2)
            except FloodWaitError:
                # Persistent flood — abort the whole send so the caller can
                # apply its own backoff. Falling through to Step 3 (full
                # download + re-upload) would only multiply API calls and
                # make the flood worse.
                raise
            except Exception as e:
                last_error = e
                diag.append(f"✗ Failed to send message {i+1}/{len(messages)} "
                            f"via send_message(file=msg.media): "
                            f"{type(e).__name__}: {e}")

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-sent to destination")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success: {sent_count}/{len(messages)} sent")
            return True, diag

        # ---- Step 3: third fallback — download to disk + send_file ----
        # The "send_message(file=msg.media)" path fails with
        # ChatForwardsRestrictedError because Telethon detects that the file
        # object references an existing message and treats it as a forward.
        # The only reliable way to send protected content is to:
        #   1. Download the media bytes to disk (Telethon allows this — you
        #      have view access as a member)
        #   2. Upload as a brand new file via send_file(file=path) — no link
        #      to the protected source, Telegram can't tell it's a "forward"
        #   3. PRESERVE the original document attributes (DocumentAttributeVideo
        #      with duration, dimensions) and mime_type so the file is sent
        #      as a PLAYABLE VIDEO (not a generic document). Pass
        #      supports_streaming=True to send_file for videos.
        diag.append("⚠ Falling back to download-to-disk + send_file (third path)")
        diag.append("  → This is slower but works for fully protected content")

        import tempfile
        import shutil
        from telethon.tl import types as tl_types
        tmp_dir = tempfile.mkdtemp(prefix="forwarder_protected_")
        try:
            sent_count = 0
            for i, msg in enumerate(messages):
                if not msg.media:
                    # Text-only message — just send the text
                    try:
                        send_kwargs = dict(
                            message=_caption_for(msg, i),
                            link_preview=False,
                        )
                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        # For albums, only first item has caption (already
                        # handled by _caption_for returning "" for i>0 when
                        # custom_caption is set; for None, the original caption
                        # of subsequent items would be repetitive in an album,
                        # so we strip them too).
                        if i > 0 and custom_caption is None:
                            send_kwargs["message"] = ""
                        await self.client.send_message(dest_entity, **send_kwargs)
                        sent_count += 1
                        diag.append(f"✓ Sent text-only message {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send text message {i+1}: {type(e).__name__}: {e}")
                    continue

                # ----- Build the progress callback for this iteration -----
                # IMPORTANT: Python closure bug — `i` and `phase` would all
                # resolve to the last loop iteration's value if we just used
                # them in a closure. We bind them as default args to make
                # each callback capture its own values.
                #
                # Also: Telethon's progress_callback can be either sync OR
                # async — Telethon awaits it via _maybe_await. So we can use
                # async def. But we keep it sync and call the outer
                # progress_callback (which is async) — _maybe_await will
                # await the returned coroutine.
                def make_progress_cb(item_index: int, total_items: int,
                                      phase: str, filename: str):
                    """Build a progress callback bound to the given iteration.
                    `phase` is 'Downloading' or 'Uploading'."""
                    def _cb(sent_bytes: int, total_bytes: int):
                        if progress_callback:
                            label = f"{phase} {filename} ({item_index+1}/{total_items})"
                            # progress_callback is async — _maybe_await will
                            # await the returned coroutine
                            return progress_callback(sent_bytes, total_bytes, label)
                    return _cb

                # ----- Helper: download thumbnail (if available) -----
                async def _download_thumbnail(msg_media, thumb_dir: str, idx: int):
                    """Download the thumbnail for a video/document as a JPEG.
                    Returns the path to the .jpg file, or None if no thumb.

                    Telegram documents have a `thumbs` list (PhotoSize
                    objects). The largest is typically a small JPEG used as
                    the video poster / preview. We download it via Telethon's
                    `download_media(msg, file=bytes, thumb=-1)` which uses
                    Telethon's _get_thumb to pick the largest size.
                    """
                    try:
                        thumb_bytes = await self.client.download_media(
                            msg_media, file=bytes, thumb=-1,
                        )
                        if not thumb_bytes:
                            return None
                        thumb_path = os.path.join(thumb_dir, f"thumb_{idx}.jpg")
                        with open(thumb_path, "wb") as f:
                            f.write(thumb_bytes)
                        return thumb_path
                    except Exception as e:
                        diag.append(f"  • (thumbnail download skipped: {type(e).__name__})")
                        return None

                # ----- Photos: MessageMediaPhoto -----
                if isinstance(msg.media, tl_types.MessageMediaPhoto):
                    out_path = os.path.join(tmp_dir, f"photo_{i+1}_{int(time.time())}.jpg")
                    dl_cb = make_progress_cb(i, len(messages), "Downloading", f"photo_{i+1}.jpg")
                    try:
                        result = await self.client.download_media(
                            msg, file=out_path, progress_callback=dl_cb,
                        )
                        if not result:
                            diag.append(f"✗ Could not download photo for message {i+1}")
                            continue
                        if isinstance(result, bytes):
                            with open(out_path, "wb") as f:
                                f.write(result)
                        else:
                            out_path = str(result)
                        sz = os.path.getsize(out_path)
                        diag.append(f"  • Downloaded photo_{i+1}.jpg ({sz/1024:.1f} KB)")
                    except Exception as e:
                        diag.append(f"✗ Failed to download photo {i+1}: {type(e).__name__}: {e}")
                        continue
                    # Send as photo — force_document=False lets Telethon treat
                    # the .jpg file as a photo (InputMediaUploadedPhoto)
                    try:
                        send_kwargs = dict(
                            file=out_path,
                            caption=_caption_for(msg, i),
                            formatting_entities=_formatting_entities_for(msg, i),
                            force_document=False,
                        )
                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        ul_cb = make_progress_cb(i, len(messages), "Uploading", f"photo_{i+1}.jpg")
                        await self.client.send_file(
                            dest_entity, progress_callback=ul_cb, **send_kwargs,
                        )
                        sent_count += 1
                        diag.append(f"✓ Sent re-uploaded photo {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send photo {i+1}: {type(e).__name__}: {e}")
                        last_error = e
                    continue

                # ----- Documents (video, audio, animation, generic document) -----
                if isinstance(msg.media, tl_types.MessageMediaDocument):
                    doc = msg.media.document
                    if not doc:
                        diag.append(f"✗ No document in message {i+1}")
                        continue

                    # Extract original mime_type
                    original_mime = doc.mime_type or "application/octet-stream"

                    # Determine media type from mime + attributes
                    is_video = original_mime.startswith("video/")
                    is_audio = original_mime.startswith("audio/")
                    is_animation = any(isinstance(a, tl_types.DocumentAttributeAnimated)
                                       for a in (doc.attributes or []))
                    is_image_doc = original_mime.startswith("image/")

                    # Find original filename
                    original_filename = None
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl_types.DocumentAttributeFilename):
                            original_filename = attr.file_name
                            break
                    if not original_filename:
                        # Generate based on mime type
                        ext_map = {
                            "video/mp4": "mp4", "video/quicktime": "mov",
                            "video/x-matroska": "mkv",
                            "audio/mpeg": "mp3", "audio/ogg": "ogg",
                            "audio/x-wav": "wav",
                            "image/jpeg": "jpg", "image/png": "png",
                        }
                        ext = ext_map.get(original_mime, "bin")
                        original_filename = f"media_{i+1}.{ext}"

                    # Build the attributes list to pass to send_file.
                    #
                    # KEY INSIGHT: We must NOT pass DocumentAttributeFilename
                    # because Telethon's get_attributes() regenerates it from
                    # the local file path. If we pass both, Telethon may
                    # override the regenerated one with our (potentially
                    # inconsistent) one — and that's actually fine.
                    # But we MUST keep DocumentAttributeVideo (with duration,
                    # w, h, supports_streaming) and DocumentAttributeAudio
                    # so Telegram knows it's a video/audio.
                    #
                    # We filter out DocumentAttributeFilename to avoid the
                    # conflict; Telethon will use the local file's basename.
                    #
                    # NEW: We ALSO strip DocumentAttributeAnimated so that
                    # GIFs/animations are sent as regular VIDEOS, not as
                    # looping GIF animations. This ensures every video
                    # (even short ones) appears as a video file with
                    # playback controls, not as a GIF.
                    original_attributes = [
                        attr for attr in (doc.attributes or [])
                        if not isinstance(attr, tl_types.DocumentAttributeFilename)
                        and not isinstance(attr, tl_types.DocumentAttributeAnimated)
                    ]

                    # If the source was an animation (GIF), we stripped
                    # DocumentAttributeAnimated above. Now we need to make
                    # sure there's still a DocumentAttributeVideo so Telegram
                    # treats it as a video. If there isn't one, add a minimal
                    # one with supports_streaming=True.
                    if is_animation and is_video:
                        has_video_attr = any(
                            isinstance(a, tl_types.DocumentAttributeVideo)
                            for a in original_attributes
                        )
                        if not has_video_attr:
                            # Add a minimal DocumentAttributeVideo so Telegram
                            # knows this is a video file (not a document)
                            original_attributes.append(
                                tl_types.DocumentAttributeVideo(
                                    duration=0,  # unknown duration
                                    w=0, h=0,  # unknown dimensions
                                    supports_streaming=True,
                                )
                            )

                    # Decide force_document — True for generic files (pdf, zip)
                    # Note: is_animation is now irrelevant because we stripped
                    # the Animated attribute — the file will be sent as a video.
                    force_document = not (is_video or is_audio or is_image_doc)

                    # Download to disk with LARGER CHUNK SIZE for speed.
                    # Telethon's auto-picker uses 128KB for <100MB files,
                    # which is 4x smaller than the 512KB max. Each chunk is a
                    # separate network round-trip, so 4x smaller = 4x slower.
                    # We bypass the auto-picker by calling _download_file with
                    # part_size_kb=512 (4x speedup).
                    out_path = os.path.join(tmp_dir, original_filename)
                    dl_cb = make_progress_cb(i, len(messages), "Downloading", original_filename)
                    try:
                        # Use Telethon's _download_file directly with a large
                        # chunk size for faster download. The InputDocumentFileLocation
                        # is what Telethon would use internally for documents.
                        from telethon.tl.types import InputDocumentFileLocation
                        thumb_size_type = ""  # main file, not a thumb
                        file_location = InputDocumentFileLocation(
                            id=doc.id,
                            access_hash=doc.access_hash,
                            file_reference=doc.file_reference,
                            thumb_size=thumb_size_type,
                        )
                        await self.client._download_file(
                            file_location,
                            out_path,
                            part_size_kb=512,  # 4x larger than auto-picked 128KB
                            file_size=doc.size,
                            progress_callback=dl_cb,
                        )
                        sz = os.path.getsize(out_path)
                        diag.append(f"  • Downloaded {original_filename} "
                                    f"({sz/1024/1024:.1f} MB, mime={original_mime})")
                    except Exception as e:
                        diag.append(f"✗ Failed to download media {i+1}: "
                                    f"{type(e).__name__}: {e}")
                        # Fallback: try the regular download_media
                        try:
                            diag.append(f"  → Retrying with regular download_media()...")
                            result = await self.client.download_media(
                                msg, file=out_path, progress_callback=dl_cb,
                            )
                            if not result:
                                continue
                            sz = os.path.getsize(out_path)
                            diag.append(f"  • Downloaded (fallback) {original_filename} "
                                        f"({sz/1024/1024:.1f} MB)")
                        except Exception as e2:
                            diag.append(f"✗ Fallback download also failed: {type(e2).__name__}: {e2}")
                            continue

                    # Download the thumbnail (for videos). The thumb is a small
                    # JPEG that Telegram shows as the video poster before play.
                    # Without it, the destination video has no preview/thumbnail.
                    thumb_path = None
                    if is_video and getattr(doc, "thumbs", None):
                        thumb_path = await _download_thumbnail(msg, tmp_dir, i)
                        if thumb_path:
                            try:
                                tsize = os.path.getsize(thumb_path)
                                diag.append(f"  • Downloaded thumbnail ({tsize/1024:.1f} KB)")
                            except OSError:
                                pass

                    # Send the file with preserved attributes + thumbnail +
                    # supports_streaming. We PRE-UPLOAD the file with a large
                    # chunk size (512KB max) for 4x speedup vs Telethon's
                    # auto-picker, then pass the resulting InputFile to send_file.
                    #
                    # For files >10MB, we use a PARALLEL uploader that splits
                    # the file into chunks and uploads them concurrently. This
                    # gives ~4x speedup for large files (the sequential
                    # bottleneck was each chunk waiting for the previous
                    # chunk's response before sending).
                    try:
                        ul_cb = make_progress_cb(i, len(messages), "Uploading", original_filename)
                        file_size_bytes = os.path.getsize(out_path)

                        if file_size_bytes > 10 * 1024 * 1024:
                            # Large file — use parallel upload (4 concurrent chunks)
                            diag.append(f"  • Using parallel upload (4 chunks) for {file_size_bytes/1024/1024:.1f} MB file")
                            file_handle = await self._parallel_upload_file(
                                out_path,
                                file_size_bytes,
                                part_size_kb=512,
                                parallel=4,
                                progress_callback=ul_cb,
                            )
                        else:
                            # Small file — sequential upload is fast enough
                            file_handle = await self.client.upload_file(
                                out_path,
                                part_size_kb=512,
                                file_size=file_size_bytes,
                                progress_callback=ul_cb,
                            )

                        send_kwargs = dict(
                            file=file_handle,  # already-uploaded InputFile — send_file skips re-upload
                            caption=_caption_for(msg, i),
                            formatting_entities=_formatting_entities_for(msg, i),
                            # Pass the original attributes (minus Filename)
                            # so Telegram sees the video duration/dims.
                            attributes=original_attributes,
                            mime_type=original_mime,
                            force_document=force_document,
                        )
                        # KEY FIX: Pass the downloaded thumbnail so the
                        # destination video shows a poster/preview image.
                        if thumb_path:
                            send_kwargs["thumb"] = thumb_path
                        # KEY FIX: Pass supports_streaming=True for videos
                        # so Telegram shows it as a streamable video.
                        if is_video:
                            send_kwargs["supports_streaming"] = True
                        # voice_note for voice messages
                        if is_audio:
                            for attr in (doc.attributes or []):
                                if isinstance(attr, tl_types.DocumentAttributeAudio) and getattr(attr, "voice", False):
                                    send_kwargs["voice_note"] = True
                                    break

                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        await self.client.send_file(dest_entity, **send_kwargs)
                        sent_count += 1
                        if is_video:
                            thumb_msg = " with thumbnail" if thumb_path else " (no thumbnail)"
                            anim_msg = " (converted from GIF/animation)" if is_animation else ""
                            diag.append(f"✓ Sent re-uploaded video {i+1}/{len(messages)} "
                                        f"(playable, streaming{thumb_msg}, "
                                        f"duration/dims preserved{anim_msg})")
                        elif is_audio:
                            diag.append(f"✓ Sent re-uploaded audio {i+1}/{len(messages)}")
                        elif is_animation:
                            # This shouldn't happen anymore (animations are
                            # converted to videos above), but keep the message
                            # for backward compatibility.
                            diag.append(f"✓ Sent re-uploaded animation {i+1}/{len(messages)} (as video)")
                        else:
                            diag.append(f"✓ Sent re-uploaded document {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send re-uploaded media {i+1}: "
                                    f"{type(e).__name__}: {e}")
                        last_error = e
        finally:
            # Cleanup tmp dir
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-uploaded (third path)")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success (third path): {sent_count}/{len(messages)} sent")
            return True, diag
        else:
            diag.append(f"✗ All sends failed. Last error: {last_error}")
            return False, diag

    # ---------- parallel upload (for large files) ----------

    async def _parallel_upload_file(
        self,
        file_path: str,
        file_size: int,
        part_size_kb: int = 512,
        parallel: int = 4,
        progress_callback=None,
    ):
        """Upload a file in PARALLEL chunks for faster throughput.

        This replaces Telethon's sequential upload_file for large files.
        The sequential approach sends one 512KB chunk, waits for the
        response, then sends the next. For a 50MB file that's 100 chunks
        × ~200ms round-trip = ~20 seconds.

        This parallel approach splits the file into chunks and uploads
        `parallel` chunks concurrently. For 4x parallel, a 50MB file
        takes ~5 seconds instead of 20.

        Telegram's SaveBigFilePartRequest is stateless — each part is
        stored independently, associated by file_id. So parallel uploads
        work: we just need to use the same file_id for all parts and
        different file_part indices.

        Args:
          file_path: Path to the file to upload.
          file_size: Size of the file in bytes.
          part_size_kb: Chunk size in KB (max 512, Telethon's limit).
          parallel: Number of concurrent chunk uploads (default 4).
          progress_callback: Sync callable(sent, total) — called after
            each chunk is uploaded.

        Returns:
          InputFileBig (for files >10MB) or InputFile (for smaller) —
          ready to pass to send_file.
        """
        import asyncio as _asyncio
        import hashlib
        from telethon.tl.functions.upload import (
            SaveBigFilePartRequest, SaveFilePartRequest,
        )
        from telethon.tl.types import InputFile, InputFileBig
        from telethon import helpers

        part_size = part_size_kb * 1024
        part_count = (file_size + part_size - 1) // part_size
        file_id = helpers.generate_random_long()
        file_name = os.path.basename(file_path)

        is_big = file_size > 10 * 1024 * 1024
        logger.info("Parallel upload: %s (%d bytes, %d chunks, %d parallel)",
                    file_name, file_size, part_count, parallel)

        # Track progress across parallel tasks
        bytes_uploaded = [0]  # mutable container for closure
        progress_lock = _asyncio.Lock()

        async def upload_one_part(part_index: int):
            """Read and upload a single chunk of the file."""
            offset = part_index * part_size
            # Read this specific part from disk
            with open(file_path, "rb") as f:
                f.seek(offset)
                data = f.read(part_size)

            # Use the correct request type based on file size
            if is_big:
                request = SaveBigFilePartRequest(
                    file_id, part_index, part_count, data,
                )
            else:
                request = SaveFilePartRequest(
                    file_id, part_index, data,
                )

            result = await self.client(request)
            if not result:
                raise RuntimeError(
                    f"Telegram rejected part {part_index}/{part_count}"
                )

            # Update progress (thread-safe via lock)
            async with progress_lock:
                bytes_uploaded[0] += len(data)
                if progress_callback:
                    try:
                        r = progress_callback(bytes_uploaded[0], file_size)
                        if inspect.isawaitable(r):
                            await r
                    except Exception:
                        pass

        # Use a semaphore to limit concurrency
        sem = _asyncio.Semaphore(parallel)

        async def bounded_upload(part_index: int):
            async with sem:
                return await upload_one_part(part_index)

        # Launch all upload tasks
        tasks = [_asyncio.create_task(bounded_upload(i)) for i in range(part_count)]
        results = await _asyncio.gather(*tasks, return_exceptions=True)

        # Check for errors
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                # Cancel any remaining tasks (shouldn't be any since gather waits)
                raise RuntimeError(
                    f"Parallel upload failed at part {i}/{part_count}: "
                    f"{type(r).__name__}: {r}"
                )

        # Build the InputFile / InputFileBig to pass to send_file
        if is_big:
            return InputFileBig(id=file_id, parts=part_count, name=file_name)
        else:
            # For small files, compute MD5 (needed for InputFile dedup)
            md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    md5.update(chunk)
            return InputFile(
                id=file_id,
                parts=part_count,
                name=file_name,
                md5_digest=md5.digest(),
            )


    # ---------- legacy: download media to disk (fallback path) ----------

    async def download_message_media(
        self,
        source_chat_id: int,
        source_message_ids: list[int],
        tmp_dir: str,
        max_bytes: int = 2 * 1024 * 1024 * 1024,  # 2GB (Telegram's hard limit)
    ) -> tuple[list[dict], str | None, str | None, list[str]]:
        """Legacy fallback: download media from source chat to disk for
        re-upload via Bot API. Used when send_to_destination fails (e.g.,
        user account is not a member of the destination).

        Returns:
          (media_paths, caption, text_only, diagnostics_log)

        media_paths is a list of {'path': str, 'type': str}
        caption is the original caption
        text_only is set if no media was downloaded (text-only message)
        """
        diag: list[str] = []
        media_paths: list[dict] = []
        caption: str | None = None
        text_only: str | None = None

        try:
            source_entity = await self._resolve_entity(source_chat_id, diag)
        except Exception as e:
            diag.append(f"✗ Failed to resolve source entity: {type(e).__name__}: {e}")
            return [], None, None, diag

        messages = await self.client.get_messages(source_entity, ids=source_message_ids)
        if isinstance(messages, list):
            messages = [m for m in messages if m is not None]
        else:
            messages = [messages] if messages else []

        if not messages:
            diag.append(f"✗ No messages found with IDs {source_message_ids}")
            return [], None, None, diag

        from telethon.tl import types as tl

        for idx, msg in enumerate(messages):
            if idx == 0:
                caption = msg.message

            if not msg.media:
                if msg.message and not media_paths:
                    text_only = msg.message
                continue

            # Skip non-downloadable media types
            if isinstance(msg.media, (tl.MessageMediaWebPage, tl.MessageMediaContact,
                                       tl.MessageMediaGeo, tl.MessageMediaVenue,
                                       tl.MessageMediaGame, tl.MessageMediaPoll,
                                       tl.MessageMediaUnsupported)):
                continue

            # Determine type
            media_type = "document"
            if isinstance(msg.media, tl.MessageMediaPhoto):
                media_type = "photo"
            elif isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if doc and doc.mime_type:
                    mt = doc.mime_type
                    if mt.startswith("video/"):
                        for attr in doc.attributes:
                            if isinstance(attr, tl.DocumentAttributeAnimated):
                                media_type = "animation"
                                break
                            if isinstance(attr, tl.DocumentAttributeVideo):
                                media_type = "video_note" if getattr(attr, "round_message", False) else "video"
                                break
                    elif mt.startswith("audio/"):
                        media_type = "voice" if "ogg" in mt else "audio"

            # Check size before download
            try:
                if isinstance(msg.media, tl.MessageMediaDocument) and msg.media.document:
                    sz = msg.media.document.size or 0
                    if sz > max_bytes:
                        diag.append(f"⚠ Skipping item {idx}: file too large "
                                    f"({sz/1024/1024:.1f} MB > {max_bytes/1024/1024:.0f} MB)")
                        continue
            except Exception:
                pass

            # Download
            out_path = os.path.join(tmp_dir, f"media_{idx}_{int(time.time())}")
            try:
                result = await self.client.download_media(msg, file=out_path)
                if not result:
                    continue
                if isinstance(result, bytes):
                    with open(out_path, "wb") as f:
                        f.write(result)
                else:
                    out_path = str(result)
                sz = os.path.getsize(out_path)
                if sz > max_bytes:
                    diag.append(f"⚠ Skipping item {idx}: downloaded {sz/1024/1024:.1f} MB "
                                f"(> {max_bytes/1024/1024:.0f} MB Telegram limit)")
                    os.remove(out_path)
                    continue
                media_paths.append({"path": out_path, "type": media_type})
                diag.append(f"✓ Downloaded item {idx} ({media_type}, {sz/1024:.1f} KB)")
            except Exception as e:
                diag.append(f"✗ Failed to download item {idx}: {type(e).__name__}: {e}")

        return media_paths, caption, text_only, diag

    # ---------- channel scraping ----------

    async def scrape_channel(
        self,
        source_chat_ref,
        dest_chat_id,
        topic_id: int | None = None,
        reverse: bool = False,
        min_id: int = 0,
        max_id: int = 0,
        cancel_event=None,
        progress_callback=None,
        status_callback=None,
        stats_callback=None,
        media_types: list[str] | None = None,
        parallel: int = 3,
        custom_caption: str | None = None,
    ) -> dict:
        """Iterate all messages in a channel and forward each media message
        to the destination chat. Used by the /scrape command.

        Args:
          source_chat_ref: chat_id (int) or username (str) of the source channel
          dest_chat_id: destination chat_id (int) or "me" for Saved Messages
          topic_id: optional topic thread (for forum destinations)
          reverse: if True, oldest first (default: newest first)
          min_id: skip messages with id <= min_id
          max_id: skip messages with id > max_id (or 0 for no upper bound)
          cancel_event: asyncio.Event — set to cancel scraping
          progress_callback: async callable(sent, total_seen, last_msg_id, label)
          status_callback: async callable(status_text) — for status updates
          media_types: list of types to include. None means all media.
            Valid values: 'photo', 'video', 'animation', 'document', 'audio',
            'voice'. If specified, only matching media is sent; others are
            skipped (counted in skipped_count).
          parallel: number of concurrent sends. Default 3 (safe for Telegram).
            Higher values risk FloodWait. Each task uses its own asyncio task.

        Returns:
          dict with keys: sent_count, failed_count, skipped_count,
                          total_seen, last_message_id, cancelled (bool),
                          flood_waits (int)

        Rate limiting:
          - Per-task 0.3 sec delay between sends (safe even with parallel=3)
          - On FloodWaitError, sleep for the requested seconds + 5s buffer
            and retry the message once

        Notes:
          - parallel sends are independent — they don't share rate-limit state.
            The actual bottleneck is Telegram's per-account rate limit, not
            our concurrency. Setting parallel=5+ risks FloodWait.
          - Each task holds its own network connection (no shared TCP overhead)
          - The order of sent messages in the destination may differ from the
            source order when parallel > 1 (out-of-order arrival)

        Optimization for massive channels (10k+ messages):
          - Entities are resolved ONCE and cached (not per-message)
          - Message objects from iter_messages are passed directly to
            send_to_destination (avoids re-fetching via get_messages)
          - pending_send_tasks is drained on EVERY iteration (no accumulation)
          - Cancellation is checked at every yield point (even during errors)
          - ConnectionError triggers auto-reconnect with resume from last msg ID
          - Per-send timeout (10 min) prevents stuck sends from blocking forever
        """
        from telethon.errors import FloodWaitError
        import asyncio as _asyncio
        import time as _time

        # Normalize media_types: lowercase, validate, default to None (all)
        if media_types is not None:
            media_types = [t.lower() for t in media_types]
            valid = {"photo", "video", "animation", "document", "audio", "voice"}
            invalid = set(media_types) - valid
            if invalid:
                if status_callback:
                    await status_callback(f"❌ Invalid media types: {invalid}. "
                                          f"Valid: {valid}")
                return {
                    "sent_count": 0, "failed_count": -1, "skipped_count": 0,
                    "total_seen": 0, "last_message_id": 0,
                    "cancelled": False, "flood_waits": 0,
                }

        result = {
            "sent_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "total_seen": 0,
            "last_message_id": 0,
            "cancelled": False,
            "flood_waits": 0,
            "in_flight": 0,
            "started_at": time.time(),
        }

        # ── Resolve entities ONCE and cache them ──────────────────────────
        # This is the #1 optimization for massive channels: the old code
        # called _resolve_entity on EVERY send (2 API calls per message).
        # For 10k messages, that's 20k unnecessary API calls → FloodWait.
        try:
            source_entity = await self._resolve_entity(source_chat_ref, None)
            dest_entity = await self._resolve_entity(dest_chat_id, None)
        except Exception as e:
            if status_callback:
                await status_callback(f"❌ Failed to resolve entities: {type(e).__name__}: {e}")
            result["failed_count"] = -1
            return result

        if status_callback:
            await status_callback(
                f"🔍 Scraping channel: {getattr(source_entity, 'title', source_chat_ref)}\n"
                f"   → destination: {getattr(dest_entity, 'title', dest_chat_id)}"
                f"{f' (topic {topic_id})' if topic_id else ''}\n"
                f"   order: {'oldest first' if reverse else 'newest first'}"
            )

        # Build kwargs for iter_messages
        iter_kwargs = {
            "reverse": reverse,
            "limit": None,
        }
        if min_id > 0:
            iter_kwargs["min_id"] = min_id
        if max_id > 0:
            iter_kwargs["max_id"] = max_id

        last_status_time = 0
        status_interval = 1.0

        # Helper: classify a message's media type
        def _classify_media(msg) -> str | None:
            if not msg.media:
                return None
            if isinstance(msg.media, tl.MessageMediaPhoto):
                return "photo"
            if isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if not doc or not doc.mime_type:
                    return "document"
                mt = doc.mime_type
                if mt.startswith("video/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAnimated):
                            return "animation"
                        if isinstance(attr, tl.DocumentAttributeVideo):
                            return "video_note" if getattr(attr, "round_message", False) else "video"
                    return "video"
                if mt.startswith("audio/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAudio) and getattr(attr, "voice", False):
                            return "voice"
                    return "audio" if "ogg" not in mt else "voice"
                return "document"
            return None

        # ── Send one message — NEVER raises, caches entities ───────────────
        async def _send_one(msg):
            """Send a single message. Passes the msg object directly to
            send_to_destination to avoid re-fetching it via get_messages.

            NEVER raises — all exceptions are caught and counted as failures.
            This ensures one bad message doesn't stop the entire scrape."""
            result["in_flight"] += 1
            try:
                # Check cancellation before sending
                if cancel_event and cancel_event.is_set():
                    return False

                # Use a timeout so a stuck send doesn't block the scrape forever.
                success, _ = await _asyncio.wait_for(
                    self.send_to_destination(
                        source_chat_id=source_entity,  # cached entity, not chat_ref
                        source_message_ids=[msg.id],
                        dest_chat_id=dest_entity,      # cached entity
                        topic_id=topic_id,
                        progress_callback=None,
                        custom_caption=custom_caption,
                        source_messages=[msg],         # pass the msg directly!
                    ),
                    timeout=600,
                )
                if success:
                    result["sent_count"] += 1
                    return True
                result["failed_count"] += 1
                return False
            except FloodWaitError as e:
                result["flood_waits"] += 1
                wait_seconds = e.seconds + 5
                if status_callback:
                    await status_callback(
                        f"⏳ Flood wait: sleeping {wait_seconds}s before retrying...\n"
                        f"   Sent so far: {result['sent_count']}"
                    )
                # Sleep in chunks so we can be interrupted by cancel
                if not await _sleep_chunks(wait_seconds, cancel_event):
                    result["failed_count"] += 1
                    return False
                # Retry once
                try:
                    success, _ = await _asyncio.wait_for(
                        self.send_to_destination(
                            source_chat_id=source_entity,
                            source_message_ids=[msg.id],
                            dest_chat_id=dest_entity,
                            topic_id=topic_id,
                            progress_callback=None,
                            custom_caption=custom_caption,
                            source_messages=[msg],
                        ),
                        timeout=600,
                    )
                    if success:
                        result["sent_count"] += 1
                        return True
                except Exception:
                    pass
                result["failed_count"] += 1
                return False
            except _asyncio.TimeoutError:
                logger.warning("scrape: send timed out for msg %d (10min limit)", msg.id)
                result["failed_count"] += 1
                return False
            except Exception as e:
                logger.warning("scrape: failed to send msg %d: %s", msg.id, e)
                result["failed_count"] += 1
                return False
            finally:
                result["in_flight"] -= 1
                if stats_callback:
                    try:
                        await stats_callback(result)
                    except Exception:
                        pass

        sem = _asyncio.Semaphore(parallel)
        pending_send_tasks: list = []

        async def _send_with_semaphore(msg):
            async with sem:
                if cancel_event and cancel_event.is_set():
                    return False
                await _send_one(msg)
                await _asyncio.sleep(0.3)

        # ── Drain completed tasks efficiently ──────────────────────────────
        def _drain_done_tasks():
            """Remove completed tasks from pending_send_tasks to prevent
            memory accumulation on massive channels."""
            nonlocal pending_send_tasks
            still_pending = []
            for t in pending_send_tasks:
                if t.done():
                    try:
                        t.result()
                    except Exception:
                        pass
                else:
                    still_pending.append(t)
            pending_send_tasks = still_pending

        # ── Main iteration loop: manual chunked pagination ─────────────────
        # WHY NOT iter_messages():
        #   iter_messages() fetches 100 msgs per getHistory call with a default
        #   inter-chunk delay of only 1s (or 0s with explicit limit). Telegram's
        #   budget is ~30s per 10 requests (3s/request). After ~1800 messages
        #   (18 calls), FloodWait escalates past Telethon's flood_sleep_threshold
        #   (60s) → FloodWaitError is raised → scrape dies.
        #
        # FIX: Manual chunked get_messages with offset_id pagination:
        #   - Fetch 100 messages per call
        #   - Sleep 3s between batches (respects Telegram's budget)
        #   - Catch FloodWaitError explicitly, sleep e.seconds, retry same offset_id
        #   - Check cancellation between every batch and every message
        #
        # offset_id is a pagination cursor (exclusive): "messages older than this ID"
        # We advance it to the OLDEST id in each batch (messages come newest→oldest)
        BATCH_SIZE = 100
        # Telegram's getHistory budget is roughly 10 requests per 30 s
        # (≈3 s per request). The old BATCH_DELAY=3 sat EXACTLY on that
        # budget with zero headroom, so scrapes of big channels started
        # flooding at ~1800-2000 messages (≈20 getHistory calls) and then
        # re-offended at the same pace after every sleep. 3.5 s keeps us
        # safely under the budget; the AdaptivePacer grows the delay
        # further whenever Telegram still complains and relaxes it back
        # after a streak of quiet batches.
        BATCH_DELAY = 3.5
        read_pacer = AdaptivePacer(base=BATCH_DELAY, maximum=15.0)
        MAX_FLOOD_RETRIES = 10

        # For reverse=True (oldest first), we need a different approach:
        # use min_id to paginate forward through history.
        # For reverse=False (newest first, default), use offset_id to paginate backward.
        current_offset_id = min_id if min_id > 0 else 0
        current_min_id_for_reverse = min_id if min_id > 0 else 0

        scrape_done = False
        while not scrape_done:
            # Check cancellation at the top of every batch
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                break

            # Ensure connected before fetching
            if not await self._ensure_connected():
                if status_callback:
                    await status_callback("❌ Connection lost and reconnect failed. Stopping scrape.")
                result["failed_count"] = -1
                return result

            # Fetch one batch of messages with FloodWait retry
            batch_msgs = None
            for attempt in range(MAX_FLOOD_RETRIES):
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    if reverse:
                        # Oldest-first: use min_id to paginate forward
                        fetch_kwargs = {"limit": BATCH_SIZE, "reverse": True}
                        if current_min_id_for_reverse > 0:
                            fetch_kwargs["min_id"] = current_min_id_for_reverse
                        batch_msgs = await self.client.get_messages(source_entity, **fetch_kwargs)
                    else:
                        # Newest-first: use offset_id to paginate backward
                        fetch_kwargs = {"limit": BATCH_SIZE}
                        if current_offset_id > 0:
                            fetch_kwargs["offset_id"] = current_offset_id
                        batch_msgs = await self.client.get_messages(source_entity, **fetch_kwargs)
                    break
                except FloodWaitError as e:
                    result["flood_waits"] += 1
                    wait_seconds = e.seconds + 2
                    new_pace = read_pacer.on_flood(e.seconds)
                    if status_callback:
                        await status_callback(
                            f"⏳ Flood wait on get_messages: sleeping {wait_seconds}s "
                            f"(attempt {attempt+1}/{MAX_FLOOD_RETRIES})...\n"
                            f"   Sent so far: {result['sent_count']}, Seen: {result['total_seen']}\n"
                            f"   Read pace auto-adjusted to {new_pace:.1f}s/batch"
                        )
                    # Sleep in chunks so we can be interrupted by cancel
                    await _sleep_chunks(wait_seconds, cancel_event)
                    if cancel_event and cancel_event.is_set():
                        break
                    # Retry the same batch (offset_id unchanged)
                    continue
                except ConnectionError as e:
                    logger.warning("scrape_channel: connection dropped during get_messages: %s", e)
                    if status_callback:
                        await status_callback(f"⚠️ Connection dropped. Reconnecting...")
                    await _asyncio.sleep(2)
                    if not await self._ensure_connected():
                        if status_callback:
                            await status_callback("❌ Reconnect failed. Stopping scrape.")
                        result["failed_count"] = -1
                        return result
                    continue  # retry the same batch
                except _asyncio.CancelledError:
                    result["cancelled"] = True
                    break
                except Exception as e:
                    logger.exception("scrape_channel: get_messages failed")
                    if status_callback:
                        await status_callback(f"❌ Fetch error: {type(e).__name__}: {e}")
                    result["failed_count"] = -1
                    return result
            else:
                # Exhausted all flood retries
                if status_callback:
                    await status_callback(
                        f"❌ Exhausted {MAX_FLOOD_RETRIES} flood retries. Stopping scrape.\n"
                        f"   Sent: {result['sent_count']}, Seen: {result['total_seen']}"
                    )
                result["failed_count"] = -1
                return result

            # Check if cancelled during fetch
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                break

            # Check if we've reached the end of history
            if not batch_msgs:
                scrape_done = True
                break

            # Process each message in the batch
            for msg in batch_msgs:
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    break

                if msg is None:
                    continue

                result["total_seen"] += 1
                result["last_message_id"] = msg.id

                # Update pagination cursor
                if reverse:
                    # For oldest-first, track the highest id to advance forward
                    if msg.id > current_min_id_for_reverse:
                        current_min_id_for_reverse = msg.id
                else:
                    # For newest-first, track the lowest id to go backward
                    # (messages come newest→oldest, so the last one is the oldest)
                    pass  # we'll set current_offset_id after the loop

            if result["cancelled"]:
                break

            # Set offset_id to the OLDEST message in this batch for next iteration
            if batch_msgs and not reverse:
                # batch_msgs is newest→oldest, so last is the oldest
                current_offset_id = batch_msgs[-1].id
            elif batch_msgs and reverse:
                # batch_msgs is oldest→newest, so last is the newest
                # current_min_id_for_reverse already updated in the loop
                pass

            # Now process messages for sending (filter + schedule sends)
            for msg in batch_msgs:
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    break
                if msg is None:
                    continue

                m_type = _classify_media(msg)
                if m_type is None:
                    result["skipped_count"] += 1
                    continue

                if media_types is not None:
                    type_check = m_type
                    if type_check == "video_note":
                        type_check = "video"
                    if type_check not in media_types:
                        result["skipped_count"] += 1
                        continue

                # Schedule the send in parallel
                task = _asyncio.create_task(_send_with_semaphore(msg))
                pending_send_tasks.append(task)

            # Drain completed tasks
            _drain_done_tasks()

            # If we have too many pending, wait for some to complete
            if len(pending_send_tasks) >= parallel * 2:
                done, pending = await _asyncio.wait(
                    pending_send_tasks, return_when=_asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        t.result()
                    except Exception:
                        pass
                pending_send_tasks = list(pending)

            # Progress callback
            if progress_callback:
                try:
                    await progress_callback(
                        result["sent_count"],
                        result["total_seen"],
                        result["last_message_id"],
                        f"Sent {result['sent_count']} / seen {result['total_seen']}",
                    )
                except Exception:
                    pass

            # Periodic status update
            now = _time.time()
            if status_callback and now - last_status_time > status_interval:
                last_status_time = now
                await status_callback(
                    f"📊 Scraping in progress...\n\n"
                    f"Total seen: {result['total_seen']}\n"
                    f"Sent: {result['sent_count']}\n"
                    f"Failed: {result['failed_count']}\n"
                    f"Skipped: {result['skipped_count']}\n"
                    f"Last msg ID: {result['last_message_id']}\n"
                    f"Parallel sends: {parallel}"
                )

            if stats_callback:
                try:
                    await stats_callback(result)
                except Exception:
                    pass

            # If we got fewer than BATCH_SIZE messages, we've reached the end
            if len(batch_msgs) < BATCH_SIZE:
                scrape_done = True
                break

            # Check cancellation before sleeping
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                break

            # Sleep between batches to respect Telegram's rate limit.
            # The batch fetched cleanly — count it towards pace recovery,
            # then sleep the (adaptive) inter-batch delay. _sleep_chunks is
            # float-safe and cancel-aware; the old `range(BATCH_DELAY)` loop
            # only supported whole seconds and ignored pacing changes.
            read_pacer.on_success()
            if not await _sleep_chunks(read_pacer.current, cancel_event):
                result["cancelled"] = True
                break

        # Wait for any remaining in-flight send tasks to complete
        if pending_send_tasks:
            if cancel_event and cancel_event.is_set():
                # Cancel all pending tasks on stop
                for t in pending_send_tasks:
                    if not t.done():
                        t.cancel()
            try:
                await _asyncio.gather(*pending_send_tasks, return_exceptions=True)
            except Exception:
                pass

        # Final status
        if status_callback and not result["cancelled"]:
            await status_callback(
                f"✅ Scraping complete!\n\n"
                f"Total seen: {result['total_seen']}\n"
                f"Sent: {result['sent_count']}\n"
                f"Failed: {result['failed_count']}\n"
                f"Skipped (filtered/no media): {result['skipped_count']}\n"
                f"Flood waits: {result['flood_waits']}\n"
                f"Last msg ID: {result['last_message_id']}\n"
                f"Parallel sends: {parallel}"
            )

        return result

    # ---------- ID-based scraping (NO getHistory — avoids rate limits) ------

    async def scrape_channel_by_ids(
        self,
        source_chat_ref,
        dest_chat_id,
        start_id: int = 1,
        end_id: int = 0,
        cancel_event=None,
        status_callback=None,
        stats_callback=None,
        drop_author: bool = True,
        drop_media_captions: bool = False,
        batch_size: int = 100,
        batch_delay: float = 3.5,
    ) -> dict:
        """Forward messages from a public channel by ID range — NO getHistory.

        This is the OPTIMAL method for bulk-forwarding large public channels:
        - Uses `forward_messages(dest, msg_ids, from_peer)` which takes IDs
          directly — no need to read messages via getHistory first
        - Uses the SEND rate-limit bucket (not the getHistory bucket)
        - 100 messages per API call (Telegram's max per forwardMessages)
        - Avoids the getHistory FloodWait cliff entirely; send-side
          FloodWaits are slept off and the pace adapts automatically

        Args:
          source_chat_ref: chat_id (int) or username (str) of the source channel
          dest_chat_id: destination chat_id (int) or "me" for Saved Messages
          start_id: first message ID to forward (default: 1 = oldest)
          end_id: last message ID to forward (0 = auto-detect latest)
          cancel_event: asyncio.Event — set to cancel
          status_callback: async callable(status_text)
          stats_callback: async callable(result_dict)
          drop_author: if True, strips "Forwarded from" header
          drop_media_captions: if True, strips ALL captions from media
          batch_size: messages per forwardMessages call (max 100)
          batch_delay: base seconds between batches. 3.5 s ≈ 28.5 msgs/s,
            safely under Telegram's ~30 msgs/s sustained account budget
            (the old 1.5 s ≈ 66 msgs/s is what triggered FloodWait around
            ~2000 messages on big channels). Grows automatically on FloodWait.

        Returns:
          dict with sent_count, failed_count, total_seen, last_message_id, etc.
        """
        from telethon.errors import FloodWaitError, MessageIdInvalidError
        from telethon.tl.functions.messages import GetSplitRangesRequest
        import asyncio as _asyncio
        import time as _time

        result = {
            "sent_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "total_seen": 0,
            "last_message_id": 0,
            "cancelled": False,
            "flood_waits": 0,
            "in_flight": 0,
            "started_at": time.time(),
        }

        # Clamp batch_size to 100 (Telegram's hard limit)
        batch_size = min(batch_size, 100)

        # Resolve entities ONCE
        try:
            source_entity = await self._resolve_entity(source_chat_ref, None)
            dest_entity = await self._resolve_entity(dest_chat_id, None)
        except Exception as e:
            if status_callback:
                await status_callback(f"❌ Failed to resolve entities: {type(e).__name__}: {e}")
            result["failed_count"] = -1
            return result

        # Auto-detect end_id if not provided
        if end_id <= 0:
            if status_callback:
                await status_callback("🔍 Detecting latest message ID...")
            try:
                latest = await self.client.get_messages(source_entity, limit=1)
                if latest:
                    msg = latest[0] if isinstance(latest, list) else latest
                    end_id = getattr(msg, "id", 0)
                    if status_callback:
                        await status_callback(
                            f"✓ Latest message ID: {end_id}\n"
                            f"  Will forward IDs {start_id} to {end_id} "
                            f"({end_id - start_id + 1} messages)"
                        )
                else:
                    if status_callback:
                        await status_callback("❌ Channel appears empty")
                    return result
            except Exception as e:
                if status_callback:
                    await status_callback(f"❌ Failed to detect latest ID: {type(e).__name__}: {e}")
                result["failed_count"] = -1
                return result

        if end_id < start_id:
            if status_callback:
                await status_callback(f"❌ end_id ({end_id}) < start_id ({start_id})")
            return result

        if status_callback:
            await status_callback(
                f"🚀 Starting ID-based forward: {source_chat_ref} → {dest_chat_id}\n"
                f"   IDs {start_id} to {end_id} ({end_id - start_id + 1} messages)\n"
                f"   Batch: {batch_size} msgs/call, delay: {batch_delay:.1f}s (adaptive)\n"
                f"   drop_author: {drop_author}"
            )

        last_status_time = 0
        status_interval = 2.0
        MAX_FLOOD_RETRIES = 10

        # Adaptive send pacer: 100 messages per forwardMessages call at the
        # old 1.5 s pace ≈ 66 msgs/s — over Telegram's ~30 msgs/s sustained
        # account budget, which is why big /scrapeid runs hit FloodWait at
        # ~2000 messages. 3.5 s/batch ≈ 28.5 msgs/s stays under it; the pacer
        # backs off further whenever Telegram complains and slowly recovers
        # afterwards, converging on the fastest flood-free pace.
        send_pacer = AdaptivePacer(base=batch_delay, maximum=20.0)

        # Build forward kwargs once — drop_media_captions needs Telethon
        # >= 1.40 (detect support instead of crashing with TypeError).
        fw_kwargs = _forward_kwargs(drop_author, drop_media_captions)

        async def _forward_ids(ids: list) -> None:
            """Forward one slice of IDs with FloodWait-aware retry.

            Handles deleted-ID ranges by bisecting: a MessageIdInvalidError
            on a multi-ID slice means SOME ids in it are deleted, so we split
            and recurse into both halves. FloodWaits sleep (cancel-aware)
            and retry the SAME slice — the old code slept but then dropped
            the half entirely, silently losing messages. Sets
            result['failed_count'] = -1 to signal a fatal abort.
            """
            for attempt in range(MAX_FLOOD_RETRIES):
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    return
                try:
                    await self.client.forward_messages(
                        dest_entity, ids, from_peer=source_entity, **fw_kwargs,
                    )
                    result["sent_count"] += len(ids)
                    if ids and ids[-1] > result["last_message_id"]:
                        result["last_message_id"] = ids[-1]
                    send_pacer.on_success()
                    return
                except MessageIdInvalidError:
                    if len(ids) <= 1:
                        # A single deleted message ID — count and move on
                        result["failed_count"] += 1
                        return
                    mid = len(ids) // 2
                    await _forward_ids(ids[:mid])
                    if result["cancelled"] or result["failed_count"] == -1:
                        return
                    # Pace between the bisection halves as well
                    if not await _sleep_chunks(send_pacer.current / 2, cancel_event):
                        result["cancelled"] = True
                        return
                    await _forward_ids(ids[mid:])
                    return
                except FloodWaitError as e:
                    result["flood_waits"] += 1
                    wait = e.seconds + 2
                    new_pace = send_pacer.on_flood(e.seconds)
                    if status_callback:
                        await status_callback(
                            f"⏳ FloodWait: sleeping {wait}s "
                            f"(attempt {attempt+1}/{MAX_FLOOD_RETRIES})...\n"
                            f"   Sent so far: {result['sent_count']}\n"
                            f"   Batch delay auto-adjusted to {new_pace:.1f}s"
                        )
                    await _sleep_chunks(wait, cancel_event)
                    continue  # retry the same slice
                except ConnectionError:
                    logger.warning("connection dropped during forward — reconnecting")
                    if status_callback:
                        await status_callback("⚠️ Connection dropped. Reconnecting...")
                    await _asyncio.sleep(2)
                    if not await self._ensure_connected():
                        result["failed_count"] = -1
                        return
                    continue
                except _asyncio.CancelledError:
                    result["cancelled"] = True
                    return
                except Exception as e:
                    logger.warning("forward batch failed: %s", e)
                    result["failed_count"] += len(ids)
                    return
            # Exhausted flood retries for this slice — fatal abort
            if status_callback:
                await status_callback(
                    f"❌ Exhausted {MAX_FLOOD_RETRIES} flood retries on IDs "
                    f"{ids[0] if ids else '?'}+ — stopping."
                )
            result["failed_count"] = -1

        # Iterate ID ranges in batches
        current_id = start_id
        while current_id <= end_id:
            # Check cancellation
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                break

            # Ensure connected
            if not await self._ensure_connected():
                if status_callback:
                    await status_callback("❌ Connection lost. Stopping.")
                result["failed_count"] = -1
                return result

            # Build batch of IDs
            batch_end = min(current_id + batch_size - 1, end_id)
            msg_ids = list(range(current_id, batch_end + 1))
            result["total_seen"] += len(msg_ids)

            # Forward this batch (FloodWait-aware; bisects around deleted IDs)
            await _forward_ids(msg_ids)

            if result["cancelled"]:
                break
            if result["failed_count"] == -1:
                # _forward_ids exhausted flood retries or lost the connection
                return result

            # Stats callback
            if stats_callback:
                try:
                    await stats_callback(result)
                except Exception:
                    pass

            # Periodic status update
            now = _time.time()
            if status_callback and now - last_status_time > status_interval:
                last_status_time = now
                elapsed = now - result["started_at"]
                throughput = result["sent_count"] / (elapsed / 60) if elapsed > 1 else 0
                pct = (result["sent_count"] / result["total_seen"] * 100) if result["total_seen"] > 0 else 0
                await status_callback(
                    f"📊 ID-based forward in progress...\n\n"
                    f"Current ID: {batch_end} / {end_id}\n"
                    f"Sent: {result['sent_count']}\n"
                    f"Failed: {result['failed_count']}\n"
                    f"Flood waits: {result['flood_waits']}\n"
                    f"Progress: {pct:.1f}%\n"
                    f"Speed: {throughput:.0f} items/min\n"
                    f"Batch delay: {send_pacer.current:.1f}s (adaptive)\n"
                    f"Elapsed: {elapsed:.0f}s"
                )

            current_id = batch_end + 1

            # Adaptive inter-batch delay. NOTE: the old code used
            # `range(int(batch_delay))`, which silently TRUNCATED fractional
            # delays (the documented 1.5 s actually slept only 1 s — even
            # faster than intended). _sleep_chunks is float-safe, honours the
            # pacer, and stays cancellable.
            if current_id <= end_id:
                if not await _sleep_chunks(send_pacer.current, cancel_event):
                    result["cancelled"] = True
                    break

        # Final status
        if status_callback:
            if result["cancelled"]:
                await status_callback(
                    f"🛑 Forward cancelled.\n\n"
                    f"Sent: {result['sent_count']}\n"
                    f"Failed: {result['failed_count']}\n"
                    f"Last ID: {result['last_message_id']}"
                )
            else:
                await status_callback(
                    f"✅ Forward complete!\n\n"
                    f"Total IDs: {result['total_seen']}\n"
                    f"Sent: {result['sent_count']}\n"
                    f"Failed: {result['failed_count']}\n"
                    f"Flood waits: {result['flood_waits']}\n"
                    f"Last ID: {result['last_message_id']}"
                )

        return result


__all__ = [
    "UserSession",
    "ParsedLink",
    "parse_telegram_link",
    "parse_channel_link",
]
