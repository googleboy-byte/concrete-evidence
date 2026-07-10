#!/usr/bin/env python3
"""
error_analysis.py

Processes the misclassified rows from the test set, queries the precedent matcher,
generates a plain-English diagnosis line for each mismatch, and outputs:
  - merge/output/error_analysis.json
  - merge/output/error_analysis_report.md
"""

import os
import sys
import json

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MERGE_DIR = os.path.dirname(_THIS_DIR)
_OUTPUT_DIR = os.path.join(_MERGE_DIR, "output")

# Add scripts directory to sys.path to import precedent_matcher
sys.path.insert(0, _THIS_DIR)
from precedent_matcher import match_precedents

def generate_cascade_string(causal_chain):
    """Extracts a sequence of unique event types in the walk order."""
    event_types_in_walk = []
    for edge in causal_chain:
        from_et = edge.get("from_event_type")
        to_et = edge.get("to_event_type")
        if from_et and from_et not in event_types_in_walk:
            event_types_in_walk.append(from_et)
        if to_et and to_et not in event_types_in_walk:
            event_types_in_walk.append(to_et)
            
    if event_types_in_walk:
        return " cascading into ".join(event_types_in_walk)
    return "no active causal chain"

def generate_diagnosis(top_shap_features, y_pred, precedent):
    """Generates a plain-English diagnosis line comparing model features to the causal chain."""
    top_feature = top_shap_features[0]["feature"] if top_shap_features else "unknown_feature"
    pred_str = "low risk" if y_pred == 0 else "high risk"
    case_name = precedent.get("case_name", "unknown precedent")
    
    cascade_str = generate_cascade_string(precedent.get("causal_chain", []))
    
    diagnosis = (
        f"model saw a single elevated {top_feature} and predicted {pred_str}, "
        f"but the matched precedent — {case_name} — shows {cascade_str}; "
        f"the model has no feature capturing that downstream cascade, only the initial condition"
    )
    return diagnosis

def main():
    print("[INFO] Starting error_analysis.py")
    
    misclassified_path = os.path.join(_OUTPUT_DIR, "misclassified.json")
    test_pred_path = os.path.join(_OUTPUT_DIR, "test_predictions.json")
    
    if not os.path.exists(misclassified_path):
        print(f"[ERROR] {misclassified_path} does not exist. Run predict_test_set.py first.")
        sys.exit(1)
        
    with open(misclassified_path, "r") as f:
        misclassified_rows = json.load(f)
        
    # Read total test cases count
    total_test_count = 0
    if os.path.exists(test_pred_path):
        with open(test_pred_path, "r") as f:
            total_test_count = len(json.load(f))
            
    analysis_results = []
    
    # Process each misclassified row
    for row in misclassified_rows:
        task_id = row["task_id"]
        y_true = row["y_true"]
        y_pred = row["y_pred"]
        proba = row["proba"]
        top_shap_features = row["top_shap_features"]
        
        # Match precedents (up to 3)
        matches = match_precedents(top_shap_features, limit=3)
        
        entry = {
            "task_id": task_id,
            "y_true": y_true,
            "y_pred": y_pred,
            "proba": proba,
            "top_shap_features": top_shap_features,
            "precedents": []
        }
        
        if matches:
            for p in matches:
                p["diagnosis"] = generate_diagnosis(top_shap_features, y_pred, p)
                entry["precedents"].append(p)
            entry["diagnosis"] = entry["precedents"][0]["diagnosis"]
        else:
            entry["diagnosis"] = "no forensic precedent in the current case set covers this feature combination — gap in case coverage, not necessarily a bad prediction"
            
        analysis_results.append(entry)
        
    # Write error_analysis.json
    error_analysis_json_path = os.path.join(_OUTPUT_DIR, "error_analysis.json")
    with open(error_analysis_json_path, "w") as f:
        json.dump(analysis_results, f, indent=2)
    print(f"[INFO] Wrote error analysis JSON data -> {error_analysis_json_path}")
    
    # Compile error_analysis_report.md
    report_path = os.path.join(_OUTPUT_DIR, "error_analysis_report.md")
    
    # Compute error rates
    err_count = len(misclassified_rows)
    err_rate = (err_count / total_test_count) if total_test_count > 0 else 0.0
    
    with open(report_path, "w") as f:
        f.write("# Forensic Precedent Error Analysis Report\n\n")
        f.write("## Overview\n")
        f.write(f"- **Total Test Tasks**: {total_test_count}\n")
        f.write(f"- **Misclassifications**: {err_count}\n")
        f.write(f"- **Error Rate**: {err_rate:.2%}\n\n")
        f.write("This report details the structural blind spots in the flat-feature XGBoost delay-prediction model ")
        f.write("by comparing its predictions with actual forensic precedents containing multi-step causal chains.\n\n")
        
        f.write("## Misclassified Tasks and Forensic Precedents\n\n")
        
        for idx, entry in enumerate(analysis_results, 1):
            f.write(f"### {idx}. Task ID: {entry['task_id']}\n")
            true_label_str = "Delayed (High-Risk)" if entry['y_true'] == 1 else "Not Delayed (Low-Risk)"
            pred_label_str = "Delayed (High-Risk)" if entry['y_pred'] == 1 else "Not Delayed (Low-Risk)"
            
            f.write(f"- **True Label**: {true_label_str}\n")
            f.write(f"- **Predicted Probability**: {entry['proba']:.2%}\n")
            f.write(f"- **Predicted Label**: {pred_label_str}\n\n")
            
            # Top 3 SHAP drivers
            f.write("#### Top 3 SHAP Drivers\n")
            for sf in entry["top_shap_features"][:3]:
                sign = "+" if sf['shap_value'] > 0 else "-"
                f.write(f"- `{sf['feature']}` = {sf['value']:.4g} (SHAP contribution: {sf['shap_value']:+.4f}, pushes toward {sign}delay)\n")
            f.write("\n")
            
            # Diagnosis Line
            f.write("#### Causal Diagnosis\n")
            f.write(f"**{entry['diagnosis']}**\n\n")
            
            # Precedent Info
            if entry["precedents"]:
                top_p = entry["precedents"][0]
                f.write(f"#### Top Matched Precedent: {top_p['case_name']}\n")
                f.write(f"- **Citation**: {top_p['citation']}\n")
                f.write(f"- **Matched Event Types**: {', '.join(top_p['matched_event_types'])}\n\n")
                
                # Causal chain walk
                f.write("##### Causal Chain DAG Walk\n")
                if top_p["causal_chain"]:
                    for step_idx, step in enumerate(top_p["causal_chain"], 1):
                        f.write(f"{step_idx}. `{step['from_event_type']}` → `{step['to_event_type']}`\n")
                        f.write(f"   - *Relationship*: {step['relationship']} (Strength: {step['causal_strength']})\n")
                        f.write(f"   - *Source*: \"{step['from_node']}\"\n")
                        f.write(f"   - *Target*: \"{step['to_node']}\"\n")
                else:
                    f.write("*No causal chain paths found for this precedent.*\n")
                f.write("\n")
                
                # Excerpts
                f.write("##### Grounding Trial Excerpts\n")
                has_excerpts = False
                for node_exc in top_p["matched_node_excerpts"]:
                    if node_exc["excerpt"]:
                        has_excerpts = True
                        f.write(f"- **{node_exc['event_type']}**:\n")
                        f.write(f"  > \"{node_exc['excerpt']}\"\n")
                        if node_exc["citation"]:
                            f.write(f"  > *— Source Citation: {node_exc['citation']}*\n")
                if not has_excerpts:
                    f.write("*No explicit trial excerpts found for matched event types.*\n")
                f.write("\n")
            else:
                f.write("#### Top Matched Precedent\n")
                f.write("*No forensic precedents mapped to the driver features of this task.*\n\n")
                
            f.write("---\n\n")
            
    print(f"[INFO] Wrote error analysis report -> {report_path}")

if __name__ == "__main__":
    main()
