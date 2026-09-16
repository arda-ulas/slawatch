"""Train and evaluate the SLA-breach classifier on the processed synthetic extract.

Usage:
    uv run slawatch-train                       # data/processed -> models/, docs/img/, scores CSV
    uv run slawatch-train --skip-db             # do not write the ticket_risk_score table
    uv run slawatch-train --quick               # tiny grids (used by the CI smoke test)

Protocol (see docs/model.md): time-based split by creation month; every choice (feature
handling, hyperparameters, threshold) is made on the validation months; the test months are
scored once, at the end, for the numbers in the model card.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import KFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import (
    FunctionTransformer,
    OneHotEncoder,
    OrdinalEncoder,
    StandardScaler,
    TargetEncoder,
)

from slawatch import db
from slawatch import model as M
from slawatch.features import OUTCOME_FIELDS
from slawatch.pipeline import PROCESSED_FILES

log = logging.getLogger("slawatch.train")

SCORES_FILE = "ticket_risk_scores.csv"
ARTIFACT_FILE = "sla_breach.joblib"
CARD_FILE = "model_card.json"
EVAL_FILE = "evaluation.json"

TIME_COLS = ["creation_hour_local", "creation_dow_local"]
OTHER_NUMERIC = [c for c in M.NUMERIC_FEATURES if c not in TIME_COLS]
TOP_SHARE = 0.10  # operating policy: review the riskiest 10 % of new tickets
MEDIUM_SHARE = 0.30  # watch list: riskiest 30 %


@dataclass(frozen=True)
class SplitConfig:
    """Inclusive month bounds (YYYY-MM). Train <= train_end < validation <= val_end < test."""

    train_end: str = "2025-12"
    val_end: str = "2026-03"


@dataclass
class TrainConfig:
    processed_dir: Path = Path("data/processed")
    raw_dir: Path = Path("data/raw")
    models_dir: Path = Path("models")
    img_dir: Path = Path("docs/img")
    split: SplitConfig = field(default_factory=SplitConfig)
    seed: int = 0
    quick: bool = False
    skip_db: bool = False
    database_url: str | None = None
    write_scores: bool = True


# ------------------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------------------
def load_processed(processed_dir: Path) -> pd.DataFrame:
    path = processed_dir / PROCESSED_FILES["tickets"]
    df = pd.read_csv(
        path,
        dtype={"active_outage_id": "string", "priority": "int64"},
        parse_dates=["creation_ts", "resolution_ts"],
        keep_default_na=True,
    )
    df["sla_breached"] = df["sla_breached"].map(
        {True: True, False: False, "True": True, "False": False}
    )
    df["sla_breached"] = df["sla_breached"].astype("boolean")
    df["is_resolved"] = df["is_resolved"].astype(bool)
    return df


def training_rows(wide: pd.DataFrame) -> pd.DataFrame:
    """Resolved tickets with a known outcome; ``y`` = 1 when the SLA was breached."""
    keep = wide["is_resolved"] & wide["sla_breached"].notna()
    out = wide.loc[keep].copy()
    out["y"] = out["sla_breached"].astype(bool).astype(int)
    return out


def assign_split(creation_ts: pd.Series, cfg: SplitConfig) -> pd.Series:
    month = creation_ts.dt.strftime("%Y-%m")
    split = pd.Series("test", index=creation_ts.index, dtype=object)
    split[month <= cfg.val_end] = "validation"
    split[month <= cfg.train_end] = "train"
    return split.rename("split")


# ------------------------------------------------------------------------------------------
# Pipelines
# ------------------------------------------------------------------------------------------
# ``as_float`` (the boolean block) is defined in ``slawatch.model`` so that the pickled
# artifact resolves it without importing this module at scoring time.
as_float = M.as_float


def cyclical_time(X: np.ndarray) -> np.ndarray:
    """[hour, dow] -> [sin h, cos h, sin d, cos d]."""
    X = np.asarray(X, dtype=float)
    h, d = X[:, 0] * 2 * np.pi / 24.0, X[:, 1] * 2 * np.pi / 7.0
    return np.column_stack([np.sin(h), np.cos(h), np.sin(d), np.cos(d)])


def _cyclical_names(_transformer, _input) -> list[str]:
    return ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]


def _time_block(time_encoding: str, scale: bool) -> tuple[str, Any, list[str]]:
    if time_encoding == "onehot":
        return ("time", OneHotEncoder(handle_unknown="ignore"), TIME_COLS)
    if time_encoding == "cyclical":
        tf = FunctionTransformer(cyclical_time, feature_names_out=_cyclical_names)
        return ("time", tf, TIME_COLS)
    if time_encoding == "numeric":
        return ("time", StandardScaler() if scale else "passthrough", TIME_COLS)
    raise ValueError(f"unknown time_encoding {time_encoding!r}")


def make_lr_pipeline(
    C: float = 1.0,
    time_encoding: str = "numeric",
    categorical: list[str] | None = None,
    class_weight: str | None = None,
) -> Pipeline:
    cats = M.CATEGORICAL_FEATURES if categorical is None else categorical
    prep = ColumnTransformer(
        [
            ("cat", OneHotEncoder(handle_unknown="ignore"), cats),
            ("num", StandardScaler(), OTHER_NUMERIC),
            _time_block(time_encoding, scale=True),
            ("bool", FunctionTransformer(as_float), M.BOOLEAN_FEATURES),
        ],
        remainder="drop",
    )
    est = LogisticRegression(C=C, max_iter=3000, class_weight=class_weight)
    return Pipeline([("prep", prep), ("est", est)])


def make_hgb_pipeline(
    params: dict[str, Any] | None = None,
    time_encoding: str = "numeric",
    categorical: list[str] | None = None,
    target_encoded: list[str] | None = None,
    class_weight: str | None = None,
    seed: int = 0,
) -> Pipeline:
    cats = M.CATEGORICAL_FEATURES if categorical is None else categorical
    te = target_encoded or []
    blocks: list[tuple[str, Any, list[str]]] = [
        (
            "cat",
            OrdinalEncoder(
                handle_unknown="use_encoded_value", unknown_value=-1, encoded_missing_value=-1
            ),
            cats,
        )
    ]
    if te:
        blocks.append(("te", TargetEncoder(cv=KFold(5, shuffle=True, random_state=seed)), te))
    blocks.append(("num", "passthrough", OTHER_NUMERIC))
    blocks.append(_time_block(time_encoding, scale=False))
    blocks.append(("bool", FunctionTransformer(as_float), M.BOOLEAN_FEATURES))
    prep = ColumnTransformer(blocks, remainder="drop")
    est = HistGradientBoostingClassifier(
        random_state=seed,
        early_stopping=False,
        categorical_features=list(range(len(cats))),
        class_weight=class_weight,
        **(params or {}),
    )
    return Pipeline([("prep", prep), ("est", est)])


# ------------------------------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------------------------------
def ranking_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    return {
        "roc_auc": float(roc_auc_score(y, p)),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))),
    }


def at_threshold(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    flagged = p >= threshold
    tp = int((flagged & (y == 1)).sum())
    prevalence = float(y.mean())
    precision = tp / flagged.sum() if flagged.sum() else 0.0
    recall = tp / (y == 1).sum() if (y == 1).sum() else 0.0
    return {
        "threshold": float(threshold),
        "flagged": int(flagged.sum()),
        "flagged_share": float(flagged.mean()),
        "true_positives": tp,
        "precision": float(precision),
        "recall": float(recall),
        "lift_vs_prevalence": float(precision / prevalence) if prevalence else 0.0,
    }


def reliability_table(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> list[dict[str, float]]:
    """Equal-count bins of predicted probability vs observed breach rate."""
    order = np.argsort(p)
    rows = []
    for chunk in np.array_split(order, n_bins):
        rows.append(
            {
                "bin_lo": float(p[chunk].min()),
                "bin_hi": float(p[chunk].max()),
                "n": int(len(chunk)),
                "mean_predicted": float(p[chunk].mean()),
                "observed_rate": float(y[chunk].mean()),
            }
        )
    return rows


def expected_calibration_error(table: list[dict[str, float]]) -> float:
    n = sum(r["n"] for r in table)
    return float(sum(r["n"] * abs(r["mean_predicted"] - r["observed_rate"]) for r in table) / n)


def segment_metrics(
    df: pd.DataFrame, p: np.ndarray, by: str, threshold: float
) -> list[dict[str, Any]]:
    rows = []
    y_all = df["y"].to_numpy()
    for level, idx in df.groupby(by, sort=True).indices.items():
        y, pp = y_all[idx], p[idx]
        row: dict[str, Any] = {by: level, "n": int(len(idx)), "breach_rate": float(y.mean())}
        if 0 < y.sum() < len(y):
            row["roc_auc"] = float(roc_auc_score(y, pp))
            row["pr_auc"] = float(average_precision_score(y, pp))
        else:
            row["roc_auc"] = row["pr_auc"] = None
        op = at_threshold(y, pp, threshold)
        row.update(
            {
                "flagged_share": op["flagged_share"],
                "precision": op["precision"],
                "recall": op["recall"],
            }
        )
        rows.append(row)
    return rows


# ------------------------------------------------------------------------------------------
# Experiments and tuning (validation only)
# ------------------------------------------------------------------------------------------
def _fit_eval(pipe: Pipeline, tr: pd.DataFrame, va: pd.DataFrame, cols: list[str]) -> dict:
    t0 = time.perf_counter()
    pipe.fit(tr[cols], tr["y"])
    p = pipe.predict_proba(va[cols])[:, 1]
    out = ranking_metrics(va["y"].to_numpy(), p)
    out["fit_seconds"] = round(time.perf_counter() - t0, 2)
    return out


def feature_experiments(tr: pd.DataFrame, va: pd.DataFrame, seed: int) -> list[dict[str, Any]]:
    """Identifier handling, hour/day encoding and class weighting, judged on validation."""
    base_cats = [c for c in M.CATEGORICAL_FEATURES if c != "customer_id"]
    rows: list[dict[str, Any]] = []

    def add(group: str, variant: str, model: str, pipe: Pipeline, extra: list[str]) -> None:
        cols = M.MODEL_FEATURES + [c for c in extra if c not in M.MODEL_FEATURES]
        cols = [c for c in cols if c in tr.columns]
        res = _fit_eval(pipe, tr, va, cols)
        rows.append({"group": group, "variant": variant, "model": model, **res})
        log.info("experiment %-14s %-40s %-4s %s", group, variant, model, _fmt(res))

    ids = ["customer_id", "site_id", "service_id"]
    add("identifiers", "no identifiers", "hgb", make_hgb_pipeline(categorical=base_cats), [])
    add("identifiers", "customer_id as native category", "hgb", make_hgb_pipeline(seed=seed), [])
    add(
        "identifiers",
        "customer_id target-encoded",
        "hgb",
        make_hgb_pipeline(categorical=base_cats, target_encoded=["customer_id"], seed=seed),
        [],
    )
    add(
        "identifiers",
        "customer_id native + site_id/service_id target-encoded",
        "hgb",
        make_hgb_pipeline(target_encoded=["site_id", "service_id"], seed=seed),
        ids,
    )
    add(
        "identifiers",
        "site_id/service_id target-encoded, no customer_id",
        "hgb",
        make_hgb_pipeline(categorical=base_cats, target_encoded=["site_id", "service_id"]),
        ids,
    )
    add("identifiers", "no identifiers", "lr", make_lr_pipeline(categorical=base_cats), [])
    add("identifiers", "customer_id one-hot", "lr", make_lr_pipeline(), [])

    for enc in ("numeric", "onehot", "cyclical"):
        add("time_encoding", enc, "lr", make_lr_pipeline(time_encoding=enc), [])
        add("time_encoding", enc, "hgb", make_hgb_pipeline(time_encoding=enc, seed=seed), [])

    for cw in (None, "balanced"):
        label = "balanced" if cw else "unweighted"
        add("class_weight", label, "lr", make_lr_pipeline(class_weight=cw), [])
        add("class_weight", label, "hgb", make_hgb_pipeline(class_weight=cw, seed=seed), [])
    return rows


def pick_time_encoding(rows: list[dict[str, Any]], model: str) -> str:
    cands = [r for r in rows if r["group"] == "time_encoding" and r["model"] == model]
    best = max(cands, key=lambda r: (round(r["pr_auc"], 4), -r["fit_seconds"]))
    return str(best["variant"])


def grids(quick: bool) -> tuple[list[float], list[dict[str, Any]]]:
    if quick:
        return [1.0], [{"learning_rate": 0.1, "max_leaf_nodes": 15, "max_iter": 50}]
    lr_c = [0.01, 0.1, 1.0, 10.0]
    hgb = [
        {
            "learning_rate": lr,
            "max_leaf_nodes": leaves,
            "min_samples_leaf": leaf_n,
            "max_iter": iters,
            "l2_regularization": 1.0,
        }
        for lr in (0.05, 0.1)
        for leaves in (15, 31)
        for leaf_n in (50, 200)
        for iters in (200,)
    ]
    return lr_c, hgb


def tune(
    tr: pd.DataFrame,
    va: pd.DataFrame,
    time_enc: dict[str, str],
    quick: bool,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    lr_c, hgb_grid = grids(quick)
    cols = M.MODEL_FEATURES
    results: list[dict[str, Any]] = []
    for C in lr_c:
        res = _fit_eval(make_lr_pipeline(C=C, time_encoding=time_enc["lr"]), tr, va, cols)
        results.append({"model": "lr", "params": {"C": C}, **res})
        log.info("grid lr C=%-5s %s", C, _fmt(res))
    for params in hgb_grid:
        pipe = make_hgb_pipeline(params, time_encoding=time_enc["hgb"], seed=seed)
        res = _fit_eval(pipe, tr, va, cols)
        results.append({"model": "hgb", "params": params, **res})
        log.info("grid hgb %s %s", params, _fmt(res))
    best = {}
    for name in ("lr", "hgb"):
        best[name] = max(
            (r for r in results if r["model"] == name), key=lambda r: round(r["pr_auc"], 6)
        )
    return results, best


def permutation_table(
    pipe: Pipeline, part: pd.DataFrame, n_repeats: int, seed: int
) -> list[dict[str, Any]]:
    """Drop in PR-AUC when each raw feature column is permuted, largest first."""
    cols = M.MODEL_FEATURES
    pi = permutation_importance(
        pipe,
        part[cols],
        part["y"],
        scoring="average_precision",
        n_repeats=n_repeats,
        random_state=seed,
    )
    return sorted(
        (
            {"feature": f, "importance_mean": float(m), "importance_std": float(s)}
            for f, m, s in zip(cols, pi.importances_mean, pi.importances_std, strict=True)
        ),
        key=lambda r: -r["importance_mean"],
    )


# ------------------------------------------------------------------------------------------
# Leakage probes and the optional regression
# ------------------------------------------------------------------------------------------
def leakage_checks(
    tr: pd.DataFrame, va: pd.DataFrame, te: pd.DataFrame, seed: int
) -> dict[str, Any]:
    cols = M.MODEL_FEATURES
    out: dict[str, Any] = {
        "outcome_fields_in_features": sorted(set(cols) & set(OUTCOME_FIELDS)),
        "dropped_creation_time_columns": M.DROPPED_CREATION_FEATURES,
    }
    rng = np.random.default_rng(seed)
    shuffled = tr.copy()
    shuffled["y"] = rng.permutation(shuffled["y"].to_numpy())
    res = _fit_eval(make_hgb_pipeline(seed=seed), shuffled, va, cols)
    out["shuffled_label_validation"] = res
    # positive control: an outcome column *would* be caught by a jump in validation score
    probe_cols = cols + ["reopen_count"]
    probe = make_hgb_pipeline(seed=seed)
    probe.steps[0] = (
        "prep",
        ColumnTransformer(
            probe.named_steps["prep"].transformers + [("leak", "passthrough", ["reopen_count"])]
        ),
    )
    out["reopen_count_positive_control_validation"] = _fit_eval(probe, tr, va, probe_cols)
    # adversarial validation: can a model tell train rows from test rows on features alone?
    both = pd.concat([tr.assign(is_test=0), te.assign(is_test=1)], ignore_index=True)
    perm = rng.permutation(len(both))
    half = len(both) // 2
    a, b = both.iloc[perm[:half]], both.iloc[perm[half:]]
    adv = make_hgb_pipeline({"max_iter": 100}, seed=seed)
    adv.fit(a[cols], a["is_test"])
    out["adversarial_train_vs_test_auc"] = float(
        roc_auc_score(b["is_test"], adv.predict_proba(b[cols])[:, 1])
    )
    return out


def time_to_resolve_regression(
    tr: pd.DataFrame, te: pd.DataFrame, time_enc: str, quick: bool, seed: int
) -> dict[str, Any]:
    """HistGradientBoostingRegressor on log1p(resolution_hours); test metrics only."""
    cols = M.MODEL_FEATURES
    base = make_hgb_pipeline(time_encoding=time_enc, seed=seed)
    est = HistGradientBoostingRegressor(
        random_state=seed,
        early_stopping=False,
        max_iter=50 if quick else 200,
        learning_rate=0.1,
        max_leaf_nodes=31,
        categorical_features=list(range(len(M.CATEGORICAL_FEATURES))),
    )
    pipe = Pipeline([("prep", base.named_steps["prep"]), ("est", est)])
    y_tr = np.log1p(tr["resolution_hours"].to_numpy(dtype=float))
    y_te = np.log1p(te["resolution_hours"].to_numpy(dtype=float))
    t0 = time.perf_counter()
    pipe.fit(tr[cols], y_tr)
    pred = pipe.predict(te[cols])
    baseline = tr.groupby(["tier", "severity"])["resolution_hours"].median()
    lookup = te[["tier", "severity"]].merge(
        baseline.rename("median_hours").reset_index(), on=["tier", "severity"], how="left"
    )
    base_pred = np.log1p(
        lookup["median_hours"].fillna(tr["resolution_hours"].median()).to_numpy(dtype=float)
    )

    def summarise(pred_log: np.ndarray) -> dict[str, float]:
        hours = np.expm1(pred_log)
        actual = np.expm1(y_te)
        resid = y_te - pred_log
        return {
            "mae_hours": float(np.abs(hours - actual).mean()),
            "median_abs_error_hours": float(np.median(np.abs(hours - actual))),
            "rmse_log1p_hours": float(np.sqrt((resid**2).mean())),
            "r2_log1p_hours": float(1 - (resid**2).sum() / ((y_te - y_te.mean()) ** 2).sum()),
        }

    return {
        "target": "log1p(resolution_hours)",
        "model": summarise(pred),
        "baseline_median_by_tier_severity": summarise(base_pred),
        "fit_seconds": round(time.perf_counter() - t0, 2),
    }


# ------------------------------------------------------------------------------------------
# Plots
# ------------------------------------------------------------------------------------------
def make_plots(
    img_dir: Path,
    y_te: np.ndarray,
    preds: dict[str, np.ndarray],
    final_name: str,
    reliability: dict[str, list[dict[str, float]]],
    importance: dict[str, list[dict[str, Any]]],
    threshold: float,
) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    img_dir.mkdir(parents=True, exist_ok=True)
    written = []
    prevalence = float(y_te.mean())
    labels = {"lr": "logistic regression", "hgb": "gradient boosting"}

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for name, p in preds.items():
        fpr, tpr, _ = roc_curve(y_te, p)
        axes[0].plot(fpr, tpr, label=f"{labels[name]} (AUC {roc_auc_score(y_te, p):.3f})")
        prec, rec, _ = precision_recall_curve(y_te, p)
        axes[1].plot(rec, prec, label=f"{labels[name]} (AP {average_precision_score(y_te, p):.3f})")
    axes[0].plot([0, 1], [0, 1], "k--", lw=0.8, label="chance")
    axes[1].axhline(prevalence, color="k", ls="--", lw=0.8, label=f"prevalence {prevalence:.3f}")
    axes[0].set(
        xlabel="false positive rate", ylabel="true positive rate", title="ROC (test months)"
    )
    axes[1].set(xlabel="recall", ylabel="precision", title="Precision-recall (test months)")
    for ax in axes:
        ax.legend(loc="lower right" if ax is axes[0] else "upper right", fontsize=8)
        ax.grid(alpha=0.3)
    fig.suptitle("SLA-breach classifier on synthetic data", fontsize=10)
    fig.tight_layout()
    fig.savefig(img_dir / "roc_pr_test.png", dpi=120)
    plt.close(fig)
    written.append("roc_pr_test.png")

    fig, ax = plt.subplots(figsize=(5.5, 5))
    for name, table in reliability.items():
        ax.plot(
            [r["mean_predicted"] for r in table],
            [r["observed_rate"] for r in table],
            marker="o",
            label=labels[name],
        )
    ax.plot([0, 1], [0, 1], "k--", lw=0.8, label="perfect calibration")
    lim = max(max(r["mean_predicted"] for t in reliability.values() for r in t), 0.5) * 1.1
    ax.set(
        xlim=(0, lim),
        ylim=(0, lim),
        xlabel="mean predicted probability (decile)",
        ylabel="observed breach rate",
        title="Reliability, test months (synthetic data)",
    )
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(img_dir / "calibration_test.png", dpi=120)
    plt.close(fig)
    written.append("calibration_test.png")

    order = [
        r["feature"] for r in sorted(importance["validation"], key=lambda r: r["importance_mean"])
    ]
    fig, ax = plt.subplots(figsize=(7.5, 6))
    y_pos = np.arange(len(order))
    for offset, (part, colour) in enumerate((("validation", "#4c72b0"), ("test", "#dd8452"))):
        by_feature = {r["feature"]: r for r in importance[part]}
        ax.barh(
            y_pos + (0.2 if offset else -0.2),
            [by_feature[f]["importance_mean"] for f in order],
            height=0.4,
            xerr=[by_feature[f]["importance_std"] for f in order],
            color=colour,
            label=f"{part} months",
        )
    ax.set_yticks(y_pos, order)
    ax.set(
        xlabel="drop in PR-AUC when the feature is permuted",
        title=f"Permutation importance, {labels[final_name]}",
    )
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(img_dir / "feature_importance.png", dpi=120)
    plt.close(fig)
    written.append("feature_importance.png")

    p = preds[final_name]
    fig, ax = plt.subplots(figsize=(7, 4))
    bins = np.linspace(0, max(p.max(), 0.5), 40)
    ax.hist(p[y_te == 0], bins=bins, alpha=0.6, label="met SLA", density=True)
    ax.hist(p[y_te == 1], bins=bins, alpha=0.6, label="breached", density=True)
    ax.axvline(threshold, color="k", ls="--", lw=0.9, label=f"review threshold {threshold:.3f}")
    ax.set(
        xlabel="predicted breach probability",
        ylabel="density",
        title=f"Score distribution by outcome, test months ({labels[final_name]})",
    )
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(img_dir / "score_distribution_test.png", dpi=120)
    plt.close(fig)
    written.append("score_distribution_test.png")
    return written


# ------------------------------------------------------------------------------------------
# Outputs
# ------------------------------------------------------------------------------------------
SCORE_COLUMNS = [
    "ticket_id",
    "creation_ts",
    "customer_id",
    "customer_name",
    "tier",
    "industry",
    "region",
    "service_type",
    "assignment_group",
    "severity",
    "ticket_type",
    "channel",
    "status",
    "probability",
    "risk_band",
    "sla_breached",
    "split",
]


def score_all_tickets(
    loaded: M.LoadedModel, wide: pd.DataFrame, split: SplitConfig
) -> pd.DataFrame:
    p = loaded.predict_proba(wide)
    out = wide.copy()
    out["probability"] = p.round(6)
    out["risk_band"] = loaded.risk_band(p)
    labelled = wide["is_resolved"] & wide["sla_breached"].notna()
    out["split"] = assign_split(wide["creation_ts"], split).where(labelled, "unlabelled")
    return out[SCORE_COLUMNS]


def write_scores_to_db(engine, scores: pd.DataFrame, model_version: str) -> int:
    rows = scores[["ticket_id", "probability", "risk_band", "split"]].copy()
    rows.insert(1, "model_version", model_version)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM ticket_risk_score")
    return db.copy_dataframe(engine, "ticket_risk_score", rows)


def _fmt(res: dict[str, Any]) -> str:
    return (
        f"AUC {res['roc_auc']:.4f} PR-AUC {res['pr_auc']:.4f} Brier {res['brier']:.4f} "
        f"({res.get('fit_seconds', 0):.1f}s)"
    )


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _training_levels(pipe: Pipeline) -> dict[str, list[str]]:
    enc = pipe.named_steps["prep"].named_transformers_["cat"]
    cats = M.CATEGORICAL_FEATURES
    return {
        name: [str(v) for v in levels] for name, levels in zip(cats, enc.categories_, strict=True)
    }


# ------------------------------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------------------------------
def train(cfg: TrainConfig) -> dict[str, Any]:
    t_start = time.perf_counter()
    wide = load_processed(cfg.processed_dir)
    data = training_rows(wide)
    data["split"] = assign_split(data["creation_ts"], cfg.split)
    data = data.reset_index(drop=True)
    X_all = M.prepare_features(data)
    for col in M.MODEL_FEATURES:  # normalised feature columns; other columns untouched
        data[col] = X_all[col]
    tr = data[data["split"] == "train"]
    va = data[data["split"] == "validation"]
    te = data[data["split"] == "test"]
    if min(len(tr), len(va), len(te)) == 0:
        raise ValueError("empty split; check --train-end/--val-end against the data window")
    log.info(
        "rows: train %d (breach %.4f), validation %d (%.4f), test %d (%.4f)",
        len(tr),
        tr["y"].mean(),
        len(va),
        va["y"].mean(),
        len(te),
        te["y"].mean(),
    )
    split_summary = {
        name: {
            "rows": int(len(part)),
            "breached": int(part["y"].sum()),
            "breach_rate": float(part["y"].mean()),
            "first_month": part["creation_ts"].min().strftime("%Y-%m"),
            "last_month": part["creation_ts"].max().strftime("%Y-%m"),
        }
        for name, part in (("train", tr), ("validation", va), ("test", te))
    }

    # 1. feature-handling experiments and hyperparameter grids, on validation only
    experiments = feature_experiments(tr, va, cfg.seed)
    time_enc = {m: pick_time_encoding(experiments, m) for m in ("lr", "hgb")}
    log.info("time encoding chosen on validation: %s", time_enc)
    grid_results, best = tune(tr, va, time_enc, cfg.quick, cfg.seed)
    final_name = max(best, key=lambda k: round(best[k]["pr_auc"], 6))
    log.info(
        "best per family: %s; final family: %s",
        {k: v["params"] for k, v in best.items()},
        final_name,
    )

    cols = M.MODEL_FEATURES
    fitted: dict[str, Pipeline] = {
        "lr": make_lr_pipeline(C=best["lr"]["params"]["C"], time_encoding=time_enc["lr"]),
        "hgb": make_hgb_pipeline(
            best["hgb"]["params"], time_encoding=time_enc["hgb"], seed=cfg.seed
        ),
    }
    for pipe in fitted.values():
        pipe.fit(tr[cols], tr["y"])
    final = fitted[final_name]

    # 2. thresholds from the validation score distribution
    p_va = final.predict_proba(va[cols])[:, 1]
    threshold = float(np.quantile(p_va, 1 - TOP_SHARE))
    band_thresholds = {"medium": float(np.quantile(p_va, 1 - MEDIUM_SHARE)), "high": threshold}
    validation_operating = at_threshold(va["y"].to_numpy(), p_va, threshold)

    # 3. permutation importance on validation (does not touch test)
    n_repeats = 2 if cfg.quick else 5
    importance = {"validation": permutation_table(final, va, n_repeats, cfg.seed)}

    # 4. leakage probes and the cheap regression (validation / test respectively)
    leakage = leakage_checks(tr, va, te, cfg.seed)
    regression = time_to_resolve_regression(tr, te, time_enc["hgb"], cfg.quick, cfg.seed)

    # 5. the single pass over the test months
    y_te = te["y"].to_numpy()
    prevalence = float(y_te.mean())
    preds = {name: pipe.predict_proba(te[cols])[:, 1] for name, pipe in fitted.items()}
    baseline_p = np.full(len(y_te), float(tr["y"].mean()))
    test_metrics: dict[str, Any] = {
        "baseline_prevalence": {
            **ranking_metrics(y_te, baseline_p),
            "note": "constant = training breach rate; PR-AUC of a random ranking = prevalence",
        }
    }
    test_metrics["baseline_prevalence"]["pr_auc"] = prevalence
    reliability: dict[str, list[dict[str, float]]] = {}
    for name, p in preds.items():
        reliability[name] = reliability_table(y_te, p)
        test_metrics[name] = {
            **ranking_metrics(y_te, p),
            "ece": expected_calibration_error(reliability[name]),
            "at_review_threshold": at_threshold(y_te, p, threshold),
            "at_0.5": at_threshold(y_te, p, 0.5),
        }
    p_final = preds[final_name]
    top_share_table = [
        at_threshold(y_te, p_final, float(np.quantile(p_final, 1 - s))) | {"top_share": s}
        for s in (0.05, 0.10, 0.20, 0.30)
    ]
    segments = {by: segment_metrics(te, p_final, by, threshold) for by in ("tier", "service_type")}
    # reported only: nothing is chosen from it (the validation window has no outage, so the
    # outage flag can only show its weight on months that contain one)
    importance["test"] = permutation_table(final, te, n_repeats, cfg.seed)
    plots = make_plots(cfg.img_dir, y_te, preds, final_name, reliability, importance, threshold)

    # 6. artifact, model card, scores
    training_levels = _training_levels(final)
    trained_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    meta_path = cfg.raw_dir / "synthetic_generation_metadata.json"
    gen_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    card: dict[str, Any] = {
        "model_version": M.MODEL_VERSION,
        "synthetic_data": True,
        "task": "P(SLA breach) for an enterprise trouble ticket, using creation-time features only",
        "trained_at": trained_at,
        "estimator": final_name,
        "hyperparameters": best[final_name]["params"],
        "time_encoding": time_enc[final_name],
        "class_weight": None,
        "data": {
            "source": str(cfg.processed_dir / PROCESSED_FILES["tickets"]),
            "generator_seed": gen_meta.get("params", {}).get("seed"),
            "generator_params": gen_meta.get("params"),
            "training_rows_filter": "is_resolved and sla_breached is not null",
            "split": split_summary,
        },
        "features": M.feature_schema(),
        "training_levels": training_levels,
        "threshold": threshold,
        "threshold_policy": f"review the riskiest {TOP_SHARE:.0%} of tickets (validation quantile)",
        "band_thresholds": band_thresholds,
        "metrics_test": {
            "prevalence": prevalence,
            "final": test_metrics[final_name],
            "baseline": test_metrics["baseline_prevalence"],
        },
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
    }
    bundle = {
        "pipeline": final,
        "features": cols,
        "threshold": threshold,
        "band_thresholds": band_thresholds,
        "training_levels": training_levels,
        "card": card,
    }
    artifact_path = cfg.models_dir / ARTIFACT_FILE
    size = M.save_model(artifact_path, bundle)
    card["artifact"] = {"path": str(artifact_path), "bytes": size}
    (cfg.models_dir / CARD_FILE).write_text(json.dumps(_jsonable(card), indent=2) + "\n")

    loaded = M.load_model(artifact_path)
    scores = score_all_tickets(loaded, wide, cfg.split)
    db_rows = None
    if cfg.write_scores:
        scores_path = cfg.processed_dir / SCORES_FILE
        scores.to_csv(scores_path, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
        log.info("wrote %d risk scores to %s", len(scores), scores_path)
        if not cfg.skip_db:
            try:
                engine = db.get_engine(cfg.database_url)
                db_rows = write_scores_to_db(engine, scores, M.MODEL_VERSION)
                log.info("loaded %d rows into ticket_risk_score", db_rows)
            except Exception as exc:  # noqa: BLE001 - the DB is optional here
                log.warning("skipping ticket_risk_score load: %s", exc)

    evaluation = {
        "synthetic_data": True,
        "trained_at": trained_at,
        "sklearn_version": sklearn.__version__,
        "split": split_summary,
        "feature_experiments": experiments,
        "time_encoding": time_enc,
        "grid": grid_results,
        "best_on_validation": best,
        "final_model": final_name,
        "threshold": threshold,
        "band_thresholds": band_thresholds,
        "validation_at_review_threshold": validation_operating,
        "test": test_metrics,
        "test_top_share": top_share_table,
        "reliability_test": reliability,
        "segments_test": segments,
        "permutation_importance_validation": importance["validation"],
        "permutation_importance_test": importance["test"],
        "leakage_checks": leakage,
        "time_to_resolve_regression_test": regression,
        "risk_scores": {
            "rows": int(len(scores)),
            "band_counts": scores["risk_band"].value_counts().to_dict(),
            "db_rows_loaded": db_rows,
        },
        "artifact_bytes": size,
        "plots": plots,
        "runtime_seconds": round(time.perf_counter() - t_start, 1),
    }
    (cfg.models_dir / EVAL_FILE).write_text(json.dumps(_jsonable(evaluation), indent=2) + "\n")
    log.info(
        "done in %.1fs: final=%s test AUC %.4f PR-AUC %.4f (prevalence %.4f) artifact %d bytes",
        evaluation["runtime_seconds"],
        final_name,
        test_metrics[final_name]["roc_auc"],
        test_metrics[final_name]["pr_auc"],
        prevalence,
        size,
    )
    return evaluation


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Train the SLA-breach classifier (synthetic data).")
    p.add_argument("--processed", type=Path, default=Path("data/processed"))
    p.add_argument("--raw", type=Path, default=Path("data/raw"), help="for the generator seed")
    p.add_argument("--models", type=Path, default=Path("models"))
    p.add_argument("--img", type=Path, default=Path("docs/img"))
    p.add_argument("--train-end", default=SplitConfig.train_end, help="last training month")
    p.add_argument("--val-end", default=SplitConfig.val_end, help="last validation month")
    p.add_argument("--seed", type=int, default=0, help="model seed (not the data seed)")
    p.add_argument("--quick", action="store_true", help="tiny grids for smoke tests")
    p.add_argument("--skip-db", action="store_true", help="do not load ticket_risk_score")
    p.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    cfg = TrainConfig(
        processed_dir=args.processed,
        raw_dir=args.raw,
        models_dir=args.models,
        img_dir=args.img,
        split=SplitConfig(train_end=args.train_end, val_end=args.val_end),
        seed=args.seed,
        quick=args.quick,
        skip_db=args.skip_db,
        database_url=args.database_url,
    )
    train(cfg)


if __name__ == "__main__":
    main()
