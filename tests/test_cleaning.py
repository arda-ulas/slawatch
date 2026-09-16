from __future__ import annotations

import pandas as pd
import pytest

from slawatch import cleaning, features


def _row(**overrides) -> dict:
    base = {
        "id": "TT-0000001",
        "name": "Circuit down",
        "description": "desc",
        "ticket_type": "incident",
        "severity": "major",
        "priority": "2",
        "status": "closed",
        "channel": "phone",
        "creation_date": "2025-03-04T18:22:10Z",
        "last_update": "2025-03-06T10:00:00Z",
        "expected_resolution_date": "2025-03-05T06:22:10Z",
        "requested_resolution_date": "",
        "resolution_date": "2025-03-05T01:00:00Z",
        "customer_id": "CUST-001",
        "site_id": "SITE-0001",
        "service_id": "SVC-00001",
        "assignment_group": "field_ontario",
        "active_outage_id": "",
        "open_backlog_at_creation": "3",
        "reopen_count": "0",
    }
    base.update(overrides)
    return base


def test_normalise_categorical_handles_case_whitespace_and_spaces():
    s = pd.Series(["  Web Portal ", "PHONE", "\tsd-wan ", "in_progress", "", None])
    out = cleaning.normalise_categorical(s)
    assert out.tolist()[:4] == ["web_portal", "phone", "sd_wan", "in_progress"]
    assert out.iloc[4] is pd.NA
    assert out.iloc[5] is pd.NA


def test_normalise_categoricals_counts_changed_cells():
    df = pd.DataFrame(
        {"severity": ["Major", "major", " minor"], "channel": ["phone", "PHONE", "api"]}
    )
    out, changed = cleaning.normalise_categoricals(df, ["severity", "channel"])
    assert changed == {"severity": 2, "channel": 1}
    assert out["severity"].tolist() == ["major", "major", "minor"]


def test_drop_duplicate_rows_exact_then_by_key():
    df = pd.DataFrame({"id": ["a", "a", "b", "b"], "v": [1, 1, 2, 3]})
    out, n_exact, n_key = cleaning.drop_duplicate_rows(df, key="id")
    assert (n_exact, n_key) == (1, 1)
    assert out["id"].tolist() == ["a", "b"]
    assert out["v"].tolist() == [1, 2]  # first wins


def test_parse_timestamps_mixed_formats_to_utc():
    df = pd.DataFrame(
        {
            "ts": [
                "2025-03-04T18:22:10Z",
                "2025-03-04T13:22:10-05:00",
                "2025-03-04 18:22:10",
                "",
                "not a date",
            ]
        }
    )
    out, bad = cleaning.parse_timestamps(df, ["ts"])
    assert bad == {"ts": 1}
    expected = pd.Timestamp("2025-03-04T18:22:10Z")
    assert out["ts"].iloc[0] == expected
    assert out["ts"].iloc[1] == expected
    assert out["ts"].iloc[2] == expected
    assert pd.isna(out["ts"].iloc[3]) and pd.isna(out["ts"].iloc[4])
    assert str(out["ts"].dt.tz) == "UTC"


def test_impute_priority_from_severity():
    df = pd.DataFrame(
        {"priority": ["1", "", None, "4"], "severity": ["critical", "major", "low", "low"]}
    )
    out, n = cleaning.impute_priority(df)
    assert n == 2
    assert out["priority"].tolist() == [1, 2, 4, 4]
    assert str(out["priority"].dtype) == "Int64"


def test_flag_impossible_rows_reasons():
    raw = pd.DataFrame(
        [
            _row(),
            _row(id="TT-2", resolution_date="2025-03-01T00:00:00Z"),
            _row(id="TT-3", creation_date="garbage"),
            _row(id="TT-4", severity="urgent"),
            _row(id="TT-5", last_update="2025-01-01T00:00:00Z"),
            _row(id="TT-6", site_id=""),
        ]
    )
    df, _ = cleaning.normalise_categoricals(raw, ["ticket_type", "severity", "status", "channel"])
    df, _ = cleaning.parse_timestamps(df, cleaning.TICKET_TIMESTAMPS)
    df["site_id"] = df["site_id"].mask(df["site_id"] == "", pd.NA)
    kept, rejected = cleaning.flag_impossible_rows(df)
    assert kept["id"].tolist() == ["TT-0000001"]
    reasons = dict(zip(rejected["id"], rejected["reject_reason"], strict=True))
    assert reasons == {
        "TT-2": "resolution_before_creation",
        "TT-3": "missing_or_unparseable_creation_date",
        "TT-4": "unknown_severity",
        "TT-5": "last_update_before_creation",
        "TT-6": "missing_site_id",
    }


def test_clean_tickets_end_to_end_on_tiny_frame():
    raw = pd.DataFrame(
        [
            _row(),
            _row(),  # exact duplicate
            _row(
                id="TT-2",
                severity=" MAJOR ",
                channel="Web Portal",
                priority="",
                creation_date="2025-03-04 18:22:10",
                description="",
            ),
            _row(id="TT-3", resolution_date="2025-03-01T00:00:00Z"),
        ]
    )
    kept, rejected, report = cleaning.clean_tickets(raw)
    assert report.input_rows == 4
    assert report.exact_duplicates_dropped == 1
    assert report.rejected_rows == {"resolution_before_creation": 1}
    assert report.priority_imputed == 1
    assert report.description_missing == 1
    assert report.output_rows == 2
    assert kept["severity"].tolist() == ["major", "major"]
    assert kept["channel"].tolist() == ["phone", "web_portal"]
    assert kept["priority"].tolist() == [2, 2]
    assert rejected["id"].tolist() == ["TT-3"]
    assert kept["creation_date"].iloc[1] == pd.Timestamp("2025-03-04T18:22:10Z")


def test_clean_status_history_drops_orphans_and_sequences():
    raw = pd.DataFrame(
        {
            "ticket_id": ["TT-1", "TT-1", "TT-1", "TT-9"],
            "status": ["Acknowledged", "in_progress", "resolved", "closed"],
            "change_date": [
                "2025-01-01T00:00:00Z",
                "2025-01-01T01:00:00Z",
                "2025-01-02T00:00:00Z",
                "2025-01-01T00:00:00Z",
            ],
            "change_reason": ["ticket_created", "assigned", "fixed", "x"],
        }
    )
    out, counts = cleaning.clean_status_history(raw, pd.Series(["TT-1"]))
    assert counts["invalid_or_orphan_rows_dropped"] == 1
    assert counts["status_cells_normalised"] == 1
    assert out["sequence_no"].tolist() == [1, 2, 3]
    assert out["status"].tolist() == ["acknowledged", "in_progress", "resolved"]


def test_outcome_fields_resolved_open_and_cancelled():
    snapshot = pd.Timestamp("2025-03-10T00:00:00Z")
    df = pd.DataFrame(
        {
            "creation_date": pd.to_datetime(["2025-03-01T00:00:00Z"] * 4, utc=True),
            "expected_resolution_date": pd.to_datetime(
                [
                    "2025-03-02T00:00:00Z",
                    "2025-03-02T00:00:00Z",
                    "2025-03-02T00:00:00Z",
                    "2025-03-20T00:00:00Z",
                ],
                utc=True,
            ),
            "resolution_date": pd.to_datetime(
                ["2025-03-01T12:00:00Z", "2025-03-03T00:00:00Z", None, None], utc=True
            ),
            "status": ["closed", "closed", "in_progress", "in_progress"],
            "sla_target_hours": [24.0, 24.0, 24.0, 456.0],
        }
    )
    out = features.add_outcome_fields(df, snapshot)
    assert out["resolution_hours"].tolist()[:2] == [12.0, 48.0]
    assert out["sla_breached"].tolist()[:2] == [False, True]
    assert out["sla_breached"].iloc[2] is True or out["sla_breached"].iloc[2] == True  # noqa: E712
    assert pd.isna(out["sla_breached"].iloc[3])  # open, still inside its window
    cancelled = df.assign(status="cancelled")
    assert features.add_outcome_fields(cancelled, snapshot)["sla_breached"].isna().all()


def test_creation_time_features_use_site_timezone():
    df = pd.DataFrame(
        {
            "creation_date": pd.to_datetime(
                ["2025-03-08T02:30:00Z", "2025-03-08T02:30:00Z"], utc=True
            ),  # Saturday 02:30 UTC
            "timezone": ["America/Vancouver", "Asia/Tokyo"],
            "requested_resolution_date": pd.to_datetime([None, None], utc=True),
        }
    )
    out = features.add_creation_time_features(df)
    # Vancouver: Friday 18:30 (after hours, weekday); Tokyo: Saturday 11:30 (weekend, in hours)
    assert out["creation_dow_local"].tolist() == [4, 5]
    assert out["is_weekend"].tolist() == [False, True]
    assert out["is_after_hours"].tolist() == [True, False]
    assert out["creation_month"].iloc[0] == pd.Timestamp("2025-03-01").date()


@pytest.mark.parametrize("col", features.CREATION_TIME_FEATURES)
def test_creation_time_features_are_not_outcomes(col):
    assert col not in features.OUTCOME_FIELDS
