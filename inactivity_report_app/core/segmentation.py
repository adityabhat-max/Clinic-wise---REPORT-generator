"""
Benefit Name -> ccat (service category) lookup.

Built from a static reference table (reference_data/segmentation.xlsx,
columns "Services" / "Treatment Type") that's maintained independently of
any single location's exports -- the same Services -> category mapping
applies everywhere, so it's bundled with the app rather than uploaded.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from core.cleaning import normalize_text

_SEGMENTATION_PATH = Path(__file__).parent / "reference_data" / "segmentation.xlsx"


def _load_service_category_map() -> dict[str, str]:
    df = pd.read_excel(_SEGMENTATION_PATH, engine="openpyxl")
    df["_norm_service"] = normalize_text(df["Services"])
    # A handful of service names repeat with an inconsistent/blank category
    # on one of their rows -- prefer the first non-blank category on record.
    df = df[df["Treatment Type"].notna()]
    df = df.drop_duplicates(subset=["_norm_service"], keep="first")
    return dict(zip(df["_norm_service"], df["Treatment Type"]))


_SERVICE_CATEGORY_MAP = _load_service_category_map()


def lookup_ccat(benefit_names: pd.Series) -> pd.Series:
    """Map a Series of Benefit Name values to their ccat (service category)."""
    return normalize_text(benefit_names).map(_SERVICE_CATEGORY_MAP)
