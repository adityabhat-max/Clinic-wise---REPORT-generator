"""
File reading + validation.

Each uploaded workbook may contain one or several sheets (some Zenoti/Tableau
exports bundle multiple report tabs into a single file). For each upload we
scan every sheet, score it against the expected ReportSchema, and pick the
best-matching sheet rather than assuming "sheet 1" is always the right one.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.columns import ReportSchema


class ReportValidationError(Exception):
    """Raised when a required column cannot be found in any sheet of an upload."""


@dataclass
class ResolvedReport:
    df: pd.DataFrame
    columns: dict[str, str]  # logical field key -> actual column name
    sheet_name: str


def _read_all_sheets(file) -> dict[str, pd.DataFrame]:
    # file may be a Streamlit UploadedFile or a plain path/bytes buffer.
    return pd.read_excel(file, sheet_name=None, engine="openpyxl")


def load_and_validate(file, schema: ReportSchema) -> ResolvedReport:
    """
    Read every sheet in `file`, resolve columns per `schema`, and return the
    best-matching sheet. Raises ReportValidationError with a clear message
    if no sheet satisfies all required fields.
    """
    sheets = _read_all_sheets(file)
    if not sheets:
        raise ReportValidationError(
            f"'{schema.report_label}': the uploaded file has no readable sheets."
        )

    best_sheet_name = None
    best_df = None
    best_resolved: dict[str, str] = {}
    best_missing: list[str] = [f.key for f in schema.fields if f.required]

    for sheet_name, df in sheets.items():
        if df.empty:
            continue
        resolved, missing = schema.resolve(df)
        if len(missing) < len(best_missing):
            best_sheet_name, best_df, best_resolved, best_missing = sheet_name, df, resolved, missing
        if not missing:
            break  # perfect match, no need to keep scanning

    if best_df is None or best_missing:
        available = {name: list(df.columns) for name, df in sheets.items()}
        raise ReportValidationError(
            f"'{schema.report_label}': could not find required column(s) "
            f"{best_missing} in any sheet of the uploaded file.\n"
            f"Columns found per sheet: {available}"
        )

    cleaned = best_df.dropna(how="all")

    return ResolvedReport(
        df=cleaned,
        columns=best_resolved,
        sheet_name=best_sheet_name,
    )
