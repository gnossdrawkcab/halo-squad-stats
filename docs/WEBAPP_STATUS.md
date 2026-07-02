# Web App Notes

This document is intentionally lightweight. It describes how to run and sanity-check the Flask UI without making stale claims about which pages are "complete" or "stubbed".

## Run Locally

```bash
python src/webapp.py
```

Default URL:

```text
http://localhost:8091
```

## Run With Docker

```bash
docker compose up --build
```

Useful commands:

```bash
docker compose logs halo-web
docker compose logs halostats
docker compose down
```

## Practical Smoke Check

Verify these pages load against your current database contents:

- `/`
- `/compare`
- `/lifetime`
- `/leaderboard`
- `/trends`
- `/player/<player_name>`
- `/settings`
- `/suggestions`
- `/columns`

Expected behavior depends on available data. Empty tables are acceptable when the underlying stats are absent; crashes and template errors are not.

## Troubleshooting

- If the UI loads but tables are empty, confirm the scraper has written rows to `halo_match_stats`.
- If player dropdowns are empty, confirm `player_gamertag` values exist in the database.
- If the page is slow, check scraper/web logs and verify the database is reachable.
- If assets look stale, hard refresh the browser so the static version changes are picked up.

## Related Files

- `src/webapp.py`: Flask routes, caching, and page builders
- `templates/`: Jinja templates
- `static/app.js`: client-side sorting and interactions
- `static/styles.css`: UI styling
