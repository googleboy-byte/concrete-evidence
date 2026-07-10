#!/usr/bin/env python3
"""
precedent_matcher.py

Handles mapping of predictive features to forensic event types using taxonomy.yaml,
queries the forensic_dags.db database (in read-only mode) to find matching cases,
ranks them, and walks their causal DAGs using anchored backward and forward traversals.
"""

import os
import sqlite3
import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
DEFAULT_TAXONOMY_PATH = os.path.join(_REPO_ROOT, "..", "taxonomy.yaml")
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "..", "forensic_dags.db")

# Confidence scale mapping
CONFIDENCE_MAP = {
    "court_finding": 5,
    "explicit": 5,
    "implied": 4,
    "expert_opinion_disputed": 3,
    "alleged_only": 2,
    "contested": 1
}

def load_taxonomy(taxonomy_path=DEFAULT_TAXONOMY_PATH):
    """Loads taxonomy.yaml and builds a reverse index from feature aliases to event types."""
    if not os.path.exists(taxonomy_path):
        # Try relative path from the script directory (up one level)
        alt_path = os.path.abspath(os.path.join(_THIS_DIR, "..", "taxonomy.yaml"))
        if os.path.exists(alt_path):
            taxonomy_path = alt_path
        else:
            raise FileNotFoundError(
                f"Could not locate taxonomy.yaml at '{taxonomy_path}' or '{alt_path}'. "
                "Please verify you are running the script from within the repository structure."
            )

    with open(taxonomy_path, "r") as f:
        data = yaml.safe_load(f)
        
    feature_to_events = {}
    event_types = data.get("event_types", {})
    for event_type, info in event_types.items():
        aliases = info.get("predictive_feature_aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if alias not in feature_to_events:
                    feature_to_events[alias] = []
                feature_to_events[alias].append(event_type)
        elif isinstance(aliases, str):
            alias = aliases
            if alias not in feature_to_events:
                feature_to_events[alias] = []
            feature_to_events[alias].append(event_type)
            
    return feature_to_events, data

def get_db_connection(db_path=DEFAULT_DB_PATH):
    """Opens a connection to SQLite database in read-only mode."""
    if not os.path.exists(db_path):
        # Try relative path from the script directory (up one level)
        alt_path = os.path.abspath(os.path.join(_THIS_DIR, "..", "forensic_dags.db"))
        if os.path.exists(alt_path):
            db_path = alt_path
        else:
            raise FileNotFoundError(
                f"Could not locate forensic_dags.db at '{db_path}' or '{alt_path}'. "
                "Please verify you are running the script from within the repository structure."
            )
            
    # Open SQLite in read-only mode
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def match_precedents(top_shap_features, db_path=DEFAULT_DB_PATH, taxonomy_path=DEFAULT_TAXONOMY_PATH, limit=3, max_depth=2):
    """
    Given a list of top SHAP features (dicts with keys 'feature', 'shap_value', 'value', 'direction'),
    returns the top-limit matching precedents with their citations, matching event types,
    and causal walk chain anchored around matched event nodes.
    """
    # 1. Load taxonomy and map features to candidate event types
    feature_to_events, _ = load_taxonomy(taxonomy_path)
    
    # Sort top_shap_features by absolute SHAP value descending and pick top 3
    sorted_features = sorted(top_shap_features, key=lambda x: abs(x["shap_value"]), reverse=True)
    top_3_features = sorted_features[:3]
    
    # Select features that increased delay risk (shap_value > 0)
    drivers = [f["feature"] for f in top_3_features if f["shap_value"] > 0]
    if not drivers:
        # Fallback to all top 3 features if no positive contributors
        drivers = [f["feature"] for f in top_3_features]
        
    query_event_types = set()
    for d in drivers:
        for et in feature_to_events.get(d, []):
            query_event_types.add(et)
            
    if not query_event_types:
        return []
        
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    # 2. Query all nodes in the DB that match the candidate event types
    placeholders = ",".join("?" for _ in query_event_types)
    query_nodes = f"""
        SELECT node_pk, case_id, node_id, event_type, category, description, date, source_citation, source_excerpt, extraction_confidence
        FROM nodes
        WHERE event_type IN ({placeholders})
    """
    cursor.execute(query_nodes, list(query_event_types))
    matching_nodes = [dict(r) for r in cursor.fetchall()]
    
    if not matching_nodes:
        conn.close()
        return []
        
    # Group matching nodes by case_id
    case_matches = {}
    for node in matching_nodes:
        c_id = node["case_id"]
        if c_id not in case_matches:
            case_matches[c_id] = []
        case_matches[c_id].append(node)
        
    # 3. Fetch case metadata for all matching cases
    case_ids = list(case_matches.keys())
    case_placeholders = ",".join("?" for _ in case_ids)
    cursor.execute(f"SELECT case_id, case_name, court, date_filed FROM cases WHERE case_id IN ({case_placeholders})", case_ids)
    cases_meta = {r["case_id"]: dict(r) for r in cursor.fetchall()}
    
    # 4. Rank cases
    ranked_cases = []
    for c_id, nodes in case_matches.items():
        meta = cases_meta.get(c_id)
        if not meta:
            continue
            
        distinct_events = set(n["event_type"] for n in nodes)
        primary_score = len(distinct_events)
        
        # Drop cases that do not have at least 2 distinct matched event types
        if primary_score < 2:
            continue
            
        secondary_score = max(CONFIDENCE_MAP.get(n["extraction_confidence"], 0) for n in nodes)
        
        ranked_cases.append({
            "case_id": c_id,
            "meta": meta,
            "nodes": nodes,
            "distinct_events": distinct_events,
            "primary_score": primary_score,
            "secondary_score": secondary_score
        })
        
    # Sort: primary descending, then secondary descending
    ranked_cases.sort(key=lambda x: (x["primary_score"], x["secondary_score"]), reverse=True)
    top_matches = ranked_cases[:limit]
    
    # 5. Build causal walks for the top matches
    results = []
    for match in top_matches:
        c_id = match["case_id"]
        meta = match["meta"]
        
        # Query all nodes for this case to build full DAG
        cursor.execute("SELECT node_id, event_type, description, source_citation, source_excerpt, extraction_confidence FROM nodes WHERE case_id = ?", (c_id,))
        all_nodes = {r["node_id"]: dict(r) for r in cursor.fetchall()}
        
        # Query all edges for this case
        cursor.execute("SELECT source_node_id, target_node_id, relationship, causal_strength, source_citation FROM edges WHERE case_id = ?", (c_id,))
        all_edges = [dict(r) for r in cursor.fetchall()]
        
        # Build DAG adjacency maps for backward and forward walks
        forward_adj = {nid: [] for nid in all_nodes}
        backward_adj = {nid: [] for nid in all_nodes}
        
        for edge in all_edges:
            src = edge["source_node_id"]
            tgt = edge["target_node_id"]
            if src in forward_adj and tgt in forward_adj:
                forward_adj[src].append(edge)
                backward_adj[tgt].append(edge)
                
        # Helper traversals for backward and forward steps
        def walk_backward(curr, depth, visited, collected_edges):
            if depth >= max_depth:
                return
            incoming = sorted(backward_adj[curr], key=lambda x: x["source_node_id"])
            for edge in incoming:
                src = edge["source_node_id"]
                edge_key = (src, curr, edge["relationship"])
                if edge_key not in visited:
                    visited.add(edge_key)
                    walk_backward(src, depth + 1, visited, collected_edges)
                    collected_edges.append(edge)
                    
        def walk_forward(curr, depth, visited, collected_edges):
            if depth >= max_depth:
                return
            outgoing = sorted(forward_adj[curr], key=lambda x: x["target_node_id"])
            for edge in outgoing:
                tgt = edge["target_node_id"]
                edge_key = (curr, tgt, edge["relationship"])
                if edge_key not in visited:
                    visited.add(edge_key)
                    collected_edges.append(edge)
                    walk_forward(tgt, depth + 1, visited, collected_edges)

        # For each matched node, run the backward and forward walks
        causal_chains = []
        sorted_matched_nodes = sorted(match["nodes"], key=lambda x: x["node_id"])
        
        for node in sorted_matched_nodes:
            matched_node_id = node["node_id"]
            matched_event_type = node["event_type"]
            
            back_edges = []
            walk_backward(matched_node_id, 0, set(), back_edges)
            
            fwd_edges = []
            walk_forward(matched_node_id, 0, set(), fwd_edges)
            
            combined_edges = back_edges + fwd_edges
            formatted_chain = []
            for edge in combined_edges:
                src = edge["source_node_id"]
                tgt = edge["target_node_id"]
                formatted_chain.append({
                    "from_node": all_nodes[src]["description"],
                    "from_event_type": all_nodes[src]["event_type"],
                    "relationship": edge["relationship"],
                    "to_node": all_nodes[tgt]["description"],
                    "to_event_type": all_nodes[tgt]["event_type"],
                    "causal_strength": edge["causal_strength"]
                })
                
            causal_chains.append({
                "anchor_node_id": matched_node_id,
                "anchor_event_type": matched_event_type,
                "chain": formatted_chain
            })
            
        # Format citation: case_name, court (date_filed)
        date_filed = meta.get("date_filed", "")
        year = date_filed.split("-")[0] if date_filed else "N/A"
        citation = f"{meta['case_name']}, {meta['court']} ({year})"
        
        # Collect excerpts/citations from matched nodes
        matched_node_excerpts = []
        for n in match["nodes"]:
            matched_node_excerpts.append({
                "event_type": n["event_type"],
                "citation": n["source_citation"] or "",
                "excerpt": n["source_excerpt"] or ""
            })
            
        first_chain = causal_chains[0]["chain"] if causal_chains else []
            
        results.append({
            "case_id": c_id,
            "case_name": meta["case_name"],
            "court": meta["court"],
            "citation": citation,
            "matched_event_types": list(match["distinct_events"]),
            "causal_chain": first_chain,
            "causal_chains": causal_chains,
            "matched_node_excerpts": matched_node_excerpts
        })
        
    conn.close()
    return results

if __name__ == "__main__":
    print("[INFO] Testing precedent matcher with site_constraint_score feature...")
    sample_features = [
        {"feature": "site_constraint_score", "shap_value": 0.85, "value": 0.72, "direction": "+increases"}
    ]
    matches = match_precedents(sample_features, limit=1)
    if matches:
        m = matches[0]
        print(f"Matched Precedent: {m['case_name']}")
        print(f"Citation: {m['citation']}")
        print(f"Matched Event Types: {m['matched_event_types']}")
        print(f"Number of Anchored Causal Chains: {len(m['causal_chains'])}")
        for i, cc in enumerate(m['causal_chains']):
            print(f"  Chain {i+1} anchored on '{cc['anchor_event_type']}': {len(cc['chain'])} steps")
    else:
        print("[WARNING] No precedents matched.")
