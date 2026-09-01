# Telegram Forwarder Bot — VPS Deployment

Run the **Telegram Forwarder Bot** + a **web dashboard** on a single VPS using Docker.

## What This Bot Does

- Forward any media you send to a destination group/topic
- Pull content from locked private channels via t.me links
- Auto-scrape entire channels (`/scrape`)
- **Bulk-forward by ID range** (`/scrapeid`) — flood-adaptive pacing, works on 50k+ message channels
- Send directly to Saved Messages (`/saved`)
- Custom captions (`/caption`)
- Media type filters (`/scrape <url> photo video`)
- Strip captions / keep "Forwarded from" header
- Convert GIFs/animations to regular video files
- Auto-reconnect on connection drops
- Real-time progress with live stats dashboard
- All-time cumulative statistics (persisted across restarts)
- 3-tier force-stop (`/stop_scrape` always works, even when stuck)

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              Your VPS                     │
                    │                                          │
   Port 8080  ───►  │  ┌──────────────┐                        │
   (Dashboard)      │  │  Dashboard   │──┐                     │
                    │  └──────────────┘  │  Docker network     │
                    │  ┌──────────────┐  │  (internal)         │
                    │  │ Forwarder    │──┘                      │
                    │  │ Bot (:8081)  │◄─── stats               │
                    │  └──────────────┘                         │
                    └─────────────────────────────────────────┘
```

- **Port 8080** — Web Dashboard (live stats, all-time stats, controls)
- Forwarder bot's stats server (port 8081) is internal-only (Docker network)

---

## Prerequisites

- A VPS with **Docker** and **Docker Compose** installed
- A **Telegram bot token** (from @BotFather)
- **Telegram API credentials** (API_ID + API_HASH from https://my.telegram.org/apps)
- A **SESSION_STRING** (Telethon user session)

### Installing Docker on your VPS (if not already installed)

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for the docker group to take effect

# Verify
docker --version
docker-compose --version
```

---

## Quick Start (4 steps)

### Step 1. Clone this repo to your VPS

```bash
git clone <your-repo-url> telegram-forwarder-vps
cd telegram-forwarder-vps
```

### Step 2. Generate SESSION_STRING (locally, NOT on the VPS)

You need to receive the Telegram login code, so run this on your laptop:

```bash
cd telegram-forwarder-bot
pip install -r requirements.txt

# Create .env with API_ID, API_HASH, PHONE
cp .env.example .env
nano .env  # fill in API_ID, API_HASH, PHONE

# Generate the session string
python login.py --string
# Copy the printed string (starts with "1")
```

### Step 3. Edit the .env file

```bash
nano telegram-forwarder-bot/.env
```

Required fields:

```env
# From @BotFather
BOT_TOKEN=123456789:ABC-DEF...

# From my.telegram.org
API_ID=1234567
API_HASH=abcdef1234567890abcdef1234567890

# From login.py --string
SESSION_STRING=1BVtsOH8Bu...

# Destination group ID (optional — use /saved or /scrape saved instead)
# DESTINATION_GROUP_ID=-1001234567890

# Your Telegram user ID (optional — for admin whitelist)
# ADMIN_IDS=123456789

# Run mode — MUST be polling on VPS (no webhook)
MODE=polling

# Database path (inside the Docker container)
DB_PATH=/app/data/forwarder.db

# Parallel sends during /scrape (default: 5, max: 10)
# Higher = faster but risks FloodWait
# PARALLEL=5

# ── Flood resilience (Telethon-first strategy) ─────────────────────
# Telethon auto-sleeps + auto-retries every FloodWait/SlowModeWait
# whose wait is <= this threshold (seconds). Waits LONGER than the
# threshold raise FloodWaitError to the bot, which shows a live
# countdown in the status message + dashboard ("[FLOOD WAIT] 12m30s
# remaining, resumes ~15:42") and sleeps it off before retrying.
# Default 60 = short waits are absorbed silently, long waits become
# VISIBLE. ⚠️ Do NOT set 86400 hoping for extra safety — it makes
# Telethon absorb EVERY wait internally with NO status updates for up
# to 24h (the scrape still survives, but it looks completely stuck).
# FLOOD_SLEEP_THRESHOLD=60

# Extended human-like break: pause this many seconds after every N sent
# messages so the account-level rate budget fully recovers on long runs.
# FLOOD_BREAK_EVERY=500
# FLOOD_BREAK_SECONDS=300

# ── Parallel scrapes ───────────────────────────────────────────────
# How many scrape jobs (/scrape and/or /scrapeid) may run at the same
# time. 2 = the classic combo (one /scrapeid + one /scrape). 1 restores
# the old one-at-a-time behavior. Hard cap 4. All jobs share ONE
# Telegram account, so the rate budget is shared — more jobs means more
# frequent (visible) flood waits, not double throughput.
# MAX_CONCURRENT_SCRAPES=2
```

### Step 4. Start all services

```bash
docker-compose up -d --build
```

That's it! The bot and dashboard are now running.

---

## Accessing the Dashboard

```
http://YOUR_VPS_IP:8080
```

The dashboard shows:

- **All-Time Statistics**: Total scrapes, total sent, total failed, total skipped, flood waits, saved forwards
- **Live Scrape**: Real-time progress with progress bar, sent/failed/skipped counts, in-flight count, speed (items/min), elapsed time, ETA
- **Bot Configuration**: Bot name, mode, destination, forum status, caption mode
- **Controls**: Stop scrape, clear caption, reset stats

Auto-refreshes every 3 seconds.

---

## Bot Commands

Send these to the bot on Telegram:

| Command | Action |
|---|---|
| `/start` | Show intro |
| `/help` | Show all commands |
| `/setgroup <id>` | Set destination group/channel |
| `/saved <url>` | Send t.me link content to Saved Messages |
| `/scrape <url> [flags]` | Scrape ALL media from a channel (uses getHistory) |
| `/scrapeid <url> [start] [end] [flags]` | **Bulk-forward by ID range** — no rate limits |
| `/stop_scrape` | Stop the active scrape (3-tier force-stop, always works) |
| `/scrape_status` | Check scrape progress |
| `/caption <text>` | Set custom caption for all forwards |
| `/caption strip` | Strip ALL captions from forwarded media |
| `/caption clear` | Restore original captions |
| `/reconnect` | Retry Telethon connection (if session failed at boot) |
| `/status` | Show bot status |
| `/info` | Show destination chat info |
| `/whoami` | Show your Telegram user ID + admin status |
| `/test_link <url>` | Diagnostic: test fetching a t.me link |

---

## `/scrape` vs `/scrapeid` — Which to Use?

### `/scrape` — History-Based Scraping

Uses `getHistory` API to read messages, then forwards them. Supports media filters and custom captions.

**Best for:** Small channels (<1000 messages), protected channels, or when you need media type filters.

| Flag | Effect |
|---|---|
| `old` | Oldest first (chronological). Default: newest first. |
| `saved` | Send to Saved Messages. Default: destination group. |
| `resume` | Continue from the last checkpoint of this channel (after a crash, flood abort, or `/stop_scrape`) |
| `photo` / `video` / `doc` / `audio` / `voice` / `animation` | Only these media types |
| `parallel=N` | Set parallel sends (default: 5, max: 10) |

**Examples:**
```
/scrape https://t.me/publicchannel
/scrape https://t.me/c/1234567890 saved old
/scrape https://t.me/c/1234567890 photo video parallel=8
/scrape https://t.me/c/1234567890 resume       # continue where the last run stopped
```

**⚠️ Rate limits:** `/scrape` reads history via `get_messages` (getHistory bucket: ~10 requests / 30 s). The bot paces reads safely under that budget with an **adaptive backoff** — if Telegram still returns a FloodWait, it sleeps it off, slows the read pace, and continues. It no longer dies at ~1800 messages, but for very large channels `/scrapeid` remains the faster option.

---

### `/scrapeid` — ID-Based Bulk Forwarding (Recommended for Large Channels)

Uses `forward_messages(ids, from_peer)` which takes message IDs directly — **no getHistory needed**. It uses the SEND rate-limit bucket (different from getHistory) and paces batches **adaptively**: ~100 msgs per call every 3.5 s (≈28 msgs/s, under Telegram's ~30 msgs/s sustained budget), growing the delay automatically whenever a FloodWait appears and relaxing it back afterwards.

**Best for:** Large public channels (1000+ messages), bulk-forwarding everything.

| Flag | Effect |
|---|---|
| `saved` | Send to Saved Messages. Default: destination group. |
| `keep` | Keep "Forwarded from" header. Default: strip. |
| `strip` | Strip ALL captions from media. Default: keep captions. |
| `resume` | Continue from the last checkpoint (skips already-forwarded IDs). |

**Usage:**
```
/scrapeid <url>                          # Forward ALL messages (auto-detect range)
/scrapeid <url> 1 5000                   # Forward IDs 1 to 5000
/scrapeid <url> 1000 2000 saved          # Forward IDs 1000-2000 to Saved Messages
/scrapeid <url> 1 5000 keep              # Keep "Forwarded from" header
/scrapeid <url> 1 5000 strip             # Strip ALL captions
/scrapeid <url> 1 5000 keep strip        # Keep header + strip captions
/scrapeid <url> saved strip              # Forward all to Saved Messages, strip captions
/scrapeid <url> resume                   # Continue after a stop/crash (no duplicates)
```

**Caption behavior:**
| Flags | "Forwarded from" header | Captions |
|---|---|---|
| *(none — default)* | Stripped | Kept |
| `keep` | Kept | Kept |
| `strip` | Stripped | Stripped |
| `keep strip` | Kept | Stripped |

**Why `/scrapeid` is faster:**
- 100 messages per API call (vs 1 per call with getHistory)
- Uses `forwardMessages` (send bucket) instead of `getHistory` (read bucket)
- Adaptive, jittered pacing stays under Telegram's ~30 msgs/s account budget, so sustained runs don't trigger FloodWait cliffs
- FloodWaits (if any) are slept off and the pace backs off automatically — the scrape never dies
- Extended breaks + per-batch checkpoints make multi-hour 50k+ runs safe and resumable

**Limitations:**
- Can't filter by media type (forwards everything)
- Can't apply custom captions (use `strip` flag instead)
- Doesn't work on protected channels (use `/scrape` for those)

---

## Caption Control

### Via `/caption` command (affects `/scrape` and `/saved`)

```
/caption <text>      — set custom caption (replaces original on all forwards)
/caption strip       — strip ALL captions (forward media without any text)
/caption clear       — restore original caption behavior
/caption             — show current setting
```

### Via `/scrapeid` flags (per-scrape control)

```
/scrapeid <url>                    — default (keep captions, strip header)
/scrapeid <url> strip              — strip captions
/scrapeid <url> keep               — keep "Forwarded from" header
/scrapeid <url> keep strip         — keep header, strip captions
```

---

## GIF/Animation Handling

All videos (including GIFs/animations) are sent as **regular video files** with playback controls — not as looping GIF animations. This applies to both `/scrape` and `/scrapeid`.

The bot strips the `DocumentAttributeAnimated` flag from GIFs and adds `DocumentAttributeVideo` with `supports_streaming=True`, so:
- Short videos appear as videos (not GIFs)
- Playback controls are available (play/pause/seek)
- Videos are streamable
- Original thumbnails are preserved

---

## Auto-Reconnect

The bot automatically handles connection drops:

- **Telethon client:** `auto_reconnect=True`, `connection_retries=10`, `retry_delay=2`, `request_retries=5`
- **`_ensure_connected()`:** Called before every command — reconnects if disconnected
- **`/reconnect` command:** Manually retry the Telethon connection without restarting the bot
- **During scrape:** Connection drops trigger auto-reconnect with resume from last message ID

---

## `/stop_scrape` — 3-Tier Force Stop

The `/stop_scrape` command always works, even when the scrape is stuck:

1. **Tier 1 (graceful):** Set the cancel event, wait up to 10 seconds
2. **Tier 2 (forceful):** Call `task.cancel()` — schedules `CancelledError` at the next await checkpoint
3. **Tier 3 (nuclear):** Disconnect the Telethon client — tears down blocked socket reads, then reconnects

This guarantees the scrape stops even when stuck in a C-level blocking I/O call.

---

## File Size Support

- Supports files up to **2GB** (Telegram's hard limit)
- Uses `InputFileBig` for files >10MB (parallel chunked upload with 4 concurrent chunks)
- No 50MB limit (removed the old Bot API cap)

---

## Monitoring & Management

### View logs

```bash
docker-compose logs -f forwarder-bot      # Bot logs
docker-compose logs -f dashboard          # Dashboard logs
```

### Check running containers

```bash
docker-compose ps
```

### Stop / Restart

```bash
docker-compose stop
docker-compose restart
```

### Rebuild after code changes

```bash
docker-compose up -d --build
```

---

## Performance Tuning

### `/scrape` speed (PARALLEL env var)

The `/scrape` command's speed is controlled by the `PARALLEL` environment variable (default: 5):

| PARALLEL | Speed | FloodWait Risk |
|---|---|---|
| 3 | ~9 msgs/sec | Very low (conservative) |
| 5 (default) | ~15 msgs/sec | Low |
| 8 | ~24 msgs/sec | Moderate |
| 10 (max) | ~30 msgs/sec | Higher (bot handles FloodWait automatically) |

Set it in `.env`:
```env
PARALLEL=8
```

### `/scrapeid` speed

`/scrapeid` is tuned to run flood-free by default — no tuning needed:
- 100 messages per API call (Telegram's max)
- 3.5 s adaptive, **randomly-jittered** delay between batches (~1,700 msgs/min sustained, under the ~30 msgs/s account budget) — the jitter keeps the traffic pattern from looking metronomic/bot-like
- On FloodWait: sleeps the requested time, retries the same batch, and increases the delay — never loses messages, never dies
- Transient non-flood errors (timeouts, RPC hiccups) get exponential backoff (1s → 2s → 4s) before a batch is counted as failed

### Flood resilience — how it all fits together

The bot uses a layered, Telethon-first strategy:

1. **Telethon built-in auto-handling** — the client is created with `flood_sleep_threshold=60`: every FloodWait/SlowModeWait ≤ 60s is silently slept off and retried *inside the library*. Longer waits raise FloodWaitError to the bot, which **shows a live countdown** (status message + dashboard + `/scrape_status`), sleeps the requested time in cancel-aware chunks, and retries the same batch — so long waits are resilient AND visible. (⚠️ `None` is NOT "wait forever" — Telethon converts it to 0, which raises every flood immediately. And 86400, while survivable, makes every wait invisible for up to 24h — the bot then looks frozen during long flood waits, which is exactly the "stuck at budget recovery" symptom.)
2. **Adaptive pacing** — an `AdaptivePacer` grows the inter-batch delay whenever a FloodWait still leaks through, and relaxes it after a streak of clean batches.
3. **Human-like jitter** — every inter-batch delay is randomized to 75–160% of the pacer value, so the request pattern isn't metronomic.
4. **Extended breaks** — after every 500 sent messages (configurable: `FLOOD_BREAK_EVERY` / `FLOOD_BREAK_SECONDS`), the bot pauses 5 minutes so the account-level budget fully recovers, like a human taking a break. The status message and dashboard show a **live countdown** during the break ("[RECOVERY BREAK] 4m12s remaining"), and `/stop_scrape` works throughout.
5. **Exponential backoff** — non-flood transient errors retry with 1s → 2s → 4s delays instead of killing the scrape.
6. **Live status everywhere** — both `/scrape` and `/scrapeid` run a 2-second status ticker that shows the active wait phase (flood wait / recovery break) with a ticking countdown, plus a "Last progress: Xs ago" liveness line; the dashboard `/stats` JSON exposes `phase`, `phase_seconds_left` and `seconds_since_progress` for the same purpose. A waiting scrape is always distinguishable from a hung one.
7. **Checkpointing (state saving)** — `last_message_id` is persisted to SQLite after **every batch**. If anything does stop the run (crash, `/stop_scrape`, reboot), re-run the same command with the `resume` flag and it continues exactly where it stopped — no duplicated sends:
   - `/scrape <url> resume` (or `/scrape <url> old resume`)
   - `/scrapeid <url> resume`

---

## All-Time Statistics

The bot tracks cumulative stats that persist across restarts (stored in SQLite):

- Total scrapes run
- Total messages sent
- Total messages failed
- Total messages skipped
- Total flood waits
- Total saved forwards

View them in the dashboard at `http://YOUR_VPS_IP:8080`, or reset them with the "Reset All Stats" button.

---

## File Structure

```
telegram-forwarder-vps/
├── telegram-forwarder-bot/       # The Forwarder Bot
│   ├── bot.py                    # Main entry + /stats endpoint for dashboard
│   ├── config.py                 # Env loader (includes PARALLEL)
│   ├── db.py                     # SQLite layer (includes cumulative stats)
│   ├── user_session.py           # Telethon manager + scrape_channel + scrape_channel_by_ids
│   ├── topics.py                 # Forum topic discovery
│   ├── login.py                  # One-time Telethon login
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── admin.py              # /scrape, /scrapeid, /saved, /caption, /reconnect, etc.
│   │   ├── direct.py             # Direct forward + batch window
│   │   └── link.py              # Locked-channel URL → fetch + forward
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── .env.example
│   └── .env                      # ← you fill this in
│
├── dashboard/                    # Web Dashboard
│   ├── dashboard.py              # aiohttp web server (port 8080)
│   ├── requirements.txt
│   └── Dockerfile
│
├── docker-compose.yml            # Runs bot + dashboard
└── README.md                     # This file
```

---

## Troubleshooting

### Bot doesn't respond to commands

- Check `ADMIN_IDS` — if set, only listed users can use the bot. Send `/whoami` to see your user ID.
- Check logs: `docker-compose logs forwarder-bot`
- Verify `SESSION_STRING` is valid (not expired/revoked)
- Send `/reconnect` to retry the Telethon connection

### "Telethon session failed to start at boot"

1. Check logs: `docker-compose logs forwarder-bot | grep Telethon`
2. Look for:
   - `Config check: API_ID=MISSING` → your `.env` doesn't have API_ID
   - `Config check: SESSION_STRING=MISSING` → your `.env` doesn't have SESSION_STRING
   - `Telethon: session exists but is NOT authorized` → SESSION_STRING is invalid/revoked
3. Send `/reconnect` to retry, or regenerate SESSION_STRING:
   ```bash
   # On your laptop:
   python login.py --string
   # Update SESSION_STRING in .env on the VPS, then:
   docker-compose restart forwarder-bot
   ```

### Dashboard shows bot as offline

- Check if containers are running: `docker-compose ps`
- Check logs: `docker-compose logs forwarder-bot`
- The dashboard queries internal Docker DNS (`forwarder-bot`) — this only works when both containers are on the same `bots-network`

### `/scrape` hits FloodWait on big channels

This used to be a hard failure around ~1800–2000 messages. It is now handled by the layered flood strategy (see "Flood resilience" above):
- Flood waits ≤ 60s are auto-slept inside Telethon; longer waits surface to the bot, which displays a **live countdown** in the status message and dashboard, sleeps the requested time, and retries the same batch — the scrape keeps going and you can see exactly when it will resume
- Read pacing (getHistory) keeps safely under Telegram's ~10 req/30 s budget with headroom, with random human-like jitter
- Send-side FloodWaits are slept off and retried — they no longer fall into the slow re-upload fallback
- An adaptive pacer slows reads/sends after any FloodWait and speeds back up once quiet
- Extended breaks (default 5 min per 500 msgs) let the account budget recover on huge runs — shown as a live countdown, never looking like a hang
- If a run still stops (crash, reboot, `/stop_scrape`), resume it with `/scrape <url> resume` — progress is checkpointed after every batch, so nothing is re-sent

**"The bot looks stuck / nothing updates"** — if the status line shows `[FLOOD WAIT]` or `[RECOVERY BREAK]` with a countdown, the bot is *waiting by design* (Telegram asked it to slow down); it will auto-resume when the countdown hits zero, and `/stop_scrape` works the whole time. A truly hung bot would show no countdown and "Last progress" climbing without a phase line — report that case.

For very large channels, `/scrapeid` is still the recommended (and much faster) path:

```
# Instead of:
/scrape https://t.me/bigchannel

# Use:
/scrapeid https://t.me/bigchannel

# And if it ever gets interrupted:
/scrapeid https://t.me/bigchannel resume
```

### `/stop_scrape` doesn't work

The 3-tier force-stop should always work. If it doesn't:
1. Wait 10 seconds (Tier 1: graceful stop)
2. The bot will try `task.cancel()` (Tier 2)
3. If still stuck, the bot will disconnect Telethon (Tier 3)
4. As a last resort: `docker-compose restart forwarder-bot`

### Scrape is slow

- For `/scrape`: Increase `PARALLEL` in `.env` (default: 5, max: 10)
- For large channels: Use `/scrapeid` instead (much faster, flood-adaptive)
- If you hit FloodWait often, lower the parallel count — the bot also backs off automatically now

### Firewall

```bash
sudo ufw allow 8080/tcp    # Dashboard
```

---

## Quick Reference

| Task | Command |
|---|---|
| Forward entire large channel | `/scrapeid https://t.me/channel` |
| Forward specific ID range | `/scrapeid https://t.me/channel 1 5000` |
| Forward to Saved Messages | `/scrapeid https://t.me/channel saved` |
| Strip captions | `/scrapeid https://t.me/channel strip` |
| Keep "Forwarded from" | `/scrapeid https://t.me/channel keep` |
| Small channel with filters | `/scrape https://t.me/channel photo video` |
| Protected channel | `/scrape https://t.me/c/123 saved` |
| Stop any scrape | `/stop_scrape` |
| Check progress | `/scrape_status` |
| Reconnect Telethon | `/reconnect` |
| Set custom caption | `/caption Check out this content!` |
| View dashboard | `http://YOUR_VPS_IP:8080` |

---

## Cost

- **VPS**: Any cheap VPS ($3-5/month) works — bot + dashboard use minimal CPU/RAM
- **Telegram**: Free (uses your personal account, not paid bot API)
- **Docker**: Free

Recommended VPS specs:
- 1 vCPU
- 1 GB RAM
- 10 GB disk
- Ubuntu 22.04 or 24.04
