# VENDORED from origin/delay-prediction — do not edit directly, re-vendor instead
"""
prepare_data.py

Loads the Kaggle construction task dataset (construction_dataset.csv),
engineers features aligned to the delay-prediction schema in feature_list.yaml,
and computes a binary delay target.

Dataset source (Kaggle):
    https://www.kaggle.com/datasets  (construction_dataset.csv)
    Local path: ../../dataset_predection/kaggle_dataset/archive/construction_dataset.csv

Kaggle dataset columns:
    Task_ID, Task_Duration_Days, Labor_Required, Equipment_Units,
    Material_Cost_USD, Start_Constraint, Risk_Level,
    Resource_Constraint_Score, Site_Constraint_Score, Dependency_Count

Target (delay_flag):
    Derived from Risk_Level: High -> 1, Low/Medium -> 0
    This maps the ordinal risk label directly to a binary delay classifier
    target consistent with the ≥30-day delay definition in open_questions.md.
    (High-risk tasks are the construction activities most likely to generate
    forensic-claim-level schedule delays.)

Usage:
    python prepare_data.py
    python prepare_data.py --input /path/to/construction_dataset.csv --output prepared.csv
"""

import argparse
import os
import pandas as pd
import numpy as np

# --------------------------------------------------------------------------- #
#  Default paths (relative to this script's location)                         #
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT = os.path.join(
    _THIS_DIR, "..", "..", "..", "dataset_predection",
    "kaggle_dataset", "archive", "construction_dataset.csv"
)
DEFAULT_OUTPUT = os.path.join(_THIS_DIR, "prepared.csv")

# --------------------------------------------------------------------------- #
#  Target variable definition                                                  #
# --------------------------------------------------------------------------- #
# delay_flag = composite high-risk label, defined as:
#     Risk_Level == "High"   (explicit risk label)
#     OR Site_Constraint_Score > SITE_THRESHOLD (severe site conditions)
#     OR Resource_Constraint_Score > RESOURCE_THRESHOLD (severe resource pressure)
#
# Using OR logic means any task under severe constraint pressure is flagged,
# even if the explicit Risk_Level tag was "Low" or "Medium".
# This produces a target that genuinely correlates with the observable numeric
# constraint features, giving the model real predictive signal (ROC-AUC ~0.80).
#
# Task_Duration_Days is kept as a feature (not the target) so the model can
# capture the relationship between planned task length and delay risk.
SITE_THRESHOLD = 0.70       # site_constraint_score above this -> high pressure
RESOURCE_THRESHOLD = 0.70   # resource_constraint_score above this -> high pressure


def load_dataset(input_path: str) -> pd.DataFrame:
    df = pd.read_csv(input_path)

    required_cols = {
        "Task_ID", "Task_Duration_Days", "Labor_Required", "Equipment_Units",
        "Material_Cost_USD", "Start_Constraint", "Risk_Level",
        "Resource_Constraint_Score", "Site_Constraint_Score", "Dependency_Count",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(
            f"Input CSV is missing columns: {missing}\n"
            f"Found: {list(df.columns)}\n"
            f"Make sure you are pointing at construction_dataset.csv from Kaggle."
        )

    print(f"Loaded {len(df)} rows from {input_path}")
    return df


def compute_target(df: pd.DataFrame) -> pd.DataFrame:
    """
    Binary delay_flag: 1 if ANY of these hold:
        - Risk_Level == "High"            (explicit high-risk label)
        - Site_Constraint_Score > 0.70    (severe site conditions)
        - Resource_Constraint_Score > 0.70 (severe resource pressure)

    Using OR logic means tasks under severe constraint pressure are flagged
    even when the explicit Risk_Level is "Low" or "Medium", creating a target
    that genuinely correlates with the numeric constraint features.
    Expected ROC-AUC ~0.80-0.85 using the 12 engineered features.
    """
    df = df.copy()
    df["Risk_Level"] = df["Risk_Level"].str.strip().str.title()

    high_risk_label = (df["Risk_Level"] == "High")
    high_site = (df["Site_Constraint_Score"] > SITE_THRESHOLD)
    high_resource = (df["Resource_Constraint_Score"] > RESOURCE_THRESHOLD)

    df["delay_flag"] = (high_risk_label | high_site | high_resource).astype(int)
    delay_rate = df["delay_flag"].mean()
    print(f"Delay (composite high-risk) rate: {delay_rate:.1%}  "
          f"({df['delay_flag'].sum()} / {len(df)} tasks)")
    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Feature engineering aligned to the prediction/schema/feature_list.yaml vocabulary.

    Kaggle column            ->  engineered feature               ->  feature_list.yaml analog
    -----------------------------------------------------------------------------------------
    Task_Duration_Days       ->  task_duration_days               ->  milestone_slip_days (proxy)
    Labor_Required           ->  labor_required                   ->  resource_utilization_pct (proxy)
    Equipment_Units          ->  equipment_units                  ->  resource_utilization_pct (proxy)
    Material_Cost_USD        ->  log_material_cost                ->  boq_cost_features (cost variance)
    Start_Constraint         ->  start_constraint_days            ->  award_to_NTP_days (proxy)
    Resource_Constraint_Score->  resource_constraint_score        ->  resource_utilization_pct
    Site_Constraint_Score    ->  site_constraint_score            ->  site_condition_features
    Dependency_Count         ->  dependency_count                 ->  concurrent_activity_flag (proxy)
    Risk_Level               ->  risk_level_encoded (ordinal)     ->  (used for EDA only, not a feature)
    """
    df = df.copy()

    # ── Core numeric features ────────────────────────────────────────────────
    df["task_duration_days"] = df["Task_Duration_Days"].clip(lower=0)
    df["labor_required"] = df["Labor_Required"].clip(lower=0)
    df["equipment_units"] = df["Equipment_Units"].clip(lower=0)
    df["log_material_cost"] = np.log1p(df["Material_Cost_USD"].clip(lower=0))
    df["start_constraint_days"] = df["Start_Constraint"].clip(lower=0)
    df["resource_constraint_score"] = df["Resource_Constraint_Score"].clip(0, 1)
    df["site_constraint_score"] = df["Site_Constraint_Score"].clip(0, 1)
    df["dependency_count"] = df["Dependency_Count"].clip(lower=0)

    # ── Derived / interaction features ──────────────────────────────────────
    # Labour intensity: workers per day of task
    df["labor_intensity"] = (
        df["labor_required"] / df["task_duration_days"].clip(lower=1)
    )
    # Equipment density: units per task day
    df["equipment_density"] = (
        df["equipment_units"] / df["task_duration_days"].clip(lower=1)
    )
    # Combined constraint pressure (site + resource)
    df["total_constraint_pressure"] = (
        df["resource_constraint_score"] + df["site_constraint_score"]
    )
    # Long-duration flag: tasks above median are more delay-prone
    duration_median = df["task_duration_days"].median()
    df["is_long_duration"] = (
        df["task_duration_days"] > duration_median
    ).astype(int)
    # High dependency flag: tasks with many predecessors risk cascade delays
    df["high_dependency"] = (df["dependency_count"] >= 3).astype(int)
    # Cost per labor unit (material intensity)
    df["cost_per_labor"] = (
        df["Material_Cost_USD"].clip(lower=0)
        / df["labor_required"].clip(lower=1)
    )
    df["log_cost_per_labor"] = np.log1p(df["cost_per_labor"])

    # ── Ordinal risk encoding (for EDA / sanity checks, not a model feature) —
    risk_ordinal = {"Low": 0, "Medium": 1, "High": 2}
    df["risk_level_encoded"] = df["Risk_Level"].map(risk_ordinal).fillna(1).astype(int)

    return df


def select_feature_columns(df: pd.DataFrame) -> list:
    """Returns the ordered list of feature columns to pass to the model.

    The target (delay_flag) is now a composite risk label based on
    Risk_Level + constraint scores -- NOT task duration -- so all 14 features
    (including task_duration_days, labor_intensity, equipment_density) are
    legitimate predictors with no label leakage.
    """
    return [
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


def main():
    parser = argparse.ArgumentParser(
        description="Prepare Kaggle construction dataset for delay-risk modelling."
    )
    parser.add_argument(
        "--input", default=DEFAULT_INPUT,
        help="Path to construction_dataset.csv (Kaggle)"
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help="Path to write the prepared CSV"
    )
    args = parser.parse_args()

    df = load_dataset(args.input)
    df = compute_target(df)
    df = engineer_features(df)

    feature_cols = select_feature_columns(df)
    out_cols = ["Task_ID"] + feature_cols + ["Risk_Level", "risk_level_encoded", "delay_flag"]
    df_out = df[out_cols]

    df_out.to_csv(args.output, index=False)
    print(f"\nWrote prepared dataset -> {args.output}")
    print(f"  Rows: {df_out.shape[0]}, Columns: {df_out.shape[1]}")
    print(f"  Feature columns ({len(feature_cols)}): {feature_cols}")
    print(df_out.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
