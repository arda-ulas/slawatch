"""Loads a small generated dataset into PostgreSQL and queries every analytical view.

Needs a reachable Postgres (``make db-up``). The database named in
SLAWATCH_TEST_DATABASE_URL is created if it does not exist. Skipped when unreachable, unless
SLAWATCH_REQUIRE_DB is set (CI), in which case an unreachable server is a failure.
"""

from __future__ import annotations

import pandas as pd
import pytest

from conftest import require_test_database
from slawatch import db, pipeline

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def loaded(small_raw_dir):
    engine = db.get_engine(require_test_database())
    tables, run_report = pipeline.build_tables(small_raw_dir)
    info = pipeline.load_database(engine, tables, run_report)
    yield engine, tables, run_report, info
    engine.dispose()


def _q(engine, sql: str) -> pd.DataFrame:
    return db.query(engine, sql)


def test_row_counts_match_frames(loaded):
    engine, tables, _, info = loaded
    for table in (
        "dim_customer",
        "dim_site",
        "dim_service",
        "sla_target",
        "outage_incident",
        "fact_ticket",
        "fact_ticket_status_history",
    ):
        assert db.scalar(engine, f"SELECT count(*) FROM {table}") == len(tables[table]), table
    assert len(info["views"]) == 7
    assert db.scalar(engine, "SELECT count(*) FROM load_run") == 1


def test_constraints_hold(loaded):
    engine, tables, _, _ = loaded
    assert (
        db.scalar(
            engine,
            "SELECT count(*) FROM fact_ticket WHERE resolution_ts < creation_ts "
            "OR last_update_ts < creation_ts",
        )
        == 0
    )
    ft = tables["fact_ticket"]
    n_breached = int(ft["sla_breached"].fillna(False).astype(bool).sum())
    assert db.scalar(engine, "SELECT count(*) FROM fact_ticket WHERE sla_breached") == n_breached


def test_sla_compliance_monthly(loaded):
    engine, tables, _, _ = loaded
    v = _q(engine, "SELECT * FROM v_sla_compliance_monthly")
    assert len(v) > 0
    assert v["sla_compliance_pct"].dropna().between(0, 100).all()
    assert (v["breached_tickets"] <= v["tickets_with_outcome"]).all()
    ft = tables["fact_ticket"]
    assert v["tickets"].sum() == (ft["status"] != "cancelled").sum()


def test_backlog_ageing_matches_pandas(loaded):
    engine, tables, _, _ = loaded
    as_of = pd.Timestamp("2025-06-01T00:00:00Z")
    ft = tables["fact_ticket"]
    end = ft["resolution_ts"].where(
        ft["resolution_ts"].notna(),
        ft["last_update_ts"].where(ft["status"] == "cancelled", pd.NaT),
    )
    open_mask = (ft["creation_ts"] <= as_of) & (end.isna() | (end > as_of))
    v = _q(engine, f"SELECT * FROM f_backlog_ageing('{as_of.isoformat()}')")
    assert v["open_tickets"].sum() == int(open_mask.sum())
    assert set(v["age_bucket"]) <= {"0-24h", "1-3d", "3-7d", "7-30d", "30d+"}
    monthly = _q(engine, "SELECT DISTINCT as_of FROM v_backlog_ageing_monthly ORDER BY as_of")
    assert len(monthly) >= 20
    current = _q(engine, "SELECT * FROM v_backlog_ageing_current")
    assert current["open_tickets"].sum() == int(
        (~ft["is_resolved"] & (ft["status"] != "cancelled")).sum()
    )
    # f_open_tickets lists the same tickets the ageing function counts, with a risk score column
    # that is empty until scores are loaded
    rows = _q(engine, f"SELECT * FROM f_open_tickets('{as_of.isoformat()}')")
    assert len(rows) == int(open_mask.sum())
    assert set(rows["ticket_id"]) == set(ft.loc[open_mask, "ticket_id"])
    assert rows["probability"].isna().all()
    by_band = rows.groupby("age_bucket").size()
    assert by_band.to_dict() == v.groupby("age_bucket")["open_tickets"].sum().to_dict()
    by_service = _q(engine, "SELECT * FROM v_backlog_ageing_monthly_by_service")
    assert (
        by_service.groupby("as_of")["open_tickets"].sum().to_dict()
        == _q(
            engine, "SELECT as_of, sum(open_tickets) AS n FROM v_backlog_ageing_monthly GROUP BY 1"
        )
        .set_index("as_of")["n"]
        .astype(int)
        .to_dict()
    )


def test_resolution_stats_grouping_sets(loaded):
    engine, tables, _, _ = loaded
    v = _q(engine, "SELECT * FROM v_resolution_stats")
    total = v[(v["severity"] == "all") & (v["service_type"] == "all")]
    assert len(total) == 1
    assert int(total["resolved_tickets"].iloc[0]) == int(tables["fact_ticket"]["is_resolved"].sum())
    assert (v["p50_hours"] <= v["p90_hours"]).all()
    assert v["breach_rate_pct"].between(0, 100).all()
    assert (v["severity"] != "all").any() and (v["service_type"] != "all").any()


def test_outage_impact(loaded):
    engine, tables, _, _ = loaded
    v = _q(engine, "SELECT * FROM v_outage_impact")
    assert len(v) == len(tables["outage_incident"])
    assert (v["tickets_during"] > 0).all()
    assert (v["volume_multiplier"].dropna() > 1).all()
    vs = _q(engine, "SELECT * FROM v_outage_vs_normal ORDER BY during_outage")
    assert vs["during_outage"].tolist() == [False, True]
    assert vs["breach_rate_pct"].iloc[1] > vs["breach_rate_pct"].iloc[0]


def test_customer_risk_trend_and_top_customers(loaded):
    engine, tables, _, _ = loaded
    v = _q(engine, "SELECT * FROM v_customer_risk_trend")
    assert len(v) > 0
    assert v["risk_rank_in_month"].min() == 1
    first_rows = v.sort_values(["customer_id", "month"]).groupby("customer_id").head(1)
    assert first_rows["prev_month_breach_rate_pct"].isna().all()
    top = _q(engine, "SELECT * FROM v_top_customers_at_risk")
    assert 0 < len(top) <= 10
    assert top["risk_rank"].tolist() == sorted(top["risk_rank"].tolist())
    assert set(top["trend"]) <= {"worsening", "improving", "flat"}


def test_weekly_kpi_matches_pandas(loaded):
    engine, tables, run_report, _ = loaded
    ft = tables["fact_ticket"]
    live = ft[ft["status"] != "cancelled"]
    v = _q(engine, "SELECT * FROM v_weekly_kpi ORDER BY week_ending")
    assert len(v) >= 100
    assert (pd.to_datetime(v["week_ending"]).dt.weekday == 6).all()
    assert (
        pd.to_datetime(v["week_start"]) + pd.Timedelta(days=6) == pd.to_datetime(v["week_ending"])
    ).all()
    # every full week before the snapshot, no partial week after it
    snapshot = pd.Timestamp(run_report["snapshot_ts"])
    assert pd.Timestamp(v["week_ending"].max(), tz="UTC") + pd.Timedelta(days=1) <= snapshot
    assert pd.Timestamp(v["week_start"].min(), tz="UTC") == live["creation_ts"].min().floor(
        "D"
    ) - pd.Timedelta(days=live["creation_ts"].min().weekday())
    ws = pd.to_datetime(v["week_start"]).dt.tz_localize("UTC")
    row = v.iloc[len(v) // 2]
    start = ws.iloc[len(v) // 2]
    end = start + pd.Timedelta(days=7)
    opened = live[(live["creation_ts"] >= start) & (live["creation_ts"] < end)]
    resolved = live[
        live["is_resolved"] & (live["resolution_ts"] >= start) & (live["resolution_ts"] < end)
    ]
    assert row["tickets_opened"] == len(opened)
    assert row["tickets_resolved"] == len(resolved)
    assert row["breached_tickets"] == int(resolved["sla_breached"].astype(bool).sum())
    assert abs(float(row["p50_resolution_hours"]) - resolved["resolution_hours"].median()) < 0.01
    open_at_end = (ft["creation_ts"] <= end) & (
        ft["resolution_ts"]
        .where(ft["resolution_ts"].notna(), ft["last_update_ts"].where(ft["status"] == "cancelled"))
        .isna()
        | (ft["resolution_ts"].fillna(ft["last_update_ts"]) > end)
    )
    assert row["open_backlog"] == int(open_at_end.sum())
    assert 0 <= row["backlog_past_due"] <= row["open_backlog"]
    # the per-customer and per-service views tile the whole desk
    bc = _q(engine, "SELECT * FROM v_weekly_kpi_by_customer")
    bs = _q(engine, "SELECT * FROM v_weekly_kpi_by_service")
    for part in (bc, bs):
        agg = part.groupby("week_ending")[
            ["tickets_opened", "tickets_resolved", "breached_tickets"]
        ].sum()
        whole = v.set_index("week_ending")[agg.columns]
        pd.testing.assert_frame_equal(agg.astype(int), whole.astype(int), check_names=False)
    assert bc["customer_id"].nunique() == len(tables["dim_customer"])


def test_risk_score_table_and_views_exist(loaded):
    engine, _, _, _ = loaded
    assert db.scalar(engine, "SELECT count(*) FROM ticket_risk_score") == 0
    assert db.scalar(engine, "SELECT count(*) FROM v_ticket_risk") == 0
    assert len(_q(engine, "SELECT * FROM v_risk_band_summary")) == 0
