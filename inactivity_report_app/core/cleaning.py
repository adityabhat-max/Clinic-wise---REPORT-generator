"""
Cleaning utilities: date parsing, guest-code normalization, dedup.

These exports were produced by a raw data dump (no Excel number formatting),
so date-like columns arrive as a mix of:
  - Excel serial numbers (e.g. 44814.318449074075)
  - dd-mm-yyyy text strings (e.g. "13-04-2026")
  - blank/missing values
`parse_mixed_dates` normalizes all of these to pandas Timestamps.
"""
from __future__ import annotations

import pandas as pd

EXCEL_EPOCH = pd.Timestamp("1899-12-30")
_SERIAL_MIN, _SERIAL_MAX = 20000, 60000  # plausible date-serial range (~1954-2064)

def _parse_one(value) -> pd.Timestamp:
    if pd.isna(value) or value == "":
        return pd.NaT

    if isinstance(value, (int, float)):
        if _SERIAL_MIN <= value <= _SERIAL_MAX:
            return EXCEL_EPOCH + pd.Timedelta(days=float(value))
        return pd.NaT

    text = str(value).strip()
    if text == "":
        return pd.NaT

    # numeric-as-text (Excel serial stored as string)
    try:
        num = float(text)
        if _SERIAL_MIN <= num <= _SERIAL_MAX:
            return EXCEL_EPOCH + pd.Timedelta(days=num)
    except ValueError:
        pass

    parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
    return parsed


def parse_mixed_dates(series: pd.Series) -> pd.Series:
    return series.map(_parse_one)


def normalize_guest_code(series: pd.Series) -> pd.Series:
    """Trim whitespace so ' ISAAC/BLR/050' and 'ISAAC/BLR/050' match."""
    return series.astype(str).str.strip()


def dedupe_by_key(df: pd.DataFrame, key_col: str) -> tuple[pd.DataFrame, int]:
    """Drop exact-duplicate key rows, keeping the first occurrence. Returns (df, n_dropped)."""
    before = len(df)
    deduped = df.drop_duplicates(subset=[key_col], keep="first")
    dropped = before - len(deduped)
    return deduped, dropped