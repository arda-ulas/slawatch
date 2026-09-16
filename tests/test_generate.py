from __future__ import annotations

import json

import numpy as np
import pandas as pd

from slawatch import config as C
from slawatch.generate import (
    FILE_NAMES,
    HISTORY_COLUMNS,
    TICKET_COLUMNS,
    GenerationParams,
    apply_dirtiness,
    generate,
)


def test_same_seed_is_deterministic():
    a = generate(GenerationParams(seed=123, n_tickets=1500))
    b = generate(GenerationParams(seed=123, n_tickets=1500))
    for name in ("customers", "sites", "services", "outages", "tickets", "status_history"):
        pd.testing.assert_frame_equal(getattr(a, name), getattr(b, name))
    pd.testing.assert_frame_equal(a.raw_tickets, b.raw_tickets)
    assert a.metadata["dirtiness_injected"] == b.metadata["dirtiness_injected"]


def test_different_seed_changes_output():
    a = generate(GenerationParams(seed=1, n_tickets=1500))
    b = generate(GenerationParams(seed=2, n_tickets=1500))
    assert not a.tickets["creation_date"].equals(b.tickets["creation_date"])


def test_schema_columns_and_dtypes(small_data):
    t = small_data.tickets
    assert list(t.columns) == TICKET_COLUMNS
    assert list(small_data.status_history.columns) == HISTORY_COLUMNS
    assert list(small_data.raw_tickets.columns) == TICKET_COLUMNS
    for col in ("creation_date", "last_update", "expected_resolution_date", "resolution_date"):
        assert str(t[col].dtype).endswith("UTC]"), col
    assert t["id"].is_unique
    assert set(t["severity"]) <= set(C.SEVERITIES)
    assert set(t["status"]) <= set(C.STATUSES)
    assert set(t["channel"]) <= set(C.CHANNELS)
    assert set(t["ticket_type"]) <= set(C.TICKET_TYPES)
    assert t["priority"].between(1, 4).all()
    assert (t["open_backlog_at_creation"] >= 0).all()


def test_dimensions_are_consistent(small_data):
    d = small_data
    assert len(d.customers) == 40
    assert set(d.customers["tier"]) <= set(C.TIERS)
    assert d.sites["customer_id"].isin(d.customers["customer_id"]).all()
    assert d.services["site_id"].isin(d.sites["site_id"]).all()
    assert d.tickets["service_id"].isin(d.services["service_id"]).all()
    assert d.tickets["site_id"].isin(d.sites["site_id"]).all()
    assert d.tickets["customer_id"].isin(d.customers["customer_id"]).all()
    assert len(d.sla_targets) == len(C.TIERS) * len(C.SEVERITIES)
    assert d.tickets["active_outage_id"].dropna().isin(d.outages["outage_id"]).all()


def test_clean_tickets_respect_time_invariants(small_data):
    t = small_data.tickets
    assert (t["creation_date"] >= pd.Timestamp(C.DEFAULT_START, tz="UTC")).all()
    assert (t["creation_date"] < pd.Timestamp(C.DEFAULT_END, tz="UTC")).all()
    assert (t["last_update"] >= t["creation_date"]).all()
    resolved = t["resolution_date"].notna()
    assert (t.loc[resolved, "resolution_date"] >= t.loc[resolved, "creation_date"]).all()
    assert (t["expected_resolution_date"] > t["creation_date"]).all()
    # status agrees with resolution: resolved/closed <=> resolution_date present
    done = t["status"].isin(["resolved", "closed"])
    assert (done == resolved).all()
    assert (t.loc[t["status"] == "cancelled", "resolution_date"].isna()).all()


def test_breach_rate_in_expected_band(small_data):
    t = small_data.tickets
    resolved = t["resolution_date"].notna()
    res_h = (t["resolution_date"] - t["creation_date"]).dt.total_seconds() / 3600
    target_h = (t["expected_resolution_date"] - t["creation_date"]).dt.total_seconds() / 3600
    rate = (res_h[resolved] > target_h[resolved]).mean()
    assert 0.08 <= rate <= 0.25, rate
    assert small_data.metadata["clean_breach_rate_resolved"] == round(float(rate), 4)


def test_sla_target_matches_tier_and_severity(small_data):
    d = small_data
    tier = d.tickets["customer_id"].map(d.customers.set_index("customer_id")["tier"])
    expected = [
        C.SLA_TARGET_HOURS[ti][se] for ti, se in zip(tier, d.tickets["severity"], strict=True)
    ]
    actual = (
        d.tickets["expected_resolution_date"] - d.tickets["creation_date"]
    ).dt.total_seconds() / 3600
    np.testing.assert_allclose(actual.to_numpy(), expected, atol=1e-6)


def test_status_history_is_ordered_and_complete(small_data):
    h = small_data.status_history
    t = small_data.tickets
    assert set(h["ticket_id"]) == set(t["id"])
    first = h.groupby("ticket_id").first()
    assert (first["status"] == "acknowledged").all()
    ordered = h.groupby("ticket_id")["change_date"].apply(lambda s: s.is_monotonic_increasing)
    assert ordered.all()
    last = h.groupby("ticket_id").last()
    assert last.loc[t["id"], "status"].tolist() == t["status"].tolist()
    assert (t["reopen_count"] > 0).any()
    assert (h["change_reason"] == "customer_reopened").sum() == t["reopen_count"].sum()


def test_outage_windows_raise_volume(small_data):
    d = small_data
    t = d.tickets
    in_outage = t["active_outage_id"].notna()
    assert in_outage.sum() > 0
    # outage tickets skew to incidents and higher severities
    assert (t.loc[in_outage, "ticket_type"] == "incident").mean() > 0.8
    share_crit = (t.loc[in_outage, "severity"].isin(["critical", "major"])).mean()
    share_base = (t.loc[~in_outage, "severity"].isin(["critical", "major"])).mean()
    assert share_crit > share_base


def test_dirtiness_counts_match_metadata(small_data):
    raw_with_dupes = small_data.raw_tickets
    clean = small_data.tickets
    dirt = small_data.metadata["dirtiness_injected"]
    assert len(raw_with_dupes) == len(clean) + dirt["duplicate_rows"]
    assert raw_with_dupes.duplicated().sum() == dirt["duplicate_rows"]
    raw = raw_with_dupes.drop_duplicates()  # per-row counts below are over unique rows
    assert (raw["description"] == "").sum() >= dirt["missing_description"]
    assert (raw["priority"] == "").sum() == dirt["missing_priority"]
    assert (
        raw["creation_date"].str.contains(r"[+-]\d{2}:\d{2}$").sum()
        == (dirt["creation_date_with_local_offset"])
    )
    assert (
        raw["creation_date"].str.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$").sum()
        == (dirt["creation_date_naive_utc"])
    )
    # case variants exist and are not present in the clean frame
    assert raw["severity"].str.contains("[A-Z]").sum() > 0
    assert not clean["severity"].str.contains("[A-Z]").any()


def test_apply_dirtiness_is_seed_driven(small_data):
    a, ca = apply_dirtiness(np.random.default_rng(5), small_data.tickets)
    b, cb = apply_dirtiness(np.random.default_rng(5), small_data.tickets)
    pd.testing.assert_frame_equal(a, b)
    assert ca == cb


def test_write_outputs_files_and_metadata(small_raw_dir):
    for name in FILE_NAMES.values():
        assert (small_raw_dir / name).exists(), name
    meta = json.loads((small_raw_dir / FILE_NAMES["metadata"]).read_text())
    assert meta["synthetic"] is True
    assert meta["params"]["n_tickets"] == 4000
    raw = pd.read_csv(small_raw_dir / FILE_NAMES["tickets"], dtype=str, keep_default_na=False)
    assert len(raw) == meta["row_counts"]["tickets_raw_extract"]


def test_shorter_window_and_size_still_work():
    d = generate(GenerationParams(seed=3, n_tickets=800, start="2025-01-01", end="2025-04-01"))
    assert len(d.tickets) == 800
    assert d.tickets["creation_date"].max() < pd.Timestamp("2025-04-01", tz="UTC")
    # only outages that fit inside the window are kept
    assert (d.outages["end_date"] <= pd.Timestamp("2025-04-01", tz="UTC")).all()


def test_cleaning_the_raw_extract_recovers_the_clean_frame(small_data):
    from slawatch.cleaning import clean_tickets

    kept, rejected, report = clean_tickets(small_data.raw_tickets)
    dirt = small_data.metadata["dirtiness_injected"]
    assert report.exact_duplicates_dropped == dirt["duplicate_rows"]
    assert report.rejected_rows == {
        "resolution_before_creation": dirt["impossible_resolution_before_creation"]
    }
    assert len(kept) == len(small_data.tickets) - dirt["impossible_resolution_before_creation"]
    assert report.priority_imputed == dirt["missing_priority"]
    assert sum(report.unparseable_timestamps.values()) == 0
    clean = small_data.tickets.set_index("id")
    got = kept.set_index("id")
    # categoricals and timestamps round-trip exactly for the surviving rows
    for col in ("severity", "channel", "status", "ticket_type", "assignment_group"):
        assert got[col].astype(object).tolist() == clean.loc[got.index, col].tolist(), col
    for col in ("creation_date", "resolution_date", "last_update"):
        pd.testing.assert_series_equal(
            got[col].dt.as_unit("ns").reset_index(drop=True),
            clean.loc[got.index, col].dt.as_unit("ns").reset_index(drop=True),
            check_names=False,
        )
