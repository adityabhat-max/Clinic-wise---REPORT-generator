"""
Core merge + business-logic pipeline.

Grain: the inclusion rules (step 7) are evaluated one row per (guest,
package invoice). The displayed output is then expanded further, to one
row per (guest, package invoice, benefit line) -- see step 8 -- so a
package invoice with two benefit lines (e.g. a facial + an injectable on
the same invoice) appears as two rows, each with its own Benefit Name,
ccat category, and that benefit's own Balance Quantity. Which packages
are included is unaffected by this -- that's still decided at the invoice
level, using the summed Balance Sessions across all of an invoice's
benefit lines (step 4).

Pipeline steps (see README for the full write-up):
  1. Load the Org/Latest-Visit Guest Report and take its set of Guest Codes.
     This report is the one reliably scoped to a single location at export
     time (Zenoti exports it pre-filtered by center) -- the Invoicing and
     Benefit Reports are NOT guaranteed to be, and can come back as
     org-wide dumps covering every location. That guest-code set is used
     as an implicit location filter in step 2.
  2. Base = Package Invoicing Report, first stripped of any fully duplicate
     rows (identical across every column -- the same as selecting the
     whole sheet and running Excel's "Remove Duplicates"), then restricted
     to only guest codes that are also present in the Visit Report (see
     step 1), deduped on (Guest Code, Package Invoice No). Rows for guests
     outside that scope (i.e. a different location) are dropped and counted.
  3. Attach Last Visit Date from the Visit Report loaded in step 1.
  4. Attach Balance Sessions from the Package Benefit Report, summed across
     that report's benefit-type rows for a given invoice (a package with
     several benefit lines is only "fully redeemed" once every one of them
     is at zero balance).
  5. Compute Inactive Days = today - Last Visit Date. Also compute
     Customer Activity ("Inactive" if Inactive Days >= threshold, else
     "Active") -- a plain comparison against the threshold, independent
     of package status.
  6. Determine each package's effective status (prefers the Package Benefit
     Report's status, falls back to the Invoicing Report's status when a
     package has no benefit-detail match). A blank status (neither report
     had one) is treated as Closed.
  7. A package makes the final report if EITHER:
       a) status is Closed AND Balance Sessions == 0 AND Inactive Days >= threshold
          ("fully redeemed and gone quiet"), or
       b) status is Active AND Inactive Days >= threshold
          ("still has sessions on the books but hasn't been in")
     Refunded packages are never included. Each row is tagged with a
     "Match Reason" showing which rule (a or b) included it.
  8. Expand both full_list and final to one row per benefit line (see
     Grain, above): each package invoice's individual Package Benefit
     Report lines are joined back in, each carrying its own Benefit Name,
     ccat category (looked up from the bundled Segmentation reference
     table -- see core/segmentation.py), and that line's own Balance
     Quantity. An invoice with no benefit-line match at all still appears
     once, with these three columns blank.

build_report() returns (full_list, final, stats):
  - full_list: every invoiced guest x their packages, scoped to the Visit
    Report's location (see step 1-2), unfiltered by the inactivity rules.
  - final: full_list filtered down by the inclusion rules above -- the
    actionable "who to follow up with" report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.cleaning import dedupe_by_key, normalize_guest_code, normalize_text, parse_mixed_dates
from core.io_utils import ResolvedReport
from core.segmentation import lookup_ccat

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
    invoicing_exact_duplicate_rows_dropped: int = 0
    invoicing_raw_rows: int = 0
    invoicing_unique_guests: int = 0
    invoicing_unique_guests_before_scope: int = 0
    invoicing_rows_outside_visit_scope: int = 0
    invoicing_blank_guest_code_rows: int = 0
    visit_sheet_name: str = ""
    visit_raw_rows: int = 0
    visit_unique_guests: int = 0
    visit_duplicates_dropped: int = 0
    visit_blank_guest_code_rows: int = 0
    benefit_sheet_name: str = ""
    benefit_raw_rows: int = 0
    benefit_has_guest_code: bool = False
    benefit_blank_guest_code_rows: int = 0
    invoicing_colliding_invoice_numbers: int = 0
    benefit_ambiguous_unresolved_rows: int = 0
    future_last_visit_rows: int = 0
    last_visit_filled_from_creation_date: int = 0
    guests_without_last_visit_sample: list[str] = field(default_factory=list)


def _ccat_summary_by_guest(df: pd.DataFrame, guest_col: str = "guest_code", ccat_col: str = "ccat") -> pd.Series:
    """
    For each guest code in df, every distinct ccat category across their
    rows, comma-separated. Vectorized: dedupes (guest, ccat) pairs first so
    the per-group string-join only runs over a handful of rows per guest
    instead of every source row -- matters once a report reaches tens of
    thousands of rows (an org-wide upload).
    """
    pairs = df[[guest_col, ccat_col]].dropna(subset=[ccat_col]).drop_duplicates().sort_values([guest_col, ccat_col])
    return pairs.groupby(guest_col)[ccat_col].agg(", ".join)


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
    "customer_activity": "Customer Activity",
    "benefit_name": "Benefit Name",
    "ccat": "Individual Category",
    "guest_summary": "Summary",
    "benefit_balance_qty": "Balance Quantity",
}


_DATE_DISPLAY_COLUMNS = ["Package Creation Date", "Last Visit Date"]


def _format_display(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in _DISPLAY_RENAME if c in df.columns]
    out = df[cols].rename(columns=_DISPLAY_RENAME)
    # Render as "08-May-2024" instead of pandas' default YYYY-MM-DD -- the
    # month name makes the date unambiguous regardless of whether the reader
    # expects DD-MM-YYYY or MM-DD-YYYY, which ISO order gets misread as.
    for col in _DATE_DISPLAY_COLUMNS:
        if col in out.columns:
            out[col] = out[col].dt.strftime("%d-%b-%Y")
    return out


def build_report(
    visit_report: ResolvedReport,
    benefit_report: ResolvedReport,
    invoicing_report: ResolvedReport,
    inactivity_threshold_days: int = INACTIVITY_THRESHOLD_DAYS,
    today: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, PipelineStats]:
    stats = PipelineStats()
    today = (today or pd.Timestamp.now()).normalize()

    # --- Step 1: load the Visit Report and take its guest-code set -----
    # This report is the one reliably scoped to a single location at export
    # time. Its guest-code set is used below as an implicit location filter
    # on the Invoicing Report, since that report (and the Benefit Report)
    # can come back as an org-wide export covering every location instead
    # of just the one being reported on.
    visit_fields = {k: k for k in ["guest_code", "last_visit_date"]}
    visits = _select_and_rename(visit_report, visit_fields)
    visits["guest_code"] = normalize_guest_code(visits["guest_code"])
    visits["last_visit_date"] = parse_mixed_dates(visits["last_visit_date"])
    stats.visit_sheet_name = visit_report.sheet_name
    stats.visit_raw_rows = len(visits)
    stats.visit_blank_guest_code_rows = int(visits["guest_code"].isna().sum())
    visits = visits[visits["guest_code"].notna()].copy()
    stats.visit_unique_guests = int(visits["guest_code"].nunique())
    # When a guest code appears more than once, keep the row with the most
    # recent VALID Last Visit Date rather than whichever row happens to come
    # first in the file -- "first" is arbitrary and can pick a stale or wrong
    # date over a more recent, correct one for the same guest. A future date
    # is a data error, not "more recent" in any useful sense, so it must not
    # be preferred over a real, valid past date just because it sorts later
    # -- rank valid (non-future) dates first, then by recency within those.
    is_future = visits["last_visit_date"] > today
    visits["_sort_key"] = visits["last_visit_date"].where(~is_future)
    visits = visits.sort_values("_sort_key", ascending=False, na_position="last").drop(columns="_sort_key")
    visits, dropped = dedupe_by_key(visits, "guest_code")
    stats.visit_duplicates_dropped = dropped

    visit_guest_codes = set(visits["guest_code"])

    # --- Step 2: base = one row per package invoice, scoped to the Visit
    #             Report's guests ---------------------------------------
    # Package Invoicing Report exports can contain fully duplicate rows
    # (identical across every column) -- an export artifact, not two
    # distinct invoices. Strip those first, the same as selecting the whole
    # sheet and running Excel's "Remove Duplicates", before anything else.
    invoicing_rows_before_exact_dedupe = len(invoicing_report.df)
    invoicing_df_deduped = invoicing_report.df.drop_duplicates(keep="first")
    stats.invoicing_exact_duplicate_rows_dropped = invoicing_rows_before_exact_dedupe - len(invoicing_df_deduped)
    invoicing_report = ResolvedReport(
        df=invoicing_df_deduped,
        columns=invoicing_report.columns,
        sheet_name=invoicing_report.sheet_name,
    )

    invoice_fields = {k: k for k in ["guest_code", "invoice_no", "package_name", "package_start_date", "status"]}
    base = _select_and_rename(invoicing_report, invoice_fields)
    base["guest_code"] = normalize_guest_code(base["guest_code"])
    base["package_start_date"] = parse_mixed_dates(base["package_start_date"])
    stats.invoicing_sheet_name = invoicing_report.sheet_name
    stats.invoicing_raw_rows = len(base)
    stats.invoicing_blank_guest_code_rows = int(base["guest_code"].isna().sum())
    # Rows with no Guest Code at all can't be attributed to anyone -- drop
    # them rather than letting them silently collapse into one fake shared
    # "guest" (which is what happened before: every blank code normalized to
    # the literal text "None"/"nan", making unrelated rows look like matches).
    base = base[base["guest_code"].notna()].copy()
    stats.invoicing_unique_guests_before_scope = int(base["guest_code"].nunique())
    stats.invoicing_rows_outside_visit_scope = int((~base["guest_code"].isin(visit_guest_codes)).sum())
    base = base[base["guest_code"].isin(visit_guest_codes)].copy()
    stats.invoicing_unique_guests = int(base["guest_code"].nunique())
    base = base.drop_duplicates(subset=["guest_code", "invoice_no"], keep="first")
    stats.invoice_rows_after_expand = len(base)

    # Invoice No is not always unique across guests -- the SAME invoice number
    # can legitimately belong to two different people's two different
    # packages. This matters below when the Benefit Report has no Guest Code
    # to join on: joining by Invoice No alone would silently mix those two
    # guests' balance/status together. Package Name (normalized) is used as a
    # tiebreaker in that case, so track it here.
    invoice_guest_counts = base.groupby("invoice_no")["guest_code"].nunique()
    stats.invoicing_colliding_invoice_numbers = int((invoice_guest_counts > 1).sum())
    if "package_name" in base.columns:
        base["_package_name_norm"] = normalize_text(base["package_name"])

    # --- Step 3: attach last visit date ---------------------------------
    merged = base.merge(visits, on="guest_code", how="left")
    stats.guests_without_last_visit = int(merged["last_visit_date"].isna().sum())
    if stats.guests_without_last_visit:
        stats.guests_without_last_visit_sample = sorted(
            merged.loc[merged["last_visit_date"].isna(), "guest_code"].unique()
        )[:15]

    # --- Step 4: attach balance sessions -------------------------------
    benefit_fields = {
        k: k for k in ["guest_code", "invoice_no", "balance_qty", "package_status", "package_name", "benefit_name"]
    }
    benefits = _select_and_rename(benefit_report, benefit_fields)
    stats.benefit_sheet_name = benefit_report.sheet_name
    stats.benefit_raw_rows = len(benefits)
    has_benefit_guest_code = "guest_code" in benefits.columns
    stats.benefit_has_guest_code = has_benefit_guest_code
    if has_benefit_guest_code:
        benefits["guest_code"] = normalize_guest_code(benefits["guest_code"])
        stats.benefit_blank_guest_code_rows = int(benefits["guest_code"].isna().sum())
        benefits = benefits[benefits["guest_code"].notna()].copy()
    benefits["balance_qty"] = pd.to_numeric(benefits["balance_qty"], errors="coerce")

    # Some locations' Benefit Report has no guest identifier column at all.
    # Invoice No alone isn't always a safe fallback join key either -- the
    # SAME invoice number can legitimately belong to two different guests'
    # two different packages (see the collision check above), which would
    # otherwise silently mix their balance/status together. When the Benefit
    # Report also has Package Name, use a normalized (Invoice No, Package
    # Name) pair to disambiguate -- this resolved every real collision found
    # in testing. If Package Name isn't available either, fall back to
    # Invoice No alone and flag the unresolved risk.
    use_package_name_tiebreak = (
        not has_benefit_guest_code
        and "package_name" in benefits.columns
        and "_package_name_norm" in base.columns
    )
    if has_benefit_guest_code:
        benefit_join_keys = ["guest_code", "invoice_no"]
    elif use_package_name_tiebreak:
        benefits["_package_name_norm"] = normalize_text(benefits["package_name"])
        benefit_join_keys = ["invoice_no", "_package_name_norm"]
    else:
        benefit_join_keys = ["invoice_no"]
        if stats.invoicing_colliding_invoice_numbers:
            colliding_invoices = invoice_guest_counts[invoice_guest_counts > 1].index
            stats.benefit_ambiguous_unresolved_rows = int(merged["invoice_no"].isin(colliding_invoices).sum())

    agg = {"balance_qty": lambda s: s.sum(min_count=1)}  # all-NaN group -> NaN, not a false 0
    if "package_status" in benefits.columns:
        agg["package_status"] = "first"
    benefit_totals = benefits.groupby(benefit_join_keys, as_index=False).agg(agg)

    merged = merged.merge(benefit_totals, on=benefit_join_keys, how="left", suffixes=("", "_benefit"))
    stats.invoices_without_benefit_match = int(merged["balance_qty"].isna().sum())

    # --- Step 5: balance + inactivity ----------------------------------
    merged["balance_sessions"] = merged["balance_qty"]

    # A Last Visit Date after today is impossible (can't have "last visited"
    # in the future) -- almost always a source-data issue in the Visit Report
    # rather than anything this pipeline can correct. Blank it out here so
    # it's treated as unusable, same as a guest with no visit match at all.
    is_future_visit = merged["last_visit_date"] > today
    stats.future_last_visit_rows = int(is_future_visit.sum())
    merged.loc[is_future_visit, "last_visit_date"] = pd.NaT

    # Wherever Last Visit Date is still unusable (no visit record at all, or
    # just blanked above for being a future date), fall back to Package
    # Creation Date as the best available substitute -- it's better than no
    # activity signal at all for a guest we otherwise know nothing about.
    stats.last_visit_filled_from_creation_date = int(merged["last_visit_date"].isna().sum())
    merged["last_visit_date"] = merged["last_visit_date"].fillna(merged["package_start_date"])

    merged["inactive_days"] = (today - merged["last_visit_date"]).dt.days

    # Customer Activity: purely a comparison of Inactive Days against the
    # inactivity threshold, independent of package status.
    merged["customer_activity"] = "Active"
    merged.loc[merged["inactive_days"] >= inactivity_threshold_days, "customer_activity"] = "Inactive"

    # --- Step 6: effective status (Benefit Report status wins, falls back
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
    # A blank Package Status (no value in either source report) is treated
    # as Closed.
    is_blank_status = merged["effective_status"].isna() | (
        merged["effective_status"].astype(str).str.strip() == ""
    )
    merged.loc[is_blank_status, "effective_status"] = "Closed"
    status_norm = merged["effective_status"].astype(str).str.strip().str.lower()

    # --- Step 7: inclusion rules -----------------------------------------
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

    if stats.future_last_visit_rows:
        stats.notes.append(
            f"{stats.future_last_visit_rows} row(s) had a Last Visit Date after "
            "today, which is impossible -- this points to a data-entry issue in "
            "the Org/Latest-Visit Report itself (e.g. a future appointment date "
            "recorded instead of an actual past visit), not something this app "
            "can correct. Their Package Creation Date was used as a substitute "
            "for Inactive Days instead."
        )
    if stats.last_visit_filled_from_creation_date:
        stats.notes.append(
            f"{stats.last_visit_filled_from_creation_date} row(s) had no usable "
            "Last Visit Date (missing, or an invalid future date); Package "
            "Creation Date was used in its place to compute Inactive Days."
        )
    if stats.invoicing_exact_duplicate_rows_dropped:
        stats.notes.append(
            f"{stats.invoicing_exact_duplicate_rows_dropped} fully duplicate row(s) "
            "(identical across every column) were removed from the Package "
            "Invoicing Report before processing."
        )
    if stats.invoicing_colliding_invoice_numbers:
        if stats.benefit_ambiguous_unresolved_rows:
            stats.notes.append(
                f"DATA INTEGRITY WARNING: {stats.invoicing_colliding_invoice_numbers} "
                "Invoice No value(s) in the Package Invoicing Report are shared by two or "
                "more different guests. The Package Benefit Report has no Guest Code or "
                "Package Name to disambiguate them, so "
                f"{stats.benefit_ambiguous_unresolved_rows} row(s) may have another guest's "
                "balance/status incorrectly attached -- do not trust those rows until resolved."
            )
        else:
            stats.notes.append(
                f"{stats.invoicing_colliding_invoice_numbers} Invoice No value(s) in the "
                "Package Invoicing Report are shared by two or more different guests. "
                "Package Name was used to correctly tell them apart when matching the "
                "Package Benefit Report."
            )
    if stats.invoicing_rows_outside_visit_scope:
        stats.notes.append(
            f"{stats.invoicing_rows_outside_visit_scope} row(s) in the Package "
            "Invoicing Report belonged to a guest not present in the Org/"
            "Latest-Visit Report (i.e. likely a different location) and were "
            "excluded from scope. The Invoicing/Benefit Report exports may not "
            "be filtered to a single location the way the Visit Report is."
        )
    if stats.invoicing_blank_guest_code_rows:
        stats.notes.append(
            f"{stats.invoicing_blank_guest_code_rows} row(s) in the Package "
            "Invoicing Report had no Guest Code at all and were excluded "
            "entirely (they can't be attributed to any guest)."
        )
    if stats.visit_blank_guest_code_rows:
        stats.notes.append(
            f"{stats.visit_blank_guest_code_rows} row(s) in the Org/Latest-Visit "
            "Report had no Guest Code at all and were ignored."
        )
    if stats.benefit_blank_guest_code_rows:
        stats.notes.append(
            f"{stats.benefit_blank_guest_code_rows} row(s) in the Package "
            "Benefit Report had no Guest Code at all and were ignored."
        )
    if stats.guests_without_last_visit:
        sample_text = ", ".join(stats.guests_without_last_visit_sample)
        more = stats.guests_without_last_visit - len(stats.guests_without_last_visit_sample)
        if more > 0:
            sample_text += f", and {more} more"
        stats.notes.append(
            f"{stats.guests_without_last_visit} guest(s) had no Last Visit Date "
            "in the Org/Latest-Visit report; Package Creation Date was used as "
            f"a substitute to compute Inactive Days. Guest Code(s): {sample_text}"
        )
    if stats.invoices_without_benefit_match:
        stats.notes.append(
            f"{stats.invoices_without_benefit_match} package invoice(s) had no "
            "matching row in the Package Benefit Report; they can still appear "
            "in the final report via the Active-but-inactive rule, but cannot "
            "be evaluated for the Fully-Redeemed rule (balance unknown)."
        )

    # --- Step 8: expand into one row per benefit line, with ccat ---------
    # Everything above (inclusion in `final`, Balance Sessions, effective
    # status) is unchanged and still computed at one-row-per-invoice grain.
    # This step is purely about how the report is DISPLAYED: each invoice's
    # individual benefit lines (e.g. "Photo Facial Pro" + "Face-Dermapen4"
    # on the same invoice) are broken out into their own row, each carrying
    # its own Benefit Name, ccat category (looked up from the bundled
    # Segmentation reference table), and that specific benefit's own
    # Balance Quantity. Invoices with no benefit-line match at all still
    # appear once, with Benefit Name/Balance Quantity blank. A blank ccat
    # (no benefit-line match, or a Benefit Name not covered by the
    # Segmentation reference table) is filled with "Other" rather than
    # left blank.
    if "benefit_name" in benefits.columns:
        benefit_detail = benefits[benefit_join_keys + ["benefit_name", "balance_qty"]].rename(
            columns={"balance_qty": "benefit_balance_qty"}
        )
        benefit_detail["ccat"] = lookup_ccat(benefit_detail["benefit_name"])
    else:
        benefit_detail = pd.DataFrame(columns=benefit_join_keys + ["benefit_name", "benefit_balance_qty", "ccat"])

    merged = merged.merge(benefit_detail, on=benefit_join_keys, how="left")
    final = final.merge(benefit_detail, on=benefit_join_keys, how="left")
    merged["ccat"] = merged["ccat"].fillna("Other")
    final["ccat"] = final["ccat"].fillna("Other")

    # Summary: every distinct ccat category across a guest's own rows in
    # this same table, comma-separated, shown on every one of that guest's
    # rows. full_list's Summary is scoped to all of a guest's packages;
    # final's Summary is scoped only to the packages that made the filtered
    # report -- each table summarizes only what's actually in it.
    merged["guest_summary"] = merged["guest_code"].map(_ccat_summary_by_guest(merged))
    final["guest_summary"] = final["guest_code"].map(_ccat_summary_by_guest(final))

    # full_list = every invoiced guest x their packages, scoped to the Visit
    # Report's location and unfiltered by the inactivity rules, carried
    # through with the enrichment columns (visit date, balance, status) attached.
    full_list = _format_display(merged)
    final = _format_display(final)

    return full_list, final, stats


def build_guest_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Roll a (possibly benefit-line-expanded) report up to one row per unique
    Guest Code:
      - Package Status: "Active" if any of the guest's packages is Active,
        else "Closed".
      - Customer Activity: "Active" if any of the guest's packages is
        Active (i.e. Inactive Days < threshold), else "Inactive".
      - Summary: every distinct ccat category across the guest's packages,
        comma-separated.
    """
    # Vectorized instead of groupby().agg(lambda ...) -- the lambda form calls
    # back into Python once per guest, which is fine on a small report but
    # takes several seconds once a report reaches tens of thousands of rows
    # (an org-wide upload, not a single location). `.any()` on a grouped
    # boolean Series is a single vectorized reduction; the ccat Summary
    # dedupes (Guest Code, ccat) pairs first so the per-group join only ever
    # runs over a handful of rows per guest instead of every source row.
    package_status = (
        (df["Package Status"] == "Active").groupby(df["Guest Code"]).any().map({True: "Active", False: "Closed"})
    )
    customer_activity = (
        (df["Customer Activity"] == "Active")
        .groupby(df["Guest Code"])
        .any()
        .map({True: "Active", False: "Inactive"})
    )
    summary = _ccat_summary_by_guest(df, guest_col="Guest Code", ccat_col="Individual Category").reindex(
        package_status.index, fill_value=""
    )

    return pd.DataFrame(
        {
            "Guest Code": package_status.index,
            "Package Status": package_status.values,
            "Customer Activity": customer_activity.values,
            "Summary": summary.values,
        }
    )
