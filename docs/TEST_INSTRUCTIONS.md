# Testing Instructions for Halo Stats Web App

This guide covers basic smoke testing for the scraper and Flask UI.

---

## Quick Smoke Test

Run the lightweight webapp smoke script:

```bash
python tests/test_webapp.py
```

This checks imports, route registration, cache initialization, and basic DB connectivity when available.

---

## Full Stack Test With Docker

### 1. Start the full stack:
```bash
docker compose up --build
```

### 2. Access the site:
- Web UI: http://localhost:8091
- Adminer (DB): http://localhost:8088

### 3. Test all pages:
Click through each page and verify:
- [ ] Home (/) - CSR overview, arena stats
- [ ] Lifetime (/lifetime) - Lifetime stats
- [ ] Compare (/compare) - Player comparison
- [ ] Settings (/settings) - Configuration
- [ ] Advanced (/advanced) - Objective stats
- [ ] Medals (/medals) - Medal statistics
- [ ] Highlights (/highlights) - Best games
- [ ] Columns (/columns) - Available data columns
- [ ] Player (/player/PlayerName) - Individual profiles
- [ ] Weapons (/weapons) - Weapon stats
- [ ] Hall (/hall) - Hall of fame/shame
- [ ] Maps (/maps) - Map statistics
- [ ] Trends (/trends) - Trend analysis
- [ ] Insights (/insights) - Advanced insights
- [ ] Suggestions (/suggestions) - Feature requests
- [ ] Leaderboard (/leaderboard) - Top players

### 4. Stop when done:
```bash
docker compose down
```

---

## Local Python Test

### 1. Setup:
```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Set environment variables:
```bash
# Windows PowerShell
$env:HALO_DB_PASSWORD="your_password"
$env:HALO_DB_HOST="localhost"  # or your DB host
$env:FLASK_DEBUG="True"
```

### 3. Run the app:
```bash
python src/webapp.py
```

### 4. Test:
- Open http://localhost:8091
- Click through all pages
- Check browser console for JavaScript errors
- Check terminal for Python errors

### 5. Stop:
Press `Ctrl+C`

---

## Before Deployment Checklist

- [ ] Run `python tests/test_webapp.py` - all tests pass
- [ ] Test with Docker - all pages load without errors
- [ ] Check browser console - no JavaScript errors
- [ ] Test with real data - verify stats are accurate
- [ ] Test all filters (player, playlist, mode)
- [ ] Test export API (CSV & JSON)
- [ ] Verify database indexes are created
- [ ] Check that cache updates work
- [ ] Test settings save/load
- [ ] Verify presence detection works (if used)

---

## Optional Manual Checks

```bash
# Syntax check
python -m py_compile src/webapp.py

# Run the scraper in a second terminal if you want fresh data
python src/entrypoint.py
```
