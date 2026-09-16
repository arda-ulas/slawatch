"""Tableau Public extracts (CSV) and a best-effort workbook (.twb) from the PostgreSQL views.

Usage:
    uv run slawatch-tableau                       # -> tableau/data/*.csv + tableau/slawatch.twb
    uv run slawatch-tableau --out tableau/data --no-twb

Tableau Public only reads files, so everything the dashboard needs is materialised here with
human-readable column labels, ISO dates and no nulls in the columns a chart would group or sum
by. All of it is synthetic; every file name says so and ``extract_metadata.json`` records the
generation. See tableau/README.md for the dashboard build spec.
"""

from __future__ import annotations

import argparse
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from slawatch import __version__, db
from slawatch.labels import (
    SPLIT_LABELS,
    channel_label,
    group_label,
    humanise,
    service_label,
)

log = logging.getLogger("slawatch.tableau")

DEFAULT_OUT_DIR = Path("tableau/data")
DEFAULT_TWB = Path("tableau/slawatch.twb")
XLSX_NAME = (
    "slawatch_synthetic.xlsx"  # every extract as one sheet, for Tableau Public web authoring
)

FILES = {
    "fact_ticket": "fact_ticket_synthetic.csv",
    "monthly_compliance": "monthly_sla_compliance_synthetic.csv",
    "backlog_ageing": "backlog_ageing_monthly_synthetic.csv",
    "risk_deciles": "risk_calibration_deciles_synthetic.csv",
    "weekly_kpi": "weekly_kpi_synthetic.csv",
    "dim_customer": "dim_customer_synthetic.csv",
    "dim_site": "dim_site_synthetic.csv",
    "dim_service": "dim_service_synthetic.csv",
    "dim_outage": "dim_outage_synthetic.csv",
    "metadata": "extract_metadata.json",
}
# The ticket-level fact is regenerated (gitignored); everything else is committed.
COMMITTED = [k for k in FILES if k != "fact_ticket"]

AGE_BANDS = ["0-24h", "1-3d", "3-7d", "7-30d", "30d+"]
RISK_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}

# Approximate city centroids (degrees) for the synthetic sites; enough for a filled/symbol map.
CITY_COORDS: dict[tuple[str, str], tuple[float, float]] = {
    ("Toronto", "ON"): (43.6532, -79.3832),
    ("Ottawa", "ON"): (45.4215, -75.6972),
    ("Mississauga", "ON"): (43.5890, -79.6441),
    ("Hamilton", "ON"): (43.2557, -79.8711),
    ("London", "ON"): (42.9849, -81.2453),
    ("Kitchener", "ON"): (43.4516, -80.4925),
    ("Windsor", "ON"): (42.3149, -83.0364),
    ("Kingston", "ON"): (44.2312, -76.4860),
    ("Sudbury", "ON"): (46.4917, -80.9930),
    ("Montreal", "QC"): (45.5017, -73.5673),
    ("Quebec City", "QC"): (46.8139, -71.2080),
    ("Laval", "QC"): (45.6066, -73.7124),
    ("Gatineau", "QC"): (45.4765, -75.7013),
    ("Sherbrooke", "QC"): (45.4042, -71.8929),
    ("Vancouver", "BC"): (49.2827, -123.1207),
    ("Surrey", "BC"): (49.1913, -122.8490),
    ("Victoria", "BC"): (48.4284, -123.3656),
    ("Kelowna", "BC"): (49.8880, -119.4960),
    ("Calgary", "AB"): (51.0447, -114.0719),
    ("Edmonton", "AB"): (53.5461, -113.4938),
    ("Red Deer", "AB"): (52.2681, -113.8112),
    ("Winnipeg", "MB"): (49.8951, -97.1384),
    ("Regina", "SK"): (50.4452, -104.6189),
    ("Saskatoon", "SK"): (52.1332, -106.6700),
    ("Halifax", "NS"): (44.6488, -63.5752),
    ("Moncton", "NB"): (46.0878, -64.7782),
    ("Fredericton", "NB"): (45.9636, -66.6431),
    ("St. John's", "NL"): (47.5615, -52.7126),
    ("Charlottetown", "PE"): (46.2382, -63.1311),
}
PROVINCE_NAMES = {
    "ON": "Ontario",
    "QC": "Quebec",
    "BC": "British Columbia",
    "AB": "Alberta",
    "SK": "Saskatchewan",
    "MB": "Manitoba",
    "NS": "Nova Scotia",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "PE": "Prince Edward Island",
}

TS = "%Y-%m-%d %H:%M:%S"


# ------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------
def _ts(s: pd.Series) -> pd.Series:
    """tz-aware timestamps -> 'YYYY-MM-DD HH:MM:SS' in UTC; NULL -> empty string."""
    t = pd.to_datetime(s, utc=True)
    return t.dt.strftime(TS).fillna("")


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.strftime("%Y-%m-%d")


def _num(s: pd.Series, decimals: int | None = None) -> pd.Series:
    out = pd.to_numeric(s, errors="coerce")
    return out.round(decimals) if decimals is not None else out


def _int(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0).astype(int)


# ------------------------------------------------------------------------------------------
# Extracts
# ------------------------------------------------------------------------------------------
def fact_ticket(engine: Engine) -> pd.DataFrame:
    df = db.query(
        engine,
        """
        SELECT r.ticket_id, r.creation_ts, r.customer_id, t.site_id, t.service_id,
               r.tier, r.region, r.service_type, r.assignment_group, r.severity, r.ticket_type,
               r.channel, r.status, r.has_active_outage, t.active_outage_id,
               r.sla_target_hours, t.resolution_ts, r.resolution_hours, t.is_resolved,
               r.sla_breached, r.probability, r.risk_band, r.split
        FROM v_ticket_risk r
        JOIN fact_ticket t USING (ticket_id)
        ORDER BY r.creation_ts, r.ticket_id
        """,
    )
    outcome = pd.Series("Pending", index=df.index)
    outcome[df["status"] == "cancelled"] = "Cancelled"
    outcome[df["sla_breached"].eq(True)] = "Breached"
    outcome[df["sla_breached"].eq(False)] = "Met"
    return pd.DataFrame(
        {
            "Ticket ID": df["ticket_id"],
            "Created At (UTC)": _ts(df["creation_ts"]),
            "Customer ID": df["customer_id"],
            "Site ID": df["site_id"],
            "Service ID": df["service_id"],
            "Tier": df["tier"].map(humanise),
            "Region": df["region"].map(humanise),
            "Service Type": df["service_type"].map(service_label),
            "Assignment Group": df["assignment_group"].map(group_label),
            "Severity": df["severity"].map(humanise),
            "Ticket Type": df["ticket_type"].map(humanise),
            "Channel": df["channel"].map(channel_label),
            "Status": df["status"].map(humanise),
            "During Outage": df["has_active_outage"].map({True: "Yes", False: "No"}),
            "Outage ID": df["active_outage_id"].fillna(""),
            "SLA Target Hours": _num(df["sla_target_hours"], 2),
            "Resolved At (UTC)": _ts(df["resolution_ts"]),
            "Resolution Hours": _num(df["resolution_hours"], 2),
            "Is Resolved": df["is_resolved"].astype(int),
            "SLA Outcome": outcome,
            "Has Outcome": df["sla_breached"].notna().astype(int),
            "Is Breached": df["sla_breached"].eq(True).astype(int),
            "Breach Probability": _num(df["probability"], 6),
            "Risk Band": df["risk_band"].map(RISK_LABELS),
            "Model Split": df["split"].map(SPLIT_LABELS),
        }
    )


def monthly_compliance(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM v_sla_compliance_monthly ORDER BY month, customer_id")
    return pd.DataFrame(
        {
            "Month": _date(df["month"]),
            "Customer ID": df["customer_id"],
            "Customer": df["customer_name"],
            "Tier": df["tier"].map(humanise),
            "Industry": df["industry"].map(humanise),
            "Service Type": df["service_type"].map(service_label),
            "Tickets": _int(df["tickets"]),
            "Resolved Tickets": _int(df["resolved_tickets"]),
            "Tickets With Outcome": _int(df["tickets_with_outcome"]),
            "Breached Tickets": _int(df["breached_tickets"]),
            "SLA Compliance Pct": _num(df["sla_compliance_pct"], 2),
            "P50 Resolution Hours": _num(df["p50_resolution_hours"], 2),
            "Avg Target Utilisation": _num(df["avg_target_utilisation"], 3),
        }
    )


def backlog_ageing(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM v_backlog_ageing_monthly_by_service")
    df["as_of"] = pd.to_datetime(df["as_of"], utc=True).dt.strftime("%Y-%m-%d")
    # Dense grid (snapshot x service x band) so a stacked area has no gaps.
    idx = pd.MultiIndex.from_product(
        [sorted(df["as_of"].unique()), sorted(df["service_type"].unique()), AGE_BANDS],
        names=["as_of", "service_type", "age_bucket"],
    )
    dense = df.set_index(["as_of", "service_type", "age_bucket"]).reindex(idx).reset_index()
    dense["bucket_order"] = dense["age_bucket"].map({b: i + 1 for i, b in enumerate(AGE_BANDS)})
    return pd.DataFrame(
        {
            "Snapshot Date": dense["as_of"],
            "Service Type": dense["service_type"].map(service_label),
            "Age Band": dense["age_bucket"],
            "Age Band Order": dense["bucket_order"].astype(int),
            "Open Tickets": _int(dense["open_tickets"]),
            "Past Due Tickets": _int(dense["past_due_tickets"]),
            "Critical Or Major": _int(dense["critical_or_major"]),
            "High Risk Tickets": _int(dense["high_risk_tickets"]),
        }
    )


def risk_deciles(engine: Engine) -> pd.DataFrame:
    df = db.query(
        engine,
        "SELECT probability, sla_breached FROM v_ticket_risk "
        "WHERE split = 'test' AND sla_breached IS NOT NULL",
    )
    if df.empty:
        return pd.DataFrame(
            columns=[
                "Decile",
                "Probability From",
                "Probability To",
                "Mean Predicted",
                "Tickets",
                "Breached",
                "Observed Breach Rate",
                "Model Split",
            ]
        )
    p = df["probability"].astype(float)
    # rank-based deciles: equal-count bins even when probabilities tie
    df["decile"] = (p.rank(method="first") - 1).floordiv(len(p) / 10).astype(int).clip(0, 9) + 1
    g = df.groupby("decile")
    out = pd.DataFrame(
        {
            "Decile": g.size().index,
            "Probability From": g["probability"].min().astype(float).round(4).to_numpy(),
            "Probability To": g["probability"].max().astype(float).round(4).to_numpy(),
            "Mean Predicted": g["probability"].mean().astype(float).round(4).to_numpy(),
            "Tickets": g.size().to_numpy(),
            "Breached": g["sla_breached"].apply(lambda s: int(s.eq(True).sum())).to_numpy(),
        }
    )
    out["Observed Breach Rate"] = (out["Breached"] / out["Tickets"]).round(4)
    out["Model Split"] = "Test"
    return out


def weekly_kpi(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM v_weekly_kpi ORDER BY week_ending")
    return pd.DataFrame(
        {
            "Week Start": _date(df["week_start"]),
            "Week Ending": _date(df["week_ending"]),
            "Tickets Opened": _int(df["tickets_opened"]),
            "Tickets Resolved": _int(df["tickets_resolved"]),
            "Breached Tickets": _int(df["breached_tickets"]),
            "SLA Compliance Pct": _num(df["sla_compliance_pct"], 2),
            "MTTR Hours": _num(df["mttr_hours"], 2),
            "P50 Resolution Hours": _num(df["p50_resolution_hours"], 2),
            "P90 Resolution Hours": _num(df["p90_resolution_hours"], 2),
            "Open Backlog": _int(df["open_backlog"]),
            "Backlog Past Due": _int(df["backlog_past_due"]),
            "High Risk Open": _int(df["high_risk_open"]),
        }
    )


def dim_customer(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM dim_customer ORDER BY customer_id")
    return pd.DataFrame(
        {
            "Customer ID": df["customer_id"],
            "Customer": df["customer_name"],
            "Industry": df["industry"].map(humanise),
            "Tier": df["tier"].map(humanise),
            "HQ Province": df["hq_province"],
            "HQ Province Name": df["hq_province"].map(PROVINCE_NAMES),
        }
    )


def dim_site(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM dim_site ORDER BY site_id")
    keys = list(zip(df["city"], df["province"], strict=True))
    coords = [CITY_COORDS.get(k) for k in keys]
    missing = sorted({k for k, xy in zip(keys, coords, strict=True) if xy is None})
    if missing:
        log.warning("no centroid for %s; Latitude/Longitude left empty", missing)
    return pd.DataFrame(
        {
            "Site ID": df["site_id"],
            "Customer ID": df["customer_id"],
            "Site": df["site_name"],
            "City": df["city"],
            "Province": df["province"],
            "Province Name": df["province"].map(PROVINCE_NAMES),
            "Region": df["region"].map(humanise),
            "Latitude": [xy[0] if xy else None for xy in coords],
            "Longitude": [xy[1] if xy else None for xy in coords],
        }
    )


def dim_service(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM dim_service ORDER BY service_id")
    return pd.DataFrame(
        {
            "Service ID": df["service_id"],
            "Site ID": df["site_id"],
            "Customer ID": df["customer_id"],
            "Service Type": df["service_type"].map(service_label),
            "Bandwidth Mbps": _num(df["bandwidth_mbps"]).astype("Int64"),
            "Assignment Group": df["assignment_group"].map(group_label),
        }
    )


def dim_outage(engine: Engine) -> pd.DataFrame:
    df = db.query(engine, "SELECT * FROM v_outage_impact ORDER BY start_ts")
    return pd.DataFrame(
        {
            "Outage ID": df["outage_id"],
            "Cause": df["cause"].map(humanise),
            "Region": df["region"].map(humanise),
            "Service Type": df["service_type"].map(service_label),
            "Start (UTC)": _ts(df["start_ts"]),
            "End (UTC)": _ts(df["end_ts"]),
            "Start Date": pd.to_datetime(df["start_ts"], utc=True).dt.strftime("%Y-%m-%d"),
            "Duration Hours": _num(df["duration_hours"], 1),
            "Tickets During": _int(df["tickets_during"]),
            "Volume Multiplier": _num(df["volume_multiplier"], 1),
            "Breach Rate During Pct": _num(df["breach_rate_during_pct"], 1),
            "Breach Rate Baseline Pct": _num(df["breach_rate_baseline_pct"], 1),
        }
    )


BUILDERS = {
    "fact_ticket": fact_ticket,
    "monthly_compliance": monthly_compliance,
    "backlog_ageing": backlog_ageing,
    "risk_deciles": risk_deciles,
    "weekly_kpi": weekly_kpi,
    "dim_customer": dim_customer,
    "dim_site": dim_site,
    "dim_service": dim_service,
    "dim_outage": dim_outage,
}


def build_extracts(engine: Engine) -> dict[str, pd.DataFrame]:
    frames = {}
    for key, fn in BUILDERS.items():
        frames[key] = fn(engine)
        log.info("%s: %d rows", FILES[key], len(frames[key]))
    return frames


def write_extracts(
    frames: dict[str, pd.DataFrame], out_dir: Path, engine: Engine | None = None
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, int]] = {}
    for key, df in frames.items():
        path = out_dir / FILES[key]
        df.to_csv(path, index=False, lineterminator="\n")
        files[FILES[key]] = {
            "rows": int(len(df)),
            "bytes": path.stat().st_size,
            "committed": key in COMMITTED,
        }
    meta: dict[str, Any] = {
        "synthetic": True,
        "disclaimer": "Synthetic data generated by slawatch. No real customers, sites or tickets.",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "generator": f"slawatch-tableau {__version__}",
        "files": files,
    }
    if engine is not None:
        meta["load_snapshot_ts"] = str(db.scalar(engine, "SELECT max(snapshot_ts) FROM load_run"))
        meta["model_version"] = db.scalar(
            engine, "SELECT min(model_version) FROM ticket_risk_score"
        )
    (out_dir / FILES["metadata"]).write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def write_xlsx(frames: dict[str, pd.DataFrame], out_dir: Path) -> Path:
    """One workbook, one sheet per extract, so web authoring can relate the tables."""
    path = out_dir / XLSX_NAME
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for key, df in frames.items():
            df.to_excel(xw, sheet_name=FILES[key][:-4].removesuffix("_synthetic")[:31], index=False)
    log.info("wrote %s (%d sheets, %.1f MB)", path, len(frames), path.stat().st_size / 1e6)
    return path


# ------------------------------------------------------------------------------------------
# Workbook (.twb) - best effort, not validated in Tableau
# ------------------------------------------------------------------------------------------
# Tableau's remote-type codes for text-file columns.
_REMOTE_TYPE = {"string": 129, "integer": 20, "real": 5, "datetime": 7, "date": 133}


def _col_type(series: pd.Series, name: str) -> str:
    if name.endswith("(UTC)"):
        return "datetime"
    if name in ("Month", "Snapshot Date", "Week Start", "Week Ending", "Start Date"):
        return "date"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "real"
    return "string"


@dataclass(frozen=True)
class CalcField:
    name: str
    formula: str
    datatype: str = "real"
    role: str = "measure"


# Calculated fields, keyed by datasource. Same formulas as in tableau/README.md.
CALCS: dict[str, list[CalcField]] = {
    "fact_ticket": [
        CalcField("SLA Compliance %", "1 - SUM([Is Breached]) / SUM([Has Outcome])"),
        CalcField("Breach Rate %", "SUM([Is Breached]) / SUM([Has Outcome])"),
        CalcField("Tickets", "COUNTD([Ticket ID])", "integer"),
        CalcField("Breaches", "SUM([Is Breached])", "integer"),
        CalcField(
            "Open Tickets", "SUM(IIF([Status] = 'Cancelled', 0, 1 - [Is Resolved]))", "integer"
        ),
        CalcField("Resolved Tickets", "SUM([Is Resolved])", "integer"),
        CalcField("Median Resolution Hours", "MEDIAN([Resolution Hours])"),
        CalcField("P90 Resolution Hours", "PERCENTILE([Resolution Hours], 0.9)"),
        CalcField("High Risk Tickets", "SUM(IIF([Risk Band] = 'High', 1, 0))", "integer"),
        CalcField(
            "Probability Decile", "INT([Breach Probability] * 10) + 1", "integer", "dimension"
        ),
        CalcField("Created Week", "DATETRUNC('week', [Created At (UTC)])", "date", "dimension"),
        CalcField("Created Month", "DATETRUNC('month', [Created At (UTC)])", "date", "dimension"),
    ],
    "monthly_compliance": [
        CalcField("Compliance %", "1 - SUM([Breached Tickets]) / SUM([Tickets With Outcome])"),
        CalcField("Breach Rate %", "SUM([Breached Tickets]) / SUM([Tickets With Outcome])"),
    ],
    "backlog_ageing": [
        CalcField("Past Due Share", "SUM([Past Due Tickets]) / SUM([Open Tickets])"),
    ],
    "risk_deciles": [
        CalcField("Calibration Gap", "SUM([Observed Breach Rate]) - SUM([Mean Predicted])"),
    ],
    "weekly_kpi": [
        CalcField("Compliance %", "1 - SUM([Breached Tickets]) / SUM([Tickets Resolved])"),
    ],
}

CAPTIONS = {
    "fact_ticket": "Tickets (synthetic)",
    "monthly_compliance": "Monthly SLA compliance (synthetic)",
    "backlog_ageing": "Backlog ageing (synthetic)",
    "risk_deciles": "Risk calibration deciles (synthetic)",
    "weekly_kpi": "Weekly KPI (synthetic)",
    "dim_customer": "Customers (synthetic)",
    "dim_site": "Sites (synthetic)",
    "dim_service": "Services (synthetic)",
    "dim_outage": "Outages (synthetic)",
}


def _datasource_xml(key: str, df: pd.DataFrame, data_dir: str) -> ET.Element:
    filename = FILES[key]
    stem = filename[:-4]
    ds_name = f"federated.{key}"
    conn_name = f"textscan.{key}"
    ds = ET.Element(
        "datasource", caption=CAPTIONS[key], inline="true", name=ds_name, version="18.1"
    )
    conn = ET.SubElement(ds, "connection", {"class": "federated"})
    ncs = ET.SubElement(conn, "named-connections")
    nc = ET.SubElement(ncs, "named-connection", caption=stem, name=conn_name)
    ET.SubElement(
        nc,
        "connection",
        {
            "class": "textscan",
            "directory": data_dir,
            "filename": filename,
            "password": "",
            "server": "",
        },
    )
    rel = ET.SubElement(
        conn, "relation", connection=conn_name, name=filename, table=f"[{stem}#csv]", type="table"
    )
    cols = ET.SubElement(
        rel,
        "columns",
        {"character-set": "UTF-8", "header": "yes", "locale": "en_US", "separator": ","},
    )
    types = {name: _col_type(df[name], name) for name in df.columns}
    for i, name in enumerate(df.columns):
        ET.SubElement(cols, "column", datatype=types[name], name=name, ordinal=str(i))
    mrs = ET.SubElement(conn, "metadata-records")
    for i, name in enumerate(df.columns):
        mr = ET.SubElement(mrs, "metadata-record", {"class": "column"})
        ET.SubElement(mr, "remote-name").text = name
        ET.SubElement(mr, "remote-type").text = str(_REMOTE_TYPE[types[name]])
        ET.SubElement(mr, "local-name").text = f"[{name}]"
        ET.SubElement(mr, "parent-name").text = f"[{filename}]"
        ET.SubElement(mr, "remote-alias").text = name
        ET.SubElement(mr, "ordinal").text = str(i)
        ET.SubElement(mr, "local-type").text = types[name]
        ET.SubElement(mr, "aggregation").text = (
            "Sum" if types[name] in ("integer", "real") else "Count"
        )
        ET.SubElement(mr, "contains-null").text = "true"
    for name in df.columns:
        t = types[name]
        measure = (
            t in ("integer", "real")
            and not name.endswith("ID")
            and name not in ("Age Band Order", "Decile")
        )
        ET.SubElement(
            ds,
            "column",
            datatype=t,
            name=f"[{name}]",
            role="measure" if measure else "dimension",
            type="quantitative"
            if measure
            else ("ordinal" if t in ("date", "datetime") else "nominal"),
        )
    for calc in CALCS.get(key, []):
        c = ET.SubElement(
            ds,
            "column",
            caption=calc.name,
            datatype=calc.datatype,
            name=f"[{calc.name}]",
            role=calc.role,
            type="quantitative" if calc.role == "measure" else "ordinal",
        )
        ET.SubElement(c, "calculation", {"class": "tableau", "formula": calc.formula})
    return ds


@dataclass(frozen=True)
class SheetSpec:
    name: str
    datasource: str
    mark: str
    rows: list[tuple[str, str]]  # (field, shelf encoding) e.g. ("Week Ending", "tmn:qk")
    cols: list[tuple[str, str]]
    color: tuple[str, str] | None = None
    title: str = ""


def _instance(field: str, enc: str) -> tuple[str, str]:
    """Shelf item name and its column-instance attributes for a field + encoding.

    enc: 'none:nk' discrete dimension, 'sum:qk' / 'avg:qk' continuous measure,
         'tmn:qk' continuous month, 'twk:qk' continuous week, 'usr:qk' calculated field.
    """
    deriv, kind = enc.split(":")
    derivation = {
        "none": "None",
        "sum": "Sum",
        "avg": "Avg",
        "tmn": "Month-Trunc",
        "twk": "Week-Trunc",
        "usr": "User",
    }[deriv]
    return f"[{deriv}:{field}:{kind}]", derivation


SHEETS: list[SheetSpec] = [
    SheetSpec(
        "Ticket volume by week",
        "weekly_kpi",
        "Line",
        rows=[("Tickets Opened", "sum:qk")],
        cols=[("Week Ending", "twk:qk")],
        title="Tickets opened per week (synthetic data)",
    ),
    SheetSpec(
        "SLA compliance heatmap",
        "monthly_compliance",
        "Square",
        rows=[("Customer", "none:nk")],
        cols=[("Service Type", "none:nk")],
        color=("Compliance %", "usr:qk"),
        title="SLA compliance by customer x service type (synthetic data)",
    ),
    SheetSpec(
        "Backlog ageing over time",
        "backlog_ageing",
        "Area",
        rows=[("Open Tickets", "sum:qk")],
        cols=[("Snapshot Date", "tmn:qk")],
        color=("Age Band", "none:nk"),
        title="Open backlog by age band, first of month (synthetic data)",
    ),
    SheetSpec(
        "Risk calibration",
        "risk_deciles",
        "Bar",
        rows=[("Observed Breach Rate", "sum:qk")],
        cols=[("Decile", "none:nk")],
        title="Observed breach rate by predicted-probability decile, test months (synthetic data)",
    ),
    SheetSpec(
        "Weekly compliance",
        "weekly_kpi",
        "Line",
        rows=[("Compliance %", "usr:qk")],
        cols=[("Week Ending", "twk:qk")],
        title="Weekly SLA compliance (synthetic data)",
    ),
]


def _worksheet_xml(spec: SheetSpec, frames: dict[str, pd.DataFrame]) -> ET.Element:
    ds_name = f"federated.{spec.datasource}"
    df = frames[spec.datasource]
    calc_names = {c.name: c for c in CALCS.get(spec.datasource, [])}
    ws = ET.Element("worksheet", name=spec.name)
    table = ET.SubElement(ws, "table")
    view = ET.SubElement(table, "view")
    dss = ET.SubElement(view, "datasources")
    ET.SubElement(dss, "datasource", caption=CAPTIONS[spec.datasource], name=ds_name)
    deps = ET.SubElement(view, "datasource-dependencies", datasource=ds_name)
    used = list(spec.rows) + list(spec.cols) + ([spec.color] if spec.color else [])
    for field, enc in used:
        if field in calc_names:
            c = calc_names[field]
            col = ET.SubElement(
                deps,
                "column",
                caption=c.name,
                datatype=c.datatype,
                name=f"[{c.name}]",
                role=c.role,
                type="quantitative" if c.role == "measure" else "ordinal",
            )
            ET.SubElement(col, "calculation", {"class": "tableau", "formula": c.formula})
        else:
            t = _col_type(df[field], field)
            measure = enc.startswith(("sum", "avg"))
            ET.SubElement(
                deps,
                "column",
                datatype=t,
                name=f"[{field}]",
                role="measure" if measure else "dimension",
                type="quantitative"
                if measure
                else ("ordinal" if t in ("date", "datetime") else "nominal"),
            )
        inst, derivation = _instance(field, enc)
        kind = enc.split(":")[1]
        ET.SubElement(
            deps,
            "column-instance",
            column=f"[{field}]",
            derivation=derivation,
            name=inst,
            pivot="key",
            type="quantitative" if kind == "qk" else "nominal",
        )
    ET.SubElement(view, "aggregation", value="true")
    ET.SubElement(table, "style")
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane")
    pv = ET.SubElement(pane, "view")
    ET.SubElement(pv, "breakdown", value="auto")
    ET.SubElement(pane, "mark", {"class": spec.mark})
    if spec.color:
        enc_el = ET.SubElement(pane, "encodings")
        inst, _ = _instance(*spec.color)
        ET.SubElement(enc_el, "color", column=f"[{ds_name}].{inst}")
    rows = " / ".join(f"[{ds_name}].{_instance(f, e)[0]}" for f, e in spec.rows)
    cols = " / ".join(f"[{ds_name}].{_instance(f, e)[0]}" for f, e in spec.cols)
    ET.SubElement(table, "rows").text = rows
    ET.SubElement(table, "cols").text = cols
    if spec.title:
        layout = ET.SubElement(ws, "layout-options")
        title = ET.SubElement(layout, "title")
        fmt = ET.SubElement(title, "formatted-text")
        ET.SubElement(fmt, "run").text = spec.title
    return ws


def build_twb(
    frames: dict[str, pd.DataFrame], data_dir: str = "data", include_sheets: bool = True
) -> ET.ElementTree:
    root = ET.Element(
        "workbook",
        {
            "original-version": "18.1",
            "source-build": "2023.1.0 (20231.23.0116.1105)",
            "source-platform": "mac",
            "version": "18.1",
            "xmlns:user": "http://www.tableausoftware.com/xml/user",
        },
    )
    root.insert(
        0,
        ET.Comment(
            " SYNTHETIC DATA. Generated by slawatch-tableau; not opened or validated in Tableau. "
            "Data sources reference the CSVs in ./data relative to this file. "
        ),
    )
    prefs = ET.SubElement(root, "preferences")
    ET.SubElement(prefs, "preference", name="ui.encoding.shelf.height", value="24")
    dss = ET.SubElement(root, "datasources")
    for key in FILES:
        if key == "metadata":
            continue
        dss.append(_datasource_xml(key, frames[key], data_dir))
    if include_sheets:
        wss = ET.SubElement(root, "worksheets")
        for spec in SHEETS:
            wss.append(_worksheet_xml(spec, frames))
        windows = ET.SubElement(root, "windows")
        for i, spec in enumerate(SHEETS):
            w = ET.SubElement(windows, "window", {"class": "worksheet", "name": spec.name})
            if i == 0:
                w.set("maximized", "true")
            cards = ET.SubElement(w, "cards")
            edge = ET.SubElement(cards, "edge", name="left")
            strip = ET.SubElement(edge, "strip", size="160")
            for card in ("pages", "filters", "marks"):
                ET.SubElement(strip, "card", type=card)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return tree


def write_twb(
    frames: dict[str, pd.DataFrame], path: Path, data_dir: str = "data", include_sheets: bool = True
) -> Path:
    tree = build_twb(frames, data_dir=data_dir, include_sheets=include_sheets)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    log.info("wrote %s (%d bytes)", path, path.stat().st_size)
    return path


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tableau Public extracts from the slawatch views.")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="extract directory")
    p.add_argument("--twb", type=Path, default=DEFAULT_TWB, help="workbook path (best effort)")
    p.add_argument("--no-twb", action="store_true", help="skip the .twb")
    p.add_argument(
        "--xlsx",
        action="store_true",
        help=f"also write {XLSX_NAME} (all extracts as sheets; slow for the 200k fact)",
    )
    p.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    engine = db.get_engine(args.database_url)
    frames = build_extracts(engine)
    meta = write_extracts(frames, args.out, engine)
    total = sum(f["bytes"] for f in meta["files"].values())
    committed = sum(f["bytes"] for f in meta["files"].values() if f["committed"])
    log.info(
        "wrote %d files to %s: %.1f MB total, %.1f MB committed",
        len(meta["files"]),
        args.out,
        total / 1e6,
        committed / 1e6,
    )
    if args.xlsx:
        write_xlsx(frames, args.out)
    if not args.no_twb:
        try:
            rel = args.out.resolve().relative_to(args.twb.resolve().parent)
        except ValueError:
            rel = args.out.resolve()
        write_twb(frames, args.twb, data_dir=str(rel))


if __name__ == "__main__":
    main()
