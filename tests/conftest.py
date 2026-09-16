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
