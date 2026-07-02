# First Run - Authentication Setup

## Required: Create tokens.json

Before you can use Halo Stats, you **must** authenticate once to create `tokens.json`. This file contains your Xbox authentication credentials.

⚠️ **Every user needs their own `tokens.json`** - it contains YOUR personal Xbox authentication tokens.

---

## Step 1: Local Authentication (Required)

### Prerequisites
- Python 3.9+
- Your Xbox account credentials
- 5 minutes

### Run Authentication

```bash
# From the project root
python src/auth.py
```

### Follow the Prompts

1. You'll see a Microsoft login URL
2. Click the link (or copy/paste into browser)
3. Sign in with your Xbox/Microsoft account
4. After login, you'll see a URL with a `code=...` parameter
5. Copy the authorization code
6. Paste it back into the terminal when prompted

### What Gets Created

When authentication succeeds, a `tokens.json` file is created in the root directory:

```bash
halostats/
├── tokens.json          # ← Created here (YOUR secret credentials!)
├── compose.yaml
├── src/
├── ...
```

---

## Step 2: Run with Docker

Once `tokens.json` exists:

```bash
docker compose up --build
```

Docker will automatically mount `tokens.json` and use it.

### Local Python (Alternative)

If running locally without Docker:

```bash
python src/entrypoint.py    # Terminal 1 - Scraper
python src/webapp.py        # Terminal 2 - Web UI
```

---

## Troubleshooting Authentication

### "Can't open file '/app/entrypoint.py'"

You're running in Docker without `tokens.json`. Complete **Step 1** first.

### "EOF when reading a line"

You're trying to authenticate in Docker. You must:
1. Run `python src/auth.py` **locally** (not in Docker)
2. Then mount `tokens.json` to Docker

### "Available keys in xui data: ..."

This is normal debug output. Your XUID is being extracted.

### Tokens expired

Run `python src/auth.py` again to refresh. It will auto-detect your existing tokens and refresh them.

---

## Security Note

⚠️ **`tokens.json` contains your personal Xbox authentication!**

- ✅ Keep it safe and local
- ❌ Never commit to GitHub (it's in `.gitignore`)
- ❌ Never share with anyone
- ❌ Don't upload to public servers

---

## Changing Players to Track

Once `tokens.json` is created, you can change which players to track:

**Option 1: The in-app /setup page (recommended)**
Open the web app and go to **/setup** — enter gamertags, XUIDs are resolved
automatically (this uses your `tokens.json`), and the roster is saved to
`players.json` in the data dir. No restart needed.

**Option 2: Edit `.env`**
```bash
HALO_TRACKED_PLAYERS='[{"gamertag": "YourGamertag", "xuid": "1234567890123456"}]'
```
(Used only when `players.json` doesn't exist.)

If neither is set, the app starts with no players and shows the /setup page.

See [docs/PLAYER_TRACKING.md](PLAYER_TRACKING.md) for details.

---

## Next Steps

1. ✅ Run `python src/auth.py` locally
2. ✅ Create `tokens.json`
3. ✅ Start Docker: `docker compose up`
4. ✅ Open http://localhost:8091

That's it! Stats will start loading. 🎮

---

## Questions?

- Check [QUICK_START.md](../QUICK_START.md) for 5-minute setup
- See [PLAYER_TRACKING.md](PLAYER_TRACKING.md) for tracking options
- Read [AZURE_SETUP.md](AZURE_SETUP.md) if you need new Azure credentials
