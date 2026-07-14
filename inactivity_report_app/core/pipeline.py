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
    # Per-source diagnostics -- which sheet was actually read, and the raw
    # row/unique-guest counts in each file BEFORE any merging, so a mismatch
    # between "what I expected" and "what the app actually loaded" is visible
    # instead of having to be inferred from the final numbers.
    invoicing_sheet_name: str = ""
    invoicing_raw_rows: int = 0
    invoicing_unique_guests: int = 0
    visit_sheet_name: str = ""
    visit_raw_rows: int = 0
    visit_unique_guests: int = 0
    visit_duplicates_dropped: int = 0
    benefit_sheet_name: str = ""
    benefit_raw_rows: int = 0
    benefit_has_guest_code: bool = False


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
    stats.invoicing_sheet_name = invoicing_report.sheet_name
    stats.invoicing_raw_rows = len(base)
    stats.invoicing_unique_guests = int(base["guest_code"].nunique())
    base = base.drop_duplicates(subset=["guest_code", "invoice_no"], keep="first")
    stats.invoice_rows_after_expand = len(base)

    # --- Step 2: attach last visit date -------------------------------
    visit_fields = {k: k for k in ["guest_code", "last_visit_date"]}
    visits = _select_and_rename(visit_report, visit_fields)
    visits["guest_code"] = normalize_guest_code(visits["guest_code"])
    visits["last_visit_date"] = parse_mixed_dates(visits["last_visit_date"])
    stats.visit_sheet_name = visit_report.sheet_name
    stats.visit_raw_rows = len(visits)
    stats.visit_unique_guests = int(visits["guest_code"].nunique())
    visits, dropped = dedupe_by_key(visits, "guest_code")
    stats.visit_duplicates_dropped = dropped

    merged = base.merge(visits, on="guest_code", how="left")
    stats.guests_without_last_visit = int(merged["last_visit_date"].isna().sum())

    # --- Step 3: attach balance sessions -------------------------------
    benefit_fields = {k: k for k in ["guest_code", "invoice_no", "balance_qty", "package_status"]}
    benefits = _select_and_rename(benefit_report, benefit_fields)
    stats.benefit_sheet_name = benefit_report.sheet_name
    stats.benefit_raw_rows = len(benefits)
    has_benefit_guest_code = "guest_code" in benefits.columns
    stats.benefit_has_guest_code = has_benefit_guest_code
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

    # Sanity check: every guest in `final` must have come from a real match in
    # the Visit Report (that's the only source of a non-null Inactive Days),
    # so the final unique-guest count can never exceed the Visit Report's own
    # unique-guest count. If it ever does, something upstream is producing
    # guest codes that don't trace back to a real source row -- surface it
    # loudly rather than silently shipping a wrong report.
    final_unique_guests = int(final["guest_code"].nunique())
    if final_unique_guests > stats.visit_unique_guests:
        stats.notes.append(
            f"DATA INTEGRITY WARNING: the final report contains {final_unique_guests} "
            f"unique guest(s), which is more than the {stats.visit_unique_guests} unique "
            "guest(s) found in the Org/Latest-Visit Report. This should be impossible and "
            "indicates a data or matching problem -- do not trust this report until resolved."
        )

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

    # full_list = every invoiced guest x their packages, unfiltered, carried
    # through with the enrichment columns (visit date, balance, status) attached.
    full_list = _format_display(merged)
    final = _format_display(final)

    return full_list, final, stats
