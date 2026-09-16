"""Derived fields, split into creation-time-safe features and post-resolution outcomes.

The classifier (a later step) must only use ``CREATION_TIME_FEATURES``; everything in
``OUTCOME_FIELDS`` is known only after the ticket progresses and would leak the label.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from slawatch import config as C

# Known at the moment the ticket is opened (plus dimension attributes joined by key).
CREATION_TIME_FEATURES = [
    "ticket_type",
    "severity",
    "priority",
    "channel",
    "customer_id",
    "tier",
    "industry",
    "site_id",
    "region",
    "province",
    "service_id",
    "service_type",
    "assignment_group",
    "active_outage_id",
    "open_backlog_at_creation",
    "sla_target_hours",
    "creation_hour_local",
    "creation_dow_local",
    "is_weekend",
    "is_after_hours",
    "has_requested_resolution_date",
]

# Only known after the ticket has progressed - never features.
OUTCOME_FIELDS = [
    "status",
    "last_update",
    "resolution_date",
    "reopen_count",
    "resolution_hours",
    "is_resolved",
    "sla_breached",
]


def join_dimensions(
    tickets: pd.DataFrame,
    customers: pd.DataFrame,
    sites: pd.DataFrame,
    services: pd.DataFrame,
) -> pd.DataFrame:
    out = tickets.merge(
        customers[["customer_id", "customer_name", "tier", "industry"]],
        on="customer_id",
        how="left",
    )
    out = out.merge(
        sites[["site_id", "city", "province", "region", "timezone"]], on="site_id", how="left"
    )
    out = out.merge(services[["service_id", "service_type"]], on="service_id", how="left")
    return out


def add_creation_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Local-time features evaluated in the site's timezone (needs ``timezone`` column)."""
    out = df.copy()
    n = len(out)
    hour = np.zeros(n, dtype="int64")
    dow = np.zeros(n, dtype="int64")
    tz = out["timezone"].fillna(C.LOCAL_TZ_FOR_PROFILE).to_numpy()
    utc = pd.DatetimeIndex(out["creation_date"])
    for zone in pd.unique(tz):
        mask = tz == zone
        local = utc[mask].tz_convert(zone)
        hour[mask] = local.hour.to_numpy()
        dow[mask] = local.dayofweek.to_numpy()
    lo, hi = C.BUSINESS_HOURS
    out["creation_hour_local"] = hour
    out["creation_dow_local"] = dow
    out["is_weekend"] = dow >= 5
    out["is_after_hours"] = (hour < lo) | (hour >= hi)
    out["has_requested_resolution_date"] = out["requested_resolution_date"].notna()
    out["creation_month"] = pd.to_datetime(
        out["creation_date"].dt.tz_convert("UTC").dt.strftime("%Y-%m-01")
    ).dt.date
    return out


def add_sla_target(df: pd.DataFrame, sla_targets: pd.DataFrame) -> pd.DataFrame:
    out = df.merge(sla_targets, on=["tier", "severity"], how="left")
    out = out.rename(columns={"target_hours": "sla_target_hours"})
    return out


def add_outcome_fields(df: pd.DataFrame, snapshot: pd.Timestamp) -> pd.DataFrame:
    """Resolution hours and the SLA outcome.

    ``sla_breached`` is True/False for resolved tickets, True for open tickets already past
    their expected resolution date at ``snapshot``, and NULL for open tickets still inside
    their window (and for cancelled tickets).
    """
    out = df.copy()
    delta = out["resolution_date"] - out["creation_date"]
    out["resolution_hours"] = (delta.dt.total_seconds() / 3600.0).round(4)
    out["is_resolved"] = out["resolution_date"].notna()
    cancelled = out["status"] == "cancelled"
    resolved_breach = out["resolution_hours"] > out["sla_target_hours"]
    open_past_due = (~out["is_resolved"]) & (snapshot > out["expected_resolution_date"])
    breached = pd.Series(pd.NA, index=out.index, dtype="boolean")
    breached = breached.mask(out["is_resolved"], resolved_breach)
    breached = breached.mask(open_past_due, True)
    breached = breached.mask(cancelled, pd.NA)
    out["sla_breached"] = breached
    return out
