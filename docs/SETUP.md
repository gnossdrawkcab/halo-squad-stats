# Installation & Setup Guide

## Requirements

- Python 3.9 or higher
- PostgreSQL 13 or higher
- Docker & Docker Compose (optional, but recommended)
- Halo Infinite API credentials (from Xbox Developer Portal)

## Option 1: Docker Setup (Recommended)

### Step 1: Clone Repository
```bash
git clone https://github.com/yourusername/halostats.git
cd halostats
```

### Step 2: Configure Environment
```bash
cp .env.example .env
```

Edit `.env` and add:
- `HALO_CLIENT_ID` - Your OAuth client ID from Azure App Registration
- `HALO_CLIENT_SECRET` - Your OAuth client secret
- `HALO_DB_PASSWORD` - PostgreSQL password (or generate a secure one)

### Step 3: Start Services
```bash
docker compose up --build
```

Wait for "halostats | Running on..." message.

### Step 4: Access Application
- Web UI: http://localhost:8091
- Adminer (database): http://localhost:8088 (optional)

### Step 5: First Time Setup
The application will:
1. Create database tables automatically
2. Prompt for Microsoft Xbox login on first run
3. Start collecting match data

## Option 2: Local Development Setup

### Step 1: Clone Repository
```bash
git clone https://github.com/yourusername/halostats.git
cd halostats
```

### Step 2: Create Virtual Environment
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 4: Setup PostgreSQL
```bash
# Install PostgreSQL if needed
# macOS: brew install postgresql
# Linux: sudo apt install postgresql postgresql-contrib
# Windows: Download from postgresql.org

# Start PostgreSQL service
# macOS: brew services start postgresql
# Linux: sudo systemctl start postgresql
# Windows: Start from Services

# Create database
createdb halostatsapi

# Optional: Create dedicated user
createuser -P halostats  # Enter password when prompted
```

### Step 5: Configure Environment
```bash
cp .env.example .env
```

Edit `.env`:
```bash
HALO_CLIENT_ID=your_client_id_here
HALO_CLIENT_SECRET=your_client_secret_here
HALO_DB_PASSWORD=your_postgres_password
HALO_DB_USER=postgres  # or your username
HALO_DB_HOST=localhost
HALO_DB_PORT=5432
HALO_DB_NAME=halostatsapi
```

### Step 6: Run Application
```bash
# Terminal 1 - Run data scraper
python src/entrypoint.py

# Terminal 2 - Run web server
python src/webapp.py
```

Application available at: http://localhost:8091

## Getting Halo API Credentials

1. Go to [Xbox Developer Portal](https://developer.xbox.com/)
2. Sign in with Microsoft account
3. Create a new application
4. Under "OAuth 2.0", get:
   - Client ID
   - Client Secret
5. Add to `.env` as `HALO_CLIENT_ID` and `HALO_CLIENT_SECRET`

## First Run Authentication

### Via Docker
```bash
docker compose run --rm halostats python src/auth.py
```

### Locally
```bash
python src/auth.py
```

Follow the prompts:
1. Get authorization URL
2. Visit URL in browser
3. Approve permissions
4. Paste authorization code back to terminal
5. Tokens saved to `tokens.json`

## Troubleshooting

### "Connection refused" - PostgreSQL
```bash
# Check PostgreSQL is running
psql -U postgres -h localhost -d postgres

# If not running:
# Docker: docker compose ps
# Local: sudo systemctl start postgresql  # Linux
#       brew services start postgresql  # macOS
```

### "permission denied" - Auth
- Verify `HALO_CLIENT_ID` and `HALO_CLIENT_SECRET`
- Check they have required OAuth scopes
- Credentials may have expired

### "Port 8091 already in use"
```bash
# Change HALO_WEB_PORT in .env to different port (e.g., 8092)

# Or find/kill process using port 8091:
# Windows: netstat -ano | findstr :8091
# Linux: lsof -i :8091
```

### "Module not found" errors
```bash
# Ensure virtual environment is activated
# Reinstall requirements
pip install --upgrade -r requirements.txt
```

## Configuration Reference

See `.env.example` for all options:

**Database**
- `HALO_DB_HOST` - PostgreSQL host (default: localhost)
- `HALO_DB_PORT` - PostgreSQL port (default: 5432)
- `HALO_DB_NAME` - Database name (default: halostatsapi)
- `HALO_DB_USER` - Database user (default: postgres)
- `HALO_DB_PASSWORD` - Database password

**API**
- `HALO_CLIENT_ID` - Xbox OAuth client ID
- `HALO_CLIENT_SECRET` - Xbox OAuth secret

**Web**
- `HALO_WEB_PORT` - Web server port (default: 8091)
- `HALO_SITE_TITLE` - Website title (default: Halo Stats)
- `HALO_TZ` - Timezone (default: UTC)

**Debug**
- `FLASK_DEBUG` - Flask debug mode (False for production)
- `FLASK_ENV` - Flask environment (production/development)

## Next Steps

1. Start collecting data - first run will fetch recent matches
2. Check the web UI - http://localhost:8091
3. Explore different stat pages
4. Check logs if issues: `logs/` directory

## Getting Help

- Check [docs/](docs/) folder
- Search GitHub Issues
- Open a new Issue with details
- Check GitHub Discussions

Enjoy tracking your Halo stats! 🎮
