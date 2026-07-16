"""
Column detection utilities.

The four source reports come from Zenoti/Tableau exports whose headers vary
slightly release to release (e.g. "UserCode" vs "Guest Code" vs "guest").
Instead of hardcoding a single header string per field, each logical field
is defined by a list of acceptable aliases. Matching is case-insensitive and
ignores spaces/underscores/punctuation, so "Package Invoice No",
"package_invoice_no" and "PackageInvoiceNo" are all treated as the same
header.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find_column(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    """
    Return the actual column name in df matching one of the aliases, or None.

    An alias starting with "*" is a suffix match instead of an exact match,
    e.g. "*guest" matches "BLR guest", "DEL guest", "MUM guest", etc. -- Zenoti
    exports sometimes prefix this column with a location code.
    """
    normalized_lookup = {_normalize(col): col for col in df.columns}
    for alias in aliases:
        if alias.startswith("*"):
            suffix = _normalize(alias[1:])
            for norm_col, actual_col in normalized_lookup.items():
                if norm_col.endswith(suffix):
                    return actual_col
            continue
        match = normalized_lookup.get(_normalize(alias))
        if match is not None:
            return match
    return None


@dataclass
class FieldSpec:
    key: str
    aliases: list[str]
    required: bool = True


@dataclass
class ReportSchema:
    """A named group of FieldSpecs for one uploaded report."""
    report_label: str
    fields: list[FieldSpec] = field(default_factory=list)

    def resolve(self, df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
        """
        Try to resolve every field against df's columns.
        Returns (resolved_map, missing_required_field_keys).
        resolved_map maps field key -> actual dataframe column name (only for matches found).
        """
        resolved: dict[str, str] = {}
        missing: list[str] = []
        for spec in self.fields:
            match = find_column(df, spec.aliases)
            if match is not None:
                resolved[spec.key] = match
            elif spec.required:
                missing.append(spec.key)
        return resolved, missing


# --- Schemas for the four expected uploads -------------------------------

VISIT_REPORT_SCHEMA = ReportSchema(
    report_label="Org/Latest-Visit Guest Report",
    fields=[
        FieldSpec("guest_code", ["UserCode", "Guest Code", "GuestCode", "Guest_Code"]),
        FieldSpec("last_visit_date", ["lastvisit", "LastVisit", "Last Visit", "Last Visit Date"]),
    ],
)

PACKAGE_BENEFIT_SCHEMA = ReportSchema(
    report_label="Package Benefit Report",
    fields=[
        # Some locations' Package Benefit Report has no guest identifier column
        # at all (only Guest Name, which isn't a reliable join key) -- so this
        # is optional here. When absent, the pipeline joins this report to the
        # rest of the data on Invoice No alone instead of (Guest Code, Invoice No).
        FieldSpec("guest_code", ["Guest Code", "GuestCode", "UserCode", "*guest"], required=False),
        FieldSpec("invoice_no", ["Invoice No", "InvoiceNo", "Invoice Number"]),
        FieldSpec("balance_qty", ["Balance Quantity", "Balance Sessions", "BalanceQty", "Balance"]),
        FieldSpec("package_status", ["Package Status", "PackageStatus", "Status"], required=False),
        FieldSpec("package_name", ["Package Name", "PackageName"], required=False),
        FieldSpec("benefit_name", ["Benefit Name", "BenefitName"], required=False),
    ],
)

PACKAGE_INVOICING_SCHEMA = ReportSchema(
    report_label="Package Invoicing Report",
    fields=[
        FieldSpec("guest_code", ["Guest Code", "GuestCode", "guest", "UserCode"]),
        FieldSpec("invoice_no", ["Package Invoice No", "Invoice No", "InvoiceNo"]),
        FieldSpec("package_name", ["Package Name", "PackageName"], required=False),
        FieldSpec("package_start_date", ["Package Start Date", "Package Creation Date", "Start Date"]),
        FieldSpec("status", ["Status", "Package Status"], required=False),
    ],
)
