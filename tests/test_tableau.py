"""Tableau extracts and the best-effort .twb against the test database (small sample + scores)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pandas as pd
import pytest

from conftest import load_scored_db
from slawatch import db, tableau
from slawatch.tableau import CALCS, FILES, SHEETS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def scored_engine(small_raw_dir, trained):
    engine = load_scored_db(small_raw_dir, trained)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def extracts(scored_engine, tmp_path_factory):
    out = tmp_path_factory.mktemp("tableau") / "data"
    frames = tableau.build_extracts(scored_engine)
    meta = tableau.write_extracts(frames, out, scored_engine)
    twb = tableau.write_twb(frames, out.parent / "slawatch.twb", data_dir="data")
    return frames, out, meta, twb


def test_every_file_is_written_and_labelled_synthetic(extracts):
    frames, out, meta, _ = extracts
    for key, name in FILES.items():
        assert (out / name).exists(), name
        assert "synthetic" in name or key == "metadata"
    assert meta["synthetic"] is True and meta["model_version"] == "test-quick"
    assert set(meta["files"]) == {v for k, v in FILES.items() if k != "metadata"}
    assert meta["files"][FILES["fact_ticket"]]["committed"] is False
    on_disk = json.loads((out / FILES["metadata"]).read_text())
    assert on_disk["files"] == meta["files"]


def test_fact_extract_columns_dates_and_flags(extracts, scored_engine):
    frames, out, _, _ = extracts
    fact = pd.read_csv(out / FILES["fact_ticket"], keep_default_na=False)
    assert len(fact) == db.scalar(scored_engine, "SELECT count(*) FROM ticket_risk_score")
    assert fact["Ticket ID"].is_unique
    assert fact["Created At (UTC)"].str.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}").all()
    open_rows = fact["Resolved At (UTC)"] == ""
    assert open_rows.sum() == int((fact["Is Resolved"] == 0).sum())
    for col in (
        "Tier",
        "Region",
        "Service Type",
        "Severity",
        "Channel",
        "Risk Band",
        "Model Split",
        "SLA Outcome",
        "During Outage",
    ):
        assert (fact[col] != "").all(), col
        assert not fact[col].str.contains("_").any(), col  # human-readable labels
    assert set(fact["Risk Band"]) <= {"Low", "Medium", "High"}
    assert set(fact["SLA Outcome"]) <= {"Met", "Breached", "Pending", "Cancelled"}
    assert set(fact["Is Breached"]) <= {0, 1} and set(fact["Has Outcome"]) <= {0, 1}
    assert (fact["Is Breached"] <= fact["Has Outcome"]).all()
    assert fact["Breach Probability"].astype(float).between(0, 1).all()
    n_breached = db.scalar(scored_engine, "SELECT count(*) FROM fact_ticket WHERE sla_breached")
    assert fact["Is Breached"].sum() == n_breached


def test_monthly_compliance_matches_view(extracts, scored_engine):
    _, out, _, _ = extracts
    m = pd.read_csv(out / FILES["monthly_compliance"])
    v = db.query(scored_engine, "SELECT * FROM v_sla_compliance_monthly")
    assert len(m) == len(v)
    assert m["Breached Tickets"].sum() == v["breached_tickets"].sum()
    assert m["Month"].str.fullmatch(r"\d{4}-\d{2}-01").all()
    assert m[["Tickets", "Tickets With Outcome", "Breached Tickets"]].notna().all().all()
    assert m["Customer"].nunique() == db.scalar(scored_engine, "SELECT count(*) FROM dim_customer")


def test_backlog_series_is_a_dense_grid(extracts, scored_engine):
    _, out, _, _ = extracts
    b = pd.read_csv(out / FILES["backlog_ageing"])
    months = b["Snapshot Date"].nunique()
    services = b["Service Type"].nunique()
    assert len(b) == months * services * len(tableau.AGE_BANDS)
    assert b["Open Tickets"].notna().all() and (b["Open Tickets"] >= 0).all()
    per_month = b.groupby("Snapshot Date")["Open Tickets"].sum()
    view = db.query(
        scored_engine,
        "SELECT as_of, sum(open_tickets) AS n FROM v_backlog_ageing_monthly GROUP BY 1",
    )
    view["as_of"] = pd.to_datetime(view["as_of"], utc=True).dt.strftime("%Y-%m-%d")
    assert per_month.to_dict() == view.set_index("as_of")["n"].astype(int).to_dict()
    assert (b["Past Due Tickets"] <= b["Open Tickets"]).all()


def test_risk_deciles_cover_the_test_split(extracts, scored_engine):
    _, out, _, _ = extracts
    d = pd.read_csv(out / FILES["risk_deciles"])
    assert d["Decile"].tolist() == list(range(1, 11))
    n_test = db.scalar(
        scored_engine,
        "SELECT count(*) FROM v_ticket_risk WHERE split = 'test' AND sla_breached IS NOT NULL",
    )
    assert d["Tickets"].sum() == n_test
    assert d["Tickets"].max() - d["Tickets"].min() <= 1
    assert (d["Probability From"].diff().dropna() >= 0).all()
    assert d["Observed Breach Rate"].between(0, 1).all()
    assert (d["Breached"] / d["Tickets"]).round(4).tolist() == d["Observed Breach Rate"].tolist()
    # the model ranks: the top decile breaches more than the bottom one
    assert d["Observed Breach Rate"].iloc[-1] > d["Observed Breach Rate"].iloc[0]


def test_dimensions_and_geography(extracts, scored_engine):
    _, out, _, _ = extracts
    sites = pd.read_csv(out / FILES["dim_site"])
    assert len(sites) == db.scalar(scored_engine, "SELECT count(*) FROM dim_site")
    assert sites[["Latitude", "Longitude"]].notna().all().all()
    assert sites["Latitude"].between(41, 60).all() and sites["Longitude"].between(-135, -52).all()
    assert sites["Province Name"].notna().all()
    customers = pd.read_csv(out / FILES["dim_customer"])
    assert customers["Customer ID"].is_unique
    assert set(customers["Tier"]) <= {"Platinum", "Gold", "Silver", "Bronze"}
    services = pd.read_csv(out / FILES["dim_service"])
    assert services["Service ID"].is_unique
    assert set(services["Site ID"]) <= set(sites["Site ID"])
    outages = pd.read_csv(out / FILES["dim_outage"])
    assert len(outages) == db.scalar(scored_engine, "SELECT count(*) FROM outage_incident")
    assert outages["Start Date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}").all()
    weekly = pd.read_csv(out / FILES["weekly_kpi"])
    assert len(weekly) == db.scalar(scored_engine, "SELECT count(*) FROM v_weekly_kpi")
    assert weekly[["Tickets Opened", "Open Backlog", "Backlog Past Due"]].notna().all().all()


def test_single_workbook_for_web_authoring(extracts):
    frames, out, _, _ = extracts
    path = tableau.write_xlsx(frames, out)
    sheets = pd.read_excel(path, sheet_name=None)
    assert len(sheets) == len(frames)
    assert len(sheets["fact_ticket"]) == len(frames["fact_ticket"])
    assert list(sheets["risk_calibration_deciles"].columns) == list(frames["risk_deciles"].columns)


def test_twb_is_well_formed_and_references_relative_csvs(extracts):
    frames, out, _, twb = extracts
    root = ET.parse(twb).getroot()
    assert root.tag == "workbook"
    ds = root.findall("datasources/datasource")
    assert len(ds) == len(FILES) - 1
    for d in ds:
        conn = d.find("connection/named-connections/named-connection/connection")
        assert conn.get("class") == "textscan" and conn.get("directory") == "data"
        assert (out / conn.get("filename")).exists()
        key = d.get("name").removeprefix("federated.")
        listed = [c.get("name") for c in d.findall("connection/relation/columns/column")]
        assert listed == list(frames[key].columns)
        calcs = {
            c.get("caption"): c.find("calculation").get("formula")
            for c in d.findall("column")
            if c.find("calculation") is not None
        }
        assert calcs == {c.name: c.formula for c in CALCS.get(key, [])}
    sheets = root.findall("worksheets/worksheet")
    assert [s.get("name") for s in sheets] == [s.name for s in SHEETS]
    for s in sheets:
        assert s.find("table/rows").text and s.find("table/cols").text
        dep = s.find("table/view/datasource-dependencies")
        assert dep.get("datasource") in {d.get("name") for d in ds}
    assert "SYNTHETIC" in twb.read_text()[:600]
