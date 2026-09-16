from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from slawatch.generate import GeneratedData, GenerationParams, generate, write_outputs

SMALL_PARAMS = GenerationParams(seed=7, n_tickets=4000)

load_dotenv()
TEST_URL = os.environ.get(
    "SLAWATCH_TEST_DATABASE_URL",
    "postgresql+psycopg://slawatch:slawatch@localhost:5432/slawatch_test",
)


@pytest.fixture(scope="session")
def small_data() -> GeneratedData:
    return generate(SMALL_PARAMS)


@pytest.fixture(scope="session")
def small_raw_dir(tmp_path_factory: pytest.TempPathFactory, small_data: GeneratedData) -> Path:
    out = tmp_path_factory.mktemp("raw")
    write_outputs(small_data, out)
    return out


@pytest.fixture(scope="session")
def small_processed_dir(tmp_path_factory: pytest.TempPathFactory, small_raw_dir: Path) -> Path:
    from slawatch import pipeline

    out = tmp_path_factory.mktemp("processed")
    tables, run_report = pipeline.build_tables(small_raw_dir)
    pipeline.write_processed(tables, run_report, out)
    return out


@pytest.fixture(scope="session")
def trained(tmp_path_factory: pytest.TempPathFactory, small_processed_dir: Path, small_raw_dir):
    """Quick training run on the small sample: (config, evaluation dict)."""
    from slawatch.train import TrainConfig, train

    models = tmp_path_factory.mktemp("models")
    cfg = TrainConfig(
        processed_dir=small_processed_dir,
        raw_dir=small_raw_dir,
        models_dir=models,
        img_dir=models / "img",
        quick=True,
        skip_db=True,
    )
    return cfg, train(cfg)


# ------------------------------------------------------------------------------------------
# PostgreSQL (integration tests). Skipped when unreachable, a failure under SLAWATCH_REQUIRE_DB.
# ------------------------------------------------------------------------------------------
def ensure_test_database(url: str = TEST_URL) -> bool:
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


def require_test_database() -> str:
    if not ensure_test_database(TEST_URL):
        if os.environ.get("SLAWATCH_REQUIRE_DB"):
            pytest.fail(f"SLAWATCH_REQUIRE_DB is set but PostgreSQL at {TEST_URL} is unreachable")
        pytest.skip("PostgreSQL not reachable; run `make db-up`")
    return TEST_URL


def load_scored_db(small_raw_dir: Path, trained) -> Engine:
    """Load the small sample into the test database with the quick model's risk scores."""
    import pandas as pd

    from slawatch import db, pipeline
    from slawatch.train import SCORES_FILE, write_scores_to_db

    engine = db.get_engine(require_test_database())
    tables, run_report = pipeline.build_tables(small_raw_dir)
    pipeline.load_database(engine, tables, run_report)
    cfg, _ = trained
    scores = pd.read_csv(cfg.processed_dir / SCORES_FILE)
    write_scores_to_db(engine, scores, "test-quick")
    return engine
