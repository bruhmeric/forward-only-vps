# Telegram Forwarder Bot — VPS Deployment

Run the **Telegram Forwarder Bot** + a **web dashboard** on a single VPS using Docker.

## What This Bot Does

- Forward any media you send to a destination group/topic
- Pull content from locked private channels via t.me links
- Auto-scrape entire channels (`/scrape`)
- Send directly to Saved Messages (`/saved`)
- Custom captions (`/caption`)
- Media type filters (`/scrape <url> photo video`)
- Real-time progress with live stats dashboard

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
- **Live Scrape**: Real-time progress with progress bar, sent/failed/skipped counts, in-flight count, speed (items/min), elapsed time
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
| `/scrape <url> [flags]` | Scrape ALL media from a channel |
| `/stop_scrape` | Stop the active scrape |
| `/scrape_status` | Check scrape progress |
| `/caption <text>` | Set custom caption for all forwards |
| `/caption strip` | Strip ALL captions from forwarded media |
| `/caption clear` | Restore original captions |
| `/status` | Show bot status |
| `/info` | Show destination chat info |

### Scrape Flags

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

The bot's scraping speed is controlled by the `PARALLEL` environment variable (default: 5):

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

The bot automatically handles FloodWait errors by sleeping the requested duration and retrying.

---

## File Structure

```
telegram-forwarder-vps/
├── telegram-forwarder-bot/       # The Forwarder Bot
│   ├── bot.py                    # Main entry + /stats endpoint for dashboard
│   ├── config.py                 # Env loader (includes PARALLEL)
│   ├── db.py                     # SQLite layer (includes cumulative stats)
│   ├── user_session.py           # Telethon manager + scrape_channel
│   ├── topics.py                 # Forum topic discovery
│   ├── login.py                  # One-time Telethon login
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── admin.py              # /scrape, /saved, /caption, etc.
│   │   ├── direct.py             # Direct forward + batch window
│   │   └── link.py              # Locked-channel URL → fetch + forward
│   ├── requirements.txt
│   ├── Dockerfile
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

### Dashboard shows bot as offline

- Check if containers are running: `docker-compose ps`
- Check logs: `docker-compose logs forwarder-bot`
- The dashboard queries internal Docker DNS (`forwarder-bot`) — this only works when both containers are on the same `bots-network`

### Scrape is slow

- Increase `PARALLEL` in `.env` (default: 5, max: 10)
- If you hit FloodWait often, lower the parallel count
- For huge channels, scraping will take time at any rate

### Firewall

```bash
sudo ufw allow 8080/tcp    # Dashboard
```

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
