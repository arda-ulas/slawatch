"""Loads a small generated dataset into PostgreSQL and queries every analytical view.

Needs a reachable Postgres (``make db-up``). The database named in
SLAWATCH_TEST_DATABASE_URL is created if it does not exist. Skipped when unreachable.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from slawatch import db, pipeline

pytestmark = pytest.mark.integration

load_dotenv()
TEST_URL = os.environ.get(
    "SLAWATCH_TEST_DATABASE_URL",
    "postgresql+psycopg://slawatch:slawatch@localhost:5432/slawatch_test",
)


def _ensure_database(url: str) -> bool:
    """Create the test database if missing. False when the server is unreachable."""
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{u.database}"'))
        return True
    except OperationalError:
        return False
    finally:
        admin.dispose()


@pytest.fixture(scope="module")
def loaded(small_raw_dir):
    if not _ensure_database(TEST_URL):
        pytest.skip("PostgreSQL not reachable; run `make db-up`")
    engine = db.get_engine(TEST_URL)
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
    assert len(info["views"]) == 6
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


def test_risk_score_table_and_views_exist(loaded):
    engine, _, _, _ = loaded
    assert db.scalar(engine, "SELECT count(*) FROM ticket_risk_score") == 0
    assert db.scalar(engine, "SELECT count(*) FROM v_ticket_risk") == 0
    assert len(_q(engine, "SELECT * FROM v_risk_band_summary")) == 0
