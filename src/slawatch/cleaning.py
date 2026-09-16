"""pandas cleaning functions for the raw synthetic ticket extract.

Each function is pure (returns a new frame plus counts) so it can be unit-tested on tiny
frames. ``clean_tickets`` and ``clean_status_history`` compose them and produce a
``CleaningReport`` that the pipeline logs and stores alongside the load.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from slawatch import config as C

TICKET_CATEGORICALS: dict[str, list[str]] = {
    "ticket_type": C.TICKET_TYPES,
    "severity": C.SEVERITIES,
    "status": C.STATUSES,
    "channel": C.CHANNELS,
}
TICKET_TIMESTAMPS = [
    "creation_date",
    "last_update",
    "expected_resolution_date",
    "requested_resolution_date",
    "resolution_date",
]
SEVERITY_TO_PRIORITY = {"critical": 1, "major": 2, "minor": 3, "low": 4}


@dataclass
class CleaningReport:
    input_rows: int = 0
    exact_duplicates_dropped: int = 0
    key_duplicates_dropped: int = 0
    categorical_cells_normalised: dict[str, int] = field(default_factory=dict)
    unparseable_timestamps: dict[str, int] = field(default_factory=dict)
    rejected_rows: dict[str, int] = field(default_factory=dict)
    priority_imputed: int = 0
    description_missing: int = 0
    output_rows: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------------------
def normalise_categorical(series: pd.Series) -> pd.Series:
    """Trim, lower-case and snake_case a categorical column ('  Web Portal ' -> 'web_portal')."""
    s = series.astype("string").str.strip().str.lower()
    s = s.str.replace(r"[\s\-]+", "_", regex=True)
    return s.mask(s == "", pd.NA)


def normalise_categoricals(
    df: pd.DataFrame, columns: list[str]
) -> tuple[pd.DataFrame, dict[str, int]]:
    out = df.copy()
    changed: dict[str, int] = {}
    for col in columns:
        before = out[col].astype("string")
        after = normalise_categorical(before)
        changed[col] = int((before.fillna("") != after.fillna("")).sum())
        out[col] = after
    return out, changed


def drop_duplicate_rows(df: pd.DataFrame, key: str = "id") -> tuple[pd.DataFrame, int, int]:
    """Drop exact duplicate rows, then any remaining duplicate keys (first wins)."""
    n0 = len(df)
    out = df.drop_duplicates()
    n_exact = n0 - len(out)
    out = out.drop_duplicates(subset=[key], keep="first")
    n_key = n0 - n_exact - len(out)
    return out.reset_index(drop=True), n_exact, n_key


def parse_timestamps(df: pd.DataFrame, columns: list[str]) -> tuple[pd.DataFrame, dict[str, int]]:
    """Parse mixed ISO-8601 representations (Z, +hh:mm offsets, naive) to UTC.

    Naive values are treated as UTC. Values that cannot be parsed become NaT and are counted.
    """
    out = df.copy()
    unparseable: dict[str, int] = {}
    for col in columns:
        raw = out[col].astype("string").str.strip()
        raw = raw.mask(raw == "", pd.NA)
        parsed = pd.to_datetime(raw, utc=True, format="ISO8601", errors="coerce")
        unparseable[col] = int((raw.notna() & parsed.isna()).sum())
        out[col] = parsed
    return out, unparseable


def impute_priority(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Fill a missing priority from severity (P1 critical ... P4 low)."""
    out = df.copy()
    pri = pd.to_numeric(out["priority"], errors="coerce")
    missing = pri.isna()
    pri = pri.where(~missing, out["severity"].map(SEVERITY_TO_PRIORITY))
    out["priority"] = pri.astype("Int64")
    return out, int(missing.sum())


def flag_impossible_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split rows that violate hard invariants. Returns (kept, rejected-with-reason)."""
    reasons = pd.Series(pd.NA, index=df.index, dtype="string")

    def _mark(mask: pd.Series, reason: str) -> None:
        nonlocal reasons
        reasons = reasons.mask(mask & reasons.isna(), reason)

    _mark(df["creation_date"].isna(), "missing_or_unparseable_creation_date")
    _mark(df["id"].isna() | (df["id"].astype("string").str.strip() == ""), "missing_id")
    _mark(
        df["resolution_date"].notna() & (df["resolution_date"] < df["creation_date"]),
        "resolution_before_creation",
    )
    _mark(
        df["last_update"].notna() & (df["last_update"] < df["creation_date"]),
        "last_update_before_creation",
    )
    _mark(
        df["expected_resolution_date"].notna()
        & (df["expected_resolution_date"] < df["creation_date"]),
        "expected_resolution_before_creation",
    )
    for col, allowed in TICKET_CATEGORICALS.items():
        _mark(~df[col].isin(allowed), f"unknown_{col}")
    for col in ("customer_id", "site_id", "service_id"):
        _mark(df[col].isna(), f"missing_{col}")

    rejected = df[reasons.notna()].copy()
    rejected["reject_reason"] = reasons[reasons.notna()]
    kept = df[reasons.isna()].reset_index(drop=True)
    return kept, rejected.reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Composed cleaners
# --------------------------------------------------------------------------------------
def clean_tickets(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, CleaningReport]:
    """Raw string extract -> typed, deduplicated, validated ticket frame."""
    report = CleaningReport(input_rows=len(raw))
    df = raw.copy()

    for col in (
        "id",
        "customer_id",
        "site_id",
        "service_id",
        "assignment_group",
        "active_outage_id",
        "name",
        "description",
    ):
        df[col] = df[col].astype("string").str.strip()
        df[col] = df[col].mask(df[col] == "", pd.NA)

    df, n_exact, n_key = drop_duplicate_rows(df, key="id")
    report.exact_duplicates_dropped = n_exact
    report.key_duplicates_dropped = n_key

    df, changed = normalise_categoricals(df, list(TICKET_CATEGORICALS) + ["assignment_group"])
    report.categorical_cells_normalised = changed

    df, unparseable = parse_timestamps(df, TICKET_TIMESTAMPS)
    report.unparseable_timestamps = unparseable

    df, n_pri = impute_priority(df)
    report.priority_imputed = n_pri
    report.description_missing = int(df["description"].isna().sum())

    for col in ("open_backlog_at_creation", "reopen_count"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    kept, rejected = flag_impossible_rows(df)
    report.rejected_rows = {k: int(v) for k, v in rejected["reject_reason"].value_counts().items()}
    report.output_rows = len(kept)
    return kept, rejected, report


def clean_status_history(
    raw: pd.DataFrame, valid_ticket_ids: pd.Series
) -> tuple[pd.DataFrame, dict]:
    df = raw.copy()
    n0 = len(df)
    df["ticket_id"] = df["ticket_id"].astype("string").str.strip()
    df["change_reason"] = df["change_reason"].astype("string").str.strip()
    df, changed = normalise_categoricals(df, ["status"])
    df, unparseable = parse_timestamps(df, ["change_date"])
    df = df.drop_duplicates()
    n_dupes = n0 - len(df)
    valid = df["status"].isin(C.STATUSES) & df["change_date"].notna()
    valid &= df["ticket_id"].isin(set(valid_ticket_ids))
    n_orphan_or_invalid = int((~valid).sum())
    df = df[valid].sort_values(["ticket_id", "change_date"], kind="stable").reset_index(drop=True)
    df["sequence_no"] = df.groupby("ticket_id").cumcount().astype("int64") + 1
    counts = {
        "input_rows": n0,
        "duplicates_dropped": int(n_dupes),
        "status_cells_normalised": changed["status"],
        "unparseable_change_date": unparseable["change_date"],
        "invalid_or_orphan_rows_dropped": n_orphan_or_invalid,
        "output_rows": int(len(df)),
    }
    return df, counts


def resolution_hours(df: pd.DataFrame) -> pd.Series:
    delta = df["resolution_date"] - df["creation_date"]
    return (delta.dt.total_seconds() / 3600.0).astype(float).where(delta.notna(), np.nan)
