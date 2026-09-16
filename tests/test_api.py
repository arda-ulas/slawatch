"""The scoring API against the committed artifact (models/sla_breach.joblib + model_card.json).

These tests use the deployed model, not the small fixture-trained one, so the golden value
below pins the exact artifact that ships in the Lambda image.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slawatch import api
from slawatch import model as M
from slawatch.api import BATCH_MAX, EXAMPLE_CALM_TICKET, EXAMPLE_TICKET, Settings, create_app

REPO = Path(__file__).resolve().parents[1]
CARD = json.loads((REPO / "models" / "model_card.json").read_text())

# Golden values measured on the committed artifact (model_version 0.1.0, trained
# 2026-09-16T19:49:21+00:00). If the model is retrained, update these together with the card.
GOLDEN = {
    "hot": 0.756159,  # EXAMPLE_TICKET: major incident, weekend night, outage, backlog 40
    "calm": 0.034924,  # EXAMPLE_CALM_TICKET: same ticket on a quiet weekday morning
}
TOL = 1e-4


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(api.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_version"] == CARD["model_version"] == "0.1.0"
    assert body["trained_at"] == CARD["trained_at"]
    assert body["training_window"] == {
        "first_month": CARD["data"]["split"]["train"]["first_month"],
        "last_month": CARD["data"]["split"]["train"]["last_month"],
    }
    assert body["synthetic_data"] is True


def test_model_card_endpoint_serves_the_committed_card(client):
    r = client.get("/v1/model")
    assert r.status_code == 200
    body = r.json()
    assert body == CARD
    assert body["synthetic_data"] is True
    assert [f["name"] for f in body["features"]] == M.MODEL_FEATURES
    assert body["metrics_test"]["final"]["roc_auc"] > body["metrics_test"]["baseline"]["roc_auc"]


def test_score_valid_ticket(client):
    r = client.post("/v1/score", json=EXAMPLE_TICKET)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "probability",
        "risk_band",
        "flag_for_review",
        "threshold",
        "model_version",
    }
    assert 0.0 <= body["probability"] <= 1.0
    assert body["risk_band"] in M.RISK_BANDS
    assert body["threshold"] == CARD["threshold"]
    assert body["model_version"] == CARD["model_version"]
    assert body["flag_for_review"] == (body["probability"] >= body["threshold"])


def test_golden_values_pin_the_committed_artifact(client):
    hot = client.post("/v1/score", json=EXAMPLE_TICKET).json()
    calm = client.post("/v1/score", json=EXAMPLE_CALM_TICKET).json()
    assert hot["probability"] == pytest.approx(GOLDEN["hot"], abs=TOL)
    assert calm["probability"] == pytest.approx(GOLDEN["calm"], abs=TOL)
    assert hot["risk_band"] == "high" and hot["flag_for_review"] is True
    assert calm["risk_band"] == "low" and calm["flag_for_review"] is False


def test_categoricals_are_case_insensitive(client):
    upper = {**EXAMPLE_TICKET, "severity": "MAJOR", "province": "on", "customer_id": "CUST-001"}
    a = client.post("/v1/score", json=EXAMPLE_TICKET).json()
    b = client.post("/v1/score", json=upper).json()
    assert a["probability"] == b["probability"]


def test_batch_scores_in_order_and_matches_single(client):
    r = client.post("/v1/score/batch", json={"tickets": [EXAMPLE_TICKET, EXAMPLE_CALM_TICKET]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["threshold"] == CARD["threshold"]
    assert body["model_version"] == CARD["model_version"]
    singles = [
        client.post("/v1/score", json=t).json() for t in (EXAMPLE_TICKET, EXAMPLE_CALM_TICKET)
    ]
    for got, single in zip(body["results"], singles, strict=True):
        assert got == {k: single[k] for k in ("probability", "risk_band", "flag_for_review")}


def test_batch_cap(client):
    ok = client.post("/v1/score/batch", json={"tickets": [EXAMPLE_TICKET] * BATCH_MAX})
    assert ok.status_code == 200
    assert ok.json()["count"] == BATCH_MAX
    too_many = client.post("/v1/score/batch", json={"tickets": [EXAMPLE_TICKET] * (BATCH_MAX + 1)})
    assert too_many.status_code == 422
    assert too_many.json()["detail"][0]["loc"] == ["body", "tickets"]
    empty = client.post("/v1/score/batch", json={"tickets": []})
    assert empty.status_code == 422


def test_batch_reports_offending_index_and_field(client):
    bad = {**EXAMPLE_TICKET, "tier": "diamond"}
    r = client.post("/v1/score/batch", json={"tickets": [EXAMPLE_TICKET, bad]})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "tickets", 1, "tier"]


@pytest.mark.parametrize(
    ("mutation", "field", "error_type"),
    [
        ({"severity": None}, "severity", "missing"),  # sentinel: key removed below
        ({"severity": "urgent"}, "severity", "value_error"),
        ({"channel": "fax"}, "channel", "value_error"),
        ({"priority": 5}, "priority", "less_than_equal"),
        ({"priority": 0}, "priority", "greater_than_equal"),
        ({"creation_hour_local": 24}, "creation_hour_local", "less_than_equal"),
        ({"creation_dow_local": -1}, "creation_dow_local", "greater_than_equal"),
        ({"open_backlog_at_creation": -3}, "open_backlog_at_creation", "greater_than_equal"),
        ({"sla_target_hours": 0}, "sla_target_hours", "greater_than"),
        ({"priority": "two"}, "priority", "int_parsing"),
        ({"has_active_outage": "maybe"}, "has_active_outage", "bool_parsing"),
        ({"sla_breached": True}, "sla_breached", "extra_forbidden"),
        ({"reopen_count": 1}, "reopen_count", "extra_forbidden"),
    ],
)
def test_validation_errors_are_422_and_name_the_field(client, mutation, field, error_type):
    ticket = {**EXAMPLE_TICKET, **mutation}
    if error_type == "missing":
        del ticket[field]
    r = client.post("/v1/score", json=ticket)
    assert r.status_code == 422, r.text
    (err,) = r.json()["detail"]
    assert err["loc"] == ["body", field]
    assert err["type"] == error_type


def test_malformed_json_is_422(client):
    r = client.post("/v1/score", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_openapi_documents_examples_and_synthetic_data(client):
    spec = client.get("/openapi.json").json()
    assert "synthetic" in spec["info"]["description"].lower()
    paths = spec["paths"]
    assert set(paths) >= {"/health", "/v1/score", "/v1/score/batch", "/v1/model"}
    body = paths["/v1/score"]["post"]["requestBody"]["content"]["application/json"]
    assert "weekend_outage" in body["examples"]
    props = spec["components"]["schemas"]["TicketFeatures"]["properties"]
    assert "incident" in props["ticket_type"]["description"]
    assert str(BATCH_MAX) in paths["/v1/score/batch"]["post"]["summary"]


# ------------------------------------------------------------------------------------------
# Startup behaviour
# ------------------------------------------------------------------------------------------
def test_missing_artifact_fails_at_startup(tmp_path):
    with pytest.raises(FileNotFoundError, match="model artifact not found"):
        create_app(Settings(model_path=tmp_path / "nope.joblib"))


def test_missing_card_fails_at_startup(tmp_path):
    with pytest.raises(FileNotFoundError, match="model card not found"):
        create_app(Settings(model_path=M.DEFAULT_MODEL_PATH, card_path=tmp_path / "nope.json"))


def test_stale_card_is_rejected(tmp_path):
    stale = dict(CARD, threshold=0.5)
    (tmp_path / "model_card.json").write_text(json.dumps(stale))
    with pytest.raises(RuntimeError, match="does not describe artifact.*threshold"):
        create_app(
            Settings(model_path=M.DEFAULT_MODEL_PATH, card_path=tmp_path / "model_card.json")
        )


def test_env_configured_app_serves_a_fixture_trained_model(trained, monkeypatch):
    """An artifact + card pair produced by `slawatch-train` elsewhere is accepted via env."""
    from slawatch.train import ARTIFACT_FILE

    cfg, _ = trained
    monkeypatch.setenv("SLAWATCH_MODEL_PATH", str(cfg.models_dir / ARTIFACT_FILE))
    monkeypatch.delenv("SLAWATCH_MODEL_CARD_PATH", raising=False)
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").json()["model_version"] == "0.1.0"
        r = c.post("/v1/score", json=EXAMPLE_TICKET)
        assert r.status_code == 200
        assert 0.0 <= r.json()["probability"] <= 1.0


def test_committed_artifact_matches_card_and_pin():
    """The traceability check itself: artifact <-> models/model_card.json <-> pyproject pin."""
    import sklearn

    model = M.load_model(M.DEFAULT_MODEL_PATH)
    card = M.check_card(model, M.DEFAULT_CARD_PATH, M.DEFAULT_MODEL_PATH)
    assert card["sklearn_version"] == sklearn.__version__
    pyproject = (REPO / "pyproject.toml").read_text()
    assert f'"scikit-learn=={sklearn.__version__}"' in pyproject
    assert M.DEFAULT_MODEL_PATH.stat().st_size == card["artifact"]["bytes"]
