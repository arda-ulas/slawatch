"""Training smoke test on the small generated sample (runs in well under 30 s)."""

from __future__ import annotations

import json

import pandas as pd

from slawatch import model as M
from slawatch.features import OUTCOME_FIELDS
from slawatch.train import (
    ARTIFACT_FILE,
    CARD_FILE,
    EVAL_FILE,
    SCORES_FILE,
    SplitConfig,
    assign_split,
)


def test_training_writes_artifact_card_scores_and_plots(trained):
    cfg, ev = trained
    assert (cfg.models_dir / ARTIFACT_FILE).exists()
    assert (cfg.models_dir / EVAL_FILE).exists()
    card = json.loads((cfg.models_dir / CARD_FILE).read_text())
    assert card["synthetic_data"] is True
    assert card["estimator"] in ("lr", "hgb")
    assert [f["name"] for f in card["features"]] == M.MODEL_FEATURES
    assert card["data"]["generator_seed"] == 7
    assert 0 < card["threshold"] < 1
    assert card["band_thresholds"]["medium"] <= card["band_thresholds"]["high"]
    assert card["artifact"]["bytes"] == (cfg.models_dir / ARTIFACT_FILE).stat().st_size
    for png in ev["plots"]:
        assert (cfg.img_dir / png).stat().st_size > 0
    assert ev["runtime_seconds"] < 60


def test_split_is_time_ordered_and_metrics_sane(trained):
    _, ev = trained
    s = ev["split"]
    assert s["train"]["last_month"] < s["validation"]["first_month"]
    assert s["validation"]["last_month"] < s["test"]["first_month"]
    final = ev["test"][ev["final_model"]]
    assert 0.5 <= final["roc_auc"] <= 1.0
    assert final["pr_auc"] > ev["test"]["baseline_prevalence"]["pr_auc"]
    assert 0 <= final["brier"] <= 0.25
    assert len(ev["reliability_test"][ev["final_model"]]) == 10
    for part in ("validation", "test"):
        got = {r["feature"] for r in ev[f"permutation_importance_{part}"]}
        assert got == set(M.MODEL_FEATURES)
    assert ev["leakage_checks"]["outcome_fields_in_features"] == []
    assert ev["leakage_checks"]["shuffled_label_validation"]["roc_auc"] < 0.6


def test_scores_csv_covers_every_ticket_with_valid_bands(trained):
    cfg, ev = trained
    scores = pd.read_csv(cfg.processed_dir / SCORES_FILE)
    tickets = pd.read_csv(cfg.processed_dir / "synthetic_tickets_clean.csv", usecols=["ticket_id"])
    assert len(scores) == len(tickets) == ev["risk_scores"]["rows"]
    assert scores["ticket_id"].is_unique
    assert scores["probability"].between(0, 1).all()
    assert set(scores["risk_band"]) <= set(M.RISK_BANDS)
    assert set(scores["split"]) <= {"train", "validation", "test", "unlabelled"}
    assert not set(scores.columns) & (set(OUTCOME_FIELDS) - {"sla_breached", "status"})


def test_assign_split_boundaries():
    ts = pd.Series(
        pd.to_datetime(["2025-12-31T23:59:59Z", "2026-01-01T00:00:00Z", "2026-04-01T00:00:00Z"])
    )
    got = assign_split(ts, SplitConfig(train_end="2025-12", val_end="2026-03")).tolist()
    assert got == ["train", "validation", "test"]
