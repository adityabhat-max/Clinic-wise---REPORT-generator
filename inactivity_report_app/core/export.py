"""Excel export helper."""
from __future__ import annotations

import io

import pandas as pd


def dataframe_to_excel_bytes(
    df: pd.DataFrame,
    sheet_name: str = "Inactivity Report",
    summary: dict[str, object] | None = None,
    guest_summary: pd.DataFrame | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        if summary or guest_summary is not None:
            next_row = 0
            if summary:
                summary_df = pd.DataFrame(list(summary.items()), columns=["Metric", "Value"])
                summary_df.to_excel(writer, index=False, sheet_name="Summary", startrow=next_row)
                next_row += len(summary_df) + 2  # header row + blank-row gap
            if guest_summary is not None:
                guest_summary.to_excel(writer, index=False, sheet_name="Summary", startrow=next_row)
    return buffer.getvalue()
