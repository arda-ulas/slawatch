"""PostgreSQL helpers: engine from env, DDL from files, fast COPY loads."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text

log = logging.getLogger("slawatch.db")

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = REPO_ROOT / "sql"
SCHEMA_FILE = SQL_DIR / "schema.sql"
VIEWS_DIR = SQL_DIR / "views"

DEFAULT_DATABASE_URL = "postgresql+psycopg://slawatch:slawatch@localhost:5432/slawatch"


def get_database_url(explicit: str | None = None, env_var: str = "DATABASE_URL") -> str:
    if explicit:
        return explicit
    load_dotenv()
    return os.environ.get(env_var, DEFAULT_DATABASE_URL)


def get_engine(url: str | None = None) -> Engine:
    return create_engine(get_database_url(url), future=True)


def run_sql_file(engine: Engine, path: Path) -> None:
    sql = path.read_text()
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    log.info("applied %s", path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path)


def apply_schema(engine: Engine) -> None:
    run_sql_file(engine, SCHEMA_FILE)


def view_files() -> list[Path]:
    return sorted(VIEWS_DIR.glob("*.sql"))


def apply_views(engine: Engine) -> list[str]:
    applied = []
    for path in view_files():
        run_sql_file(engine, path)
        applied.append(path.name)
    return applied


def copy_dataframe(engine: Engine, table: str, df: pd.DataFrame, chunk_rows: int = 50_000) -> int:
    """Bulk-load a frame with psycopg COPY ... FROM STDIN (CSV). Empty strings become NULL."""
    cols = ", ".join(df.columns)
    stmt = f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"
    with engine.begin() as conn:
        cur = conn.connection.driver_connection.cursor()
        with cur.copy(stmt) as copy:
            for start in range(0, len(df), chunk_rows):
                buf = io.StringIO()
                df.iloc[start : start + chunk_rows].to_csv(
                    buf, index=False, header=False, na_rep="", date_format="%Y-%m-%dT%H:%M:%S%z"
                )
                copy.write(buf.getvalue())
    log.info("loaded %d rows into %s", len(df), table)
    return len(df)


def scalar(engine: Engine, sql: str) -> object:
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def query(engine: Engine, sql: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)
