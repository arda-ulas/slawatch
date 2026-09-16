"""Tableau Public extracts and the packaged dashboard workbook from the PostgreSQL views.

Usage:
    uv run slawatch-tableau            # -> tableau/data/*.csv, data/hyper/*.hyper,
                                       #    tableau/slawatch.twb, tableau/slawatch.twbx
    uv run slawatch-tableau --out tableau/data --no-twb   # CSVs only

Tableau Public only opens workbooks whose data sources are Hyper extracts, so each source is
written as a ``.hyper`` file (table "Extract"."Extract") and packaged with the workbook XML into
``slawatch.twbx``. The CSVs hold the same rows for anything else. Everything is synthetic; every
file name and every sheet title says so and ``extract_metadata.json`` records the generation.
The workbook XML mirrors the shape Tableau itself writes (the Superstore and World Indicators
samples that ship with Tableau Public). See tableau/README.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
               c.customer_name, c.industry, s.site_name, s.city, s.province,
               r.tier, r.region, r.service_type, r.assignment_group, r.severity, r.ticket_type,
               r.channel, r.status, r.has_active_outage, t.active_outage_id,
               r.sla_target_hours, t.resolution_ts, r.resolution_hours, t.is_resolved,
               r.sla_breached, r.probability, r.risk_band, r.split
        FROM v_ticket_risk r
        JOIN fact_ticket t USING (ticket_id)
        JOIN dim_customer c ON c.customer_id = r.customer_id
        JOIN dim_site s ON s.site_id = t.site_id
        ORDER BY r.creation_ts, r.ticket_id
        """,
    )
    outcome = pd.Series("Pending", index=df.index)
    outcome[df["status"] == "cancelled"] = "Cancelled"
    outcome[df["sla_breached"].eq(True)] = "Breached"
    outcome[df["sla_breached"].eq(False)] = "Met"
    # Denormalised on purpose: Tableau Public gets one ticket extract with the customer and
    # site attributes on it, so the workbook needs no relationships (see tableau/README.md).
    coords = [CITY_COORDS.get(k) for k in zip(df["city"], df["province"], strict=True)]
    return pd.DataFrame(
        {
            "Ticket ID": df["ticket_id"],
            "Created At (UTC)": _ts(df["creation_ts"]),
            "Customer ID": df["customer_id"],
            "Customer": df["customer_name"],
            "Industry": df["industry"].map(humanise),
            "Site ID": df["site_id"],
            "Site": df["site_name"],
            "City": df["city"],
            "Province": df["province"],
            "Province Name": df["province"].map(PROVINCE_NAMES),
            "Latitude": [xy[0] if xy else None for xy in coords],
            "Longitude": [xy[1] if xy else None for xy in coords],
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
# Hyper extracts (.hyper) - Tableau Public only opens workbooks whose sources are extracts
# ------------------------------------------------------------------------------------------
# The five sources the workbook uses; the dimension CSVs stay on disk for anyone who wants
# them, but the ticket fact already carries the customer and site attributes.
WORKBOOK_SOURCES = [
    "fact_ticket",
    "monthly_compliance",
    "backlog_ageing",
    "risk_deciles",
    "weekly_kpi",
]
HYPER_SUBDIR = "hyper"  # <extract dir>/hyper/*.hyper, regenerated (gitignored)
TWBX_DATA_DIR = "Data/slawatch"  # where the .twbx keeps the .hyper files; the .twb points there
DEFAULT_TWBX = Path("tableau/slawatch.twbx")

# Tableau's remote-type codes, as written in the reference workbooks' metadata-records.
_REMOTE_TYPE = {"string": 129, "integer": 20, "real": 5, "datetime": 7, "date": 133}
DATE_COLUMNS = ("Month", "Snapshot Date", "Week Start", "Week Ending", "Start Date")


def _col_type(series: pd.Series, name: str) -> str:
    if name.endswith("(UTC)"):
        return "datetime"
    if name in DATE_COLUMNS:
        return "date"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "real"
    return "string"


def column_types(df: pd.DataFrame) -> dict[str, str]:
    return {name: _col_type(df[name], name) for name in df.columns}


def hyper_name(key: str) -> str:
    return FILES[key][:-4] + ".hyper"


def _hyper_values(series: pd.Series, kind: str) -> list[Any]:
    """A pandas column as the Python objects the Hyper inserter wants; blanks become NULL."""
    vals = series.astype(object).where(series.notna(), None).tolist()
    if kind == "date":
        return [date.fromisoformat(v) if v else None for v in vals]
    if kind == "datetime":
        return [datetime.strptime(v, TS) if v else None for v in vals]
    if kind == "integer":
        return [int(v) if v is not None else None for v in vals]
    if kind == "real":
        return [float(v) if v is not None else None for v in vals]
    return [str(v) if v not in (None, "") else None for v in vals]


def write_hyper(frames: dict[str, pd.DataFrame], hyper_dir: Path) -> dict[str, Path]:
    """One .hyper per workbook source, table "Extract"."Extract", typed like the .twb says."""
    from tableauhyperapi import (
        Connection,
        CreateMode,
        HyperProcess,
        Inserter,
        SchemaName,
        SqlType,
        TableDefinition,
        TableName,
        Telemetry,
    )

    sql = {
        "date": SqlType.date(),
        "datetime": SqlType.timestamp(),
        "integer": SqlType.big_int(),
        "real": SqlType.double(),
        "string": SqlType.text(),
    }
    hyper_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    with HyperProcess(
        telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_config": ""}
    ) as hp:
        for key in WORKBOOK_SOURCES:
            df = frames[key]
            types = column_types(df)
            path = hyper_dir / hyper_name(key)
            table = TableDefinition(
                TableName("Extract", "Extract"),
                [TableDefinition.Column(name, sql[types[name]]) for name in df.columns],
            )
            with Connection(hp.endpoint, str(path), CreateMode.CREATE_AND_REPLACE) as conn:
                conn.catalog.create_schema(SchemaName("Extract"))
                conn.catalog.create_table(table)
                columns = [_hyper_values(df[name], types[name]) for name in df.columns]
                with Inserter(conn, table) as ins:
                    ins.add_rows(zip(*columns, strict=True))
                    ins.execute()
            paths[key] = path
            log.info("wrote %s (%d rows, %.1f MB)", path, len(df), path.stat().st_size / 1e6)
    return paths


# ------------------------------------------------------------------------------------------
# Workbook (.twb / .twbx) - XML in the shape Tableau itself writes (see the bundled samples)
# ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CalcField:
    name: str
    formula: str
    datatype: str = "real"
    role: str = "measure"
    fmt: str | None = None


PCT = "p0.0%"
INT = "n#,##0;-#,##0"
DEC = "n#,##0.0;-#,##0.0"

# Calculated fields, keyed by datasource. Same formulas as in tableau/README.md.
CALCS: dict[str, list[CalcField]] = {
    "fact_ticket": [
        CalcField("SLA Compliance %", "1 - SUM([Is Breached]) / SUM([Has Outcome])", fmt=PCT),
        CalcField("Breach Rate %", "SUM([Is Breached]) / SUM([Has Outcome])", fmt=PCT),
        CalcField("Tickets", "COUNTD([Ticket ID])", "integer", fmt=INT),
        CalcField("Breaches", "SUM([Is Breached])", "integer", fmt=INT),
        CalcField(
            "Open Tickets",
            "SUM(IIF([Status] = 'Cancelled', 0, 1 - [Is Resolved]))",
            "integer",
            fmt=INT,
        ),
        CalcField("Resolved Tickets", "SUM([Is Resolved])", "integer", fmt=INT),
        CalcField("Median Resolution Hours", "MEDIAN([Resolution Hours])", fmt=DEC),
        CalcField("P90 Resolution Hours", "PERCENTILE([Resolution Hours], 0.9)", fmt=DEC),
        CalcField("High Risk Tickets", "SUM(IIF([Risk Band] = 'High', 1, 0))", "integer", fmt=INT),
        CalcField("Outage Tickets", "SUM(IIF([During Outage] = 'Yes', 1, 0))", "integer", fmt=INT),
        CalcField(
            "Probability Decile", "INT([Breach Probability] * 10) + 1", "integer", "dimension"
        ),
        CalcField("Created Week", "DATETRUNC('week', [Created At (UTC)])", "date", "dimension"),
        CalcField("Created Month", "DATETRUNC('month', [Created At (UTC)])", "date", "dimension"),
    ],
    "monthly_compliance": [
        CalcField(
            "Compliance %", "1 - SUM([Breached Tickets]) / SUM([Tickets With Outcome])", fmt=PCT
        ),
        CalcField(
            "Breach Rate %", "SUM([Breached Tickets]) / SUM([Tickets With Outcome])", fmt=PCT
        ),
    ],
    "backlog_ageing": [
        CalcField("Past Due Share", "SUM([Past Due Tickets]) / SUM([Open Tickets])", fmt=PCT),
    ],
    "risk_deciles": [
        CalcField(
            "Calibration Gap", "SUM([Observed Breach Rate]) - SUM([Mean Predicted])", fmt=PCT
        ),
    ],
    "weekly_kpi": [
        CalcField("Compliance %", "1 - SUM([Breached Tickets]) / SUM([Tickets Resolved])", fmt=PCT),
    ],
}

CAPTIONS = {
    "fact_ticket": "Tickets (synthetic)",
    "monthly_compliance": "Monthly SLA compliance (synthetic)",
    "backlog_ageing": "Backlog ageing (synthetic)",
    "risk_deciles": "Risk calibration deciles (synthetic)",
    "weekly_kpi": "Weekly KPI (synthetic)",
}

# Default number formats for raw columns (calculated fields carry their own).
FORMATS = {
    "Observed Breach Rate": PCT,
    "Mean Predicted": PCT,
    "Probability From": PCT,
    "Probability To": PCT,
    "Breach Probability": PCT,
}
# Integer columns that are labels, not quantities.
DIMENSION_INTS = {"Age Band Order", "Decile"}
GEO_ROLES = {
    "Latitude": "[Geographical].[Latitude]",
    "Longitude": "[Geographical].[Longitude]",
    "City": "[City].[Name]",
    "Province Name": "[State].[Name]",
}

# Palette (tableau/README.md section 5).
NAVY, TEAL, AMBER, RED, GREEN, GREY = (
    "#1F4E78",
    "#2A9D8F",
    "#E9A23B",
    "#C0392B",
    "#2E7D32",
    "#8A8F98",
)
AGE_BAND_COLOURS = dict(zip(AGE_BANDS, ["#CFE1F2", "#93C4E0", "#4A90C4", NAVY, RED], strict=True))
DIVERGING = [RED, "#F4D35E", GREEN]  # bad -> good

DASHBOARD_NAME = "slawatch: enterprise service-desk SLA risk (synthetic data)"
DASHBOARD_W, DASHBOARD_H = 1200, 900
REPO_URL = "https://github.com/arda-ulas/slawatch"


def _ds(key: str) -> str:
    return f"federated.{key}"


def _lit(s: str) -> str:
    """A string literal the way Tableau writes it inside alias keys, buckets and members."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "\\%") + '"'


_DERIVATION = {
    "none": "None",
    "sum": "Sum",
    "avg": "Avg",
    "ctd": "CountD",
    "usr": "User",
    "twk": "Week-Trunc",
    "tmn": "Month-Trunc",
}
_KIND = {"qk": "quantitative", "nk": "nominal", "ok": "ordinal"}


def _inst(field: str, enc: str) -> str:
    """Column-instance name for a field and encoding, e.g. ('Tickets', 'usr:qk')."""
    deriv, kind = enc.split(":")
    return f"[{deriv}:{field}:{kind}]"


def _fq(key: str, field: str, enc: str | None = None) -> str:
    """Fully qualified shelf reference: [federated.x].[sum:Field:qk] or [federated.x].[Field]."""
    return f"[{_ds(key)}].{_inst(field, enc) if enc else f'[{field}]'}"


class Source:
    """The workbook-side description of one extract: its column XML and instances."""

    def __init__(self, key: str, df: pd.DataFrame) -> None:
        self.key = key
        self.name = _ds(key)
        self.caption = CAPTIONS[key]
        self.df = df
        self.types = column_types(df)
        self.columns: dict[str, ET.Element] = {}
        for name in df.columns:
            t = self.types[name]
            measure = (
                t in ("integer", "real") and not name.endswith("ID") and name not in DIMENSION_INTS
            )
            attrs = {
                "datatype": t,
                "name": f"[{name}]",
                "role": "measure" if measure else "dimension",
            }
            if measure:
                attrs["type"] = "quantitative"
            elif t in ("date", "datetime", "integer"):
                attrs["type"] = "ordinal"
            else:
                attrs["type"] = "nominal"
            if name in FORMATS:
                attrs["default-format"] = FORMATS[name]
            if name in GEO_ROLES:
                attrs["semantic-role"] = GEO_ROLES[name]
            self.columns[name] = ET.Element("column", attrs)
        for calc in CALCS.get(key, []):
            attrs = {
                "caption": calc.name,
                "datatype": calc.datatype,
                "name": f"[{calc.name}]",
                "role": calc.role,
                "type": "quantitative" if calc.role == "measure" else "ordinal",
            }
            if calc.fmt:
                attrs["default-format"] = calc.fmt
            c = ET.Element("column", attrs)
            ET.SubElement(c, "calculation", {"class": "tableau", "formula": calc.formula})
            self.columns[calc.name] = c

    def instance(self, field: str, enc: str) -> ET.Element:
        deriv, kind = enc.split(":")
        return ET.Element(
            "column-instance",
            column=f"[{field}]",
            derivation=_DERIVATION[deriv],
            name=_inst(field, enc),
            pivot="key",
            type=_KIND[kind],
        )

    def dependencies(self, used: list[tuple[str, str]]) -> ET.Element:
        """<datasource-dependencies> for the fields a sheet or dashboard touches."""
        deps = ET.Element("datasource-dependencies", datasource=self.name)
        for field in sorted({f for f, _ in used}, key=str.lower):
            deps.append(deepcopy(self.columns[field]))
        for field, enc in sorted(set(used), key=lambda fe: _inst(*fe).lower()):
            deps.append(self.instance(field, enc))
        return deps

    def xml(self) -> ET.Element:
        filename = FILES[self.key]
        conn_name = f"hyper.{self.key}"
        ds = ET.Element(
            "datasource", caption=self.caption, inline="true", name=self.name, version="18.1"
        )
        conn = ET.SubElement(ds, "connection", {"class": "federated"})
        ncs = ET.SubElement(conn, "named-connections")
        nc = ET.SubElement(ncs, "named-connection", caption=filename[:-4], name=conn_name)
        ET.SubElement(
            nc,
            "connection",
            {
                "authentication": "auth-none",
                "author-locale": "en_US",
                "class": "hyper",
                "dbname": f"{TWBX_DATA_DIR}/{hyper_name(self.key)}",
                "default-settings": "yes",
                "port": "",
                "sslmode": "",
                "username": "tableau_internal_user",
            },
        )
        ET.SubElement(
            conn,
            "relation",
            connection=conn_name,
            name="Extract",
            table="[Extract].[Extract]",
            type="table",
        )
        mrs = ET.SubElement(conn, "metadata-records")
        for i, name in enumerate(self.df.columns):
            t = self.types[name]
            mr = ET.SubElement(mrs, "metadata-record", {"class": "column"})
            ET.SubElement(mr, "remote-name").text = name
            ET.SubElement(mr, "remote-type").text = str(_REMOTE_TYPE[t])
            ET.SubElement(mr, "local-name").text = f"[{name}]"
            ET.SubElement(mr, "parent-name").text = "[Extract]"
            ET.SubElement(mr, "remote-alias").text = name
            ET.SubElement(mr, "ordinal").text = str(i)
            ET.SubElement(mr, "family").text = filename
            ET.SubElement(mr, "local-type").text = t
            ET.SubElement(mr, "aggregation").text = {
                "integer": "Sum",
                "real": "Sum",
                "date": "Year",
                "datetime": "Year",
                "string": "Count",
            }[t]
            ET.SubElement(mr, "contains-null").text = "true"
            if t == "string":
                ET.SubElement(mr, "collation", flag="0", name="LEN_RUS")
        ET.SubElement(ds, "aliases", enabled="yes")
        for col in self.columns.values():
            ds.append(deepcopy(col))
        return ds


def _measure_names_aliases(key: str, measures: dict[str, str]) -> ET.Element:
    """[:Measure Names] column with readable aliases for the calculated measures."""
    col = ET.Element(
        "column", datatype="string", name="[:Measure Names]", role="dimension", type="nominal"
    )
    aliases = ET.SubElement(col, "aliases")
    for field, alias in measures.items():
        ET.SubElement(aliases, "alias", key=_lit(_fq(key, field, "usr:qk")), value=alias)
    return col


def _palette_style(field_inst: str, mapping: dict[str, str]) -> ET.Element:
    enc = ET.Element("encoding", attr="color", field=field_inst, type="palette")
    for bucket, colour in mapping.items():
        m = ET.SubElement(enc, "map", to=colour.lower())
        ET.SubElement(m, "bucket").text = _lit(bucket)
    return enc


def _diverging_style(
    field_fq: str, lo: float, mid: float, hi: float, colours: list[str]
) -> ET.Element:
    enc = ET.Element(
        "encoding",
        attr="color",
        center=str(mid),
        field=field_fq,
        max=str(hi),
        min=str(lo),
        type="custom-interpolated",
    )
    pal = ET.SubElement(enc, "color-palette", custom="true", name="", type="ordered-diverging")
    for c in colours:
        ET.SubElement(pal, "color").text = c.lower()
    return enc


def _formatted_text(runs: list[tuple[str, dict[str, str]]]) -> ET.Element:
    ft = ET.Element("formatted-text")
    for text, attrs in runs:
        ET.SubElement(ft, "run", attrs).text = text
    return ft


# --- worksheets ----------------------------------------------------------------------------
# Global quick filters on the ticket source; the same filter-group in every sheet is how
# Tableau stores "apply to all worksheets using this data source".
TICKET_FILTERS = [("Tier", "none:nk", 2), ("Service Type", "none:nk", 3), ("Region", "none:nk", 4)]
DATE_FILTER_GROUP = 1


class Sheet:
    def __init__(self, name: str, src: Source, title: str, mapsource: bool = False) -> None:
        self.name, self.src, self.title = name, src, title
        self.used: list[tuple[str, str]] = []
        self.ws = ET.Element("worksheet", name=name)
        layout = ET.SubElement(self.ws, "layout-options")
        t = ET.SubElement(layout, "title")
        t.append(_formatted_text([(title, {})]))
        self.table = ET.SubElement(self.ws, "table")
        self.view = ET.SubElement(self.table, "view")
        dss = ET.SubElement(self.view, "datasources")
        ET.SubElement(dss, "datasource", caption=src.caption, name=src.name)
        if mapsource:
            ms = ET.SubElement(self.view, "mapsources")
            ET.SubElement(ms, "mapsource", name="Tableau")
        self.deps_slot = len(self.view)  # datasource-dependencies go here, before filters
        self.filters: list[ET.Element] = []
        self.slices: list[str] = []
        self.style = ET.Element("style")
        self.panes = ET.Element("panes")

    def f(self, field: str, enc: str) -> str:
        """Use a field on this sheet (records the dependency) and return its shelf reference."""
        self.used.append((field, enc))
        return _fq(self.src.key, field, enc)

    def categorical_filter(
        self, field: str, group: int | None = None, enc: str = "none:nk"
    ) -> None:
        col = self.f(field, enc)
        attrs = {"class": "categorical", "column": col}
        if group is not None:
            attrs["filter-group"] = str(group)
        flt = ET.Element("filter", attrs)
        ET.SubElement(
            flt,
            "groupfilter",
            {
                "function": "level-members",
                "level": _inst(field, enc),
                "user:ui-enumeration": "all",
                "user:ui-marker": "enumerate",
            },
        )
        self.filters.append(flt)
        self.slices.append(col)

    def range_filter(self, field: str, lo: str, hi: str, group: int) -> None:
        col = self.f(field, "none:qk")
        flt = ET.Element(
            "filter",
            {
                "class": "quantitative",
                "column": col,
                "filter-group": str(group),
                "included-values": "in-range",
            },
        )
        ET.SubElement(flt, "min").text = f"#{lo}#"
        ET.SubElement(flt, "max").text = f"#{hi}#"
        self.filters.append(flt)
        self.slices.append(col)

    def measure_names(self, measures: list[tuple[str, str]]) -> str:
        """Filter + order for [:Measure Names]; returns the shelf reference."""
        col = f"[{self.src.name}].[:Measure Names]"
        members = [_lit(self.f(field, enc)) for field, enc in measures]
        flt = ET.Element("filter", {"class": "categorical", "column": col})
        union = ET.SubElement(flt, "groupfilter", {"function": "union", "user:op": "manual"})
        for m in members:
            ET.SubElement(
                union, "groupfilter", function="member", level="[:Measure Names]", member=m
            )
        self.filters.append(flt)
        sort = ET.Element("manual-sort", column=col, direction="ASC")
        d = ET.SubElement(sort, "dictionary")
        for m in members:
            ET.SubElement(d, "bucket").text = m
        self.filters.append(sort)
        self.slices.append(col)
        return col

    def manual_sort(self, field: str, enc: str, order: list[str]) -> None:
        sort = ET.Element("manual-sort", column=self.f(field, enc), direction="ASC")
        d = ET.SubElement(sort, "dictionary")
        for v in order:
            ET.SubElement(d, "bucket").text = _lit(v)
        ET.SubElement(d, "bucket").text = "%all%"
        self.filters.append(sort)

    def computed_sort(
        self, field: str, enc: str, using: str, using_enc: str, direction: str
    ) -> None:
        self.filters.append(
            ET.Element(
                "computed-sort",
                column=self.f(field, enc),
                direction=direction,
                using=self.f(using, using_enc),
            )
        )

    def style_rule(self, element: str, formats: list[dict[str, str]]) -> ET.Element:
        rule = ET.SubElement(self.style, "style-rule", element=element)
        for fmt in formats:
            ET.SubElement(rule, "format", fmt)
        return rule

    def pane(
        self,
        mark: str,
        encodings: list[tuple[str, str]] | None = None,
        mark_formats: list[dict[str, str]] | None = None,
        cell_formats: list[dict[str, str]] | None = None,
    ) -> ET.Element:
        pane = ET.SubElement(
            self.panes, "pane", {"selection-relaxation-option": "selection-relaxation-disallow"}
        )
        pv = ET.SubElement(pane, "view")
        ET.SubElement(pv, "breakdown", value="auto")
        ET.SubElement(pane, "mark", {"class": mark})
        if encodings:
            enc = ET.SubElement(pane, "encodings")
            for kind, col in encodings:
                ET.SubElement(enc, kind, column=col)
        if mark_formats or cell_formats:
            st = ET.SubElement(pane, "style")
            if cell_formats:
                rule = ET.SubElement(st, "style-rule", element="cell")
                for fmt in cell_formats:
                    ET.SubElement(rule, "format", fmt)
            if mark_formats:
                rule = ET.SubElement(st, "style-rule", element="mark")
                for fmt in mark_formats:
                    ET.SubElement(rule, "format", fmt)
        return pane

    def finish(self, rows: str, cols: str, tooltips: bool = True) -> ET.Element:
        self.view.insert(self.deps_slot, self.src.dependencies(self.used))
        for flt in self.filters:
            self.view.append(flt)
        if self.slices:
            sl = ET.SubElement(self.view, "slices")
            for col in dict.fromkeys(self.slices):
                ET.SubElement(sl, "column").text = col
        ET.SubElement(self.view, "aggregation", value="true")
        if len(self.style):
            self.table.append(self.style)
        self.table.append(self.panes)
        ET.SubElement(self.table, "rows").text = rows or None
        ET.SubElement(self.table, "cols").text = cols or None
        if not tooltips:
            ET.SubElement(self.table, "tooltip-style", {"tooltip-mode": "none"})
        return self.ws


def _ticket_filters(sheet: Sheet, date_range: tuple[str, str]) -> None:
    sheet.range_filter("Created At (UTC)", *date_range, group=DATE_FILTER_GROUP)
    for field, enc, group in TICKET_FILTERS:
        sheet.categorical_filter(field, group, enc)


KPI_MEASURES = [
    ("Tickets", "Tickets"),
    ("SLA Compliance %", "SLA compliance"),
    ("Breaches", "SLA breaches"),
    ("Open Tickets", "Open at snapshot"),
    ("Median Resolution Hours", "Median resolution (h)"),
    ("High Risk Tickets", "High-risk tickets"),
]
DRILLDOWN_MEASURES = [
    "Tickets",
    "SLA Compliance %",
    "Breaches",
    "Open Tickets",
    "High Risk Tickets",
    "Median Resolution Hours",
]


def sheet_kpi(src: Source, date_range: tuple[str, str]) -> ET.Element:
    s = Sheet("KPI BANs", src, "Key figures (synthetic data)")
    names = s.measure_names([(m, "usr:qk") for m, _ in KPI_MEASURES])
    _ticket_filters(s, date_range)
    s.style_rule(
        "cell",
        [
            {"attr": "text-align", "value": "center"},
            {"attr": "font-weight", "value": "bold"},
            {"attr": "font-size", "value": "22"},
            {"attr": "color", "value": NAVY},
        ],
    )
    s.style_rule(
        "label",
        [
            {"attr": "text-align", "value": "center"},
            {"attr": "font-size", "field": names, "value": "9"},
            {"attr": "color", "field": names, "value": "#555555"},
        ],
    )
    s.style_rule("table-div", [{"attr": "line-visibility", "scope": "cols", "value": "off"}])
    s.style_rule("worksheet", [{"attr": "display-field-labels", "scope": "cols", "value": "false"}])
    s.pane(
        "Automatic",
        [("text", f"[{src.name}].[Multiple Values]")],
        mark_formats=[{"attr": "mark-labels-show", "value": "true"}],
    )
    return s.finish("", names, tooltips=False)


def sheet_volume(src: Source, date_range: tuple[str, str]) -> ET.Element:
    s = Sheet(
        "Ticket volume over time", src, "Tickets opened per week; tickets during an outage in red"
    )
    week = s.f("Created At (UTC)", "twk:qk")
    tickets = s.f("Tickets", "usr:qk")
    outage = s.f("During Outage", "none:nk")
    _ticket_filters(s, date_range)
    s.manual_sort("During Outage", "none:nk", ["No", "Yes"])
    s.style_rule(
        "axis",
        [
            {"attr": "title", "class": "0", "field": tickets, "scope": "rows", "value": "Tickets"},
            {"attr": "title", "class": "0", "field": week, "scope": "cols", "value": "Week opened"},
        ],
    )
    s.pane(
        "Bar",
        [("color", outage), ("tooltip", s.f("SLA Compliance %", "usr:qk"))],
    )
    return s.finish(tickets, week)


def sheet_heatmap(src: Source, month_range: tuple[str, str]) -> ET.Element:
    s = Sheet("SLA compliance heatmap", src, "SLA compliance: customer x service type")
    customer = s.f("Customer", "none:nk")
    service = s.f("Service Type", "none:nk")
    comp = s.f("Compliance %", "usr:qk")
    s.range_filter("Month", *month_range, group=11)
    s.categorical_filter("Tier", 12)
    s.computed_sort("Customer", "none:nk", "Compliance %", "usr:qk", "ASC")
    rule = s.style_rule("mark", [])
    rule.append(_diverging_style(comp, 0.7, 0.85, 1.0, DIVERGING))
    s.style_rule("label", [{"attr": "text-format", "field": comp, "value": "p0%"}])
    s.style_rule("cell", [{"attr": "font-size", "value": "7"}])
    s.style_rule("header", [{"attr": "font-size", "value": "7"}])
    s.style_rule(
        "worksheet",
        [
            {"attr": "display-field-labels", "scope": "rows", "value": "false"},
            {"attr": "display-field-labels", "scope": "cols", "value": "false"},
        ],
    )
    s.pane(
        "Square",
        [("color", comp), ("text", comp), ("tooltip", s.f("Tickets With Outcome", "sum:qk"))],
        mark_formats=[{"attr": "mark-labels-show", "value": "true"}],
    )
    return s.finish(customer, service)


def sheet_backlog(src: Source) -> ET.Element:
    s = Sheet("Backlog ageing", src, "Open backlog by age band (1st of month)")
    month = s.f("Snapshot Date", "tmn:qk")
    open_ = s.f("Open Tickets", "sum:qk")
    band = s.f("Age Band", "none:nk")
    s.categorical_filter("Service Type", 21)
    s.manual_sort("Age Band", "none:nk", AGE_BANDS)
    s.style_rule(
        "axis",
        [
            {
                "attr": "title",
                "class": "0",
                "field": open_,
                "scope": "rows",
                "value": "Open tickets",
            },
            {"attr": "title", "class": "0", "field": month, "scope": "cols", "value": "Snapshot"},
        ],
    )
    s.pane(
        "Area",
        [
            ("color", band),
            ("tooltip", s.f("Past Due Tickets", "sum:qk")),
            ("tooltip", s.f("Past Due Share", "usr:qk")),
        ],
    )
    return s.finish(open_, month)


def sheet_calibration(src: Source) -> ET.Element:
    s = Sheet(
        "Risk calibration",
        src,
        "Predicted vs observed breach rate by decile (test months)",
    )
    decile = s.f("Decile", "none:ok")
    names = s.measure_names([("Mean Predicted", "sum:qk"), ("Observed Breach Rate", "sum:qk")])
    values = f"[{src.name}].[Multiple Values]"
    s.style_rule(
        "axis",
        [
            {
                "attr": "title",
                "class": "0",
                "field": values,
                "scope": "rows",
                "value": "Breach rate",
            },
            {
                "attr": "title",
                "class": "0",
                "field": decile,
                "scope": "cols",
                "value": "Predicted-probability decile",
            },
        ],
    )
    s.style_rule(
        "label",
        [
            {"attr": "text-format", "field": values, "value": "p0%"},
            {"attr": "display", "field": names, "value": "false"},
        ],
    )
    s.pane("Bar", [("color", names), ("tooltip", s.f("Tickets", "sum:qk"))])
    return s.finish(values, f"({decile} / {names})")


def sheet_map(src: Source, date_range: tuple[str, str]) -> ET.Element:
    s = Sheet("Site map", src, "Ticket volume and breach rate by city", mapsource=True)
    lat = s.f("Latitude", "avg:qk")
    lon = s.f("Longitude", "avg:qk")
    tickets = s.f("Tickets", "usr:qk")
    breach = s.f("Breach Rate %", "usr:qk")
    _ticket_filters(s, date_range)
    rule = s.style_rule("mark", [])
    rule.append(_diverging_style(breach, 0.05, 0.15, 0.3, list(reversed(DIVERGING))))
    s.style_rule("map", [{"attr": "washout", "value": "0.3"}])
    s.pane(
        "Circle",
        [
            ("color", breach),
            ("size", tickets),
            ("lod", s.f("City", "none:nk")),
            ("lod", s.f("Province Name", "none:nk")),
            ("tooltip", s.f("High Risk Tickets", "usr:qk")),
        ],
        mark_formats=[{"attr": "mark-transparency", "value": "200"}],
    )
    return s.finish(lat, lon)


def sheet_drilldown(src: Source, date_range: tuple[str, str]) -> ET.Element:
    s = Sheet("Customer drill-down", src, "Customers ranked by SLA breaches")
    customer = s.f("Customer", "none:nk")
    tier = s.f("Tier", "none:nk")
    names = s.measure_names([(m, "usr:qk") for m in DRILLDOWN_MEASURES])
    _ticket_filters(s, date_range)
    s.computed_sort("Customer", "none:nk", "Breaches", "usr:qk", "DESC")
    s.style_rule("cell", [{"attr": "text-align", "value": "right"}])
    s.style_rule("worksheet", [{"attr": "display-field-labels", "scope": "rows", "value": "false"}])
    s.pane(
        "Automatic",
        [("text", f"[{src.name}].[Multiple Values]")],
        mark_formats=[{"attr": "mark-labels-show", "value": "true"}],
    )
    return s.finish(f"({customer} / {tier})", names, tooltips=False)


def sheet_weekly(src: Source) -> ET.Element:
    s = Sheet("Weekly compliance", src, "Weekly SLA compliance, resolved tickets")
    week = s.f("Week Ending", "twk:qk")
    comp = s.f("Compliance %", "usr:qk")
    s.style_rule(
        "axis",
        [
            {
                "attr": "title",
                "class": "0",
                "field": comp,
                "scope": "rows",
                "value": "SLA compliance",
            }
        ],
    )
    s.pane(
        "Line",
        [
            ("tooltip", s.f("Tickets Resolved", "sum:qk")),
            ("tooltip", s.f("Breached Tickets", "sum:qk")),
        ],
        mark_formats=[{"attr": "mark-color", "value": NAVY.lower()}],
    )
    return s.finish(comp, week)


SHEET_NAMES = [
    "KPI BANs",
    "Ticket volume over time",
    "SLA compliance heatmap",
    "Backlog ageing",
    "Risk calibration",
    "Site map",
    "Customer drill-down",
    "Weekly compliance",
]
# On the dashboard; the drill-down and the weekly line stay as their own tabs.
DASHBOARD_SHEETS = SHEET_NAMES[:6]


# --- dashboard -------------------------------------------------------------------------------
class Zones:
    """Dashboard zones in Tableau's 1/100000 coordinates, from a 1200 x 900 pixel layout."""

    def __init__(self) -> None:
        self.next_id = 1

    def zone(self, x: int, y: int, w: int, h: int, **attrs: str) -> ET.Element:
        geo = {
            "h": str(round(h * 100000 / DASHBOARD_H)),
            "id": str(self.next_id),
            "w": str(round(w * 100000 / DASHBOARD_W)),
            "x": str(round(x * 100000 / DASHBOARD_W)),
            "y": str(round(y * 100000 / DASHBOARD_H)),
        }
        self.next_id += 1
        return ET.Element("zone", {**attrs, **geo})

    def flow(
        self, x: int, y: int, w: int, h: int, direction: str, fixed: int | None = None
    ) -> ET.Element:
        attrs = {"param": direction, "type-v2": "layout-flow"}
        if fixed is not None:
            attrs.update({"fixed-size": str(fixed), "is-fixed": "true"})
        return self.zone(x, y, w, h, **attrs)

    def sheet(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        name: str,
        fixed: int | None = None,
        show_title: bool = True,
    ) -> ET.Element:
        attrs = {"name": name}
        if not show_title:
            attrs["show-title"] = "false"
        if fixed is not None:
            attrs.update({"fixed-size": str(fixed), "is-fixed": "true"})
        z = self.zone(x, y, w, h, **attrs)
        _zone_style(z, margin="4")
        return z

    def text(self, x: int, y: int, w: int, h: int, runs: list, fixed: int) -> ET.Element:
        z = self.zone(
            x, y, w, h, **{"fixed-size": str(fixed), "is-fixed": "true", "type-v2": "text"}
        )
        z.append(_formatted_text(runs))
        _zone_style(z, margin="4")
        return z

    def filter(
        self, x: int, y: int, w: int, h: int, sheet: str, param: str, mode: str | None, fixed: int
    ) -> ET.Element:
        attrs = {
            "fixed-size": str(fixed),
            "is-fixed": "true",
            "name": sheet,
            "param": param,
            "type-v2": "filter",
        }
        if mode:
            attrs["mode"] = mode
        z = self.zone(x, y, w, h, **attrs)
        _zone_style(z, margin="4", background="#f5f5f5")
        return z


def _zone_style(z: ET.Element, margin: str, background: str | None = None) -> None:
    st = ET.SubElement(z, "zone-style")
    ET.SubElement(st, "format", attr="border-color", value="#000000")
    ET.SubElement(st, "format", attr="border-style", value="none")
    ET.SubElement(st, "format", attr="border-width", value="0")
    ET.SubElement(st, "format", attr="margin", value=margin)
    if background:
        ET.SubElement(st, "format", attr="background-color", value=background)


def dashboard_xml(sources: dict[str, Source], meta: dict[str, Any] | None) -> ET.Element:
    fact, backlog = sources["fact_ticket"], sources["backlog_ageing"]
    db = ET.Element("dashboard", name=DASHBOARD_NAME)
    layout = ET.SubElement(db, "layout-options")
    ET.SubElement(layout, "title").append(_formatted_text([(DASHBOARD_NAME, {})]))
    ET.SubElement(
        db,
        "size",
        maxheight=str(DASHBOARD_H),
        maxwidth=str(DASHBOARD_W),
        minheight=str(DASHBOARD_H),
        minwidth=str(DASHBOARD_W),
    )
    dss = ET.SubElement(db, "datasources")
    for src in (fact, backlog):
        ET.SubElement(dss, "datasource", caption=src.caption, name=src.name)
    db.append(
        fact.dependencies(
            [("Created At (UTC)", "none:qk")] + [(f, e) for f, e, _ in TICKET_FILTERS]
        )
    )
    db.append(backlog.dependencies([("Service Type", "none:nk")]))

    z = Zones()
    W, H = DASHBOARD_W, DASHBOARD_H
    title_h, kpi_h, foot_h = 50, 84, 34
    left_w, right_w = 780, W - 780
    body_y, body_h = title_h + kpi_h, H - title_h - kpi_h - foot_h
    chart_h = 240
    bottom_y, bottom_h = body_y + 2 * chart_h, body_h - 2 * chart_h
    filter_hs = [
        ("Created At (UTC)", "none:qk", None, 70),
        ("Tier", "none:nk", "checkdropdown", 48),
        ("Service Type", "none:nk", "checkdropdown", 48),
        ("Region", "none:nk", "checkdropdown", 48),
    ]

    zones = ET.SubElement(db, "zones")
    root = z.zone(0, 0, W, H, **{"type-v2": "layout-basic"})
    zones.append(root)
    col = z.flow(0, 0, W, H, "vert")
    root.append(col)
    n_tickets = meta["files"][FILES["fact_ticket"]]["rows"] if meta else len(fact.df)
    col.append(
        z.text(
            0,
            0,
            W,
            title_h,
            [(DASHBOARD_NAME, {"bold": "true", "fontcolor": NAVY.lower(), "fontsize": "18"})],
            fixed=title_h,
        )
    )
    col.append(z.sheet(0, title_h, W, kpi_h, "KPI BANs", fixed=kpi_h, show_title=False))
    body = z.flow(0, body_y, W, body_h, "horz")
    col.append(body)
    left = z.flow(0, body_y, left_w, body_h, "vert", fixed=left_w)
    body.append(left)
    left.append(z.sheet(0, body_y, left_w, chart_h, "Ticket volume over time", fixed=chart_h))
    left.append(z.sheet(0, body_y + chart_h, left_w, chart_h, "Site map", fixed=chart_h))
    bottom = z.flow(0, bottom_y, left_w, bottom_h, "horz")
    left.append(bottom)
    bottom.append(z.sheet(0, bottom_y, left_w // 2, bottom_h, "Backlog ageing", fixed=left_w // 2))
    bottom.append(
        z.sheet(left_w // 2, bottom_y, left_w - left_w // 2, bottom_h, "Risk calibration")
    )
    right = z.flow(left_w, body_y, right_w, body_h, "vert")
    body.append(right)
    y = body_y
    for field, enc, mode, h in filter_hs:
        right.append(
            z.filter(
                left_w, y, right_w, h, "KPI BANs", _fq("fact_ticket", field, enc), mode, fixed=h
            )
        )
        y += h
    right.append(z.sheet(left_w, y, right_w, body_y + body_h - y, "SLA compliance heatmap"))
    col.append(
        z.text(
            0,
            H - foot_h,
            W,
            foot_h,
            [
                (
                    f"Synthetic data generated by slawatch ({n_tickets:,} tickets, "
                    "Jul 2024 to Jun 2026). No real customers, sites or tickets. Code and method: ",
                    {"fontcolor": "#555555", "fontsize": "9"},
                ),
                (
                    REPO_URL,
                    {
                        "auto-url": "true",
                        "fontcolor": NAVY.lower(),
                        "fontsize": "9",
                        "hyperlink": f"tabdoc:load-url url=&quot;{REPO_URL}&quot;",
                    },
                ),
            ],
            fixed=foot_h,
        )
    )
    return db


# --- workbook ----------------------------------------------------------------------------------
def _window_xml(
    name: str, cls: str, sheets: list[str] | None = None, maximized: bool = False
) -> ET.Element:
    attrs = {"class": cls, "name": name}
    if maximized:
        attrs["maximized"] = "true"
    w = ET.Element("window", attrs)
    if cls == "dashboard":
        vps = ET.SubElement(w, "viewpoints")
        for s in sheets or []:
            vp = ET.SubElement(vps, "viewpoint", name=s)
            ET.SubElement(vp, "zoom", type="entire-view")
        ET.SubElement(w, "active", id="-1")
    else:
        cards = ET.SubElement(w, "cards")
        left = ET.SubElement(cards, "edge", name="left")
        strip = ET.SubElement(left, "strip", size="160")
        for card in ("pages", "filters", "marks"):
            ET.SubElement(strip, "card", type=card)
        top = ET.SubElement(cards, "edge", name="top")
        for card in ("columns", "rows", "title"):
            strip = ET.SubElement(top, "strip", size="2147483647")
            ET.SubElement(strip, "card", type=card)
    return w


def _date_range(series: pd.Series) -> tuple[str, str]:
    vals = series[series != ""]
    return str(vals.min()), str(vals.max())


def build_twb(
    frames: dict[str, pd.DataFrame], meta: dict[str, Any] | None = None
) -> ET.ElementTree:
    root = ET.Element(
        "workbook",
        {
            "source-build": "2023.1.0 (20231.23.0116.1105)",
            "source-platform": "mac",
            "version": "18.1",
            "xmlns:user": "http://www.tableausoftware.com/xml/user",
        },
    )
    root.insert(
        0,
        ET.Comment(
            " SYNTHETIC DATA. Generated by slawatch-tableau. The data sources are the Hyper "
            f"extracts packaged under {TWBX_DATA_DIR}/ in slawatch.twbx. "
        ),
    )
    # Format flags, as in Tableau's own files: SortTagCleanup makes the schema accept the
    # <manual-sort> / <computed-sort> elements the reference workbooks use.
    manifest = ET.SubElement(root, "document-format-change-manifest")
    ET.SubElement(manifest, "SortTagCleanup")
    prefs = ET.SubElement(root, "preferences")
    ET.SubElement(prefs, "preference", name="ui.encoding.shelf.height", value="24")
    ET.SubElement(prefs, "preference", name="ui.shelf.height", value="26")
    sources = {key: Source(key, frames[key]) for key in WORKBOOK_SOURCES}
    fact, monthly, backlog, risk, weekly = (sources[k] for k in WORKBOOK_SOURCES)

    dss = ET.SubElement(root, "datasources")
    for key, src in sources.items():
        ds = src.xml()
        if key == "fact_ticket":
            ds.append(_measure_names_aliases(key, dict(KPI_MEASURES)))
            # a palette keyed by a column instance needs that instance declared on the source
            ds.append(src.instance("During Outage", "none:nk"))
            ds.append(src.instance("Risk Band", "none:nk"))
            style = ET.SubElement(ds, "style")
            rule = ET.SubElement(style, "style-rule", element="mark")
            rule.append(_palette_style(_inst("During Outage", "none:nk"), {"No": NAVY, "Yes": RED}))
            rule.append(
                _palette_style(
                    _inst("Risk Band", "none:nk"), {"Low": GREEN, "Medium": AMBER, "High": RED}
                )
            )
        elif key == "backlog_ageing":
            ds.append(src.instance("Age Band", "none:nk"))
            style = ET.SubElement(ds, "style")
            rule = ET.SubElement(style, "style-rule", element="mark")
            rule.append(_palette_style(_inst("Age Band", "none:nk"), AGE_BAND_COLOURS))
        elif key == "risk_deciles":
            style = ET.SubElement(ds, "style")
            rule = ET.SubElement(style, "style-rule", element="mark")
            rule.append(
                _palette_style(
                    "[:Measure Names]",
                    {
                        _fq(key, "Mean Predicted", "sum:qk"): AMBER,
                        _fq(key, "Observed Breach Rate", "sum:qk"): NAVY,
                    },
                )
            )
        dss.append(ds)
    ms = ET.SubElement(root, "mapsources")
    ET.SubElement(ms, "mapsource", name="Tableau")

    date_range = _date_range(fact.df["Created At (UTC)"])
    month_range = _date_range(monthly.df["Month"])
    wss = ET.SubElement(root, "worksheets")
    for ws in (
        sheet_kpi(fact, date_range),
        sheet_volume(fact, date_range),
        sheet_heatmap(monthly, month_range),
        sheet_backlog(backlog),
        sheet_calibration(risk),
        sheet_map(fact, date_range),
        sheet_drilldown(fact, date_range),
        sheet_weekly(weekly),
    ):
        wss.append(ws)
    dbs = ET.SubElement(root, "dashboards")
    dbs.append(dashboard_xml(sources, meta))

    windows = ET.SubElement(root, "windows", {"source-height": "30"})
    windows.append(_window_xml(DASHBOARD_NAME, "dashboard", DASHBOARD_SHEETS, maximized=True))
    for name in SHEET_NAMES:
        windows.append(_window_xml(name, "worksheet"))
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return tree


def write_twb(
    frames: dict[str, pd.DataFrame], path: Path, meta: dict[str, Any] | None = None
) -> Path:
    tree = build_twb(frames, meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    log.info("wrote %s (%d bytes)", path, path.stat().st_size)
    return path


def write_twbx(twb: Path, hyper_paths: dict[str, Path], path: Path) -> Path:
    """Package the .twb with its Hyper extracts at the paths the XML references."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(twb, twb.name)
        for key, hp in hyper_paths.items():
            zf.write(hp, f"{TWBX_DATA_DIR}/{hyper_name(key)}")
    log.info("wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tableau Public extracts from the slawatch views.")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="extract directory")
    p.add_argument("--twb", type=Path, default=DEFAULT_TWB, help="workbook XML path")
    p.add_argument("--twbx", type=Path, default=DEFAULT_TWBX, help="packaged workbook path")
    p.add_argument(
        "--no-twb", action="store_true", help="skip the Hyper extracts, the .twb and the .twbx"
    )
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
        hyper_paths = write_hyper(frames, args.out / HYPER_SUBDIR)
        twb = write_twb(frames, args.twb, meta)
        write_twbx(twb, hyper_paths, args.twbx)


if __name__ == "__main__":
    main()
