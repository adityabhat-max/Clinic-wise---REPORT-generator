"""
CLI smoke test: run the real pipeline against the fixture files generated
by make_test_files.py, print stats, and verify the unfiltered full_list
output's (Guest Code, Package Invoice No) keys EXACTLY match the DPR
file's keys (DPR is the unfiltered "all Bangalore-guest packages" list,
not a business-rule-filtered target -- see README).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from core.columns import (
    GUEST_REPORT_SCHEMA,
    PACKAGE_BENEFIT_SCHEMA,
    PACKAGE_INVOICING_SCHEMA,
    VISIT_REPORT_SCHEMA,
)
from core.io_utils import load_and_validate
from core.pipeline import build_report

FIXTURES = Path(__file__).resolve().parent / "fixtures"

guest_resolved = load_and_validate(FIXTURES / "guest_report.xlsx", GUEST_REPORT_SCHEMA)
visit_resolved = load_and_validate(FIXTURES / "org_visit_report.xlsx", VISIT_REPORT_SCHEMA)
benefit_resolved = load_and_validate(FIXTURES / "package_benefit_report.xlsx", PACKAGE_BENEFIT_SCHEMA)
invoicing_resolved = load_and_validate(FIXTURES / "package_invoicing_report.xlsx", PACKAGE_INVOICING_SCHEMA)

print("Resolved columns:")
print("  Guest Report:      ", guest_resolved.columns, "| sheet:", guest_resolved.sheet_name)
print("  Visit Report:      ", visit_resolved.columns, "| sheet:", visit_resolved.sheet_name)
print("  Benefit Report:    ", benefit_resolved.columns, "| sheet:", benefit_resolved.sheet_name)
print("  Invoicing Report:  ", invoicing_resolved.columns, "| sheet:", invoicing_resolved.sheet_name)

# Use the workbook's own creation date (2026-07-10) as "today" so results are
# reproducible regardless of when this test is actually run.
today = pd.Timestamp("2026-07-10")

full_list, final, stats = build_report(
    guest_resolved, visit_resolved, benefit_resolved, invoicing_resolved, today=today
)

print("\n--- Pipeline stats ---")
print(stats)

print(f"\nFinal report rows: {len(final)}")
print(final.head(10).to_string())

# Sanity checks -------------------------------------------------------
assert stats.guest_report_rows == 5531
assert stats.invoice_rows_after_expand > 0
assert len(final) > 0, "Expected at least some inactive clients"
assert (final["Inactive Days"] >= 60).all()
assert final["Guest Code"].notna().all()

closed_rows = final[final["Match Reason"] == "Fully Redeemed & Inactive"]
active_rows = final[final["Match Reason"] == "Active Package but Inactive"]
assert (closed_rows["Balance Sessions"] == 0).all()
assert (closed_rows["Package Status"].str.lower() == "closed").all()
assert (active_rows["Package Status"].str.lower() == "active").all()
assert len(closed_rows) + len(active_rows) == len(final)
print(f"\nFully Redeemed & Inactive: {len(closed_rows)}  |  Active Package but Inactive: {len(active_rows)}")

# Exact-match check against DPR --------------------------------------
dpr = pd.read_excel(FIXTURES / "dpr_validation_reference.xlsx", engine="openpyxl")
dpr_keys = set(zip(dpr["Guest Code"].astype(str).str.strip(), dpr["Package Invoice No"].astype(str).str.strip()))
full_keys = set(zip(full_list["Guest Code"].astype(str).str.strip(), full_list["Package Invoice No"].astype(str).str.strip()))
final_keys = set(zip(final["Guest Code"].astype(str).str.strip(), final["Package Invoice No"].astype(str).str.strip()))

print(f"\nDPR unique keys: {len(dpr_keys)}")
print(f"Our full_list unique keys: {len(full_keys)}")
assert full_keys == dpr_keys, (
    f"full_list should exactly match DPR's key set. "
    f"In full_list but not DPR: {len(full_keys - dpr_keys)}. "
    f"In DPR but not full_list: {len(dpr_keys - full_keys)}."
)
print("EXACT MATCH: full_list's (Guest Code, Package Invoice No) keys == DPR's keys")

assert final_keys.issubset(dpr_keys), "Filtered inactivity report must remain a subset of DPR"
print(f"Filtered inactivity report ({len(final_keys)} rows) confirmed as a subset of DPR")

print("\nALL CHECKS PASSED")
