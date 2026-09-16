"""SLA-breach model: feature schema, artifact loading and scoring.

This module is what the scoring API imports. It knows nothing about training (see
``train.py``); it only needs the saved artifact and a raw feature frame or a list of records.

Feature contract
----------------
``MODEL_FEATURES`` is the exact set of columns the saved pipeline expects, derived from
``features.CREATION_TIME_FEATURES`` with three deliberate changes:

* ``active_outage_id`` (an identifier) becomes the boolean ``has_active_outage``;
* ``site_id`` and ``service_id`` are dropped (high-cardinality identifiers that added nothing
  on validation; see docs/model.md);
* ``customer_id`` is kept: 40 stable enterprise accounts with a real per-account effect.

Everything in ``features.OUTCOME_FIELDS`` is rejected at scoring time by construction: the
pipeline only ever sees ``MODEL_FEATURES``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from pydantic import BaseModel, ConfigDict, Field, field_validator

from slawatch import config as C
from slawatch.features import OUTCOME_FIELDS

MODEL_VERSION = "0.1.0"
DEFAULT_MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
DEFAULT_MODEL_PATH = DEFAULT_MODELS_DIR / "sla_breach.joblib"
DEFAULT_CARD_PATH = DEFAULT_MODELS_DIR / "model_card.json"

RISK_BANDS = ("low", "medium", "high")

# Allowed levels for every categorical feature. ``customer_id`` is validated against the
# levels seen in training (stored in the artifact), not here.
ASSIGNMENT_GROUPS = sorted(
    {f"field_{r}" for r in C.REGIONS} | {"voice_ops", "cloud_ops", "security_ops"}
)
PROVINCES = sorted(C.PROVINCE_TO_REGION)
CATEGORICAL_LEVELS: dict[str, list[str]] = {
    "ticket_type": list(C.TICKET_TYPES),
    "severity": list(C.SEVERITIES),
    "channel": list(C.CHANNELS),
    "tier": list(C.TIERS),
    "industry": list(C.INDUSTRIES),
    "region": sorted(C.REGIONS),
    "province": PROVINCES,
    "service_type": list(C.SERVICE_TYPES),
    "assignment_group": ASSIGNMENT_GROUPS,
}

CATEGORICAL_FEATURES = list(CATEGORICAL_LEVELS) + ["customer_id"]
NUMERIC_FEATURES = [
    "priority",
    "open_backlog_at_creation",
    "sla_target_hours",
    "creation_hour_local",
    "creation_dow_local",
]
BOOLEAN_FEATURES = [
    "has_active_outage",
    "is_weekend",
    "is_after_hours",
    "has_requested_resolution_date",
]
MODEL_FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES + BOOLEAN_FEATURES

DROPPED_CREATION_FEATURES = ["site_id", "service_id", "active_outage_id"]

assert not set(MODEL_FEATURES) & set(OUTCOME_FIELDS), "an outcome field leaked into the features"


def as_float(X: Any) -> np.ndarray:
    """Boolean block of the saved pipeline (``FunctionTransformer(as_float)``).

    Lives here, not in ``train.py``, because the artifact pickles a reference to this
    function by module path: the scoring runtime must be able to unpickle it without importing
    the training module (and, through it, the database layer).
    """
    return np.asarray(X, dtype=float)


def _levels_doc(name: str) -> str:
    return "One of: " + ", ".join(CATEGORICAL_LEVELS[name]) + " (case-insensitive)."


class TicketFeatures(BaseModel):
    """One ticket at creation time, as the scoring API receives it.

    Field order and names match ``MODEL_FEATURES``. Unknown fields are rejected so that a
    caller cannot silently pass an outcome column (``sla_breached``, ``reopen_count``...).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket_type: str = Field(description=_levels_doc("ticket_type"))
    severity: str = Field(description=_levels_doc("severity"))
    channel: str = Field(description="Intake channel. " + _levels_doc("channel"))
    tier: str = Field(description="Customer support tier. " + _levels_doc("tier"))
    industry: str = Field(description="Customer industry. " + _levels_doc("industry"))
    region: str = Field(description="Site region. " + _levels_doc("region"))
    province: str = Field(description="Site province code. " + _levels_doc("province"))
    service_type: str = Field(description="Affected service. " + _levels_doc("service_type"))
    assignment_group: str = Field(
        description="Technician group the ticket is routed to. " + _levels_doc("assignment_group")
    )
    customer_id: str = Field(
        min_length=1,
        max_length=64,
        description="Enterprise account id (``cust-001`` ... ``cust-040`` in the synthetic "
        "data). Ids unseen in training are scored with the one-hot column all zero, "
        "not rejected.",
    )
    priority: int = Field(ge=1, le=4, description="1 (highest) to 4 (lowest).")
    open_backlog_at_creation: int = Field(
        ge=0, description="Open tickets in the assignment group when this one was created."
    )
    sla_target_hours: float = Field(
        gt=0, description="Resolution SLA for this customer tier x severity, in hours."
    )
    creation_hour_local: int = Field(ge=0, le=23, description="Local hour of creation, 0-23.")
    creation_dow_local: int = Field(
        ge=0, le=6, description="Local day of week of creation, 0 = Monday ... 6 = Sunday."
    )
    has_active_outage: bool = Field(
        description="A regional outage incident was open at the site when the ticket was created."
    )
    is_weekend: bool = Field(description="Created on Saturday or Sunday (local time).")
    is_after_hours: bool = Field(description="Created outside 08:00-18:00 local time.")
    has_requested_resolution_date: bool = Field(
        description="The customer asked for a specific resolution date."
    )

    @field_validator(*CATEGORICAL_LEVELS, mode="before")
    @classmethod
    def _known_level(cls, value: Any, info) -> Any:
        if isinstance(value, str):
            value = normalise_level(info.field_name, value)
        allowed = CATEGORICAL_LEVELS[info.field_name]
        if value not in allowed:
            raise ValueError(f"{info.field_name!r} must be one of {allowed}, got {value!r}")
        return value


def normalise_level(name: str, value: str) -> str:
    """Categorical values are lower-case snake_case, except two-letter province codes."""
    value = value.strip()
    return value.upper() if name == "province" else value.lower()


def feature_schema() -> list[dict[str, Any]]:
    """Machine-readable feature list for the model card / API docs."""
    out: list[dict[str, Any]] = []
    for name in MODEL_FEATURES:
        spec: dict[str, Any] = {"name": name}
        if name in CATEGORICAL_LEVELS:
            spec["dtype"] = "category"
            spec["allowed_values"] = CATEGORICAL_LEVELS[name]
        elif name == "customer_id":
            spec["dtype"] = "category"
            spec["allowed_values"] = "customer ids seen in training (see training_levels)"
        elif name in BOOLEAN_FEATURES:
            spec["dtype"] = "bool"
        elif name == "sla_target_hours":
            spec["dtype"] = "float"
            spec["min"] = 0
        else:
            spec["dtype"] = "int"
            spec["min"], spec["max"] = {
                "priority": (1, 4),
                "open_backlog_at_creation": (0, None),
                "creation_hour_local": (0, 23),
                "creation_dow_local": (0, 6),
            }[name]
        out.append(spec)
    return out


# ------------------------------------------------------------------------------------------
# Feature preparation
# ------------------------------------------------------------------------------------------
def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return exactly ``MODEL_FEATURES`` from a wider creation-time frame.

    Accepts the processed extract (``synthetic_tickets_clean.csv``), the ``fact_ticket``
    table joined to its dimensions, or a frame built from ``TicketFeatures`` records.
    ``has_active_outage`` is derived from ``active_outage_id`` when absent. Extra columns are
    ignored; missing feature columns raise ``ValueError`` naming them.
    """
    out = df.copy()
    if "has_active_outage" not in out.columns:
        if "active_outage_id" not in out.columns:
            raise ValueError("missing feature columns: ['has_active_outage']")
        ids = out["active_outage_id"]
        out["has_active_outage"] = ids.notna() & (ids.astype(str).str.strip() != "")
    missing = [c for c in MODEL_FEATURES if c not in out.columns]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")
    out = out[MODEL_FEATURES].copy()
    for col in CATEGORICAL_FEATURES:
        values = out[col].astype("string").str.strip()
        out[col] = (values.str.upper() if col == "province" else values.str.lower()).astype(object)
    for col in BOOLEAN_FEATURES:
        out[col] = _to_bool(out[col])
    for col in NUMERIC_FEATURES:
        out[col] = pd.to_numeric(out[col], errors="raise")
    for col in (
        "priority",
        "open_backlog_at_creation",
        "creation_hour_local",
        "creation_dow_local",
    ):
        out[col] = out[col].astype("int64")
    out["sla_target_hours"] = out["sla_target_hours"].astype("float64")
    unknown = {
        col: sorted(set(out[col].dropna()) - set(levels))
        for col, levels in CATEGORICAL_LEVELS.items()
        if not set(out[col].dropna()) <= set(levels)
    }
    if unknown:
        raise ValueError(f"unknown categorical values: {unknown}")
    return out


def _to_bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    mapping = {"true": True, "false": False, "1": True, "0": False, "t": True, "f": False}
    converted = s.map(lambda v: mapping.get(str(v).strip().lower(), v) if pd.notna(v) else v)
    if converted.isna().any() or not converted.map(lambda v: isinstance(v, bool | int)).all():
        raise ValueError(f"column {s.name!r} must be boolean")
    return converted.astype(bool)


def records_to_frame(records: list[dict[str, Any]] | list[TicketFeatures]) -> pd.DataFrame:
    """Validate records through ``TicketFeatures`` and return a feature frame."""
    validated = [r if isinstance(r, TicketFeatures) else TicketFeatures(**r) for r in records]
    return pd.DataFrame([v.model_dump() for v in validated], columns=MODEL_FEATURES)


# ------------------------------------------------------------------------------------------
# Artifact
# ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LoadedModel:
    pipeline: Any  # fitted sklearn Pipeline taking a MODEL_FEATURES frame
    features: list[str]
    threshold: float  # "flag for review" cut-off (probability)
    band_thresholds: dict[str, float]  # {"medium": p, "high": p}
    training_levels: dict[str, list[str]]  # categorical levels seen in training
    card: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.card.get("model_version", MODEL_VERSION))

    def predict_proba(self, X: pd.DataFrame) -> pd.Series:
        X = prepare_features(X)
        p = self.pipeline.predict_proba(X[self.features])[:, 1]
        return pd.Series(p, index=X.index, name="probability").clip(0.0, 1.0)

    def risk_band(self, p: pd.Series) -> pd.Series:
        hi, mid = self.band_thresholds["high"], self.band_thresholds["medium"]
        band = pd.Series("low", index=p.index, dtype=object)
        band[p >= mid] = "medium"
        band[p >= hi] = "high"
        return band.rename("risk_band")


def save_model(path: Path, bundle: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path, compress=3)
    return path.stat().st_size


def load_model(path: Path | str = DEFAULT_MODEL_PATH) -> LoadedModel:
    """Load the artifact. Fails clearly when it is missing or was pickled by another sklearn.

    The artifact is a pickle of a fitted scikit-learn pipeline, so it is only guaranteed to
    unpickle under the exact version that wrote it; ``pyproject.toml`` pins that version and
    this check makes a mismatch a loud error instead of a silent warning.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"model artifact not found at {path}; run `make train`")
    bundle = joblib.load(path)
    features = list(bundle["features"])
    if features != MODEL_FEATURES:
        raise ValueError(
            "artifact feature list does not match slawatch.model.MODEL_FEATURES; "
            f"artifact={features}"
        )
    card = dict(bundle.get("card", {}))
    trained_with = card.get("sklearn_version")
    if trained_with is not None and trained_with != sklearn.__version__:
        raise RuntimeError(
            f"artifact {path} was trained with scikit-learn {trained_with} but "
            f"{sklearn.__version__} is installed; pin the version or retrain (`make train`)"
        )
    return LoadedModel(
        pipeline=bundle["pipeline"],
        features=features,
        threshold=float(bundle["threshold"]),
        band_thresholds={k: float(v) for k, v in bundle["band_thresholds"].items()},
        training_levels={k: list(v) for k, v in bundle["training_levels"].items()},
        card=card,
    )


def check_card(
    model: LoadedModel, card_path: Path | str, artifact_path: Path | str
) -> dict[str, Any]:
    """Verify that ``models/model_card.json`` describes exactly this artifact.

    The card is the documented, committed record of the model (version, thresholds, metrics,
    training window); the artifact is what actually scores. Both are committed, so a stale
    pair is possible; the API refuses to start on one. Returns the card on success.
    """
    card = read_model_card(card_path)
    artifact_path = Path(artifact_path)
    mismatches: list[str] = []

    def _cmp(name: str, ours: Any, theirs: Any) -> None:
        if ours != theirs:
            mismatches.append(f"{name}: artifact={ours!r} card={theirs!r}")

    _cmp("model_version", model.version, card.get("model_version"))
    _cmp("threshold", model.threshold, card.get("threshold"))
    _cmp("band_thresholds", model.band_thresholds, card.get("band_thresholds"))
    _cmp("trained_at", model.card.get("trained_at"), card.get("trained_at"))
    _cmp("sklearn_version", model.card.get("sklearn_version"), card.get("sklearn_version"))
    _cmp("features", [f["name"] for f in card.get("features", [])], model.features)
    _cmp("artifact.bytes", artifact_path.stat().st_size, card.get("artifact", {}).get("bytes"))
    if mismatches:
        raise RuntimeError(
            f"model card {card_path} does not describe artifact {artifact_path}: "
            + "; ".join(mismatches)
        )
    return card


@lru_cache(maxsize=1)
def get_default_model() -> LoadedModel:
    return load_model(DEFAULT_MODEL_PATH)


def score(
    df_or_records: pd.DataFrame | list[dict[str, Any]] | list[TicketFeatures],
    model: LoadedModel | None = None,
) -> list[dict[str, Any]]:
    """Score tickets. Returns one ``{"probability": float, "risk_band": str}`` per row."""
    model = model or get_default_model()
    if isinstance(df_or_records, pd.DataFrame):
        X = df_or_records
    else:
        if len(df_or_records) == 0:
            return []
        X = records_to_frame(list(df_or_records))
    p = model.predict_proba(X)
    bands = model.risk_band(p)
    return [
        {"probability": float(round(prob, 6)), "risk_band": str(band)}
        for prob, band in zip(p.to_numpy(), bands.to_numpy(), strict=True)
    ]


def read_model_card(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
