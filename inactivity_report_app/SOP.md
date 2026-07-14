# Client Inactivity Report — Standard Operating Procedure

## 1. What this app does

The Client Inactivity Report app finds clients whose package activity has gone quiet, by combining three Zenoti exports and applying two business rules. It produces:

- An **actionable list** of clients to follow up with
- A **full, unfiltered list** of every guest and their packages (used to sanity-check against Zenoti's own DPR export)

The app runs locally (one location at a time) via Streamlit.

---

## 2. Prerequisites (one-time setup)

- Python 3.12 installed
- From the `inactivity_report_app` folder, install dependencies:
  ```
  pip install -r requirements.txt
  ```
  (Installs `streamlit`, `pandas`, `openpyxl`.)

---

## 3. Step-by-step: generating a report

**Step 1 — Export 3 reports from Zenoti** for the location and date range needed:
1. **Org / Latest-Visit Guest Report** — each guest's last visit date
2. **Package Benefit Report** — remaining session balance per package
3. **Package Invoicing Report** — one row per package invoice (guest, invoice number, package name, start date, status)

Save each as `.xlsx` (or `.xls`). Exact column header wording doesn't need to match anything precisely — see Section 5 for how column detection works.

**Step 2 — Launch the app**
Double-click `run_app.bat`. A browser tab opens automatically.

**Step 3 — Upload the 3 files** into their matching upload boxes.

**Step 4 — Set the inactivity threshold** (defaults to 60 days) if a different cutoff is needed.

**Step 5 — Click "Generate Report."**
The app validates each file, merges them, and applies the two inclusion rules (see Section 4).

**Step 6 — Review the results**
- Success banner shows total matching rows and the split between the two rules
- "Processing summary" expander shows diagnostics (rows without a visit match, invoices without a benefit match, etc.)

**Step 7 — Check the two output tabs**

| Tab | Contents | Use it to... |
|---|---|---|
| **Inactivity Report (filtered)** | Only guests/packages matching one of the two inclusion rules | Follow up with these clients |
| **Full Package List (matches DPR)** | Every guest with at least one package, unfiltered | Sanity-check the merge against Zenoti's DPR export |

Each tab shows a live **unique guest count** in-app, and the downloaded Excel file for each includes a **Summary** sheet with that same count.

**Step 8 — Download and distribute** the Inactivity Report to whoever follows up with these clients.

---

## 4. How the logic works

### Inputs and roles

| File | Role | Key fields used |
|---|---|---|
| Package Invoicing Report | **Base** of the pipeline — one row per package | Guest Code, Invoice No, Package Name, Start Date, Status |
| Org/Latest-Visit Report | Attaches each guest's last visit date | Guest Code, Last Visit Date |
| Package Benefit Report | Attaches remaining balance sessions per package | Guest Code, Invoice No, Balance Quantity, Package Status |

There is **no separate guest roster/location filter** — every guest who appears in the Package Invoicing Report is in scope. This makes the app location-agnostic: run it once per location by uploading that location's 3 files.

### Pipeline steps

1. **Base** = Package Invoicing Report, deduplicated on (Guest Code, Package Invoice No).
2. **Attach Last Visit Date** — left join on Guest Code from the Visit Report.
3. **Attach Balance Sessions** — the Benefit Report can have multiple rows per package (one per benefit line); these are summed per (Guest Code, Invoice No) first, then left-joined on. A package with several benefit lines is only "fully redeemed" once *every* line is at zero balance.
4. **Compute Inactive Days** = today − Last Visit Date.
5. **Determine effective Package Status** — the Benefit Report's status wins; falls back to the Invoicing Report's status if there's no benefit-detail match for that invoice.
6. **Apply inclusion rules** — a package makes the final report if **either**:
   - **Fully Redeemed & Inactive**: status = Closed **and** Balance Sessions = 0 **and** Inactive Days ≥ threshold, or
   - **Active Package but Inactive**: status = Active **and** Inactive Days ≥ threshold

   Refunded (or any other status) packages are never included.

### Output columns

`Guest Code`, `Package Invoice No`, `Package Name`, `Package Creation Date`, `Last Visit Date`, `Inactive Days`, `Package Status`

(No guest name/phone/center/membership columns — those only existed in the old Guest Report input, which was removed. No Balance Sessions or Match Reason columns — removed from the output by request; the underlying data is still used internally to decide inclusion, just not displayed.)

---

## 5. Column detection (why header wording is flexible)

Each source file's headers can vary release to release (e.g. `"UserCode"` vs `"Guest Code"` vs `"guest"`). The app matches columns by a list of known aliases per field, case-insensitively and ignoring spaces/punctuation — `"Package Invoice No"`, `"package_invoice_no"`, and `"PackageInvoiceNo"` are all treated as the same header.

One field has a special pattern: the Package Benefit Report's Guest Code column is sometimes prefixed with a location code (Bangalore's export uses `"BLR guest"`). The app recognizes **any** `"<CITY> guest"` pattern automatically (e.g. `"DEL guest"`, `"MUM guest"`), so this doesn't need to be updated per location.

If a workbook has multiple sheets, the app scans all of them and picks whichever sheet best matches the expected columns — it doesn't assume "Sheet 1" is correct.

---

## 6. Troubleshooting

**"Could not find required column(s) [...]"**
The uploaded file is missing a column the app needs, under any known alias. The error message lists every column actually found in every sheet of that file — check the source export wasn't missing a column, or add a new alias to `core/columns.py` if the location uses genuinely different wording.

**Guest count doesn't match a manual count**
- Compare against the **Full Package List's** unique guest count, not the Inactivity Report's — the Inactivity Report is deliberately filtered down to only inactive guests, so it will always be smaller than DPR's total guest count. That's expected, not a bug.
- The Full Package List's unique guest count *should* match DPR's Guest Code count exactly. If it doesn't, check for blank Guest Code cells in the Invoicing Report, or a possible multi-sheet mismatch (see Section 5).

**Using this for a new location for the first time**
The pipeline has no hardcoded location logic, but it also hasn't been tested against every location's real export. Two things to watch on a first run:
1. **Column headers** — if a location's export uses different wording than Bangalore's for a required field, you'll get the clear validation error above (safe failure, not silent).
2. **Package status wording** — the inclusion rules check for the literal words "Closed" and "Active" (case-insensitive). If a location's system records status differently (e.g. "Complete" instead of "Closed"), those packages would silently fail to match either rule with **no error** — always sanity-check the Processing Summary and row counts on a location's first run.

---

## 7. Known limitations

- One location per run — no built-in multi-location batch mode.
- Inactivity is calculated from whatever date the app is run on (not a fixed reporting date), so re-running on a different day can shift which guests qualify.
- No automated test suite currently reflects the app's current (3-file, no-Guest-Report) shape — `tests/run_pipeline_test.py` and `tests/make_test_files.py` still reference the old 4-file flow and will need updating before they can be run again.
