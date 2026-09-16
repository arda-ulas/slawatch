"""Tableau extracts, Hyper files and the packaged workbook against the test database."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
import pytest

from conftest import load_scored_db
from slawatch import db, tableau
from slawatch.tableau import CALCS, DASHBOARD_SHEETS, FILES, SHEET_NAMES, WORKBOOK_SOURCES

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
    hyper = tableau.write_hyper(frames, out / tableau.HYPER_SUBDIR)
    twb = tableau.write_twb(frames, out.parent / "slawatch.twb", meta)
    tableau.write_twbx(twb, hyper, out.parent / "slawatch.twbx")
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
    # denormalised customer / site attributes for the map and the drill-down
    assert (fact["Customer"] != "").all() and (fact["City"] != "").all()
    assert fact["Latitude"].astype(float).between(41, 60).all()
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


def test_hyper_extracts_hold_every_row_with_sql_types(extracts):
    from tableauhyperapi import Connection, HyperProcess, TableName, Telemetry, TypeTag

    frames, out, _, _ = extracts
    expected = {"date": TypeTag.DATE, "datetime": TypeTag.TIMESTAMP, "integer": TypeTag.BIG_INT}
    with HyperProcess(
        Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_config": ""}
    ) as hp:
        for key in WORKBOOK_SOURCES:
            path = out / tableau.HYPER_SUBDIR / tableau.hyper_name(key)
            with Connection(hp.endpoint, str(path)) as conn:
                table = TableName("Extract", "Extract")
                assert conn.execute_scalar_query(f"SELECT count(*) FROM {table}") == len(
                    frames[key]
                )
                defn = conn.catalog.get_table_definition(table)
                assert [c.name.unescaped for c in defn.columns] == list(frames[key].columns)
                types = tableau.column_types(frames[key])
                for c in defn.columns:
                    t = types[c.name.unescaped]
                    if t in expected:
                        assert c.type.tag == expected[t], c.name
                if key == "fact_ticket":
                    n_open = conn.execute_scalar_query(
                        f'SELECT count(*) FROM {table} WHERE "Resolved At (UTC)" IS NULL'
                    )
                    assert n_open == int((frames[key]["Is Resolved"] == 0).sum())


def test_twb_mirrors_tableau_format_and_the_twbx_packages_its_extracts(extracts):
    frames, out, _, twb = extracts
    root = ET.parse(twb).getroot()
    assert root.tag == "workbook" and root.find("document-format-change-manifest") is not None
    ds = root.findall("datasources/datasource")
    assert [d.get("name").removeprefix("federated.") for d in ds] == WORKBOOK_SOURCES
    for d in ds:
        key = d.get("name").removeprefix("federated.")
        assert "synthetic" in d.get("caption")
        conn = d.find("connection/named-connections/named-connection/connection")
        assert conn.get("class") == "hyper"
        assert conn.get("dbname") == f"{tableau.TWBX_DATA_DIR}/{tableau.hyper_name(key)}"
        rel = d.find("connection/relation")
        assert rel.get("table") == "[Extract].[Extract]"
        records = d.findall("connection/metadata-records/metadata-record")
        assert [r.find("remote-name").text for r in records] == list(frames[key].columns)
        assert all(r.find("parent-name").text == "[Extract]" for r in records)
        calcs = {
            c.get("caption"): c.find("calculation").get("formula")
            for c in d.findall("column")
            if c.find("calculation") is not None
        }
        assert calcs == {c.name: c.formula for c in CALCS.get(key, [])}
    sheets = root.findall("worksheets/worksheet")
    assert [s.get("name") for s in sheets] == SHEET_NAMES
    ds_names = {d.get("name") for d in ds}
    for s in sheets:
        assert list(s)[0].tag == "layout-options"  # the schema wants it before <table>
        assert s.find("table/cols") is not None and s.find("table/rows") is not None
        dep = s.find("table/view/datasource-dependencies")
        assert dep.get("datasource") in ds_names
        # every shelf / filter reference is declared as a dependency of the sheet
        declared = {c.get("name") for c in dep.findall("column-instance")}
        declared |= {c.get("name") for c in dep.findall("column")}
        text = ET.tostring(s, encoding="unicode")
        # "%" is backslash-escaped inside quoted literals (alias keys, members), as Tableau does
        refs = {
            r.replace("\\%", "%") for r in re.findall(r"\[federated\.[a-z_]+\]\.(\[[^\]]+\])", text)
        }
        assert refs - {"[:Measure Names]", "[Multiple Values]"} <= declared, s.get("name")
    dashboards = root.findall("dashboards/dashboard")
    assert len(dashboards) == 1 and "synthetic" in dashboards[0].get("name")
    size = dashboards[0].find("size")
    assert (size.get("minwidth"), size.get("minheight")) == ("1200", "900")
    assert size.get("maxwidth") == "1200" and size.get("maxheight") == "900"
    zone_sheets = {z.get("name") for z in dashboards[0].iter("zone") if z.get("name")}
    assert set(DASHBOARD_SHEETS) <= zone_sheets
    text_zones = [z for z in dashboards[0].iter("zone") if z.get("type-v2") == "text"]
    footer = ET.tostring(text_zones[-1], encoding="unicode")
    assert "Synthetic" in footer and tableau.REPO_URL in footer
    windows = root.findall("windows/window")
    assert windows[0].get("class") == "dashboard" and windows[0].get("maximized") == "true"
    assert "SYNTHETIC" in twb.read_text()[:800]
    with zipfile.ZipFile(twb.with_suffix(".twbx")) as zf:
        names = set(zf.namelist())
    assert twb.name in names
    for key in WORKBOOK_SOURCES:
        assert f"{tableau.TWBX_DATA_DIR}/{tableau.hyper_name(key)}" in names
