"""
Client Inactivity Report generator.

Combines three Zenoti exports (Org/Latest-Visit Guest Report, Package
Benefit Report, Package Invoicing Report) to find clients whose package is
fully redeemed (Balance Sessions == 0) AND who have not visited in at least
60 days.
"""
from __future__ import annotations

import streamlit as st

from core.columns import (
    PACKAGE_BENEFIT_SCHEMA,
    PACKAGE_INVOICING_SCHEMA,
    VISIT_REPORT_SCHEMA,
)
from core.export import dataframe_to_excel_bytes
from core.io_utils import ReportValidationError, load_and_validate
from core.pipeline import INACTIVITY_THRESHOLD_DAYS, build_guest_summary, build_report

st.set_page_config(page_title="Client Inactivity Report", layout="wide")
st.title("Client Inactivity Report")
st.caption(
    "Upload the three Zenoti reports below, then click Generate Report. "
    f"A client appears in the output if their package is fully redeemed "
    f"(0 balance sessions) AND they've been inactive for {INACTIVITY_THRESHOLD_DAYS}+ days."
)

col1, col2 = st.columns(2)
with col1:
    visit_file = st.file_uploader("Org / Latest-Visit Guest Report", type=["xlsx", "xls"])
    benefit_file = st.file_uploader("Package Benefit Report", type=["xlsx", "xls"])
with col2:
    invoicing_file = st.file_uploader("Package Invoicing Report", type=["xlsx", "xls"])

threshold = st.number_input(
    "Inactivity threshold (days)", min_value=1, value=INACTIVITY_THRESHOLD_DAYS, step=1
)

generate = st.button("Generate Report", type="primary", disabled=not all(
    [visit_file, benefit_file, invoicing_file]
))

if not all([visit_file, benefit_file, invoicing_file]):
    st.info("Upload all three files to enable report generation.")

if generate:
    try:
        with st.spinner("Reading and validating uploaded files..."):
            visit_resolved = load_and_validate(visit_file, VISIT_REPORT_SCHEMA)
            benefit_resolved = load_and_validate(benefit_file, PACKAGE_BENEFIT_SCHEMA)
            invoicing_resolved = load_and_validate(invoicing_file, PACKAGE_INVOICING_SCHEMA)

        with st.spinner("Merging reports and applying business rules..."):
            full_list_df, final_df, stats = build_report(
                visit_resolved,
                benefit_resolved,
                invoicing_resolved,
                inactivity_threshold_days=int(threshold),
            )

        st.success(f"Report generated: {stats.final_report_rows} package(s) match the criteria.")
        st.caption(
            f"Fully Redeemed & Inactive: {stats.final_closed_redeemed}  |  "
            f"Active Package but Inactive: {stats.final_active_unused}"
        )

        with st.expander("Processing summary", expanded=True):
            st.write("**Package Invoicing Report**")
            st.write(
                f"- Sheet read: `{stats.invoicing_sheet_name}` — {stats.invoicing_raw_rows} row(s) "
                f"after removing {stats.invoicing_exact_duplicate_rows_dropped} exact-duplicate row(s), "
                f"{stats.invoicing_unique_guests_before_scope} unique guest(s) before location scoping"
            )
            st.write(
                f"- {stats.invoicing_rows_outside_visit_scope} row(s) excluded as outside the "
                f"Visit Report's location scope — {stats.invoicing_unique_guests} unique guest(s) remain"
            )
            st.write("**Org / Latest-Visit Guest Report**")
            st.write(
                f"- Sheet read: `{stats.visit_sheet_name}` — {stats.visit_raw_rows} row(s), "
                f"{stats.visit_unique_guests} unique guest(s) before dedup "
                f"({stats.visit_duplicates_dropped} duplicate guest row(s) dropped)"
            )
            st.write("**Package Benefit Report**")
            st.write(
                f"- Sheet read: `{stats.benefit_sheet_name}` — {stats.benefit_raw_rows} row(s), "
                f"guest identifier column {'found' if stats.benefit_has_guest_code else 'NOT found (joined on Invoice No only)'}"
            )
            st.write("---")
            st.write(f"Package invoice rows (after dedup): {stats.invoice_rows_after_expand}")
            st.write(f"Matched via Fully Redeemed & Inactive rule: {stats.final_closed_redeemed}")
            st.write(f"Matched via Active Package but Inactive rule: {stats.final_active_unused}")
            for note in stats.notes:
                st.warning(note)

        tab_inactivity, tab_full = st.tabs(["Inactivity Report (filtered)", "Full Package List (matches DPR)"])

        with tab_inactivity:
            final_unique_guests = final_df["Guest Code"].nunique()
            st.caption(f"{final_unique_guests} unique guest(s) in this filtered report.")
            st.dataframe(final_df, use_container_width=True)
            excel_bytes = dataframe_to_excel_bytes(
                final_df,
                summary={"Unique Guests": final_unique_guests},
                guest_summary=build_guest_summary(final_df),
            )
            st.download_button(
                "Download Inactivity Report (Excel)",
                data=excel_bytes,
                file_name="client_inactivity_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        with tab_full:
            full_unique_guests = full_list_df["Guest Code"].nunique()
            st.caption(
                f"{len(full_list_df)} rows, {full_unique_guests} unique guest(s) — every "
                "guest with at least one package, unfiltered. This is the same dataset a DPR "
                "export represents; the unique guest count here should match DPR's Guest Code "
                "count exactly. Use it to sanity-check the joins independently of the "
                "inactivity rule above."
            )
            st.dataframe(full_list_df, use_container_width=True)
            full_excel_bytes = dataframe_to_excel_bytes(
                full_list_df,
                sheet_name="Full Package List",
                summary={"Unique Guests": full_unique_guests},
                guest_summary=build_guest_summary(full_list_df),
            )
            st.download_button(
                "Download Full Package List (Excel)",
                data=full_excel_bytes,
                file_name="full_package_list.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    except ReportValidationError as e:
        st.error(str(e))
    except Exception as e:  # noqa: BLE001 - surface unexpected errors to the user
        st.error(f"Unexpected error while generating the report: {e}")
        raise
