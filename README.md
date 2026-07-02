# 🎮 Halo Stats - Competitive Ranking & Performance Analytics

A comprehensive web application for tracking and analyzing competitive Halo Infinite match statistics, CSR progression, and team performance metrics with customizable player tracking.

![Python](https://img.shields.io/badge/Python-3.9+-blue?logo=python)
![Flask](https://img.shields.io/badge/Flask-2.0+-lightgrey?logo=flask)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13+-336791?logo=postgresql)
![Docker](https://img.shields.io/badge/Docker-Supported-2496ED?logo=docker)

## ✨ Features

- **Flexible Player Tracking** - Track any players by their Xbox XUID/gamertag
- **CSR Tracking** - Real-time ranking with historical trends
- **Match Analytics** - Detailed stats per player (KDA, accuracy, win rate)
- **Leaderboards** - Global rankings and player comparison
- **Trend Analysis** - 7/30/90/365 day performance trends
- **Map Statistics** - Win rates per map and game mode
- **Advanced Metrics** - Objective stats, medals, highlights, and more
- **Hall of Fame/Shame** - Notable achievements and records

## 📋 Prerequisites

Before you start, you'll need:

1. **Microsoft account** (free) - To create your own Azure app registration
2. **PostgreSQL 13+** - For data storage (Docker handles this)
3. **Python 3.9+** - For local development
4. **Xbox Gamertag(s)** - The player(s) you want to track

## 🚀 Quick Start (5 Minutes)

Everything — API credentials, Xbox authorization, and player selection — is
done **in the app** on its first-run **/setup** page. No `.env` credential
editing, no CLI auth script.

### Option 1: Docker (Recommended) ✨

**Step 1: Clone and setup**
```bash
git clone https://github.com/yourusername/halostats.git
cd halostats
cp .env.example .env
```
Edit `.env` and set the required basics (`HALO_DB_PASSWORD`,
`HALO_SECRET_KEY`, `HALO_ADMIN_PASSWORD`).

**Step 2: Run with Docker**
```bash
docker compose up --build
```

**Step 3: Finish setup in the app**

Open http://localhost:8091 — with no players configured yet you land on the
**/setup** page, which walks you through three steps:

1. **API credentials** — create a free Azure app registration
   (portal.azure.com → App registrations → New registration → add a
   *Mobile and desktop applications* platform with redirect URI
   `http://localhost` → create a client secret) and paste the client ID +
   secret into the page. Saved to `api_config.json` in the data dir.
2. **Authorize with Xbox Live** — click the authorize link, sign in with your
   Microsoft account, then paste the `http://localhost/?code=...` URL your
   browser lands on back into the page. The app exchanges it and writes
   `tokens.json` for you.
3. **Players** — enter the gamertags you want to track (one per line); XUIDs
   are resolved automatically via the Halo profile API (this uses your fresh
   `tokens.json`). If a lookup fails you can paste the XUID manually. The
   scraper picks up the new roster on its next cycle — no restart needed.

### Option 2: Local Python Setup

```bash
# Clone & setup
git clone https://github.com/yourusername/halostats.git
cd halostats
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: set HALO_DB_PASSWORD / HALO_SECRET_KEY / HALO_ADMIN_PASSWORD

# Run (in two terminals)
python src/entrypoint.py    # Terminal 1 - Scraper
python src/webapp.py        # Terminal 2 - Web UI
```

Then open the web UI and finish the three /setup steps (API credentials →
Xbox authorization → players), same as the Docker flow above.

---

## 🛠️ Setup

Getting from a fresh clone to live stats is a three-step flow, completed
entirely on the app's **/setup** page (you're redirected there until a roster
exists; saving requires the admin login when `HALO_ADMIN_PASSWORD` is set):

1. **API credentials.** Create your own (free) Azure app registration — see
   the Azure section below — and paste its **Application (client) ID** and
   **client secret value** into step 1 of /setup. They're saved to
   `api_config.json` in the data dir. The secret is never displayed again —
   the page just shows a "configured ✓" state.
2. **Authorize with Xbox Live → `tokens.json`.** Step 2 of /setup shows an
   *Authorize with Xbox Live* link. Sign in with your Microsoft account and
   approve; your browser then lands on a dead `http://localhost/?code=...`
   page — that's expected. Paste that full URL (or just the `code=` value)
   back into /setup and the app exchanges it for the whole Halo token chain,
   writing `tokens.json` to the data dir. The scraper and the automatic XUID
   lookup both use it, and the scraper refreshes it automatically from then on.
3. **Players.** Enter gamertags one per line (`SomeGamertag`), or with an
   explicit XUID (`SomeGamertag, 2533274800000001`). Missing XUIDs are
   resolved via the Halo profile API (works once steps 1–2 are done); failures
   drop to a manual-entry form with a hint on where to find XUIDs. The roster
   is saved to `players.json` in the data dir and the scraper re-reads it every
   cycle, so changes apply without a restart.

**Optional/advanced env alternatives:** `HALO_CLIENT_ID`/`HALO_CLIENT_SECRET`
in `.env` work as a fallback when `api_config.json` doesn't exist (the file
takes priority), and `python src/auth.py` still offers a CLI OAuth flow —
useful for headless setups. Likewise `HALO_TRACKED_PLAYERS` is a roster
fallback used only when `players.json` doesn't exist.

### Optional integrations (all env-configured, all off by default)

| Integration | Enables | Env vars |
|-------------|---------|----------|
| **Twitch** | Live-stream embeds on /live, "went live" alerts | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `HALO_TWITCH_CHANNELS` (gamertag→channel JSON map) |
| **ntfy** | Push alerts for rank-ups, streaks, PBs, session recaps | `HALO_NTFY_URL`, `HALO_NTFY_TOPIC` |
| **Ollama** | AI Coach pages (local LLM analysis) | `HALO_OLLAMA_URL`, `HALO_OLLAMA_MODEL` |
| **Web Push** | Browser/PWA notifications (keys auto-generated) | `HALO_VAPID_SUBJECT` (contact mailto) |
| **MultiTwitch** | Synced co-stream "rewatch" links on matches | `MULTITWITCH_URL`, `HALO_MULTITWITCH_API` |

Leave any of these unset and the corresponding feature simply stays hidden.

---

## 🔐 Getting Xbox API Credentials (Azure)

The app talks to the Halo API with credentials from **your own** (free) Azure
app registration. The /setup page repeats these instructions inline:

### Step 1: Create Azure App Registration

1. Visit [Azure Portal](https://portal.azure.com/)
2. Sign in with your Microsoft account
3. Search for **"App registrations"** and click it
4. Click **"New registration"**
5. Enter app name: `Halo Stats`
6. Under *Supported account types* choose **Personal Microsoft accounts**
   (or "any organizational directory + personal Microsoft accounts")
7. Under *Redirect URI* select platform **"Mobile and desktop applications"**
   and enter `http://localhost` (you can also add it afterwards via
   **Authentication → Add a platform**)
8. Click **"Register"**

### Step 2: Get the Client ID

1. On the app's **"Overview"** tab, copy **"Application (client) ID"**

### Step 3: Create a Client Secret

1. Click **"Certificates & secrets"** (left menu)
2. Click **"New client secret"**
3. Add description: `Halo Stats App`
4. Click **"Add"**
5. **Copy the secret VALUE** (not the Secret ID!) — it's only shown once

### Step 4: Paste both into the app

Open the app's **/setup** page and paste the client ID + secret into
**Step 1 — Halo API credentials**, then continue with **Step 2 — Authorize
with Xbox Live**. That's it — no `.env` editing needed.

(Advanced alternative: set `HALO_CLIENT_ID`/`HALO_CLIENT_SECRET` in `.env`
instead; the in-app `api_config.json` takes priority when both exist.)

---

## 👥 Tracking Players

### Add Players to Track

The easy way is the in-app **/setup** page (see Setup above) — it saves to
`players.json` in the data dir and takes effect on the next scrape cycle.

Alternatively, set `HALO_TRACKED_PLAYERS` in `.env` (used only when
`players.json` doesn't exist):

```bash
HALO_TRACKED_PLAYERS='[
  {"gamertag": "Player One", "xuid": "2533274800000001"},
  {"gamertag": "Player Two", "xuid": "2533274800000002"},
  {"gamertag": "Player Three", "xuid": "2533274800000003"}
]'
```

### How to Find a Player's XUID

You can find XUID from several sources:

1. **Halo Waypoint** - Visit [halowaypoint.com](https://www.halowaypoint.com) and search for the player
2. **TrueAchievements** - Search the player on [trueachievements.com](https://www.trueachievements.com)
3. **API Call** - Use the Halo API directly with their gamertag

### Changing Players

Edit the roster on **/setup** — no restart needed. (If you manage players via
`HALO_TRACKED_PLAYERS` instead, update `.env` and `docker compose restart`.)

---

## 📁 Project Structure

```
halostats/
├── compose.yaml                # Docker Compose setup (repo root)
├── src/                        # Application source code
│   ├── webapp.py              # Flask web interface
│   ├── stats.py               # Data scraping & processing
│   ├── entrypoint.py          # Main scraper loop
│   ├── auth.py                # Xbox OAuth / token refresh (CLI + scraper)
│   ├── api_config.py          # Azure credentials store + in-app OAuth helpers
│   ├── players.py             # Shared tracked-player roster (players.json / env)
│   ├── notify.py              # ntfy push alerts (rank/streak/PB/session)
│   ├── push.py                # Web Push (VAPID) notifications
│   ├── twitch_live.py         # Twitch Helix live-status lookups
│   ├── grades.py              # Report-card grading logic
│   ├── util.py                # Leaf helpers shared by the webapp
│   └── halo_paths.py          # Data-dir path helpers
│
├── config/                     # Container build
│   └── Dockerfile             # Container definition
│
├── templates/                  # Web interface (HTML)
│   ├── base.html             # Shared layout/nav
│   ├── index.html            # Dashboard
│   ├── setup.html            # First-run player setup
│   ├── compare.html          # Player comparison
│   ├── trends.html           # Trend analysis
│   └── ...                   # Other pages
│
├── static/                     # Web assets
│   ├── app.js                # JavaScript
│   └── styles.css            # Styling
│
├── docs/                       # Additional documentation
├── tests/                      # Test suite
├── requirements.txt           # Python dependencies
├── .env.example              # Config template (COPY THIS!)
└── README.md                 # This file
```

---

## 🔧 Configuration

### Environment Variables

Key variables in `.env`:

| Variable | Description | Example |
|----------|-------------|---------|
| `HALO_TRACKED_PLAYERS` | Fallback roster when no `players.json` (JSON) | See above |
| `HALO_CLIENT_ID` | Optional fallback when no `api_config.json` (set on /setup) | From Azure |
| `HALO_CLIENT_SECRET` | Optional fallback when no `api_config.json` (set on /setup) | From Azure |
| `HALO_DB_PASSWORD` | Database password | Any secure string |
| `HALO_DB_HOST` | Database host | `localhost` or `halodb` |
| `HALO_WEB_PORT` | Web server port | `8091` |
| `HALO_SITE_TITLE` | Browser title | `Halo Stats` |
| `HALO_TZ` | Timezone | `UTC` |
| `HALO_UPDATE_INTERVAL` | Scraper interval (seconds) | `60` |
| `HALO_MATCH_LIMIT` | Matches to fetch per call | `500` |

Full reference: See `.env.example`

---

## 🌐 Web Pages

Once running, access:

- **Home** - http://localhost:8091 - CSR overview
- **Lifetime** - Player lifetime statistics
- **Compare** - Side-by-side player comparison
- **Leaderboard** - Global rankings
- **Trends** - Historical analysis
- **Maps** - Map-specific statistics
- **Advanced** - Objective mode stats
- **Medals** - Achievement tracking
- **Highlights** - Notable games
- **Hall of Fame** - Records & achievements

---

## 🧪 Testing

```bash
# Run tests
python -m pytest tests/

# Run with coverage
python -m pytest --cov=src tests/
```

---

## 🐛 Troubleshooting

### "Connection refused" - Database

**Docker:**
```bash
docker compose ps
docker compose logs halodb
```

**Local:**
- Ensure PostgreSQL is running
- Check connection settings in `.env`

### "Invalid credentials" - Xbox API

- Re-save the client ID/secret on **/setup** (step 1) — make sure you pasted
  the secret's **Value**, not its Secret ID, and that it hasn't expired
- If you use the `.env` fallback instead, verify `HALO_CLIENT_ID` and
  `HALO_CLIENT_SECRET` (note: `api_config.json` from /setup takes priority)
- Ensure the Azure app registration has a *Mobile and desktop applications*
  redirect URI of `http://localhost`

### "Port 8091 in use"

Change in `.env`:
```bash
HALO_WEB_PORT=8092
```

### "Can't find XUID"

Verify the XUID format (should be 16 digits):
- Wrong: `Player123` (that's a gamertag)
- Right: `2533274800000001` (that's a XUID)

Check [halowaypoint.com](https://www.halowaypoint.com) for the correct XUID.

---

## 📊 Viewing Data

The app stores data in PostgreSQL. To inspect:

**Via Web UI:**
- All stats visible in the web interface

**Via Database:**
```bash
# Docker
docker compose exec halodb psql -U postgres -d halostatsapi

# Local
psql -U postgres -d halostatsapi
```

---

## 📞 Support

- 📖 [Setup Guide](docs/SETUP.md) - Detailed installation
- 🤝 [Contributing Guide](CONTRIBUTING.md) - How to help
- 🐛 Issues - Report bugs on GitHub
- 💬 Discussions - Ask questions

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file

---

## ⚠️ Disclaimer

Unofficial project. Not affiliated with Bungie, Microsoft, or Xbox.
Halo is a trademark of Bungie and/or Microsoft Corporation.

**Use at your own risk** and respect Microsoft's API terms of service.
