"""Leaf helpers (formatting, numeric coercion, heatmap classes) extracted
from webapp.py. Pure — depends only on pandas + re. webapp does
`from util import *`.
"""
import re
import pandas as pd


def safe_int(value) -> int:
    try:
        if pd.isna(value):
            return 0
        return int(value)
    except Exception:
        return 0


def safe_float(value) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def format_int(value) -> str:
    return f'{safe_int(value):,}' if safe_int(value) else '0'


def format_float(value, digits=2) -> str:
    return f'{safe_float(value):.{digits}f}' if safe_float(value) else '0'


def format_pct(value) -> str:
    pct = safe_float(value)
    if pct <= 1.0:
        pct *= 100
    return f'{pct:.1f}%' if pct else '0%'


def format_optional_int(value) -> str:
    if value is None or pd.isna(value):
        return '-'
    int_val = safe_int(value)
    return '-' if int_val == 0 else f'{int_val:,}'


def format_optional_float(value, digits=2) -> str:
    if value is None or pd.isna(value):
        return '-'
    float_val = safe_float(value)
    return '-' if float_val == 0 else f'{float_val:.{digits}f}'


def format_optional_pct(value) -> str:
    if value is None or pd.isna(value):
        return '-'
    pct_val = safe_float(value)
    if pct_val <= 1.0:
        pct_val *= 100
    return '-' if pct_val == 0 else f'{pct_val:.1f}%'


def normalize_map_name(map_name: str) -> str:
    if map_name is None or pd.isna(map_name):
        return ''
    text = str(map_name).strip()
    if not text:
        return ''
    text = re.sub(r'\s-\sranked(?:\s+arena)?$', '', text, flags=re.IGNORECASE).strip()
    return text


def add_normalized_map_column(df: pd.DataFrame, source_col: str = 'map') -> pd.DataFrame:
    if df.empty or source_col not in df.columns:
        return df
    working = df.copy()
    working['_map_normalized'] = working[source_col].map(normalize_map_name)
    return working


def series_max(df: pd.DataFrame, col: str):
    if col not in df.columns:
        return None
    series = pd.to_numeric(df[col], errors='coerce')
    if series.dropna().empty:
        return None
    return series.max()


def series_min(df: pd.DataFrame, col: str):
    if col not in df.columns:
        return None
    series = pd.to_numeric(df[col], errors='coerce')
    if series.dropna().empty:
        return None
    return series.min()


def numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors='coerce').fillna(0)
    return pd.Series([0] * len(df), index=df.index)


def score_series(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    if 'personal_score' in df.columns:
        return numeric_series(df, 'personal_score')
    if 'score' in df.columns:
        return numeric_series(df, 'score')
    return pd.Series(dtype=float)


def objective_score_series(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    base_score = score_series(df)
    if base_score.empty:
        return pd.Series(dtype=float)
    kills = numeric_series(df, 'kills')
    assists = numeric_series(df, 'assists')
    callouts = numeric_series(df, 'callout_assists')
    return base_score - (kills * 100) - (assists * 50) - (callouts * 10)


def to_number(value) -> float | None:
    """Best-effort conversion of formatted strings to float for heatmaps."""
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


def add_heatmap_classes(rows: list, stat_columns: dict) -> None:
    """Mutates rows in-place adding <col>_heat CSS class fields."""
    if not rows:
        return
    
    for col, higher_better in stat_columns.items():
        values = [to_number(r.get(col)) for r in rows]
        for r in rows:
            r[f'{col}_heat'] = get_heatmap_class(r.get(col), values, higher_better)


def get_heatmap_class(value, values_list, higher_is_better=True):
    """Return CSS class based on value's percentile within values_list."""
    try:
        if not values_list or value is None:
            return ''
        
        val = to_number(value)
        if val is None:
            return ''
        
        vals = [to_number(v) for v in values_list]
        vals = [v for v in vals if v is not None]
        
        if not vals or len(vals) < 2:
            return ''
        
        below_count = sum(1 for v in vals if v < val)
        percentile = below_count / len(vals)
        
        if not higher_is_better:
            percentile = 1 - percentile
        
        if percentile >= 0.8:
            return 'heat-excellent'
        if percentile >= 0.6:
            return 'heat-good'
        if percentile >= 0.4:
            return 'heat-average'
        if percentile >= 0.2:
            return 'heat-below'
        return 'heat-poor'
    except (ValueError, TypeError):
        return ''

