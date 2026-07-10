# VENDORED from origin/delay-prediction — do not edit directly, re-vendor instead
"""
train_baseline.py

Trains the baseline XGBoost delay-risk classifier on the Kaggle construction
dataset (prepared by prepare_data.py), evaluates it with ROC-AUC / F1, and
computes SHAP explanations per prediction.

This is Stage 2 (correlational baseline) of the Concrete Evidence plan.
The retrieval hook at the bottom is a stub for Stage 3 integration, once
Mainak's forensic DAG extraction + event_types.yaml crosswalk exist.

Dataset:
    Kaggle construction_dataset.csv (1301 task rows)
    Target: delay_flag = 1 if Risk_Level == "High"

Usage:
    # Run end-to-end (prepare + train in one call):
    python train_baseline.py

    # Or point at an already-prepared CSV:
    python train_baseline.py --input prepared.csv

    # Skip SHAP (use native XGBoost importances instead — faster on first run):
    python train_baseline.py --no-shap

Expected ROC-AUC on Kaggle data: ~0.80-0.85
Top SHAP drivers expected: site_constraint_score, resource_constraint_score,
                            total_constraint_pressure, dependency_count
(these mirror the forensic patterns Mainak's court cases surface)
"""

import argparse
import os
import subprocess
import sys
import numpy as np
import pandas as pd

import xgboost as xgb
from sklearn.model_selection import train_test_split, StratifiedKFold, cross_val_score
from sklearn.metrics import (
    roc_auc_score, precision_recall_fscore_support, classification_report,
)

# SHAP is optional: fast on cached runs, slow on first-run numba JIT compile.
# Use --no-shap flag or set SKIP_SHAP=1 env var to fall back to XGBoost
# native feature importances (always fast).
SKIP_SHAP_ENV = os.environ.get("SKIP_SHAP", "0") == "1"
try:
    import shap
    SHAP_AVAILABLE = True
except ImportError:
    SHAP_AVAILABLE = False

# --------------------------------------------------------------------------- #
#  Paths                                                                       #
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PREPARED_CSV = os.path.join(_THIS_DIR, "prepared.csv")
DEFAULT_RAW_CSV = os.path.join(
    _THIS_DIR, "..", "..", "..", "dataset_predection",
    "kaggle_dataset", "archive", "construction_dataset.csv"
)

# --------------------------------------------------------------------------- #
#  Feature columns (must match prepare_data.select_feature_columns())         #
# --------------------------------------------------------------------------- #
FEATURE_COLS = [
    "task_duration_days",
    "labor_required",
    "equipment_units",
    "log_material_cost",
    "start_constraint_days",
    "resource_constraint_score",
    "site_constraint_score",
    "dependency_count",
    "labor_intensity",
    "equipment_density",
    "total_constraint_pressure",
    "is_long_duration",
    "high_dependency",
    "log_cost_per_labor",
]

META_COLS = ["Task_ID", "Risk_Level"]
TARGET_COL = "delay_flag"


# --------------------------------------------------------------------------- #
#  Data loading                                                                #
# --------------------------------------------------------------------------- #
def ensure_prepared(prepared_path: str, raw_path: str) -> None:
    """Auto-run prepare_data.py if the prepared CSV doesn't exist yet."""
    if not os.path.exists(prepared_path):
        print(f"[INFO] {prepared_path} not found — running prepare_data.py ...")
        prepare_script = os.path.join(_THIS_DIR, "prepare_data.py")
        subprocess.run(
            [sys.executable, prepare_script,
             "--input", raw_path,
             "--output", prepared_path],
            check=True,
        )


def load_features(prepared_path: str):
    df = pd.read_csv(prepared_path)

    missing_features = [c for c in FEATURE_COLS if c not in df.columns]
    if missing_features:
        raise ValueError(
            f"Prepared CSV is missing expected feature columns: {missing_features}\n"
            f"Re-run prepare_data.py to regenerate."
        )

    X = df[FEATURE_COLS].copy()
    y = df[TARGET_COL].copy()
    meta = df[[c for c in META_COLS if c in df.columns]].copy()
    return X, y, meta


# --------------------------------------------------------------------------- #
#  Model                                                                       #
# --------------------------------------------------------------------------- #
def train_model(X_train: pd.DataFrame, y_train: pd.Series) -> xgb.XGBClassifier:
    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    scale_pos_weight = (n_neg / n_pos) if n_pos > 0 else 1.0

    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        use_label_encoder=False,
        random_state=42,
        verbosity=0,
    )
    model.fit(X_train, y_train)
    return model


# --------------------------------------------------------------------------- #
#  Evaluation                                                                  #
# --------------------------------------------------------------------------- #
def evaluate(model: xgb.XGBClassifier, X_test: pd.DataFrame,
             y_test: pd.Series) -> dict:
    proba = model.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)

    auc = roc_auc_score(y_test, proba)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_test, preds, average="binary", zero_division=0
    )

    print("\n" + "=" * 50)
    print("  MODEL EVALUATION")
    print("=" * 50)
    print(f"  ROC-AUC  : {auc:.4f}")
    print(f"  Precision: {precision:.4f}")
    print(f"  Recall   : {recall:.4f}")
    print(f"  F1       : {f1:.4f}")
    print()
    print(classification_report(y_test, preds, zero_division=0,
                                target_names=["Not Delayed", "Delayed (High-Risk)"]))
    return {"auc": auc, "precision": precision, "recall": recall, "f1": f1}


def cross_validate(X: pd.DataFrame, y: pd.Series) -> None:
    """5-fold stratified CV — gives a stable AUC estimate on the small dataset."""
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    spw = (n_neg / n_pos) if n_pos > 0 else 1.0
    cv_model = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
        eval_metric="logloss", use_label_encoder=False,
        random_state=42, verbosity=0,
    )
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(cv_model, X, y, cv=cv, scoring="roc_auc")
    print(f"\n5-Fold CV ROC-AUC: {scores.mean():.4f} ± {scores.std():.4f}  "
          f"(folds: {[f'{s:.3f}' for s in scores]})")


# --------------------------------------------------------------------------- #
#  SHAP explanations                                                           #
# --------------------------------------------------------------------------- #
def explain_global(model: xgb.XGBClassifier, X_test: pd.DataFrame,
                   top_n: int = 10) -> tuple:
    """Global SHAP feature importance across all test predictions."""
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_test)

    mean_abs_shap = np.abs(shap_values).mean(axis=0)
    importance = (
        pd.Series(mean_abs_shap, index=X_test.columns)
        .sort_values(ascending=False)
        .head(top_n)
    )
    print(f"\n{'=' * 50}")
    print(f"  TOP {top_n} SHAP FEATURE IMPORTANCES (mean |SHAP|)")
    print(f"{'=' * 50}")
    for feat, val in importance.items():
        bar = "#" * max(1, int(val * 80 / importance.iloc[0]))
        print(f"  {feat:<35} {val:+.4f}  {bar}")

    return explainer, shap_values


def explain_single(explainer, shap_values: np.ndarray, X_test: pd.DataFrame,
                   meta: pd.DataFrame, row_idx: int, top_n: int = 6) -> None:
    """
    Human-readable per-prediction explanation.
    Final output format: "82% delay risk — driven by: site_constraint_score (+),
    resource_constraint_score (+)..." aligning with forensic case narratives.
    """
    row_shap = shap_values[row_idx]
    row_features = X_test.iloc[row_idx]
    task_id = meta.iloc[row_idx]["Task_ID"] if "Task_ID" in meta.columns else f"row_{row_idx}"
    risk_label = meta.iloc[row_idx]["Risk_Level"] if "Risk_Level" in meta.columns else "?"

    top_contributors = (
        pd.Series(row_shap, index=X_test.columns)
        .reindex(
            pd.Series(row_shap, index=X_test.columns)
            .abs().sort_values(ascending=False).index
        )
        .head(top_n)
    )

    print(f"\n{'=' * 50}")
    print(f"  EXPLANATION -- Task {task_id}  (actual risk label: {risk_label})")
    print(f"{'=' * 50}")
    for feat, shap_val in top_contributors.items():
        direction = "+increases" if shap_val > 0 else "-decreases"
        print(f"  {feat:<35} = {row_features[feat]:.3g}  -> {direction} delay risk  ({shap_val:+.4f})")


# --------------------------------------------------------------------------- #
#  Stage 3 forensic retrieval stub                                             #
# --------------------------------------------------------------------------- #
def retrieve_forensic_precedent(feature_row: pd.Series,
                                event_types_crosswalk_path: str = None) -> list:
    """
    STUB — fill in once event_types.yaml (the crosswalk between Mainak's
    forensic event types and this model's feature names from feature_list.yaml)
    exists, and once Mainak has extracted structured DAGs from CourtListener cases.

    Intended final behaviour:
      1. Load event_types.yaml → map high-SHAP features to forensic event_type
         categories (e.g. high site_constraint_score → "differing_site_conditions").
      2. Query Mainak's DAG store for cases tagged with matching event types.
      3. Return top-k matching cases with causal narrative + citation.

    Returns:
        list[dict]: e.g. [{"case_name": ..., "event_types": [...], "citation": ...}]
    """
    if event_types_crosswalk_path is None:
        return []  # not wired up yet — output falls back to bare SHAP explanation
    raise NotImplementedError(
        "Wire this up once event_types.yaml + Mainak's DAG extraction exist."
    )


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="Train XGBoost delay-risk model on Kaggle construction data."
    )
    parser.add_argument(
        "--input", default=DEFAULT_PREPARED_CSV,
        help="Path to prepared.csv from prepare_data.py"
    )
    parser.add_argument(
        "--raw", default=DEFAULT_RAW_CSV,
        help="Path to raw construction_dataset.csv (used if --input doesn't exist yet)"
    )
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--no-cv", action="store_true",
                        help="Skip 5-fold cross-validation (faster)")
    parser.add_argument("--no-shap", action="store_true",
                        help="Use XGBoost native importances instead of SHAP (faster on first run)")
    args = parser.parse_args()

    use_shap = SHAP_AVAILABLE and not args.no_shap and not SKIP_SHAP_ENV

    # Auto-prepare if needed
    ensure_prepared(args.input, args.raw)

    X, y, meta = load_features(args.input)
    print(f"\nDataset: {len(X)} tasks, {len(FEATURE_COLS)} features")
    print(f"Delay (High-risk) rate: {y.mean():.1%}  ({y.sum()} / {len(y)})")
    print(f"SHAP explanations: {'enabled' if use_shap else 'disabled (using XGBoost importances)'}")

    # 5-fold CV for stable AUC estimate
    if not args.no_cv:
        cross_validate(X, y)

    # Train / test split
    X_train, X_test, y_train, y_test, meta_train, meta_test = train_test_split(
        X, y, meta, test_size=args.test_size, random_state=42, stratify=y
    )
    print(f"\nTrain: {len(X_train)} rows | Test: {len(X_test)} rows")

    model = train_model(X_train, y_train)
    metrics = evaluate(model, X_test, y_test)

    proba = model.predict_proba(X_test)[:, 1]
    highest_risk_idx = int(np.argmax(proba))
    print(f"\nHighest predicted delay risk in test set: {proba[highest_risk_idx]:.1%}")

    if use_shap:
        # SHAP tree explanations (accurate, but slow on first run due to numba JIT)
        explainer, shap_values = explain_global(model, X_test)
        explain_single(explainer, shap_values, X_test, meta_test,
                       row_idx=highest_risk_idx)
    else:
        # Fallback: XGBoost native gain-based feature importances (always fast)
        print(f"\n{'=' * 50}")
        print(f"  TOP FEATURE IMPORTANCES (XGBoost 'gain')")
        print(f"{'=' * 50}")
        importances = pd.Series(
            model.get_booster().get_score(importance_type='gain'),
            name='gain'
        ).sort_values(ascending=False).head(10)
        for feat, val in importances.items():
            bar = "#" * max(1, int(val * 40 / importances.iloc[0]))
            print(f"  {feat:<35} {val:>8.2f}  {bar}")

        # Per-prediction explanation using feature values + importances
        task_id = meta_test.iloc[highest_risk_idx]['Task_ID'] if 'Task_ID' in meta_test.columns else f'row_{highest_risk_idx}'
        risk_label = meta_test.iloc[highest_risk_idx]['Risk_Level'] if 'Risk_Level' in meta_test.columns else '?'
        print(f"\n{'=' * 50}")
        print(f"  EXPLANATION — Task {task_id}  (actual: {risk_label})")
        print(f"{'=' * 50}")
        for feat in importances.index[:6]:
            if feat in X_test.columns:
                val = X_test.iloc[highest_risk_idx][feat]
                imp = importances.get(feat, 0)
                print(f"  {feat:<35} = {val:.3g}  (importance: {imp:.1f})")

    # Forensic retrieval (Stage 3 stub)
    precedents = retrieve_forensic_precedent(X_test.iloc[highest_risk_idx])
    print(f"\nForensic precedent matches: "
          f"{precedents if precedents else '(none yet -- retrieval not wired up)'}")

    # Save model
    model_path = os.path.join(_THIS_DIR, "delay_baseline_xgb.json")
    model.save_model(model_path)
    print(f"\nSaved model -> {model_path}")
    print(f"\nPipeline complete. ROC-AUC = {metrics['auc']:.4f}")


if __name__ == "__main__":
    main()
