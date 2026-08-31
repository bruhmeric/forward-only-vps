# Telegram Forwarder Bot — VPS Deployment

Run the **Telegram Forwarder Bot** + a **web dashboard** on a single VPS using Docker.

## What This Bot Does

- Forward any media you send to a destination group/topic
- Pull content from locked private channels via t.me links
- Auto-scrape entire channels (`/scrape`)
- **Bulk-forward by ID range** (`/scrapeid`) — no rate limits, works on 50k+ message channels
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
| `photo` / `video` / `doc` / `audio` / `voice` / `animation` | Only these media types |
| `parallel=N` | Set parallel sends (default: 5, max: 10) |

**Examples:**
```
/scrape https://t.me/publicchannel
/scrape https://t.me/c/1234567890 saved old
/scrape https://t.me/c/1234567890 photo video parallel=8
```

**⚠️ Limitation:** Fails after ~1800 messages due to getHistory rate limits (30s per 10 requests). For large channels, use `/scrapeid` instead.

---

### `/scrapeid` — ID-Based Bulk Forwarding (Recommended for Large Channels)

Uses `forward_messages(ids, from_peer)` which takes message IDs directly — **no getHistory needed**. Uses the SEND rate-limit bucket (different from getHistory), so it can handle 50k+ messages without rate limits.

**Best for:** Large public channels (1000+ messages), bulk-forwarding everything.

| Flag | Effect |
|---|---|
| `saved` | Send to Saved Messages. Default: destination group. |
| `keep` | Keep "Forwarded from" header. Default: strip. |
| `strip` | Strip ALL captions from media. Default: keep captions. |

**Usage:**
```
/scrapeid <url>                          # Forward ALL messages (auto-detect range)
/scrapeid <url> 1 5000                   # Forward IDs 1 to 5000
/scrapeid <url> 1000 2000 saved          # Forward IDs 1000-2000 to Saved Messages
/scrapeid <url> 1 5000 keep              # Keep "Forwarded from" header
/scrapeid <url> 1 5000 strip             # Strip ALL captions
/scrapeid <url> 1 5000 keep strip        # Keep header + strip captions
/scrapeid <url> saved strip              # Forward all to Saved Messages, strip captions
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
- Uses `forwardMessages` (send bucket, ~30/sec) instead of `getHistory` (read bucket, ~10/30s)
- ~4000 messages/minute throughput
- No ~1800-message FloodWait cliff

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

`/scrapeid` is already optimized — no tuning needed:
- 100 messages per API call (Telegram's max)
- 1.5s delay between batches (safe for send limits)
- ~4000 messages/minute throughput

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

### `/scrape` fails after ~1800 messages

This is a known Telegram rate limit on `getHistory` (30s per 10 requests). **Use `/scrapeid` instead** — it uses `forwardMessages` which has a different rate-limit bucket and doesn't hit this limit:

```
# Instead of:
/scrape https://t.me/bigchannel

# Use:
/scrapeid https://t.me/bigchannel
```

### `/stop_scrape` doesn't work

The 3-tier force-stop should always work. If it doesn't:
1. Wait 10 seconds (Tier 1: graceful stop)
2. The bot will try `task.cancel()` (Tier 2)
3. If still stuck, the bot will disconnect Telethon (Tier 3)
4. As a last resort: `docker-compose restart forwarder-bot`

### Scrape is slow

- For `/scrape`: Increase `PARALLEL` in `.env` (default: 5, max: 10)
- For large channels: Use `/scrapeid` instead (much faster, no rate limits)
- If you hit FloodWait often, lower the parallel count

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
