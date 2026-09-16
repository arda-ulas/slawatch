"""Raw synthetic CSVs -> cleaned frames -> PostgreSQL + Tableau-ready CSV extract.

Usage:
    uv run slawatch-pipeline --raw data/raw --processed data/processed
    uv run slawatch-pipeline --skip-db          # only write the processed extract
    uv run slawatch-pipeline --views-only       # (re)apply sql/views/*.sql
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from slawatch import cleaning, db, features
from slawatch.generate import FILE_NAMES

log = logging.getLogger("slawatch.pipeline")

PROCESSED_FILES = {
    "tickets": "synthetic_tickets_clean.csv",
    "status_history": "synthetic_ticket_status_history_clean.csv",
    "rejected": "synthetic_tickets_rejected.csv",
    "report": "pipeline_report.json",
}

TICKET_RENAMES = {
    "id": "ticket_id",
    "creation_date": "creation_ts",
    "last_update": "last_update_ts",
    "expected_resolution_date": "expected_resolution_ts",
    "requested_resolution_date": "requested_resolution_ts",
    "resolution_date": "resolution_ts",
}

FACT_TICKET_COLUMNS = [
    "ticket_id",
    "name",
    "description",
    "ticket_type",
    "severity",
    "priority",
    "status",
    "channel",
    "creation_ts",
    "last_update_ts",
    "expected_resolution_ts",
    "requested_resolution_ts",
    "resolution_ts",
    "customer_id",
    "site_id",
    "service_id",
    "assignment_group",
    "active_outage_id",
    "open_backlog_at_creation",
    "sla_target_hours",
    "creation_hour_local",
    "creation_dow_local",
    "is_weekend",
    "is_after_hours",
    "reopen_count",
    "resolution_hours",
    "is_resolved",
    "sla_breached",
]
HISTORY_COLUMNS = ["ticket_id", "sequence_no", "status", "change_ts", "change_reason"]


def read_raw(raw_dir: Path) -> dict[str, pd.DataFrame]:
    def _csv(key: str, **kw) -> pd.DataFrame:
        return pd.read_csv(raw_dir / FILE_NAMES[key], dtype=str, keep_default_na=False, **kw)

    frames = {
        k: _csv(k)
        for k in (
            "customers",
            "sites",
            "services",
            "sla_targets",
            "outages",
            "tickets",
            "status_history",
        )
    }
    meta_path = raw_dir / FILE_NAMES["metadata"]
    frames["metadata"] = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return frames


def snapshot_from(frames: dict, tickets: pd.DataFrame) -> pd.Timestamp:
    end = frames.get("metadata", {}).get("params", {}).get("end")
    if end:
        return pd.Timestamp(end, tz="UTC")
    return tickets["last_update"].max()


def build_tables(raw_dir: Path) -> tuple[dict[str, pd.DataFrame], dict]:
    """Read + clean + derive. Returns table frames keyed like the database tables."""
    t0 = time.perf_counter()
    frames = read_raw(raw_dir)
    log.info("read raw extract from %s (%d ticket rows)", raw_dir, len(frames["tickets"]))

    customers = frames["customers"]
    sites = frames["sites"]
    services = frames["services"].copy()
    services["bandwidth_mbps"] = pd.to_numeric(services["bandwidth_mbps"], errors="coerce").astype(
        "Int64"
    )
    sla_targets = frames["sla_targets"].copy()
    sla_targets["target_hours"] = sla_targets["target_hours"].astype(float)
    outages, _ = cleaning.parse_timestamps(frames["outages"], ["start_date", "end_date"])

    tickets, rejected, report = cleaning.clean_tickets(frames["tickets"])
    log.info("cleaning: %s", json.dumps(report.to_dict()))

    # referential integrity: a ticket must point at known dimension rows
    ref_ok = (
        tickets["customer_id"].isin(set(customers["customer_id"]))
        & tickets["site_id"].isin(set(sites["site_id"]))
        & tickets["service_id"].isin(set(services["service_id"]))
        & (
            tickets["active_outage_id"].isna()
            | tickets["active_outage_id"].isin(set(outages["outage_id"]))
        )
    )
    if (~ref_ok).any():
        orphans = tickets[~ref_ok].copy()
        orphans["reject_reason"] = "unknown_dimension_key"
        rejected = pd.concat([rejected, orphans], ignore_index=True)
        report.rejected_rows["unknown_dimension_key"] = int((~ref_ok).sum())
        tickets = tickets[ref_ok].reset_index(drop=True)
        report.output_rows = len(tickets)

    history, history_report = cleaning.clean_status_history(frames["status_history"], tickets["id"])
    log.info("status history: %s", json.dumps(history_report))

    snapshot = snapshot_from(frames, tickets)
    wide = features.join_dimensions(tickets, customers, sites, services)
    wide = features.add_creation_time_features(wide)
    wide = features.add_sla_target(wide, sla_targets)
    wide = features.add_outcome_fields(wide, snapshot)
    wide = wide.rename(columns=TICKET_RENAMES)

    fact_ticket = wide[FACT_TICKET_COLUMNS]
    fact_history = history.rename(columns={"change_date": "change_ts"})[HISTORY_COLUMNS]

    resolved = fact_ticket["is_resolved"]
    breach_rate = float(fact_ticket.loc[resolved, "sla_breached"].astype(float).mean())
    run_report = {
        "synthetic": True,
        "source_dir": str(raw_dir),
        "snapshot_ts": snapshot.isoformat(),
        "tickets": report.to_dict(),
        "status_history": history_report,
        "row_counts": {
            "dim_customer": len(customers),
            "dim_site": len(sites),
            "dim_service": len(services),
            "sla_target": len(sla_targets),
            "outage_incident": len(outages),
            "fact_ticket": len(fact_ticket),
            "fact_ticket_status_history": len(fact_history),
            "rejected_tickets": len(rejected),
        },
        "breach_rate_resolved": round(breach_rate, 4),
        "open_tickets": int((~resolved).sum()),
        "build_seconds": round(time.perf_counter() - t0, 2),
    }
    tables = {
        "dim_customer": customers,
        "dim_site": sites,
        "dim_service": services,
        "sla_target": sla_targets,
        "outage_incident": outages.rename(columns={"start_date": "start_ts", "end_date": "end_ts"}),
        "fact_ticket": fact_ticket,
        "fact_ticket_status_history": fact_history,
        "_wide": wide,
        "_rejected": rejected,
    }
    return tables, run_report


def write_processed(tables: dict[str, pd.DataFrame], run_report: dict, processed_dir: Path) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)
    wide = tables["_wide"].drop(columns=["timezone"])
    wide.to_csv(
        processed_dir / PROCESSED_FILES["tickets"], index=False, date_format="%Y-%m-%dT%H:%M:%SZ"
    )
    tables["fact_ticket_status_history"].to_csv(
        processed_dir / PROCESSED_FILES["status_history"],
        index=False,
        date_format="%Y-%m-%dT%H:%M:%SZ",
    )
    tables["_rejected"].to_csv(processed_dir / PROCESSED_FILES["rejected"], index=False)
    (processed_dir / PROCESSED_FILES["report"]).write_text(json.dumps(run_report, indent=2) + "\n")
    log.info("wrote processed extract to %s", processed_dir)


def load_database(engine: Engine, tables: dict[str, pd.DataFrame], run_report: dict) -> dict:
    t0 = time.perf_counter()
    db.apply_schema(engine)
    counts = {}
    for name in (
        "dim_customer",
        "dim_site",
        "dim_service",
        "sla_target",
        "outage_incident",
        "fact_ticket",
        "fact_ticket_status_history",
    ):
        counts[name] = db.copy_dataframe(engine, name, tables[name])
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO load_run (snapshot_ts, source_dir, synthetic, row_counts, "
                "cleaning_report) VALUES (:snap, :src, true, CAST(:rc AS jsonb), "
                "CAST(:rep AS jsonb))"
            ),
            {
                "snap": run_report["snapshot_ts"],
                "src": run_report["source_dir"],
                "rc": json.dumps(counts),
                "rep": json.dumps(
                    {
                        "tickets": run_report["tickets"],
                        "status_history": run_report["status_history"],
                    }
                ),
            },
        )
        conn.exec_driver_sql("ANALYZE")
    views = db.apply_views(engine)
    elapsed = round(time.perf_counter() - t0, 2)
    log.info("database load complete in %.1fs; views: %s", elapsed, ", ".join(views))
    return {"loaded_rows": counts, "views": views, "load_seconds": elapsed}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Clean synthetic tickets and load PostgreSQL.")
    p.add_argument("--raw", type=Path, default=Path("data/raw"))
    p.add_argument("--processed", type=Path, default=Path("data/processed"))
    p.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL")
    p.add_argument("--skip-db", action="store_true", help="only write the processed extract")
    p.add_argument("--views-only", action="store_true", help="only (re)apply sql/views")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.views_only:
        applied = db.apply_views(db.get_engine(args.database_url))
        log.info("applied views: %s", ", ".join(applied))
        return
    tables, run_report = build_tables(args.raw)
    write_processed(tables, run_report, args.processed)
    if args.skip_db:
        return
    engine = db.get_engine(args.database_url)
    load_info = load_database(engine, tables, run_report)
    run_report.update(load_info)
    (args.processed / PROCESSED_FILES["report"]).write_text(json.dumps(run_report, indent=2) + "\n")


if __name__ == "__main__":
    main()
