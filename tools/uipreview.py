#!/usr/bin/env python3
"""UI preview harness — serve the webapp with SYNTHETIC data, no DB needed.

Monkeypatches the webapp's data layer (webapp.cache / webapp.medal_cache /
webapp.count_cache) with a fake N-player × M-game Ranked Arena dataframe so
every page renders with realistic numbers. Used to eyeball / screenshot the UI
at any roster size (the layout must scale cleanly from 1 to 20 players).

Usage:
    python3 tools/uipreview.py --players 5 --port 5601
    python3 tools/uipreview.py --players 20 --games 400 --port 5601

    # then screenshot with the companion script (one chromium at a time):
    NODE_PATH=/usr/local/lib/node_modules node tools/uipreview_shots.js \
        --base http://127.0.0.1:5601 --out /tmp/shots --tag n5

Notes:
  * Gamertags are generic (Player01..Player20) — keep it that way; the public
    repo's personal-data sweep must stay zero-hit.
  * Writes its throwaway data dir (players.json etc.) under /tmp — nothing in
    the repo is touched.
  * Refresh trigger: if load_dataframe()/_rc_build_arrays() in src/webapp.py
    grow new required columns, add them to build_frame() here.
"""
import argparse
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MAPS = ['Aquarius', 'Live Fire', 'Recharge', 'Streets', 'Solitude', 'Empyrean']
MODES = ['Slayer', 'CTF', 'Oddball', 'Strongholds', 'King of the Hill']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--players', type=int, default=5, help='roster size (1-20)')
    ap.add_argument('--games', type=int, default=240,
                    help='total matches to synthesize (spread over ~10 sessions)')
    ap.add_argument('--port', type=int, default=5601)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--solo-latest', action='store_true',
                    help='make the MOST RECENT synthetic session a solo grind '
                         '(exercises the squad dash cross-link strip to /solo)')
    return ap.parse_args()


def setup_env(n_players: int) -> list:
    """Point the app at a throwaway data dir with a synthetic roster."""
    data_dir = Path(tempfile.mkdtemp(prefix=f'uipreview-n{n_players}-'))
    os.environ['HALO_DATA_DIR'] = str(data_dir)
    os.environ.setdefault('HALO_DB_PASSWORD', 'uipreview')
    os.environ.setdefault('HALO_DB_WAIT_TIMEOUT', '1')
    os.environ.setdefault('HALO_DB_WAIT_INTERVAL', '0.1')
    os.environ.setdefault('HALO_DB_HOST', '127.0.0.1')
    os.environ.setdefault('HALO_DB_PORT', '59999')  # nothing listens — fail fast
    names = [f'Player{i + 1:02d}' for i in range(n_players)]
    import json
    (data_dir / 'players.json').write_text(json.dumps(
        [{'gamertag': gt, 'xuid': str(2533274800000000 + i)}
         for i, gt in enumerate(names)]))
    return names


def build_frame(names: list, n_games: int, seed: int = 7, solo_latest: bool = False):
    """Synthetic ranked-arena history shaped like load_dataframe()'s output.

    Column names mirror what the dashboards read (see load_dataframe /
    _rc_build_arrays / _rc_session_stats in src/webapp.py): outcome, kills,
    deaths, assists, kda, accuracy, damage_dealt/taken, duration,
    personal_score, team_personal_score, average_life_duration, date,
    match_id, player_gamertag, playlist, map, game_type, csr columns,
    shots_fired/hit, callout_assists and the small named medal keep-set.

    Roughly every third session is a SOLO grind (exactly one tracked player in
    the match → ppm == 1) so the Solo Dash (/solo), the Latest Solo Sessions
    table and the squad dash's solo cross-link strip all have data to render.
    """
    import pandas as pd
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # Per-player skill offsets so the cards/tables don't all look identical.
    skill = {gt: rng.uniform(-0.25, 0.35) for gt in names}
    csr = {gt: rng.randint(1050, 1650) for gt in names}

    rows = []
    games_left = n_games
    session_idx = 0
    while games_left > 0:
        # one session every ~3 days, most recent session last night
        session_games = min(games_left, rng.randint(6, 10))
        session_start = now - timedelta(days=3 * session_idx, hours=rng.randint(1, 4))
        # Solo session = one tracked player queueing alone. Newest session
        # (idx 0) stays a squad night unless --solo-latest flips it.
        solo_session = len(names) > 1 and (
            (session_idx == 0 and solo_latest) or (session_idx > 0 and session_idx % 3 == 2))
        roster = [names[session_idx % len(names)]] if solo_session else names
        for g in range(session_games):
            mid = f'm{session_idx:03d}-{g:02d}'
            when = session_start - timedelta(minutes=14 * (session_games - g))
            duration = rng.randint(360, 840)
            team_win = rng.random() < 0.52
            mode = rng.choice(MODES)
            gmap = rng.choice(MAPS)
            team_score_total = 0
            match_rows = []
            for gt in roster:
                sk = skill[gt]
                kills = max(0, round(rng.gauss(14 + 6 * sk, 4)))
                deaths = max(1, round(rng.gauss(13 - 4 * sk, 3.5)))
                assists = max(0, round(rng.gauss(6, 2.5)))
                kda = kills + assists / 3 - deaths
                dmg_dealt = max(400, round(rng.gauss(2500 + 700 * sk, 450)))
                dmg_taken = max(400, round(rng.gauss(2450 - 250 * sk, 400)))
                fired = max(50, round(rng.gauss(220, 40)))
                acc = min(72.0, max(28.0, rng.gauss(45 + 6 * sk, 4)))
                hit = round(fired * acc / 100)
                obj_bonus = rng.randint(0, 900) if mode != 'Slayer' else 0
                pscore = kills * 100 + assists * 50 + obj_bonus
                team_score_total += pscore
                delta = rng.randint(4, 14) * (1 if team_win else -1)
                pre = csr[gt]
                csr[gt] = max(400, csr[gt] + delta)
                match_rows.append({
                    'match_id': mid,
                    'date': when.isoformat(),
                    'match_datetime': when.isoformat(),
                    'player_gamertag': gt,
                    'playlist': 'Ranked Arena',
                    'game_type': mode,
                    'map': gmap,
                    'outcome': 'win' if team_win else 'loss',
                    'kills': kills,
                    'deaths': deaths,
                    'assists': assists,
                    'kda': round(kda, 2),
                    'accuracy': round(acc, 1),
                    'damage_dealt': dmg_dealt,
                    'damage_taken': dmg_taken,
                    'dmg_difference': dmg_dealt - dmg_taken,
                    'duration': duration,
                    'personal_score': pscore,
                    'score': pscore,
                    'average_life_duration': round(max(8.0, rng.gauss(38 + 6 * sk, 7)), 1),
                    'shots_fired': fired,
                    'shots_hit': hit,
                    'callout_assists': rng.randint(0, 8),
                    'pre_match_csr': pre,
                    'post_match_csr': csr[gt],
                    'medal_count': rng.randint(2, 14),
                    'medal_snipe': rng.randint(0, 2),
                    'medal_no_scope': rng.randint(0, 2),
                    'medal_perfect': rng.randint(0, 3),
                    'rounds_won': rng.randint(0, 5),
                    'rounds_lost': rng.randint(0, 5),
                    'max_killing_spree': rng.randint(0, 9),
                    'headshot_kills': round(kills * rng.uniform(0.2, 0.6)),
                    'melee_kills': rng.randint(0, 5),
                    'grenade_kills': rng.randint(0, 4),
                    'power_weapon_kills': rng.randint(0, 6),
                })
            team_dmg = sum(r['damage_dealt'] for r in match_rows)
            team_kills = sum(r['kills'] for r in match_rows)
            team_mmr = round(sum(csr[gt] for gt in roster) / len(roster)) + rng.randint(-40, 40)
            for r in match_rows:
                r['team_personal_score'] = team_score_total
                r['team_damage_dealt'] = team_dmg
                r['team_kills'] = team_kills
                r['team_id'] = 0
                r['team_mmr'] = team_mmr
                r['team_score'] = 50 if team_win else rng.randint(20, 49)
                r['team_rank'] = 1 if team_win else 2
                r['enemy_team_damage_dealt'] = round(team_dmg * rng.uniform(0.85, 1.15))
                r['enemy_team_mmr'] = team_mmr + rng.randint(-60, 60)
            rows.extend(match_rows)
        games_left -= session_games
        session_idx += 1

    df = pd.DataFrame(rows)
    # newest first, like the SQL ORDER BY match_datetime DESC
    return df.iloc[::-1].reset_index(drop=True)


class FakeDataCache:
    def __init__(self, df):
        self.df = df
        self.last_count = len(df)
        self.last_check = 0.0

    def get(self):
        return self.df

    def force_reload(self):
        return self.df


class FakeCountCache:
    def __init__(self, count):
        self.count = count

    def get(self):
        return self.count

    def set(self, count):
        pass


def main():
    args = parse_args()
    n = max(1, min(20, args.players))
    names = setup_env(n)

    sys.path.insert(0, str(ROOT / 'src'))
    import webapp  # noqa: E402  (env must be set before this import)

    frame = webapp.normalize_df(build_frame(names, args.games, args.seed,
                                            solo_latest=args.solo_latest))
    webapp.cache = FakeDataCache(frame)
    webapp.medal_cache = FakeDataCache(frame)
    webapp.count_cache = FakeCountCache(len(frame))
    # response cache keys on count — constant here, so entries stay valid
    print(f'uipreview: {n} players × {frame["match_id"].nunique()} matches '
          f'({len(frame)} rows) → http://127.0.0.1:{args.port}', flush=True)
    webapp.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
