"""
Cleaning utilities: date parsing, guest-code normalization, dedup.

These exports were produced by a raw data dump (no Excel number formatting),
so date-like columns arrive as a mix of:
  - Native Excel date/datetime cells (already a real date -- openpyxl hands
    these to pandas as `datetime`/`Timestamp` objects, not text)
  - Excel serial numbers (e.g. 44814.318449074075)
  - dd-mm-yyyy text strings (e.g. "13-04-2026")
  - blank/missing values
`parse_mixed_dates` normalizes all of these to pandas Timestamps.
"""
from __future__ import annotations

import datetime

import pandas as pd

EXCEL_EPOCH = pd.Timestamp("1899-12-30")
_SERIAL_MIN, _SERIAL_MAX = 20000, 60000  # plausible date-serial range (~1954-2064)

def _parse_one(value) -> pd.Timestamp:
    if pd.isna(value) or value == "":
        return pd.NaT

    # Already a real date -- use it directly. Do NOT fall through to the text
    # branch below: converting a datetime to a string ("2026-07-12 00:00:00")
    # and re-parsing it with dayfirst=True is NOT a safe round-trip -- pandas
    # can misread that ISO-ordered string as day-first (year=2026, "07" as
    # day, "12" as month), silently turning July 12 into December 7. Skipping
    # the text conversion entirely avoids that ambiguity altogether.
    if isinstance(value, datetime.date):
        return pd.Timestamp(value)

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
    """
    Trim whitespace so ' ISAAC/BLR/050' and 'ISAAC/BLR/050' match.

    Blank/missing values are normalized to a true missing value (NaN), not
    the literal text "nan"/"None" that `.astype(str)` would otherwise produce.
    Otherwise every row with a missing guest code would look like the SAME
    guest to the rest of the pipeline (a false match across unrelated rows),
    instead of being correctly treated as unidentifiable.
    """
    cleaned = series.astype(str).str.strip()
    is_blank = series.isna() | cleaned.str.lower().isin(["", "nan", "none"])
    return cleaned.mask(is_blank)


def normalize_text(series: pd.Series) -> pd.Series:
    """
    Aggressively normalize free text (lowercase, strip everything but
    letters/digits) so the same real value survives punctuation/spacing
    differences between two exports of the same field -- e.g. one report's
    Package Name has "+" between items and another's has a plain space in
    the same spot ("2SS PEEL FACE + 2SS LASER" vs "2SS PEEL FACE  2SS LASER").
    """
    return series.astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)


def dedupe_by_key(df: pd.DataFrame, key_col: str) -> tuple[pd.DataFrame, int]:
    """Drop exact-duplicate key rows, keeping the first occurrence. Returns (df, n_dropped)."""
    before = len(df)
    deduped = df.drop_duplicates(subset=[key_col], keep="first")
    dropped = before - len(deduped)
    return deduped, dropped