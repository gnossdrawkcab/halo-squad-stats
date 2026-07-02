# Player Tracking Configuration

This guide explains how to set up the players you want to track with Halo Stats.

> **The easy way:** use the in-app **/setup** page — enter gamertags, XUIDs are
> resolved automatically, and the roster is saved to `players.json` in the data
> dir (hot-reloaded by the scraper, no restart). The env var below is a
> fallback used only when `players.json` doesn't exist.

## Quick Start

Edit your `.env` file and update the `HALO_TRACKED_PLAYERS` variable:

```bash
HALO_TRACKED_PLAYERS='[{"gamertag": "YourGamertag", "xuid": "1234567890123456"}]'
```

---

## Understanding XUIDs vs Gamertags

**Gamertag** (Display Name):
- What you see in-game
- Example: `SomeGamertag` or `Player One`
- **Not unique** - multiple people can have similar gamertags

**XUID** (Xbox User ID):
- Unique identifier assigned by Xbox
- Example: `2533274800000001`
- Always 16 digits
- **Required** for accurate tracking

---

## Finding a Player's XUID

### Method 1: Halo Waypoint (Easiest)

1. Go to [halowaypoint.com](https://www.halowaypoint.com)
2. Search for the player's gamertag
3. Look at their profile page URL
4. The XUID is in the URL

Example URL: `https://www.halowaypoint.com/en-US/stats/profile/2533274800000001`
- XUID: `2533274800000001`

### Method 2: TrueAchievements

1. Go to [trueachievements.com](https://www.trueachievements.com)
2. Search for the gamertag
3. Click on their profile
4. XUID may be visible in URL or profile

### Method 3: Halo Tracker

1. Go to [halotracker.com](https://www.halotracker.com)
2. Search the gamertag
3. Profile URL contains the XUID

### Method 4: Waypoint API

If you can't find it online:

```bash
# Using curl (replace with your XUID)
curl "https://service.halowaypoint.com/hi/players/GAMERTAG/matches"
```

---

## Configuration Examples

### Single Player

```bash
HALO_TRACKED_PLAYERS='[{"gamertag": "Player Name", "xuid": "2533274800000001"}]'
```

### Multiple Players

```bash
HALO_TRACKED_PLAYERS='[
  {"gamertag": "PlayerOne", "xuid": "2533274800000001"},
  {"gamertag": "PlayerTwo", "xuid": "2533274800000002"},
  {"gamertag": "PlayerThree", "xuid": "2533274800000003"}
]'
```

### Team (5 Players)

```bash
HALO_TRACKED_PLAYERS='[
  {"gamertag": "Player1", "xuid": "2533274800000001"},
  {"gamertag": "Player2", "xuid": "2533274800000002"},
  {"gamertag": "Player3", "xuid": "2533274800000003"},
  {"gamertag": "Player4", "xuid": "2533274800000004"},
  {"gamertag": "Player5", "xuid": "2533274800000005"}
]'
```

### Many Players (10+)

```bash
HALO_TRACKED_PLAYERS='[
  {"gamertag": "Player1", "xuid": "2533274800000001"},
  {"gamertag": "Player2", "xuid": "2533274800000002"},
  {"gamertag": "Player3", "xuid": "2533274800000003"},
  {"gamertag": "Player4", "xuid": "2533274800000004"},
  {"gamertag": "Player5", "xuid": "2533274800000005"},
  {"gamertag": "Player6", "xuid": "2536000000000001"},
  {"gamertag": "Player7", "xuid": "2536000000000002"},
  {"gamertag": "Player8", "xuid": "2536000000000003"},
  {"gamertag": "Player9", "xuid": "2536000000000004"},
  {"gamertag": "Player10", "xuid": "2536000000000005"}
]'
```

---

## Adding/Changing Players

### To Add a Player

1. Edit your `.env` file
2. Add the player to the `HALO_TRACKED_PLAYERS` array
3. Restart the app:

**Docker:**
```bash
docker compose restart
```

**Local:**
```bash
# Stop the running app (Ctrl+C)
# Restart with: python src/entrypoint.py
```

### To Remove a Player

1. Edit your `.env` file
2. Remove their entry from the array
3. Restart the app

### To Change All Players

Replace the entire `HALO_TRACKED_PLAYERS` value and restart.

---

## Validation

The app will validate your configuration on startup:

✅ **Valid output:**
```
✅ Loaded 3 players from HALO_TRACKED_PLAYERS
   - Player1 (2533274800000001)
   - Player2 (2533274800000002)
   - Player3 (2533274800000003)
```

❌ **Error output:**
```
⚠️ Invalid player format. Each player needs 'gamertag' and 'xuid'. Using defaults.
```

If you see an error:
- Check JSON syntax (quotes, commas, brackets)
- Verify each player has both `gamertag` and `xuid`
- Ensure XUIDs are 16 digits

---

## Troubleshooting

### "Using defaults" Warning

You'll see this if:
- JSON is malformed (missing quotes, brackets, etc.)
- Missing `gamertag` or `xuid` fields

With no valid roster anywhere (no `players.json`, no env var), the app starts
with zero players and the web UI redirects to **/setup**.

**Fix:** Check your `.env` syntax

### JSON Validation

Test your JSON here: [jsonlint.com](https://www.jsonlint.com/)

Paste your `HALO_TRACKED_PLAYERS` value (without the variable name) to validate.

### Special Characters in Gamertag

If gamertag has special characters:

```bash
# This is fine - use the actual gamertag
HALO_TRACKED_PLAYERS='[{"gamertag": "l Some GT l", "xuid": "2533274800000001"}]'
```

Just make sure to escape quotes properly (use single quotes around the whole value).

---

## Data Privacy

✅ **What's tracked:**
- Match history
- Stats (kills, deaths, etc.)
- CSR ranking
- Performance metrics

❌ **What's not tracked:**
- Account credentials
- Personal information
- Location data
- Session IPs

All data is stored locally in your database.

---

## Performance Notes

- **1-5 players:** Fast, ~1 second per update
- **5-20 players:** Normal, ~5 seconds per update
- **20+ players:** Slower, ~15-30 seconds per update

Update frequency is configured with `HALO_UPDATE_INTERVAL` in `.env` (default: 60 seconds).

---

## Getting Help

1. Check `.env.example` for format reference
2. Verify XUID format (16 digits)
3. Test JSON at [jsonlint.com](https://www.jsonlint.com/)
4. Check app logs for error messages
5. Create GitHub issue if stuck

---

Next: [Setup Guide](SETUP.md) | [Azure Credentials](AZURE_SETUP.md)
