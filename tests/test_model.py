"""The scoring module the API will import: load, validate, score, determinism."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from slawatch import model as M
from slawatch.features import CREATION_TIME_FEATURES, OUTCOME_FIELDS
from slawatch.train import ARTIFACT_FILE

RECORD = {
    "ticket_type": "incident",
    "severity": "major",
    "channel": "email",
    "tier": "gold",
    "industry": "retail",
    "region": "ontario",
    "province": "ON",
    "service_type": "sd_wan",
    "assignment_group": "field_ontario",
    "customer_id": "CUST-001",
    "priority": 2,
    "open_backlog_at_creation": 40,
    "sla_target_hours": 12.0,
    "creation_hour_local": 22,
    "creation_dow_local": 6,
    "has_active_outage": True,
    "is_weekend": True,
    "is_after_hours": True,
    "has_requested_resolution_date": False,
}


@pytest.fixture(scope="module")
def loaded(trained) -> M.LoadedModel:
    cfg, _ = trained
    return M.load_model(cfg.models_dir / ARTIFACT_FILE)


def test_feature_contract_is_creation_time_only():
    assert not set(M.MODEL_FEATURES) & set(OUTCOME_FIELDS)
    raw = set(CREATION_TIME_FEATURES) - set(M.DROPPED_CREATION_FEATURES)
    assert set(M.MODEL_FEATURES) == raw | {"has_active_outage"}
    assert len(M.MODEL_FEATURES) == len(set(M.MODEL_FEATURES))


def test_load_missing_artifact_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError, match="make train"):
        M.load_model(tmp_path / "nope.joblib")


def test_scores_hand_built_record(loaded):
    out = M.score([RECORD], model=loaded)
    assert len(out) == 1
    assert 0.0 <= out[0]["probability"] <= 1.0
    assert out[0]["risk_band"] in M.RISK_BANDS
    # a weekend-night outage ticket should score above a quiet business-hours one
    calm = dict(
        RECORD,
        has_active_outage=False,
        is_weekend=False,
        is_after_hours=False,
        creation_hour_local=10,
        creation_dow_local=1,
        channel="monitoring",
        open_backlog_at_creation=5,
    )
    p_hot, p_calm = (r["probability"] for r in M.score([RECORD, calm], model=loaded))
    assert p_hot > p_calm


def test_scores_dataframe_and_records_agree(loaded):
    df = pd.DataFrame([RECORD, RECORD])
    a = M.score(df, model=loaded)
    b = M.score([RECORD, RECORD], model=loaded)
    assert a == b
    assert M.score([], model=loaded) == []


def test_missing_feature_rejected(loaded):
    rec = dict(RECORD)
    del rec["severity"]
    with pytest.raises(ValidationError):
        M.score([rec], model=loaded)
    with pytest.raises(ValueError, match="missing feature columns: \\['severity'\\]"):
        M.score(pd.DataFrame([rec]), model=loaded)


def test_unknown_feature_and_outcome_columns_rejected(loaded):
    with pytest.raises(ValidationError):
        M.score([dict(RECORD, reopen_count=1)], model=loaded)
    with pytest.raises(ValidationError):
        M.score([dict(RECORD, sla_breached=True)], model=loaded)
    with pytest.raises(ValidationError):
        M.TicketFeatures(**dict(RECORD, severity="urgent"))
    with pytest.raises(ValueError, match="unknown categorical values"):
        M.score(pd.DataFrame([dict(RECORD, tier="diamond")]), model=loaded)


def test_dataframe_extras_ignored_and_outage_id_derived(loaded):
    df = pd.DataFrame([dict(RECORD, reopen_count=3, sla_breached=True)])
    df = df.drop(columns=["has_active_outage"]).assign(active_outage_id=["OUT-001"])
    X = M.prepare_features(df)
    assert list(X.columns) == M.MODEL_FEATURES
    assert bool(X.loc[0, "has_active_outage"]) is True
    assert (
        M.score(df, model=loaded)[0]["probability"]
        == M.score([RECORD], model=loaded)[0]["probability"]
    )


def test_unseen_customer_is_scored_not_rejected(loaded):
    out = M.score([dict(RECORD, customer_id="CUST-999")], model=loaded)
    assert 0.0 <= out[0]["probability"] <= 1.0


def test_probabilities_in_unit_interval_on_many_rows(loaded, trained):
    cfg, _ = trained
    from slawatch.train import load_processed

    wide = load_processed(cfg.processed_dir).head(500)
    p = loaded.predict_proba(wide)
    assert p.between(0, 1).all()
    bands = loaded.risk_band(p)
    assert (p[bands == "high"] >= loaded.band_thresholds["high"]).all()
    assert (p[bands == "low"] < loaded.band_thresholds["medium"]).all()


def test_training_is_deterministic_for_the_seed(trained, tmp_path):
    from slawatch.train import TrainConfig, train

    cfg, ev = trained
    cfg2 = TrainConfig(
        processed_dir=cfg.processed_dir,
        raw_dir=cfg.raw_dir,
        models_dir=tmp_path / "models",
        img_dir=tmp_path / "img",
        quick=True,
        skip_db=True,
        write_scores=False,
    )
    ev2 = train(cfg2)
    assert ev2["final_model"] == ev["final_model"]
    assert ev2["threshold"] == ev["threshold"]
    a = M.load_model(cfg.models_dir / ARTIFACT_FILE)
    b = M.load_model(tmp_path / "models" / ARTIFACT_FILE)
    rows = [RECORD, dict(RECORD, severity="low", tier="bronze")]
    np.testing.assert_array_equal(
        [r["probability"] for r in M.score(rows, model=a)],
        [r["probability"] for r in M.score(rows, model=b)],
    )
    assert M.score(rows, model=a) == M.score(rows, model=a)


def test_feature_schema_matches_pydantic_model():
    schema = M.feature_schema()
    assert [s["name"] for s in schema] == list(M.TicketFeatures.model_fields)
    for spec in schema:
        if spec["name"] in M.CATEGORICAL_LEVELS:
            assert spec["allowed_values"] == M.CATEGORICAL_LEVELS[spec["name"]]
