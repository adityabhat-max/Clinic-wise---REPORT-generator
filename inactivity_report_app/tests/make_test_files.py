"""
Splits the real 'Banglore package report.xlsx' (5 sheets in one workbook)
into 4 standalone files, mimicking what a real user would upload, so the
pipeline can be exercised against real data end-to-end.
"""
from pathlib import Path

import pandas as pd

SRC = Path(__file__).resolve().parents[2] / "Banglore package report.xlsx"
OUT_DIR = Path(__file__).resolve().parent / "fixtures"
OUT_DIR.mkdir(exist_ok=True)

SHEET_TO_FILE = {
    "Guests": "guest_report.xlsx",
    "Guests (13)": "org_visit_report.xlsx",
    "Package Benefits Detail": "package_benefit_report.xlsx",
    "Sheet 2_Full Data_data (7)": "package_invoicing_report.xlsx",
    "Sheet 2_Full Data_data (DPR)": "dpr_validation_reference.xlsx",
}

sheets = pd.read_excel(SRC, sheet_name=list(SHEET_TO_FILE.keys()), engine="openpyxl")

for sheet_name, out_name in SHEET_TO_FILE.items():
    df = sheets[sheet_name]
    out_path = OUT_DIR / out_name
    df.to_excel(out_path, index=False, engine="openpyxl")
    print(f"Wrote {out_path} ({len(df)} rows)")
