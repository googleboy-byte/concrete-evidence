"""
fetch_courtlistener_cases.py

Automates pulling construction-delay-related case opinions from the
CourtListener REST API (v4) for use in forensic causal-chain extraction.

Setup:
    1. Create a free account at https://www.courtlistener.com
    2. Get your API token from your profile page
    3. pip install python-dotenv
    4. Create a .env file in the same directory as this script, containing:
         COURTLISTENER_APITOKEN=your_token_here

Usage:
    python fetch_courtlistener_cases.py

Output:
    Writes one JSON file per matched case into ./courtlistener_cases/
    plus a manifest.csv summarizing what was pulled.
"""

import os
import time
import json
import csv
import requests
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

load_dotenv()

API_TOKEN = os.environ.get("COURTLISTENER_APITOKEN", "")
BASE_URL = "https://www.courtlistener.com/api/rest/v4"
OUTPUT_DIR = Path("courtlistener_cases")
OUTPUT_DIR.mkdir(exist_ok=True)

# Search queries to run -- each is a separate CourtListener full-text search.
# Tune these based on what surfaces good vs. noisy results (see notes at bottom).
SEARCH_QUERIES = [
    '"critical path method" delay construction',
    '"differing site conditions" delay claim',
    '"liquidated damages" construction delay',
    '"extension of time" construction contract delay',
    '"concurrent delay" construction',
]

MAX_RESULTS_PER_QUERY = 5    # free tier: 125 calls/day total, ~2+ calls per case -- start small
REQUEST_DELAY_SECONDS = 15   # free tier is 5 calls/minute -- 15s gives real-world headroom

HEADERS = {
    "Authorization": f"Token {API_TOKEN}",
    "User-Agent": "research-script/1.0 (academic forensic delay-chain research)",
}

DAILY_CALL_BUDGET = 115  # stay a bit under the 125/day free-tier ceiling
_call_count = 0


def _guarded_get(url, max_retries=4, backoff_seconds=30, **kwargs):
    """
    Wrapper around requests.get that:
      - tracks calls against the daily budget
      - automatically retries on 429 with backoff (instead of failing the call)
    """
    global _call_count

    for attempt in range(max_retries):
        if _call_count >= DAILY_CALL_BUDGET:
            print(f"\nDaily call budget ({DAILY_CALL_BUDGET}) reached -- stopping for today.")
            raise SystemExit(0)

        _call_count += 1
        resp = requests.get(url, **kwargs)

        if resp.status_code == 429:
            wait = backoff_seconds * (attempt + 1)  # 30s, 60s, 90s, 120s
            print(f"  429 rate limited -- backing off {wait}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue

        return resp

    # exhausted retries -- return the last (failing) response so caller's
    # raise_for_status() surfaces a clear error rather than us silently
    # returning None
    return resp

# ---------------------------------------------------------------------------
# CORE FUNCTIONS
# ---------------------------------------------------------------------------

def search_opinions(query, max_results=40):
    """
    Query CourtListener's search endpoint for case law opinions.
    Returns a list of result dicts (metadata only -- not full text yet).
    """
    results = []
    url = f"{BASE_URL}/search/"
    params = {
        "q": query,
        "type": "o",          # 'o' = opinions (case law)
        "order_by": "score desc",
    }

    while url and len(results) < max_results:
        resp = _guarded_get(url, headers=HEADERS, params=params, timeout=30)
        params = None  # only needed on first request; subsequent pages use 'next' URL directly
        resp.raise_for_status()

        data = resp.json()
        results.extend(data.get("results", []))
        url = data.get("next")  # pagination cursor
        time.sleep(REQUEST_DELAY_SECONDS)

    return results[:max_results]


def fetch_full_opinion_text(cluster_id):
    """
    Given an opinion cluster ID, fetch the full text of the opinion(s).
    Prefers html_with_citations, falls back to plain_text.
    """
    url = f"{BASE_URL}/clusters/{cluster_id}/"
    resp = _guarded_get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    cluster = resp.json()
    time.sleep(REQUEST_DELAY_SECONDS)

    full_texts = []
    for sub_op_url in cluster.get("sub_opinions", []):
        op_resp = _guarded_get(sub_op_url, headers=HEADERS, timeout=30)
        op_resp.raise_for_status()
        op_data = op_resp.json()
        text = (
            op_data.get("html_with_citations")
            or op_data.get("plain_text")
            or op_data.get("html")
            or ""
        )
        full_texts.append(text)
        time.sleep(REQUEST_DELAY_SECONDS)

    return "\n\n---\n\n".join(full_texts), cluster


def looks_relevant(text, min_hits=2):
    """
    Cheap Stage-2 pre-filter (see prior discussion): require multiple
    construction-specific terms to co-occur, and reject if it looks like
    a pure civil-procedure case (e.g. 'service of process') rather than
    a substantive construction delay dispute.
    """
    text_lower = text.lower()

    construction_terms = [
        "contractor", "subcontract", "construction contract", "project schedule",
        "critical path", "liquidated damages", "change order", "site conditions",
        "extension of time", "delay claim", "punch list", "certificate of completion",
    ]
    procedure_red_flags = [
        "service of the claim form", "service of process",
        "jurisdiction to try the claim", "personal jurisdiction over the defendant",
    ]

    hits = sum(1 for term in construction_terms if term in text_lower)
    red_flags = any(flag in text_lower for flag in procedure_red_flags)

    return hits >= min_hits and not red_flags


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------

def main():
    if not API_TOKEN:
        raise SystemExit(
            "No API token found. Create a .env file next to this script with:\n"
            "  COURTLISTENER_APITOKEN=your_token_here"
        )

    manifest_rows = []
    seen_cluster_ids = set()

    # Resume support: skip any cluster_id we already have a saved file for,
    # so reruns (e.g. after hitting the daily cap) don't waste calls
    # re-fetching cases from previous runs.
    already_saved_ids = set()
    for existing_file in OUTPUT_DIR.glob("*.json"):
        existing_id = existing_file.stem.split("_", 1)[0]
        if existing_id.isdigit():
            already_saved_ids.add(int(existing_id))
    if already_saved_ids:
        print(f"Resuming: {len(already_saved_ids)} cases already saved, will skip those.\n")

    for query in SEARCH_QUERIES:
        print(f"\nSearching: {query}")
        results = search_opinions(query, max_results=MAX_RESULTS_PER_QUERY)
        print(f"  -> {len(results)} candidates")

        for r in results:
            cluster_id = r.get("cluster_id") or r.get("id")
            if cluster_id in seen_cluster_ids:
                continue
            seen_cluster_ids.add(cluster_id)

            if cluster_id in already_saved_ids:
                print(f"  skip (already saved): cluster {cluster_id}")
                continue

            try:
                full_text, cluster_meta = fetch_full_opinion_text(cluster_id)
            except requests.HTTPError as e:
                print(f"  ! failed to fetch cluster {cluster_id}: {e}")
                print("  Likely the daily call budget has been hit -- stopping this run.")
                write_manifest(manifest_rows)
                return
            except SystemExit:
                write_manifest(manifest_rows)
                return

            if not looks_relevant(full_text):
                continue  # Stage 2 filter -- skip likely false positives

            case_name = cluster_meta.get("case_name", "unknown_case")
            date_filed = cluster_meta.get("date_filed", "unknown_date")
            court = r.get("court", "unknown_court")

            record = {
                "cluster_id": cluster_id,
                "case_name": case_name,
                "date_filed": date_filed,
                "court": court,
                "source_query": query,
                "absolute_url": f"https://www.courtlistener.com{cluster_meta.get('absolute_url', '')}",
                "full_text": full_text,
            }

            safe_name = "".join(c if c.isalnum() else "_" for c in case_name)[:80]
            out_path = OUTPUT_DIR / f"{cluster_id}_{safe_name}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)

            manifest_rows.append({
                "cluster_id": cluster_id,
                "case_name": case_name,
                "date_filed": date_filed,
                "court": court,
                "source_query": query,
                "file": str(out_path),
            })
            print(f"  saved: {case_name}")

    write_manifest(manifest_rows)


def write_manifest(new_rows):
    """
    Write manifest.csv, merging with any existing manifest so that rows
    from previous runs (skipped this time via resume logic) aren't lost.
    """
    manifest_path = OUTPUT_DIR / "manifest.csv"
    fieldnames = ["cluster_id", "case_name", "date_filed", "court", "source_query", "file"]

    existing_rows = []
    if manifest_path.exists():
        with open(manifest_path, "r", newline="", encoding="utf-8") as f:
            existing_rows = list(csv.DictReader(f))

    combined = {row["cluster_id"]: row for row in existing_rows}
    for row in new_rows:
        combined[str(row["cluster_id"])] = row

    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(combined.values())

    print(f"\nDone this run. {len(new_rows)} new cases saved.")
    print(f"Total in manifest: {len(combined)}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# NOTES
# ---------------------------------------------------------------------------
# - Tune SEARCH_QUERIES based on what you're actually finding -- run a small
#   batch first (MAX_RESULTS_PER_QUERY=5) and manually eyeball the manifest
#   before scaling up.
# - looks_relevant() is a blunt first-pass filter. You'll still want an LLM
#   classification pass (Stage 3) on top of this before doing DAG extraction,
#   since keyword co-occurrence alone won't catch everything.
# - Rate limits: default CourtListener accounts get several thousand
#   requests/hour; this script's REQUEST_DELAY_SECONDS keeps you well under
#   that, but raise it if you start seeing 429s.
# - Full opinion text can be long (some run 50-100+ pages for complex
#   construction disputes) -- consider chunking before sending to an LLM
#   for extraction rather than passing the whole thing in one call.