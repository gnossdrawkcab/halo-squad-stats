# 🚀 Quick Start Guide - Halo Stats Setup

Get Halo Stats running in **5 minutes**!

---

## What You Need

- 🎮 Xbox gamertag(s) to track
- ☁️ Microsoft account (free)
- 🐳 Docker + Docker Compose (optional)
- 📁 Git installed

---

## 2-Step Setup

Everything Halo-specific (API credentials, Xbox sign-in, players) happens
**inside the app** on its first-run **/setup** page — no `.env` credential
editing, no CLI auth script.

### Step 1: Clone, Configure Basics & Run

```bash
git clone https://github.com/yourusername/halostats.git
cd halostats
cp .env.example .env
# edit .env: set HALO_DB_PASSWORD, HALO_SECRET_KEY, HALO_ADMIN_PASSWORD
```

**Run with Docker:**
```bash
docker compose up --build
```

**Or Local Python:**
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python src/entrypoint.py    # Terminal 1
python src/webapp.py        # Terminal 2
```

### Step 2: Finish Setup In The App

Open http://localhost:8091 — you land on **/setup**, which walks you through:

1. **API credentials (~3 min, one time).** Create your own free Azure app at
   [portal.azure.com](https://portal.azure.com/): **App registrations** →
   **New registration** → name it anything → account type *Personal Microsoft
   accounts* → add a **Mobile and desktop applications** platform with
   redirect URI `http://localhost` → Register. Copy the **Application
   (client) ID**, then create a secret under **Certificates & secrets** and
   copy its **Value**. Paste both into the page.
2. **Authorize with Xbox Live.** Click the authorize link, sign in, and your
   browser ends on a dead `http://localhost/?code=...` page (expected!).
   Paste that full URL back into the page — the app mints `tokens.json`.
3. **Players.** Enter the gamertags to track (one per line). XUIDs are looked
   up automatically (uses the fresh `tokens.json`); if a lookup fails you can
   paste the XUID manually. The scraper picks the roster up on its next cycle.

✨ You're done! Stats should start loading.

(Prefer env config? `HALO_CLIENT_ID`/`HALO_CLIENT_SECRET` and
`HALO_TRACKED_PLAYERS` in `.env` still work as fallbacks when
`api_config.json`/`players.json` don't exist — see the README.)

---

## 📊 What It Shows

- **CSR Rankings** - Real-time rating with history
- **Match Stats** - Kills, deaths, accuracy, etc.
- **Leaderboards** - Rankings across all players
- **Trends** - Performance over time
- **Maps** - Win rates per map
- **Medals** - Achievements tracked
- And more!

---

## 🔍 Finding Player XUIDs

Need to find a player's XUID?

1. Go to [halowaypoint.com](https://www.halowaypoint.com)
2. Search the gamertag
3. Copy XUID from URL (e.g., `halowaypoint.com/stats/profile/2533274800000001`)

---

## ⚙️ Common Changes

### Change Players

Open **/setup** in the app, edit the roster, save — no restart needed.

### Change Port

Edit `.env`:
```bash
HALO_WEB_PORT=8092  # Instead of 8091
```

### Change Update Frequency

Edit `.env`:
```bash
HALO_UPDATE_INTERVAL=30  # Check every 30 seconds (default: 60)
```

---

## 🐛 Troubleshooting

| Problem | Solution |
|---------|----------|
| Port 8091 in use | Change `HALO_WEB_PORT` in `.env` |
| "Invalid credentials" | Re-save the Azure client ID + secret **Value** on /setup |
| "Authorization failed" on /setup | Codes are single-use & short-lived — click the authorize link again and paste the new URL promptly |
| Database error | Ensure `HALO_DB_PASSWORD` is set |
| No players loading | Add players on /setup (or check `HALO_TRACKED_PLAYERS` JSON) |
| Players not updating | Check internet connection and API limits |

---

## 📚 Full Guides

- **[Azure Setup](docs/AZURE_SETUP.md)** - Detailed Azure configuration
- **[Player Tracking](docs/PLAYER_TRACKING.md)** - How to add/change players
- **[Installation](docs/SETUP.md)** - Complete installation guide
- **[README](README.md)** - Full documentation

---

## 🆘 Need Help?

1. Check the docs in the `docs/` folder
2. Look at `.env.example` for all options
3. Check app logs for error messages
4. Open a GitHub issue with details

---

## ✅ You're Ready!

You now have a personal Halo stats tracker for any players you want!

**Next:** Add your players on the **/setup** page and watch the data come in. 🚀

---

**Questions?** Check the documentation or create a GitHub issue!
