# Azure Setup Guide for Halo Stats

This guide walks you through setting up Xbox API credentials using Microsoft Azure.

## 📋 Overview

The Halo Stats app uses the Xbox API to fetch player data. To use the API, you need:
1. A Microsoft Developer Account
2. An Azure App Registration
3. API credentials (Client ID & Secret)

Total time: ~10 minutes

---

## Step 1: Create a Microsoft Developer Account

If you don't have one:

1. Visit [Microsoft Developer](https://developer.microsoft.com/)
2. Click **"Sign up"** (top right)
3. Sign in with your Microsoft account (or create one)
4. Complete the registration

---

## Step 2: Access Azure Portal

1. Go to [Azure Portal](https://portal.azure.com/)
2. Sign in with your Microsoft account
3. You should see the Azure dashboard

**Note:** Don't worry if you see "Free trial" - you get credits. The Halo Stats app won't cost anything for personal use.

---

## Step 3: Create App Registration

### Navigate to App Registrations

1. In Azure Portal, search for **"App registrations"** (use search bar at top)
2. Click on **"App registrations"** from results
3. Click **"New registration"** (top left)

### Fill in App Details

Fill in the registration form:

| Field | Value |
|-------|-------|
| **Name** | `Halo Stats` |
| **Supported account types** | `Single tenant` (default) |
| **Redirect URI** | `http://localhost` |

Click **"Register"**

---

## Step 4: Get Your Client ID

After registering:

1. You'll be on your app's **"Overview"** page
2. Look for **"Application (client) ID"**
3. **Copy this value** - you'll need it for `.env`

Add to your `.env` file:
```bash
HALO_CLIENT_ID=your_value_here
```

---

## Step 5: Create Client Secret

1. Click **"Certificates & secrets"** (left menu)
2. Click the **"New client secret"** button
3. Under "Description", type: `Halo Stats Secret`
4. Leave "Expires" as default (24 months)
5. Click **"Add"**

### Copy the Secret Value

⚠️ **IMPORTANT:** Copy the **VALUE**, not the ID!

- Click the copy icon next to the value
- Add to your `.env` file:
```bash
HALO_CLIENT_SECRET=your_secret_value_here
```

**⚠️ Don't share this secret with anyone!**

---

## Step 6: Configure API Permissions

1. Click **"API permissions"** (left menu)
2. Click **"Add a permission"**
3. Search for **"Xbox"** or **"Xbox Services"**
4. Select **"Xbox Services API"**
5. Select delegated permissions needed for your use case:
   - User profile access
   - Match data access
   - etc.
6. Click **"Add permissions"**

---

## Step 7: Verify Your Setup

Check that you have:

✅ `HALO_CLIENT_ID` - 36 character string with dashes (looks like: `12345678-1234-1234-1234-123456789012`)

✅ `HALO_CLIENT_SECRET` - Long random string

Both should be in your `.env` file.

---

## Step 8: Test It Works

### Docker
```bash
docker compose up --build
```

### Local
```bash
python src/entrypoint.py
```

You should see:
```
✅ XUID: 2535417291060944
✅ Token obtained successfully
```

---

## Troubleshooting

### "Invalid client ID or secret"

- Double-check you copied the **Value** (not ID) for the secret
- Ensure there are no extra spaces in your `.env`
- Verify the client ID matches the Overview page

### "Unauthorized" or "Forbidden"

- Check app permissions in Azure
- Ensure you added the Xbox API permissions
- Wait a few minutes - permissions can take time to propagate

### "Connection refused"

- Make sure you're online
- Check your firewall isn't blocking the request
- Verify the Halo API endpoint is reachable

### Can't find Xbox Services API

Try searching for:
- "Xbox"
- "Xbox Services"
- "Halo"

If still not found, contact Xbox support.

---

## Security Tips

🔒 **Protect your credentials:**

1. **Never commit `.env` to GitHub** - Already configured in `.gitignore`
2. **Don't share your secret** - Treat it like a password
3. **Use environment variables** - Don't hardcode in Python files
4. **Rotate secrets regularly** - Create new ones in Azure
5. **Use strong database passwords** - Especially if exposed online

---

## Next Steps

Once configured:

1. Update `.env` with your players to track
2. Start the app
3. Check http://localhost:8091

---

## Additional Resources

- [Azure App Registration Docs](https://docs.microsoft.com/en-us/azure/active-directory/develop/quickstart-register-app)
- [Xbox API Docs](https://docs.microsoft.com/en-us/gaming/xbox-live/api-ref/xbox-live-rest/uri/xboxliveuris)
- [Halo Infinite API](https://github.com/DavidAshton/HaloInfiniteAPI)

---

## Support

If you get stuck:

1. Check the app's README.md
2. Look at existing issues on GitHub
3. Create a new GitHub issue with details

Good luck! 🎮
