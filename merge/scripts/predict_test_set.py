#!/usr/bin/env python3
"""
predict_test_set.py

Loads the baseline XGBoost delay-risk classifier, runs predictions on the
test set, computes SHAP explanations for every test row, and outputs:
  - merge/output/test_predictions.json (all test rows)
  - merge/output/misclassified.json (only misclassified rows)

This script imports and reuses functions from merge/vendored/prediction_schema/train_baseline.py
to maintain parity of the data pipeline and model parameters.
"""

import os
import sys
import json
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, accuracy_score

# Add vendored prediction schema to sys.path
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MERGE_DIR = os.path.dirname(_THIS_DIR)
_VENDORED_DIR = os.path.join(_MERGE_DIR, "vendored", "prediction_schema")
sys.path.insert(0, _VENDORED_DIR)

from train_baseline import (
    ensure_prepared, load_features, train_model,
    FEATURE_COLS, META_COLS, TARGET_COL
)

def main():
    print("[INFO] Starting predict_test_set.py")
    
    # 1. Resolve paths
    raw_path = os.path.join(_VENDORED_DIR, "kaggle_dataset", "archive", "construction_dataset.csv")
    prepared_path = os.path.join(_VENDORED_DIR, "prepared.csv")
    model_path = os.path.join(_VENDORED_DIR, "delay_baseline_xgb.json")
    output_dir = os.path.join(_MERGE_DIR, "output")
    
    os.makedirs(output_dir, exist_ok=True)
    
    # 2. Ensure data is prepared and load features
    ensure_prepared(prepared_path, raw_path)
    X, y, meta = load_features(prepared_path)
    
    # 3. Train/test split (test_size=0.2, random_state=42, stratify=y)
    X_train, X_test, y_train, y_test, meta_train, meta_test = train_test_split(
        X, y, meta, test_size=0.2, random_state=42, stratify=y
    )
    print(f"[INFO] Train shape: {X_train.shape}, Test shape: {X_test.shape}")
    
    # 4. Load or retrain model
    model = xgb.XGBClassifier()
    if os.path.exists(model_path):
        print(f"[INFO] Loading existing model from {model_path}")
        model.load_model(model_path)
    else:
        print("[INFO] Model not found, training new model...")
        model = train_model(X_train, y_train)
        model.save_model(model_path)
        print(f"[INFO] Saved model to {model_path}")
        
    # 5. Predict probabilities
    proba = model.predict_proba(X_test)[:, 1]
    y_pred = (proba >= 0.5).astype(int)
    
    # 6. Compute SHAP values for all rows in the test set
    print("[INFO] Computing SHAP values for the test set...")
    try:
        import shap
        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(X_test)
        
        # Handle SHAP library output differences (if list, grab positive class SHAP values)
        if isinstance(shap_values, list) and len(shap_values) == 2:
            shap_values = shap_values[1]
        shap_available = True
    except ImportError:
        print("[WARNING] shap library not found. Falling back to native feature importances for pseudo-SHAP.")
        # Create pseudo-SHAP values based on native gain-based importances for compatibility
        booster = model.get_booster()
        gain_importances = booster.get_score(importance_type="gain")
        # Normalize and map to features
        max_gain = max(gain_importances.values()) if gain_importances else 1.0
        feature_gains = {feat: gain_importances.get(feat, 0.0) / max_gain for feat in FEATURE_COLS}
        
        # Pseudo SHAP values per row
        shap_values = np.zeros((len(X_test), len(FEATURE_COLS)))
        for i in range(len(X_test)):
            row = X_test.iloc[i]
            for col_idx, col in enumerate(FEATURE_COLS):
                # Simple heuristic: positive if feature is above mean, weighted by gain
                val = row[col]
                mean_val = X_train[col].mean()
                direction = 1 if val > mean_val else -1
                shap_values[i, col_idx] = direction * feature_gains[col] * 0.1
        shap_available = False

    # 7. Generate records
    records = []
    for i in range(len(X_test)):
        task_id = str(meta_test.iloc[i]["Task_ID"]) if "Task_ID" in meta_test.columns else f"row_{i}"
        actual = int(y_test.iloc[i])
        predicted = int(y_pred[i])
        probability = float(proba[i])
        correct = bool(actual == predicted)
        
        row_shap = shap_values[i]
        row_features = X_test.iloc[i]
        
        # Build SHAP features list
        shap_feats = []
        for col_idx, col in enumerate(FEATURE_COLS):
            val = float(row_features[col])
            s_val = float(row_shap[col_idx])
            direction = "+increases" if s_val > 0 else "-decreases"
            shap_feats.append({
                "feature": col,
                "value": val,
                "shap_value": s_val,
                "direction": direction
            })
            
        # Sort by absolute SHAP value descending
        shap_feats.sort(key=lambda x: abs(x["shap_value"]), reverse=True)
        top_shap = shap_feats[:6]
        
        records.append({
            "task_id": task_id,
            "y_true": actual,
            "y_pred": predicted,
            "proba": probability,
            "correct": correct,
            "top_shap_features": top_shap
        })
        
    # 8. Write outputs
    all_pred_path = os.path.join(output_dir, "test_predictions.json")
    with open(all_pred_path, "w") as f:
        json.dump(records, f, indent=2)
        
    misclassified = [r for r in records if not r["correct"]]
    misclassified_path = os.path.join(output_dir, "misclassified.json")
    with open(misclassified_path, "w") as f:
        json.dump(misclassified, f, indent=2)
        
    # 9. Log summary statistics to stdout
    accuracy = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, proba)
    
    print(f"\n{'=' * 50}")
    print("  PREDICTION PIPELINE COMPLETED")
    print(f"{'=' * 50}")
    print(f"  Test set size      : {len(X_test)} rows")
    print(f"  Accuracy           : {accuracy:.4%}")
    print(f"  ROC-AUC            : {auc:.4f}")
    print(f"  Misclassified count: {len(misclassified)}")
    print(f"  All predictions -> {all_pred_path}")
    print(f"  Misclassified   -> {misclassified_path}")
    print(f"{'=' * 50}\n")

if __name__ == "__main__":
    main()
