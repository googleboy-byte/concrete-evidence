# Delay-Risk Prediction Integration & Forensic Precedent Matching Pipeline

This directory (`merge/`) implements the integration of a construction delay-risk prediction model with a forensic Case DAG database. The goal is to perform error analysis by matching predictive model failures (misclassifications) against real-world trial precedents to diagnose structural blind spots.

---

## Directory Structure

```
merge/
├── README.md                          # This documentation file
├── vendored/
│   └── prediction_schema/             # Read-only vendored copy of origin/delay-prediction branch
│       ├── README.md                  # Vendored prediction schema readme
│       ├── event_type_reference.md    # Vendored event types
│       ├── feature_list.yaml          # Features used by Praneeth's model
│       ├── prepare_data.py            # Logic to generate prepared.csv
│       ├── train_baseline.py          # Baseline model code (XGBoost)
│       └── delay_baseline_xgb.json    # The pre-trained baseline model weights
├── scripts/
│   ├── predict_test_set.py            # Generates test set predictions & SHAP values
│   ├── precedent_matcher.py           # Maps drivers to event types and queries database
│   └── error_analysis.py              # Combines predictions and matches to generate report
└── output/
    ├── test_predictions.json          # Predictions & SHAP values for all test cases
    ├── misclassified.json             # Subset of predictions that were misclassified
    ├── error_analysis.json            # Structured error analysis matching data
    └── error_analysis_report.md       # Human-readable markdown report with diagnoses
```

---

## Pipeline Components

### 1. Predict Test Set (`merge/scripts/predict_test_set.py`)
Reuses the baseline model training and feature preprocessing from `train_baseline.py` to:
- Partition the prepared dataset into the standard 80/20 train/test split.
- Load the pre-trained `delay_baseline_xgb.json` model (or train a new one if it is missing).
- Predict delay probabilities for the entire test set.
- Compute SHAP feature contributions for every test case using the `shap` library (or fall back to pseudo-SHAP if the library is unavailable).
- Output all predictions to `test_predictions.json` and misclassified test rows to `misclassified.json`.

### 2. Precedent Matcher (`merge/scripts/precedent_matcher.py`)
- Maps predictive features from the model to actual forensic event types using `taxonomy.yaml`.
- Establishes a reverse index lookup for event types.
- Connects to the SQLite database `forensic_dags.db` in read-only mode to find matching cases.
- Ranks matches based on:
  1. **Primary**: Number of distinct matching event types.
  2. **Secondary (tie-breaker)**: Highest extraction confidence among matching nodes (`explicit`/`court_finding` > `implied` > `expert_opinion_disputed` > `alleged_only` > `contested`).
- Performs a Depth-First Search (DFS) walk starting from the root nodes of the case's DAG (nodes with in-degree 0) to output a deterministic causal chain of events.

### 3. Error Analysis (`merge/scripts/error_analysis.py`)
- Runs the precedent matching workflow on all misclassified test cases.
- Compares the model's flat feature-based prediction against the matched precedent's causal cascade.
- Generates a plain-English diagnosis line explaining why the model failed to capture the downstream cascade.
- Compiles the final structured data (`error_analysis.json`) and a detailed markdown report (`error_analysis_report.md`).

---

## How to Run the Pipeline

Follow these commands from the repository root:

```bash
# 1. Activate the virtual environment
source venv/bin/activate

# 2. Run test set predictions & SHAP calculation
python3 merge/scripts/predict_test_set.py

# 3. Run precedent matching & compile error analysis reports
python3 merge/scripts/error_analysis.py
```

---

## Data Schema & Output Details

### `error_analysis.json` Schema
For each misclassified task:
```json
{
  "task_id": "T102",
  "y_true": 1,
  "y_pred": 0,
  "proba": 0.1402,
  "top_shap_features": [
    {
      "feature": "resource_constraint_score",
      "value": 0.46,
      "shap_value": -0.9107,
      "direction": "-decreases"
    },
    ...
  ],
  "precedents": [
    {
      "case_id": "...",
      "case_name": "...",
      "court": "...",
      "citation": "... (Year)",
      "matched_event_types": ["...", "..."],
      "causal_chain": [
        {
          "from_node": "...",
          "from_event_type": "...",
          "relationship": "...",
          "to_node": "...",
          "to_event_type": "...",
          "causal_strength": "..."
        }
      ],
      "matched_node_excerpts": [
        {
          "event_type": "...",
          "citation": "...",
          "excerpt": "..."
        }
      ],
      "diagnosis": "model saw a single elevated ... and predicted ... but the matched precedent ... shows ... cascading into ...; the model has no feature capturing that downstream cascade..."
    }
  ],
  "diagnosis": "..."
}
```

---

## Diagnosis Findings: Model Blind Spots

XGBoost makes a prediction based on flat tabular features (e.g. `resource_constraint_score` or `site_constraint_score`). However, construction delay disputes are defined by multi-step causal cascades where one event triggers another, eventually impacting the critical path (e.g. `differing_site_conditions` triggering `design_freeze` triggering `weather_delay` triggering `critical_path_impact`). 

Because the model lacks structural representations of these causal DAG chains, it frequently underpredicts risk when a single metric appears low, or overpredicts when an isolated risk feature is high. By matching predictions to forensic precedents, we highlight these gaps in the predictive model's reasoning.
