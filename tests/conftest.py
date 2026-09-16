from __future__ import annotations

from pathlib import Path

import pytest

from slawatch.generate import GeneratedData, GenerationParams, generate, write_outputs

SMALL_PARAMS = GenerationParams(seed=7, n_tickets=4000)


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
