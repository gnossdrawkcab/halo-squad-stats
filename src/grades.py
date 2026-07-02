"""Grading engine for the Halo tracker — extracted from webapp.py (Phase 4 refactor).

Self-contained: depends only on pandas plus private copies of the few primitive
helpers it needs, so webapp.py can `from grades import *` without any circular
import. Public API is pinned in __all__; the leaf helpers in webapp.py are left
untouched (they keep their own copies).
"""
import pandas as pd

__all__ = [
    'grade_from_percentile', 'grade_class', 'compute_match_grade',
    'build_player_report_card', 'add_composite_grades',
    'add_weighted_composite_grades', 'build_trend_grade_rows',
    'REPORT_CARD_GRADE_WEIGHTS',
]


# --- Private primitive helpers (copies of webapp leaf helpers) --------------
def _safe_float(value) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _format_float(value, digits=2) -> str:
    return f'{_safe_float(value):.{digits}f}' if _safe_float(value) else '0'


def _to_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text == '-':
        return None
    try:
        text = text.replace(',', '')
        if text.endswith('%'):
            text = text[:-1]
        return float(text)
    except Exception:
        return None


def _numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors='coerce').fillna(0)
    return pd.Series([0] * len(df), index=df.index)


# --- Grading engine --------------------------------------------------------
def grade_from_percentile(percentile: float) -> str:
    if percentile <= 1:
        percentile *= 100
    if percentile >= 90:
        return 'S'
    if percentile >= 83:
        return 'A+'
    if percentile >= 75:
        return 'A'
    if percentile >= 67:
        return 'A-'
    if percentile >= 58:
        return 'B+'
    if percentile >= 50:
        return 'B'
    if percentile >= 42:
        return 'B-'
    if percentile >= 33:
        return 'C+'
    if percentile >= 25:
        return 'C'
    if percentile >= 17:
        return 'C-'
    if percentile >= 10:
        return 'D+'
    if percentile >= 6:
        return 'D'
    if percentile >= 3:
        return 'D-'
    return 'F'


def grade_class(grade: str) -> str:
    return {
        'S': 'grade-s',
        'A': 'grade-a',
        'B': 'grade-b',
        'C': 'grade-c',
        'D': 'grade-d',
        'F': 'grade-f',
    }.get(str(grade or '').rstrip('+-'), '')


def _lerp_anchors(x, anchors):
    """Piecewise-linear map of x through sorted (input, output) anchors, clamped
    to the endpoints. Used to score a raw stat onto an absolute 0-100 scale."""
    if x is None:
        return None
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    lo = anchors[0]
    for hi in anchors[1:]:
        if x <= hi[0]:
            span = hi[0] - lo[0]
            frac = (x - lo[0]) / span if span else 0.0
            return float(lo[1] + frac * (hi[1] - lo[1]))
        lo = hi
    return float(anchors[-1][1])


# Absolute scoring anchors calibrated for ranked Halo Infinite 4v4. The goal is
# that a roughly average ranked game lands around C/C+, a strong game around
# B+/A-, a dominant game around A/S, and a rough game around D/F — independent of
# the rest of the squad (unlike the relative percentile grades below).
# NOTE: this app's KDA is *additive* (kills + assists/3 - deaths), not a ratio,
# so ~0 is an even game, positive is good. Anchors reflect that scale.
_GRADE_KDA_ANCHORS = [(-8, 0), (-4, 12), (-1, 28), (0, 38), (2, 52), (4, 64),
                      (6, 76), (9, 88), (13, 100)]
_GRADE_ACC_ANCHORS = [(25, 0), (35, 18), (42, 38), (48, 56), (54, 74),
                      (60, 90), (68, 100)]
_GRADE_DMGRATIO_ANCHORS = [(0.4, 0), (0.7, 20), (1.0, 42), (1.3, 62), (1.7, 80),
                           (2.2, 93), (3.0, 100)]


def compute_match_grade(kda=None, accuracy=None, dmg_dealt=None, dmg_taken=None,
                        outcome=None) -> dict | None:
    """Absolute single-game performance grade, independent of the squad/lobby.

    Blends KDA (dominant), shot accuracy, and damage dealt/taken ratio onto a
    0-100 scale, with a small nudge for the match result. Whichever inputs are
    available are weighted and renormalised, so it works both on the rich player
    history rows and on the leaner scoreboard rows (no damage-taken)."""
    parts = []
    k = _to_number(kda)
    if k is not None:
        parts.append((_lerp_anchors(k, _GRADE_KDA_ANCHORS), 0.60))
    acc = _to_number(accuracy)
    if acc is not None:
        if acc <= 1.0:
            acc *= 100
        parts.append((_lerp_anchors(acc, _GRADE_ACC_ANCHORS), 0.22))
    dealt = _to_number(dmg_dealt)
    taken = _to_number(dmg_taken)
    if dealt is not None and taken:
        parts.append((_lerp_anchors(dealt / taken, _GRADE_DMGRATIO_ANCHORS), 0.18))
    if not parts:
        return None
    total_weight = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / total_weight if total_weight else 50.0
    oc = str(outcome or '').strip().lower()
    if oc == 'win' or oc.startswith('win'):
        score = min(100.0, score + 3)
    elif oc in ('loss', 'lose', 'defeat', 'lost'):
        score = max(0.0, score - 3)
    grade = grade_from_percentile(score)
    return {
        'grade': grade,
        'grade_class': grade_class(grade),
        'grade_score': round(score),
        'grade_tip': (f'Game grade {grade} ({round(score)}/100) — absolute '
                      'single-game rating from KDA, accuracy & damage'),
    }


# Absolute anchors for the per-player report card categories.
_GRADE_LIFE_ANCHORS = [(12, 8), (20, 28), (30, 50), (42, 70), (55, 86), (75, 100)]
# Calibrated to the ACTUAL per-game medal_count distribution for this squad
# (distinct medals/game: p10=2, median=6, p90=9, max≈15) — the old anchors topped
# out at 30, so even a 15-medal game scored ~B- and the median player got a D.
_GRADE_MEDALS_ANCHORS = [(0, 0), (2, 20), (4, 38), (6, 54), (8, 70), (10, 84), (13, 96), (15, 100)]


def _abs_grade(score: float) -> dict:
    """Map an absolute 0-100 score to a letter grade dict."""
    g = grade_from_percentile(score)
    return {'grade': g, 'grade_class': grade_class(g), 'score': round(score)}


def build_player_report_card(ranked_df: pd.DataFrame) -> dict | None:
    """Absolute skills report card for one player from their ranked games.

    Unlike the squad-relative percentile grades, each category is graded on a
    fixed Halo scale, so a player's card reflects their own skill, not just how
    they stack up against the tracked roster."""
    if ranked_df is None or ranked_df.empty:
        return None
    games = len(ranked_df)
    if games < 3:
        return None

    def col_mean(col):
        s = _numeric_series(ranked_df, col)
        return float(s.mean()) if len(s) else 0.0

    kda = col_mean('kda')
    acc = col_mean('accuracy')
    if acc <= 1.0:
        acc *= 100
    dealt = float(_numeric_series(ranked_df, 'damage_dealt').sum())
    taken = float(_numeric_series(ranked_df, 'damage_taken').sum())
    ratio = (dealt / taken) if taken else None
    life = col_mean('average_life_duration')
    medals = col_mean('medal_count')

    cats = []
    cats.append({'label': 'Slaying', 'detail': f'{kda:+.1f} KDA', 'weight': 0.35,
                 **_abs_grade(_lerp_anchors(kda, _GRADE_KDA_ANCHORS))})
    cats.append({'label': 'Gunplay', 'detail': f'{acc:.0f}% acc', 'weight': 0.20,
                 **_abs_grade(_lerp_anchors(acc, _GRADE_ACC_ANCHORS))})
    if ratio is not None:
        cats.append({'label': 'Impact', 'detail': f'{ratio:.2f}x dmg', 'weight': 0.25,
                     **_abs_grade(_lerp_anchors(ratio, _GRADE_DMGRATIO_ANCHORS))})
    cats.append({'label': 'Survival', 'detail': f'{life:.0f}s life', 'weight': 0.10,
                 **_abs_grade(_lerp_anchors(life, _GRADE_LIFE_ANCHORS))})
    cats.append({'label': 'Medals', 'detail': f'{medals:.1f}/game', 'weight': 0.10,
                 **_abs_grade(_lerp_anchors(medals, _GRADE_MEDALS_ANCHORS))})

    total_weight = sum(c['weight'] for c in cats)
    overall_score = (sum(c['score'] * c['weight'] for c in cats) / total_weight
                     if total_weight else 50.0)
    return {
        'overall': _abs_grade(overall_score),
        'categories': cats,
        'games': games,
    }


def add_composite_grades(rows: list, stat_columns: dict, label: str = 'Grade') -> None:
    """Mutates rows in-place adding grade, grade_class, and grade_tip."""
    if not rows or not stat_columns:
        return
    numeric_by_col = {}
    for col in stat_columns:
        vals = [_to_number(r.get(col)) for r in rows]
        numeric_by_col[col] = [v for v in vals if v is not None]

    for row in rows:
        scores = []
        for col, higher_better in stat_columns.items():
            val = _to_number(row.get(col))
            vals = numeric_by_col.get(col) or []
            if val is None or len(vals) < 2:
                continue
            below_count = sum(1 for v in vals if v < val)
            equal_count = sum(1 for v in vals if v == val)
            pct = (below_count + 0.5 * equal_count) / len(vals) * 100
            if not higher_better:
                pct = 100 - pct
            scores.append(pct)
        avg = sum(scores) / len(scores) if scores else 50
        grade = grade_from_percentile(avg)
        row['grade'] = grade
        row['grade_class'] = grade_class(grade)
        row['grade_tip'] = f"{label}: {avg:.0f}th percentile in this section (S=top 10%, A=top 25%, B=top 50%)"


def add_weighted_composite_grades(rows: list, weighted_columns: dict, label: str = 'Grade') -> None:
    """Mutates rows in-place adding report-card-style weighted grades."""
    if not rows or not weighted_columns:
        return
    numeric_by_col = {}
    for col in weighted_columns:
        vals = [_to_number(row.get(col)) for row in rows]
        numeric_by_col[col] = [v for v in vals if v is not None]
    for row in rows:
        total_weight = 0.0
        weighted_score = 0.0
        for col, config in weighted_columns.items():
            higher_better = bool(config.get('higher_better', True))
            weight = float(config.get('weight', 1.0))
            val = _to_number(row.get(col))
            vals = numeric_by_col.get(col) or []
            if val is None or len(vals) < 2 or weight <= 0:
                continue
            below_count = sum(1 for v in vals if v < val)
            equal_count = sum(1 for v in vals if v == val)
            pct = (below_count + 0.5 * equal_count) / len(vals) * 100
            if not higher_better:
                pct = 100 - pct
            weighted_score += pct * weight
            total_weight += weight
        avg = weighted_score / total_weight if total_weight else 50
        grade = grade_from_percentile(avg)
        row['grade'] = grade
        row['grade_class'] = grade_class(grade)
        row['grade_tip'] = f"{label}: weighted {avg:.0f}th percentile (KDA 30%, Dmg± 27%, Life 16%, Obj 11%, Sc% 9%, Dmg/m 7%)"


REPORT_CARD_GRADE_WEIGHTS = {
    'kda': {'higher_better': True, 'weight': 0.30},
    'dmg_diff': {'higher_better': True, 'weight': 0.27},
    'avg_life': {'higher_better': True, 'weight': 0.16},
    'obj_score': {'higher_better': True, 'weight': 0.11},
    'score_pct': {'higher_better': True, 'weight': 0.09},
    'dmg_per_min': {'higher_better': True, 'weight': 0.07},
}


def build_trend_grade_rows(
    trends: dict,
    value_key: str,
    label: str,
    higher_better: bool = True,
    digits: int = 1,
    suffix: str = '',
    limit: int = 5
) -> list[dict]:
    rows = []
    for player, series in (trends or {}).items():
        if not series:
            continue
        ordered = sorted(series, key=lambda point: str(point.get('date', '')))
        values = [_safe_float(point.get(value_key)) for point in ordered if point.get(value_key) is not None]
        if not values:
            continue
        current = values[-1]
        rows.append({
            'player': player,
            'grade_value': _format_float(current, digits),
            'value': f"{_format_float(current, digits)}{suffix}"
        })

    add_composite_grades(rows, {'grade_value': higher_better}, f'{label} chart grade')
    rows.sort(key=lambda row: _to_number(row.get('grade_value')) or 0, reverse=higher_better)
    return rows[:limit]
