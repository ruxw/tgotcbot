# OTC Utility Bot

A production-grade, asynchronous Telegram bot engineered specifically for Over-The-Counter (OTC) trading groups and communities.

---

## Features

### 1. Pin Management & Scheduling
- **Manual Reply Pinning**: `/pin`, `/pin 10m`, `/pin 2h`, `/pin 1day`
- **Reply Unpinning**: `/unpin`
- **Cancel Auto-Unpin**: `/cancelpin`
- **Tracked Pins View**: `/pinlist` with pagination

### 2. Automatic Member Pinning (`/apin`)
- Temporary automatic pin mode initiated by OTC administrators.
- Selection of presets (`1 Hour`, `5 Hours`, `10 Hours`, `12 Hours`) or **Custom Duration** (e.g. `30m`, `3h`, `2d`).
- Pins the very next message sent by a non-admin member.
- Starts countdown from the exact time of pinning (restart-safe via SQLite).
- Automatically unpins when the duration elapses.

### 3. Interactive Settings Panel (`/settings`)
Configure per-group settings directly inside Telegram:
- **Service Messages**: Toggle auto-deletion of join, leave, pinned, or general service notifications.
- **Pin Controls**: Enable/disable manual pin commands or automatic pinning.
- **AI Settings**: Enable/disable AI analysis; toggle admin message exclusion.
- **General**: Toggle bot responsiveness.

### 4. Multi-Owner Privileged System
- Support for multiple owner IDs via `BOT_OWNER_IDS`.
- Server-side authorization for all privileged endpoints.
- Auto-configured private command scopes via Telegram `setMyCommands`.

### 5. System-Wide Sponsored Content (`/sponsored`)
- Owner can configure global sponsored text and inline buttons.
- Preserves full formatting: bold, italic, links, blockquotes, and Telegram Premium custom emojis.
- Configurable button styles (`#primary`, `#success`, `#danger`).
- Centralized injection wrapper: attaches automatically to relevant bot messages without duplicate buttons or text.

### 6. Asynchronous Broadcast Engine (`/bd`)
- Broadcast to **All Bot Users** or **All Registered OTC Groups**.
- Full media support: text, photos, videos, documents, and rich captions.
- Step-by-step confirmation wizard with exact preview and target counts.
- Polite rate-limiting (~25 msg/s), FloodWait handling, and live progress updates.
- Detailed recipient tracking and summary report (with downloadable `.txt` log file for large broadcasts).

### 7. Per-OTC AI Insights (`/ai`, `/prompt`)
- Analyzes the latest 100 eligible member messages (excluding admins if configured).
- Clean, structured Telegram HTML output format (Overview, Developments, Member Activity, Concerns, Conclusion).
- Custom prompt configuration directly via chat or `.txt` upload (`/prompt`).
- Dual provider support: Google Gemini (`gemini-flash-latest`) or OpenAI (`gpt-4.1-mini`).

### 8. Telegram Premium Custom Emoji System
- Supports custom Telegram Premium emoji IDs (`<tg-emoji emoji-id="...">`) with graceful unicode fallbacks.
- Strict prevention of duplicate emojis.
- Clean unicode fallback rendering on inline keyboard buttons.

### 9. Cloud Port-Scan Listener ($PORT)
- Built-in lightweight HTTP health server listening on `0.0.0.0:$PORT`.
- Prevents port-scan timeouts on platforms like Render (Free Tier), Railway, and Koyeb.

---

## Commands Reference

| Command | Scope | Description |
|---|---|---|
| `/start` | All / Private | Open private dashboard or register OTC group |
| `/help` | All | Display categorized command guide |
| `/info` | All | Display bot health, active OTCs, and system stats |
| `/pin [duration]` | OTC Admins | Pin replied message (indefinitely or for duration) |
| `/unpin` | OTC Admins | Unpin replied message |
| `/cancelpin` | OTC Admins | Cancel scheduled unpin for replied message |
| `/pinlist` | OTC Admins | Paginated list of tracked pins |
| `/apin` | OTC Admins | Initiate automatic pinning for next member message |
| `/settings` | OTC Admins | Interactive settings dashboard |
| `/ai` | OTC Admins | Generate structured AI discussion insights |
| `/register` | OTC Admins | Explicitly register OTC in private dashboard |
| `/prompt` | Owners | Configure custom AI system prompt |
| `/sponsored` | Owners | Configure system-wide sponsored text & button |
| `/bd` | Owners | Broadcast message to users or OTC groups |
| `/cancel` | Owners / Admins | Cancel active wizard or pending input |

---

## Quick Start & Installation

### Local Setup

1. **Clone repository**:
   ```bash
   git clone https://github.com/ruxw/tgotcbot.git
   cd tgotcbot
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

4. **Run the bot**:
   ```bash
   python otcbot.py
   ```

5. **Run test suite**:
   ```bash
   python -m unittest test_otcbot.py -v
   ```

---

## Cloud Deployment (Render Free Tier)

1. Create a new **Web Service** on Render and connect your repository (`https://github.com/ruxw/tgotcbot`).
2. Set configuration:
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python otcbot.py`
3. Add Environment Variables in Render:
   - `BOT_TOKEN`: Your Telegram Bot API token
   - `BOT_OWNER_IDS`: Numeric Telegram IDs of owners (e.g. `123456789,987654321`)
   - `GEMINI_API_KEY` (or `OPENAI_API_KEY`)
4. Deploy: The bot will bind to Render's allocated `$PORT` automatically and answer all port-scan health checks with `200 OK`.
