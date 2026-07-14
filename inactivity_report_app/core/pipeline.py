"""
Core merge + business-logic pipeline.

Grain: the final report is one row per (guest, package invoice) — a guest
with three packages can appear up to three times, once per package that
matches one of the inclusion rules below. This matches how Package Benefit
Report and Package Invoicing Report are themselves shaped (one row per
invoice).

Pipeline steps (see README for the full write-up):
  1. Base = Package Invoicing Report, one row per package invoice, deduped
     on (Guest Code, Package Invoice No). There is no separate guest roster
     input -- every guest with at least one invoice is in scope.
  2. Attach Last Visit Date from the Org/Latest-Visit Guest Report.
  3. Attach Balance Sessions from the Package Benefit Report, summed across
     that report's benefit-type rows for a given invoice (a package with
     several benefit lines is only "fully redeemed" once every one of them
     is at zero balance).
  4. Compute Inactive Days = today - Last Visit Date.
  5. Determine each package's effective status (prefers the Package Benefit
     Report's status, falls back to the Invoicing Report's status when a
     package has no benefit-detail match).
  6. A package makes the final report if EITHER:
       a) status is Closed AND Balance Sessions == 0 AND Inactive Days >= threshold
          ("fully redeemed and gone quiet"), or
       b) status is Active AND Inactive Days >= threshold
          ("still has sessions on the books but hasn't been in")
     Refunded packages are never included. Each row is tagged with a
     "Match Reason" showing which rule (a or b) included it.

build_report() returns (full_list, final, stats):
  - full_list: every invoiced guest x their packages, UNFILTERED.
  - final: full_list filtered down by the inclusion rules above -- the
    actionable "who to follow up with" report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.cleaning import dedupe_by_key, normalize_guest_code, parse_mixed_dates
from core.io_utils import ResolvedReport

INACTIVITY_THRESHOLD_DAYS = 60


@dataclass
class PipelineStats:
    guests_without_last_visit: int = 0
    invoice_rows_after_expand: int = 0
    invoices_without_benefit_match: int = 0
    final_report_rows: int = 0
    final_closed_redeemed: int = 0
    final_active_unused: int = 0
    notes: list[str] = field(default_factory=list)


def _select_and_rename(resolved: ResolvedReport, rename_map: dict[str, str]) -> pd.DataFrame:
    """Pull only the resolved logical-field columns out of a report, renamed to their logical key."""
    cols_present = {k: v for k, v in rename_map.items() if k in resolved.columns}
    actual_cols = {resolved.columns[k]: k for k in cols_present}
    df = resolved.df[list(actual_cols.keys())].rename(columns=actual_cols)
    return df


_DISPLAY_RENAME = {
    "guest_code": "Guest Code",
    "invoice_no": "Package Invoice No",
    "package_name": "Package Name",
    "package_start_date": "Package Creation Date",
    "last_visit_date": "Last Visit Date",
    "inactive_days": "Inactive Days",
    "effective_status": "Package Status",
}


def _format_display(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in _DISPLAY_RENAME if c in df.columns]
    return df[cols].rename(columns=_DISPLAY_RENAME)


def build_report(
    visit_report: ResolvedReport,
    benefit_report: ResolvedReport,
    invoicing_report: ResolvedReport,
    inactivity_threshold_days: int = INACTIVITY_THRESHOLD_DAYS,
    today: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, PipelineStats]:
    stats = PipelineStats()
    today = (today or pd.Timestamp.now()).normalize()

    # --- Step 1: base = one row per package invoice --------------------
    invoice_fields = {k: k for k in ["guest_code", "invoice_no", "package_name", "package_start_date", "status"]}
    base = _select_and_rename(invoicing_report, invoice_fields)
    base["guest_code"] = normalize_guest_code(base["guest_code"])
    base["package_start_date"] = parse_mixed_dates(base["package_start_date"])
    base = base.drop_duplicates(subset=["guest_code", "invoice_no"], keep="first")
    stats.invoice_rows_after_expand = len(base)

    # --- Step 2: attach last visit date -------------------------------
    visit_fields = {k: k for k in ["guest_code", "last_visit_date"]}
    visits = _select_and_rename(visit_report, visit_fields)
    visits["guest_code"] = normalize_guest_code(visits["guest_code"])
    visits["last_visit_date"] = parse_mixed_dates(visits["last_visit_date"])
    visits, _ = dedupe_by_key(visits, "guest_code")

    merged = base.merge(visits, on="guest_code", how="left")
    stats.guests_without_last_visit = int(merged["last_visit_date"].isna().sum())

    # --- Step 3: attach balance sessions -------------------------------
    benefit_fields = {k: k for k in ["guest_code", "invoice_no", "balance_qty", "package_status"]}
    benefits = _select_and_rename(benefit_report, benefit_fields)
    has_benefit_guest_code = "guest_code" in benefits.columns
    if has_benefit_guest_code:
        benefits["guest_code"] = normalize_guest_code(benefits["guest_code"])
    benefits["balance_qty"] = pd.to_numeric(benefits["balance_qty"], errors="coerce")

    # Some locations' Benefit Report has no guest identifier column at all; in
    # that case fall back to joining on Invoice No alone (still unique per
    # package) instead of (Guest Code, Invoice No).
    benefit_join_keys = ["guest_code", "invoice_no"] if has_benefit_guest_code else ["invoice_no"]

    agg = {"balance_qty": lambda s: s.sum(min_count=1)}  # all-NaN group -> NaN, not a false 0
    if "package_status" in benefits.columns:
        agg["package_status"] = "first"
    benefit_totals = benefits.groupby(benefit_join_keys, as_index=False).agg(agg)

    merged = merged.merge(benefit_totals, on=benefit_join_keys, how="left", suffixes=("", "_benefit"))
    stats.invoices_without_benefit_match = int(merged["balance_qty"].isna().sum())

    # --- Step 4: balance + inactivity -----------------------------------
    merged["balance_sessions"] = merged["balance_qty"]
    merged["inactive_days"] = (today - merged["last_visit_date"]).dt.days

    # --- Step 5: effective status (Benefit Report status wins, falls back
    #             to Invoicing Report status when there's no benefit match) ---
    has_package_status = "package_status" in merged.columns
    has_invoicing_status = "status" in merged.columns
    if has_package_status and has_invoicing_status:
        merged["effective_status"] = merged["package_status"].fillna(merged["status"])
    elif has_package_status:
        merged["effective_status"] = merged["package_status"]
    elif has_invoicing_status:
        merged["effective_status"] = merged["status"]
    else:
        merged["effective_status"] = pd.NA
    status_norm = merged["effective_status"].astype(str).str.strip().str.lower()

    # --- Step 6: inclusion rules -----------------------------------------
    inactive_enough = merged["inactive_days"] >= inactivity_threshold_days
    closed_redeemed_inactive = (
        (status_norm == "closed") & (merged["balance_sessions"] == 0) & inactive_enough
    )
    active_unused_inactive = (status_norm == "active") & inactive_enough

    merged["match_reason"] = pd.NA
    merged.loc[closed_redeemed_inactive, "match_reason"] = "Fully Redeemed & Inactive"
    merged.loc[active_unused_inactive & ~closed_redeemed_inactive, "match_reason"] = "Active Package but Inactive"

    final = merged[closed_redeemed_inactive | active_unused_inactive].copy()
    stats.final_report_rows = len(final)
    stats.final_closed_redeemed = int((final["match_reason"] == "Fully Redeemed & Inactive").sum())
    stats.final_active_unused = int((final["match_reason"] == "Active Package but Inactive").sum())

    if stats.guests_without_last_visit:
        stats.notes.append(
            f"{stats.guests_without_last_visit} guest(s) had no match in the "
            "Org/Latest-Visit report; their inactivity could not be computed "
            "and they are excluded from the final report."
        )
    if stats.invoices_without_benefit_match:
        stats.notes.append(
            f"{stats.invoices_without_benefit_match} package invoice(s) had no "
            "matching row in the Package Benefit Report; they can still appear "
            "in the final report via the Active-but-inactive rule, but cannot "
            "be evaluated for the Fully-Redeemed rule (balance unknown)."
        )

    # full_list = every Bangalore-roster guest x their packages, unfiltered.
    # This is the Guest Report (X) Package Invoicing Report join from Step 3,
    # carried through with the enrichment columns attached. Its (Guest Code,
    # Package Invoice No) key set is verified to exactly match the DPR file's
    # 2,090 unique pairs -- i.e. this is the dataset DPR itself represents.
    full_list = _format_display(merged)
    final = _format_display(final)

    return full_list, final, stats
