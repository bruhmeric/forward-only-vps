"""Forwarder Bot Dashboard — single-bot monitoring & control.

Runs on port 8080. Proxies requests to:
  - Forwarder Bot: http://forwarder-bot:8081/stats
"""
import asyncio
import json
import logging
import os
from aiohttp import web, ClientSession
from urllib.parse import quote

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

# Bot endpoint (Docker container name)
FORWARDER_STATS_URL = os.environ.get("FORWARDER_STATS_URL",
                                      "http://forwarder-bot:8081/stats")
FORWARDER_STOP_SCRAPE_URL = os.environ.get("FORWARDER_STOP_SCRAPE_URL",
                                           "http://forwarder-bot:8081/stop_scrape")
FORWARDER_CANCEL_CAPTION_URL = os.environ.get("FORWARDER_CANCEL_CAPTION_URL",
                                              "http://forwarder-bot:8081/cancel_caption")
FORWARDER_RESET_STATS_URL = os.environ.get("FORWARDER_RESET_STATS_URL",
                                           "http://forwarder-bot:8081/reset_stats")

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))


async def fetch_json(url: str, timeout: float = 3.0) -> dict | None:
    """Fetch JSON from a URL with a short timeout. Returns None on error."""
    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception:
        return None


async def post_json(url: str, timeout: float = 5.0) -> dict | None:
    """POST to a URL with a short timeout. Returns None on error."""
    try:
        async with ClientSession() as session:
            async with session.post(url, timeout=timeout) as resp:
                try:
                    return await resp.json()
                except Exception:
                    return {"ok": False, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----- API handlers -----

async def api_stats(request: web.Request) -> web.Response:
    """GET /api/stats — fetch stats from the forwarder bot."""
    forwarder_stats = await fetch_json(FORWARDER_STATS_URL)
    return web.json_response({
        "forwarder": forwarder_stats,
        "forwarder_online": forwarder_stats is not None,
    })


async def api_stop_scrape(request: web.Request) -> web.Response:
    """POST /api/stop_scrape?job=J1|all — stop scrape job(s) on the bot.
    The optional `job` query param is forwarded verbatim (J1 / 1 / all);
    without it the bot stops every running scrape."""
    job = (request.query.get("job") or "").strip()
    url = FORWARDER_STOP_SCRAPE_URL
    if job:
        url += f"?job={quote(job)}"
    result = await post_json(url)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"})


async def api_cancel_caption(request: web.Request) -> web.Response:
    """POST /api/cancel_caption — clear the custom caption."""
    result = await post_json(FORWARDER_CANCEL_CAPTION_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"})


async def api_reset_stats(request: web.Request) -> web.Response:
    """POST /api/reset_stats — reset all cumulative stats to 0."""
    result = await post_json(FORWARDER_RESET_STATS_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"})


async def api_health(request: web.Request) -> web.Response:
    """GET /api/health — simple health check."""
    return web.json_response({"ok": True, "service": "dashboard"})


# ----- HTML dashboard -----

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Forwarder Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0a0e1a; color: #e0e0e0; padding: 20px; min-height: 100vh; }
        .header { text-align: center; margin-bottom: 25px; }
        .header h1 { font-size: 26px; color: #fff; font-weight: 700; }
        .header .subtitle { color: #6b7280; font-size: 13px; margin-top: 5px; }

        .status-bar { display: flex; justify-content: center; gap: 20px; margin-bottom: 25px;
                      flex-wrap: wrap; }
        .status-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px;
                       background: #1a1f2e; border-radius: 20px; border: 1px solid #2a3142;
                       font-size: 13px; font-weight: 500; }
        .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
        .status-online { background: #10b981; box-shadow: 0 0 8px #10b981; }
        .status-offline { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
        .status-unknown { background: #6b7280; }

        .container { max-width: 1200px; margin: 0 auto; }

        .section-title { font-size: 16px; color: #8b95a7; margin: 25px 0 12px;
                         text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }

        /* All-time stats grid */
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                      gap: 15px; margin-bottom: 25px; }
        .stat-card { background: linear-gradient(135deg, #1a1f2e 0%, #161a26 100%);
                     border-radius: 12px; padding: 20px; border: 1px solid #2a3142;
                     transition: transform 0.2s, border-color 0.2s; }
        .stat-card:hover { transform: translateY(-2px); border-color: #3a4252; }
        .stat-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase;
                            letter-spacing: 0.8px; font-weight: 600; }
        .stat-card .value { font-size: 28px; font-weight: 800; margin-top: 8px; color: #fff; }
        .stat-card .value.green { color: #10b981; }
        .stat-card .value.red { color: #ef4444; }
        .stat-card .value.orange { color: #f59e0b; }
        .stat-card .value.blue { color: #3b82f6; }
        .stat-card .value.purple { color: #a78bfa; }
        .stat-card .icon { font-size: 20px; margin-bottom: 8px; }

        /* Live scrape panel */
        .scrape-panel { background: #1a1f2e; border-radius: 12px; padding: 20px;
                        border: 1px solid #2a3142; margin-bottom: 20px; }
        .scrape-panel.active { border-color: #10b981; box-shadow: 0 0 20px rgba(16,185,129,0.15); }
        .scrape-panel h2 { font-size: 18px; margin-bottom: 15px; display: flex;
                           align-items: center; gap: 10px; }
        .scrape-badge { font-size: 11px; padding: 3px 10px; border-radius: 12px;
                        font-weight: 600; text-transform: uppercase; }
        .badge-running { background: #10b981; color: #fff; }
        .badge-idle { background: #2a3142; color: #6b7280; }

        /* One card per concurrent scrape job (e.g. /scrape + /scrapeid) */
        .job-card { background: #141928; border: 1px solid #2a3142;
                    border-radius: 10px; padding: 14px; margin-bottom: 12px; }
        .job-card-header { display: flex; align-items: center; gap: 8px;
                           flex-wrap: wrap; margin-bottom: 8px; font-size: 13px; }
        .job-chip { font-size: 11px; font-weight: 700; padding: 2px 8px;
                    border-radius: 6px; letter-spacing: 0.5px; }
        .job-chip-id { background: #3b82f6; color: #fff; }
        .job-chip-kind { background: #2a3142; color: #cbd5e1; }
        .job-chip-scrapeid { background: #8b5cf6; color: #fff; }
        .job-meta { color: #8b95a7; font-size: 12.5px; }

        .progress-bar-container { background: #0a0e1a; border-radius: 6px; height: 24px;
                                  overflow: hidden; margin: 12px 0; position: relative; }
        .progress-bar-fill { height: 100%; background: linear-gradient(90deg, #10b981, #34d399);
                             transition: width 0.5s; border-radius: 6px; }
        .progress-text { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
                         font-size: 12px; font-weight: 700; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,0.5); }

        .live-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
                      gap: 12px; margin: 15px 0; }
        .live-stat { background: #0a0e1a; padding: 12px; border-radius: 8px; text-align: center; }
        .live-stat .num { font-size: 22px; font-weight: 700; color: #fff; }
        .live-stat .lbl { font-size: 10px; color: #6b7280; text-transform: uppercase;
                          letter-spacing: 0.5px; margin-top: 4px; }

        .btn { padding: 10px 18px; border: none; border-radius: 8px; cursor: pointer;
               font-size: 13px; font-weight: 600; transition: all 0.2s; }
        .btn-stop { background: #ef4444; color: #fff; }
        .btn-stop:hover { background: #dc2626; }
        .btn-warn { background: #f59e0b; color: #000; }
        .btn-warn:hover { background: #d97706; }
        .btn-secondary { background: #2a3142; color: #e0e0e0; }
        .btn-secondary:hover { background: #3a4252; }
        .btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-row { display: flex; gap: 10px; margin-top: 15px; flex-wrap: wrap; }

        /* Info cards */
        .info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                     gap: 12px; margin-bottom: 20px; }
        .info-card { background: #1a1f2e; border-radius: 8px; padding: 14px;
                     border: 1px solid #2a3142; }
        .info-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase;
                            letter-spacing: 0.5px; font-weight: 600; }
        .info-card .value { font-size: 14px; color: #e0e0e0; margin-top: 4px; font-weight: 500; }

        .footer { text-align: center; margin-top: 30px; color: #4b5563; font-size: 12px;
                  padding-top: 20px; border-top: 1px solid #1a1f2e; }
        .refresh-indicator { display: inline-block; width: 8px; height: 8px;
                            border-radius: 50%; background: #10b981; margin-left: 8px;
                            animation: pulse 1.5s infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

        .empty-state { text-align: center; padding: 40px; color: #6b7280; }
        .empty-state .icon { font-size: 48px; margin-bottom: 12px; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Forwarder Bot Dashboard</h1>
        <div class="subtitle">Real-time monitoring &amp; control <span class="refresh-indicator"></span></div>
    </div>

    <div class="status-bar">
        <div class="status-pill">
            <span class="status-dot status-unknown" id="bot-dot"></span>
            <span id="bot-status-text">Checking...</span>
        </div>
        <div class="status-pill">
            <span class="status-dot status-unknown" id="telethon-dot"></span>
            <span>Telethon: <span id="telethon-status">?</span></span>
        </div>
    </div>

    <div class="container">
        <!-- All-Time Stats -->
        <div class="section-title">All-Time Statistics</div>
        <div class="stats-grid" id="cumulative-stats">
            <div class="stat-card">
                <div class="icon">🔍</div>
                <div class="label">Total Scrapes</div>
                <div class="value blue" id="cum-scrapes">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">📤</div>
                <div class="label">Total Sent</div>
                <div class="value green" id="cum-sent">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">❌</div>
                <div class="label">Total Failed</div>
                <div class="value red" id="cum-failed">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">⏭</div>
                <div class="label">Total Skipped</div>
                <div class="value orange" id="cum-skipped">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">⏳</div>
                <div class="label">Flood Waits</div>
                <div class="value orange" id="cum-flood">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">💾</div>
                <div class="label">Saved Forwards</div>
                <div class="value purple" id="cum-saved">0</div>
            </div>
        </div>

        <!-- Live Scrape -->
        <div class="section-title">Live Scrape</div>
        <div class="scrape-panel" id="scrape-panel">
            <h2>
                Scrape Status
                <span class="scrape-badge badge-idle" id="scrape-badge">IDLE</span>
            </h2>
            <div id="scrape-content">
                <div class="empty-state">
                    <div class="icon">💤</div>
                    <div>No scrape running</div>
                    <div style="font-size:12px; margin-top:8px; color:#4b5563;">
                        Send /scrape &lt;url&gt; to the bot to start
                    </div>
                </div>
            </div>
        </div>

        <!-- Bot Info -->
        <div class="section-title">Bot Configuration</div>
        <div class="info-grid" id="bot-info"></div>

        <!-- Controls -->
        <div class="section-title">Controls</div>
        <div class="btn-row">
            <button class="btn btn-warn" onclick="cancelCaption()">Clear Caption</button>
            <button class="btn btn-secondary" onclick="resetStats()">Reset All Stats</button>
        </div>
    </div>

    <div class="footer">
        Auto-refreshing every 3 seconds · Last update: <span id="last-update">--</span>
    </div>

    <script>
        async function fetchStats() {
            try {
                const resp = await fetch('/api/stats');
                const data = await resp.json();
                updateBot(data);
                updateCumulative(data);
                updateScrape(data);
                document.getElementById('last-update').textContent =
                    new Date().toLocaleTimeString();
            } catch (e) {
                console.error('Fetch failed:', e);
            }
        }

        function updateBot(data) {
            const dot = document.getElementById('bot-dot');
            const text = document.getElementById('bot-status-text');
            const tDot = document.getElementById('telethon-dot');
            const tStatus = document.getElementById('telethon-status');

            if (!data.forwarder_online) {
                dot.className = 'status-dot status-offline';
                text.textContent = 'Bot Offline';
                tDot.className = 'status-dot status-offline';
                tStatus.textContent = 'offline';
                document.getElementById('bot-info').innerHTML = '';
                return;
            }

            dot.className = 'status-dot status-online';
            text.textContent = 'Bot Online';
            const f = data.forwarder || {};

            // Telethon status
            if (f.telethon === 'connected') {
                tDot.className = 'status-dot status-online';
            } else {
                tDot.className = 'status-dot status-offline';
            }
            tStatus.textContent = f.telethon || 'unknown';

            // Bot info
            let capMode = 'Original captions';
            if (f.custom_caption === '') capMode = 'STRIP (no captions)';
            else if (f.custom_caption) capMode = '"' + f.custom_caption.substring(0,40) + (f.custom_caption.length > 40 ? '...' : '') + '"';

            const isForum = f.destination_is_forum ? 'Yes' : 'No';
            document.getElementById('bot-info').innerHTML = `
                <div class="info-card"><div class="label">Bot Username</div><div class="value">@${f.bot_name || '?'}</div></div>
                <div class="info-card"><div class="label">Mode</div><div class="value">${f.mode || '?'}</div></div>
                <div class="info-card"><div class="label">Destination</div><div class="value">${f.destination_chat_title || f.destination_group || 'Not set'}</div></div>
                <div class="info-card"><div class="label">Is Forum</div><div class="value">${isForum}</div></div>
                <div class="info-card"><div class="label">Caption Mode</div><div class="value">${capMode}</div></div>
            `;
        }

        function updateCumulative(data) {
            if (!data.forwarder_online) return;
            const c = (data.forwarder || {}).cumulative || {};
            document.getElementById('cum-scrapes').textContent = c.total_scrapes || 0;
            document.getElementById('cum-sent').textContent = c.total_sent || 0;
            document.getElementById('cum-failed').textContent = c.total_failed || 0;
            document.getElementById('cum-skipped').textContent = c.total_skipped || 0;
            document.getElementById('cum-flood').textContent = c.total_flood_waits || 0;
            document.getElementById('cum-saved').textContent = c.total_saved_forwards || 0;
        }

        function updateScrape(data) {
            if (!data.forwarder_online) return;
            const f = data.forwarder || {};
            const panel = document.getElementById('scrape-panel');
            const badge = document.getElementById('scrape-badge');
            const content = document.getElementById('scrape-content');

            // Multi-job payload: f.scrapes is an array of job objects.
            // (Older bots only send the single f.scrape object — wrap it.)
            let jobs = Array.isArray(f.scrapes) ? f.scrapes.slice() : [];
            if (!jobs.length && f.scrape && (f.scrape_running || f.scrape.total_seen > 0)) {
                jobs = [Object.assign({}, f.scrape, {running: !!f.scrape_running})];
            }
            const active = jobs.filter(j => j.running);

            if (active.length > 0) {
                panel.classList.add('active');
                badge.className = 'scrape-badge badge-running';
                badge.textContent = active.length > 1
                    ? 'RUNNING (' + active.length + ')' : 'RUNNING';
                let html = active.map(jobCard).join('');
                if (active.length > 1) {
                    html += '<div class="btn-row"><button class="btn btn-stop" onclick="stopScrape(\'all\')">Stop All Jobs</button></div>';
                }
                content.innerHTML = html;
            } else {
                panel.classList.remove('active');
                badge.className = 'scrape-badge badge-idle';
                badge.textContent = 'IDLE';
                // Show last scrape results if available
                const s = f.scrape || {};
                if (s && s.total_seen > 0) {
                    content.innerHTML = `
                        <div style="color:#8b95a7; font-size:13px; margin-bottom:10px;">Last scrape results${s.job_id ? ' — ' + esc(s.job_id) + (s.kind ? ' (/' + esc(s.kind) + ')' : '') : ''}:</div>
                        <div class="live-stats">
                            <div class="live-stat"><div class="num" style="color:#10b981">${s.sent_count || 0}</div><div class="lbl">Sent</div></div>
                            <div class="live-stat"><div class="num" style="color:#ef4444">${s.failed_count || 0}</div><div class="lbl">Failed</div></div>
                            <div class="live-stat"><div class="num" style="color:#f59e0b">${s.skipped_count || 0}</div><div class="lbl">Skipped</div></div>
                            <div class="live-stat"><div class="num">${s.total_seen || 0}</div><div class="lbl">Seen</div></div>
                            <div class="live-stat"><div class="num">${Math.floor(s.elapsed_sec || 0)}s</div><div class="lbl">Elapsed</div></div>
                        </div>
                        <div style="color:#6b7280; font-size:12px; margin-top:8px;">Source: ${esc(s.source_ref || '?')} → ${esc(s.dest_label || '?')}</div>
                    `;
                } else {
                    content.innerHTML = `
                        <div class="empty-state">
                            <div class="icon">💤</div>
                            <div>No scrape has been run yet</div>
                            <div style="font-size:12px; margin-top:8px; color:#4b5563;">
                                Send /scrape &lt;url&gt; to the bot to start
                            </div>
                        </div>
                    `;
                }
            }
        }

        function esc(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function fmtLeft(left) {
            left = Math.max(0, Math.floor(left || 0));
            if (left >= 3600) return Math.floor(left / 3600) + 'h' + String(Math.floor((left % 3600) / 60)).padStart(2, '0') + 'm';
            if (left >= 60) return Math.floor(left / 60) + 'm' + String(left % 60).padStart(2, '0') + 's';
            return left + 's';
        }

        // One card per scrape job — each job (a /scrape and/or a
        // /scrapeid running concurrently) gets its own activity line,
        // live wait-phase countdown, progress bar, counters, and Stop button.
        function jobCard(s) {
            const jid = esc(s.job_id || '');
            const kind = s.kind === 'scrapeid' ? 'scrapeid' : (s.kind || 'scrape');
            const pct = s.total_seen > 0 ? (s.sent_count / s.total_seen * 100) : 0;
            const elapsed = s.elapsed_sec || 0;
            const speed = elapsed > 0 ? (s.sent_count / (elapsed / 60)).toFixed(1) : 0;
            const inFlight = s.in_flight || 0;

            // Live wait phase (flood wait / recovery break) with a
            // ticking countdown — makes long waits clearly visible
            // instead of looking like a frozen dashboard.
            let phaseHtml = '';
            if (s.phase) {
                const label = s.phase === 'break' ? '☕ Recovery break'
                            : s.phase === 'flood' ? '⏳ Flood wait' : '⏳ ' + esc(s.phase);
                const color = s.phase === 'break' ? '#3b82f6' : '#f59e0b';
                phaseHtml = `<div style="margin-bottom:8px; color:${color}; font-weight:600; font-size:14px;">${label}: ${fmtLeft(s.phase_seconds_left)} remaining</div>`;
            }

            // Liveness: how long since the last real progress update
            let livenessHtml = '';
            if (s.seconds_since_progress != null && s.seconds_since_progress > 30) {
                livenessHtml = `<div style="margin-bottom:8px; color:#6b7280; font-size:12px;">Last progress ${Math.floor(s.seconds_since_progress)}s ago</div>`;
            }

            let activity = 'Scanning...';
            if (s.phase === 'flood') activity = 'Flood wait — auto-resumes';
            else if (s.phase === 'break') activity = 'Recovery break — auto-resumes';
            else if (inFlight > 0) activity = 'Sending ' + inFlight + ' item(s)...';

            return `
                <div class="job-card">
                    <div class="job-card-header">
                        <span class="job-chip job-chip-id">${jid}</span>
                        <span class="job-chip job-chip-${kind}">/${kind}</span>
                        <span class="job-meta">${esc(s.source_ref || '?')} → ${esc(s.dest_label || '?')}</span>
                    </div>
                    <div style="color:#8b95a7; font-size:12.5px; margin-bottom:6px;">
                        <strong>Order:</strong> ${esc(s.order || '?')} &nbsp;|&nbsp;
                        <strong>Filter:</strong> ${esc(s.filter || 'ALL')} &nbsp;|&nbsp;
                        <strong>Parallel:</strong> ${s.parallel || 5}
                    </div>
                    <div style="margin-bottom:8px; color:#10b981; font-weight:600; font-size:14px;">${activity}</div>
                    ${phaseHtml}
                    ${livenessHtml}
                    <div class="progress-bar-container">
                        <div class="progress-bar-fill" style="width:${pct}%"></div>
                        <div class="progress-text">${pct.toFixed(0)}%</div>
                    </div>
                    <div class="live-stats">
                        <div class="live-stat"><div class="num" style="color:#10b981">${s.sent_count || 0}</div><div class="lbl">Sent</div></div>
                        <div class="live-stat"><div class="num" style="color:#3b82f6">${inFlight}</div><div class="lbl">In-flight</div></div>
                        <div class="live-stat"><div class="num" style="color:#ef4444">${s.failed_count || 0}</div><div class="lbl">Failed</div></div>
                        <div class="live-stat"><div class="num" style="color:#f59e0b">${s.skipped_count || 0}</div><div class="lbl">Skipped</div></div>
                        <div class="live-stat"><div class="num">${s.total_seen || 0}</div><div class="lbl">Seen</div></div>
                        <div class="live-stat"><div class="num">${Math.floor(elapsed)}s</div><div class="lbl">Elapsed</div></div>
                        <div class="live-stat"><div class="num">${speed}</div><div class="lbl">Items/min</div></div>
                        <div class="live-stat"><div class="num">${s.last_message_id || 0}</div><div class="lbl">Last ID</div></div>
                    </div>
                    <div class="btn-row">
                        <button class="btn btn-stop" onclick="stopScrape('${jid}')">Stop ${jid}</button>
                    </div>
                </div>
            `;
        }

        async function stopScrape(jobId) {
            try {
                const url = jobId
                    ? '/api/stop_scrape?job=' + encodeURIComponent(jobId)
                    : '/api/stop_scrape';
                await fetch(url, { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function cancelCaption() {
            if (!confirm('Clear the custom caption?')) return;
            try {
                await fetch('/api/cancel_caption', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function resetStats() {
            if (!confirm('Reset ALL cumulative stats to 0? This cannot be undone.')) return;
            try {
                await fetch('/api/reset_stats', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        // Auto-refresh every 3 seconds
        fetchStats();
        setInterval(fetchStats, 3000);
    </script>
</body>
</html>"""


async def dashboard_handler(request: web.Request) -> web.Response:
    """Serve the HTML dashboard."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def main():
    app = web.Application()
    app.router.add_get("/", dashboard_handler)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/health", api_health)
    app.router.add_post("/api/stop_scrape", api_stop_scrape)
    app.router.add_post("/api/cancel_caption", api_cancel_caption)
    app.router.add_post("/api/reset_stats", api_reset_stats)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
    await site.start()
    logger.info("Dashboard listening on 0.0.0.0:%d", DASHBOARD_PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
