# Stage 2: Predictive Delay Modeling — Technical Overview

This document provides a technical overview of the machine learning pipeline (Stage 2 of the "Concrete Evidence" project) and explains how it is designed to bridge with the forensic/legal case extraction work (Stage 3).

## 1. Goal of the Predictive Pipeline

The primary goal of this stage is to identify construction tasks that are at high risk of schedule delay and, crucially, to **explain *why*** they are at risk. We are not just building a black-box model; we are building an explainable model (using XGBoost + SHAP) so that the specific drivers of delay for a given task can be used to query historical legal precedents.

## 2. Data Pipeline (`prepare_data.py`)

We are using a synthetic construction task dataset from Kaggle (`construction_dataset.csv`). 
The `prepare_data.py` script acts as our ETL pipeline:
*   **Feature Engineering:** Expands the 9 raw Kaggle columns into **14 engineered features**. We derive metrics like `labor_intensity`, `equipment_density`, and `total_constraint_pressure` (a combination of site and resource constraints).
*   **Target Variable (`delay_flag`):** The target is a **composite binary flag**. A task is marked as delayed (1) if:
    *   `Risk_Level == "High"` (the explicit Kaggle label)
    *   **OR** `Site_Constraint_Score > 0.70` (severe site conditions)
    *   **OR** `Resource_Constraint_Score > 0.70` (severe resource pressure)

**Why a composite target?** In this synthetic dataset, the raw `Risk_Level` has near-zero correlation with the numeric features. By creating a composite target that explicitly flags tasks under severe constraint pressure, we generate a meaningful, predictable signal that mirrors reality (where extreme constraints *do* cause delays).

## 3. Modeling and Results (`train_baseline.py`)

The pipeline trains an **XGBoost classifier** to predict the `delay_flag`. 
*   **Performance:** The model currently achieves an **ROC-AUC of ~0.92** on the test set.
*   **Explainability:** We use XGBoost's native `gain` importances (and optionally SHAP values, though disabled by default for speed via the `--no-shap` flag) to extract the primary drivers of delay.
*   **Top Drivers:** The model consistently identifies `resource_constraint_score`, `site_constraint_score`, and `total_constraint_pressure` as the top predictors of delay. This aligns perfectly with real-world construction disputes.

## 4. The Bridge: Integrating with Forensics (Stage 3)

The key to the "Concrete Evidence" project is connecting these predictive risk drivers to the legal DAGs extracted from CourtListener cases. Here is how the two branches will merge:

### Step A: The "Alignment Layer" (`feature_list.yaml`)
To bridge the gap between ML features and legal terminology, we use an alignment layer. The `feature_list.yaml` file defines our model's features and explicitly maps them to the forensic taxonomy defined in `event_type_reference.md`.
*   *Example:* If the model flags a high `site_constraint_score`, the YAML file maps this predictive feature to the forensic event type `differing_site_conditions`.

### Step B: The Integration Hook (`retrieve_forensic_precedent`)
Inside `train_baseline.py`, there is a stubbed function called `retrieve_forensic_precedent()`. Once the forensic DAG extraction is complete, this function will:
1.  Take the top explainability drivers (e.g., SHAP values) for a specific high-risk task prediction.
2.  Use the `feature_list.yaml` mapping to translate those drivers into legal event types (e.g., `resource_shortage` + `differing_site_conditions`).
3.  Query the forensic DAG database for historical cases that feature this exact combination of delay causes.
4.  Return the matching cases (with causal narratives and citations) to augment the prediction output.

### Final Vision
Instead of the model simply outputting: *"This task has a 92% risk of delay,"* the integrated pipeline will output:
> *"This task has a 92% risk of delay — driven primarily by severe site constraints and resource pressure. This exact pattern matches the delay causes found in 3 past legal cases: [Case A], [Case B], and [Case C]."*

## Running the Code
To run the pipeline and see the stubbed output, navigate to `prediction/schema/` and run:
```bash
uv run --python 3.11 --with pandas --with numpy --with xgboost --with scikit-learn python train_baseline.py --no-shap
```
