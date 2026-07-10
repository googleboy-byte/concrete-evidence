"""
extract_dags.py

Stage 2 of the pipeline: converts scored CourtListener case JSON files into
structured causal DAGs (nodes + edges), validated against taxonomy.yaml,
and stores them in a queryable SQLite database.

Pipeline position:
  fetch_courtlistener_cases.py  -> raw case JSON (full_text)
  score_cases_nvidia_llm.py     -> adds llm_score (relevance/richness)
  extract_dags.py (this file)   -> adds structured nodes/edges, stores in DB

Requires in the same directory:
  - taxonomy.yaml          (the alignment layer -- event types/categories/feature aliases)
  - dag_schema.json         (reference only -- describes the shape this script produces)
  - courtlistener_cases/*.json  (already fetched + scored)

Setup:
    pip install requests python-dotenv pyyaml
    .env:
        NIM_API_KEY=nvapi-your_key_here

Usage:
    python extract_dags.py

Output:
    - dags/<case_id>.json          -- one structured DAG file per case (audit trail)
    - forensic_dags.db             -- SQLite DB with cases, nodes, edges tables
"""

import os
import re
import json
import time
import glob
import sqlite3
import yaml
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from sambanova import SambaNova

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

load_dotenv()

# Find all SambaNova API keys sorted by suffix
SAMBANOVA_KEYS = []
for k, v in os.environ.items():
    match = re.match(r"^SAMBANOVA_APIKEY_(\d+)$", k)
    if match and v.strip():
        SAMBANOVA_KEYS.append((int(match.group(1)), v.strip()))

SAMBANOVA_KEYS.sort(key=lambda x: x[0])
SAMBANOVA_KEYS = [v for _, v in SAMBANOVA_KEYS]

if not SAMBANOVA_KEYS:
    raise SystemExit("No SambaNova API keys found. Add to .env:\n  SAMBANOVA_APIKEY_1=your_key_here")

MODEL = "DeepSeek-V3.1"
FALLBACK_MODELS = []

CASES_DIR = Path("courtlistener_cases")
DAGS_DIR = Path("dags")
DAGS_DIR.mkdir(exist_ok=True)
DB_PATH = Path("forensic_dags.db")
TAXONOMY_PATH = Path("taxonomy.yaml")

# Only extract DAGs from cases that passed Stage 1 scoring.
MIN_RELEVANCE_SCORE = 4
MIN_RICHNESS_SCORE = 4
REQUIRE_IS_DELAY_CAUSATION_CASE = True

CHUNK_CHAR_LIMIT = 15000  # per-chunk size; cases longer than this get split across multiple calls
REQUEST_DELAY_SECONDS = 2.0
MAX_RETRIES = 4

SCHEMA_VERSION = "0.1"

# ---------------------------------------------------------------------------
# TAXONOMY LOADING
# ---------------------------------------------------------------------------

def load_taxonomy(path=TAXONOMY_PATH):
    with open(path, "r", encoding="utf-8") as f:
        tax = yaml.safe_load(f)
    return tax


TAXONOMY = load_taxonomy()
TAXONOMY_VERSION = TAXONOMY.get("version", "unknown")
VALID_EVENT_TYPES = set(TAXONOMY.get("event_types", {}).keys()) | {"unclassified"}
EVENT_TYPE_TO_CATEGORY = {
    k: v.get("category") for k, v in TAXONOMY.get("event_types", {}).items()
}
EVENT_TYPE_TO_CATEGORY["unclassified"] = "unclassified"

# ---------------------------------------------------------------------------
# TEXT PROCESSING -- prioritize fact-finding/conclusion sections over
# citation-heavy legal-standard boilerplate (see prior discussion re:
# Old Veteran Construction excerpt getting cut off mid-boilerplate)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TEXT PROCESSING -- prioritize fact-finding/conclusion sections over
# citation-heavy legal-standard boilerplate.
#
# IMPORTANT: CourtListener sources use INCONSISTENT markup across courts.
# Some (e.g. Court of Federal Claims opinions like Old Veteran Construction)
# use clean <p id="...">HEADER</p> tags. Others (e.g. ASBCA opinions like
# BCI Construction) are plain text with \x0c form-feed page breaks and no
# <p> tags at all. A regex built against one format silently matches
# nothing on the other -- this was caught by testing against real data
# (BCI Construction produced zero section matches despite having a clear
# "STATEMENT OF FACTS (SOF) FOR PURPOSES OF THE MOTIONS" heading).
#
# Fix: normalize to plain text FIRST (strip tags, convert block-level
# boundaries to paragraph breaks, collapse form-feeds), then detect both
# headers and citation density on that normalized text. This works
# regardless of source markup style.
# ---------------------------------------------------------------------------

BLOCK_BOUNDARY_TAGS = re.compile(r"</(p|blockquote|div)>|<br\s*/?>", re.IGNORECASE)
ANY_TAG = re.compile(r"<[^>]+>")

HEADER_LINE_PATTERN = re.compile(
    r'^[ \t]*(FINDINGS OF FACT|STATEMENT OF FACTS|BACKGROUND|DISCUSSION|'
    r'ANALYSIS|CONCLUSION|OPINION)\b[^\n]{0,60}$',
    re.IGNORECASE | re.MULTILINE
)

# Legal citation reporter patterns (U.S., F.2d/F.3d, S.Ct., L.Ed., Fed.Cl.,
# Fed.Appx.) -- used as a citation-density signal that works on plain text,
# not dependent on <span class="citation"> tags being present.
CITATION_PATTERN = re.compile(
    r'\b\d+\s+(U\.S\.|F\.\s?\d?d|S\.\s?Ct\.|L\.\s?Ed\.\s?\d?d?|Fed\.\s?Cl\.|Fed\.\s?Appx\.|Cl\.\s?Ct\.)\s+\d+',
    re.IGNORECASE
)


def normalize_to_paragraphs(raw_full_text):
    """
    Converts raw HTML/XML/plain-text CourtListener content into normalized
    plain text with paragraph boundaries preserved as double-newlines,
    regardless of the source's original markup style.
    """
    text = BLOCK_BOUNDARY_TAGS.sub("\n\n", raw_full_text)
    text = ANY_TAG.sub(" ", text)
    text = text.replace("\x0c", "\n\n")           # form-feed page breaks -> paragraph break
    text = re.sub(r"[ \t]+", " ", text)             # collapse horizontal whitespace only
    text = re.sub(r"\n[ \t]*\n[ \t]*(\n[ \t]*)*", "\n\n", text)  # collapse 3+ newlines to exactly 2
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)     # trim spaces around single newlines
    return text.strip()


def citation_density(paragraph_text):
    """Citations per 1000 chars, using reporter-pattern matching that works
    on plain text regardless of whether the source had HTML citation tags."""
    length = max(len(paragraph_text), 1)
    citation_count = len(CITATION_PATTERN.findall(paragraph_text))
    return citation_count / (length / 1000)


def get_priority_content(raw_full_text):
    """
    Returns the FULL prioritized content (no truncation) -- FINDINGS OF FACT /
    STATEMENT OF FACTS / BACKGROUND / CONCLUSION sections in full, plus
    low-citation-density paragraphs from DISCUSSION/ANALYSIS/OPINION sections.
    Chunking (chunk_content) handles splitting this for cases too long for
    one call. Format-agnostic -- see module note above.
    """
    plain = normalize_to_paragraphs(raw_full_text)
    matches = list(HEADER_LINE_PATTERN.finditer(plain))

    if not matches:
        # no detectable section structure -- fall back to a straightforward
        # excerpt, skipping likely caption boilerplate at the very start
        start = min(800, len(plain) // 10)
        return plain[start:]

    sections = []
    for i, m in enumerate(matches):
        header = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plain)
        sections.append((header, plain[start:end]))

    priority_order = ["FINDINGS OF FACT", "STATEMENT OF FACTS", "BACKGROUND", "CONCLUSION"]
    secondary = ["DISCUSSION", "ANALYSIS", "OPINION"]

    selected_paragraphs = []

    # Single pass in original document order, so e.g. an OPINION intro
    # paragraph that passes the citation filter doesn't get reordered to
    # after a later CONCLUSION section just because priority sections were
    # processed in a separate pass.
    for header, chunk in sections:
        if header in priority_order:
            for para in chunk.split("\n\n"):
                para = para.strip()
                if len(para) > 20:
                    selected_paragraphs.append(para)
        elif header in secondary:
            for para in chunk.split("\n\n"):
                para = para.strip()
                if len(para) > 40 and citation_density(para) < 3:
                    selected_paragraphs.append(para)

    return "\n\n".join(selected_paragraphs)





def chunk_content(content, chunk_char_limit=CHUNK_CHAR_LIMIT):
    """
    Splits prioritized content into ordered chunks, breaking on paragraph
    boundaries (double-newline) rather than mid-sentence where possible.
    Returns a list of chunk strings, in document order.
    """
    if len(content) <= chunk_char_limit:
        return [content]

    paragraphs = content.split("\n\n")
    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # account for the rejoin separator
        if current_len + para_len > chunk_char_limit and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks


# ---------------------------------------------------------------------------
# EXTRACTION PROMPT
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT_TEMPLATE = """You are extracting a structured causal chain (DAG: nodes + edges) from a US construction-delay case opinion, for a forensic research database.

Case name: {case_name}
Court: {court}

You MUST classify every node's event_type using ONLY one of these exact values:
{event_type_list}

If nothing fits, use "unclassified" and explain why in extraction_notes -- do not invent new event_type values.

Respond with ONLY a JSON object (no markdown fences, no preamble, no text before or after) matching this exact schema:

{{
  "nodes": [
    {{
      "node_id": "n1",
      "event_type": "<one of the allowed values above>",
      "description": "concise 1-2 sentence description of this specific event as it occurred in this case",
      "date": "YYYY-MM-DD or null if undated",
      "date_confidence": "exact" | "approximate" | "inferred" | "undated",
      "source_citation": "paragraph id, page number, or other locator from the text supporting this node",
      "source_excerpt": "short paraphrase (under 50 words) of the supporting text -- NOT a verbatim quote",
      "extraction_confidence": "explicit" | "implied" | "contested"
    }}
  ],
  "edges": [
    {{
      "edge_id": "e1",
      "source_node_id": "n1",
      "target_node_id": "n2",
      "relationship": "caused" | "contributed_to" | "delayed_start_of" | "concurrent_with" | "mitigated",
      "causal_strength": "court_finding" | "expert_opinion_undisputed" | "expert_opinion_disputed" | "alleged_only",
      "source_citation": "paragraph id or locator supporting this specific causal claim"
    }}
  ],
  "case_metadata": {{
    "project_type": "short description if determinable from text, else null",
    "government_contract_number": "if present, else null"
  }},
  "extraction_notes": ["any ambiguities or unclassifiable elements worth human review"]
}}

Guidance:
- Extract 3-12 nodes typically -- enough to capture the real causal chain, not every incidental fact mentioned.
- extraction_confidence "contested" is valid and useful: if a party alleged a causal link the court rejected or didn't resolve, extract it anyway and mark it contested. Rejected causal claims are valuable negative-example data.
- Only create edges between nodes you've actually extracted (source_node_id/target_node_id must reference node_id values in your nodes list).
- Paraphrase source_excerpt in your own words -- do not copy verbatim text from the opinion.

Opinion excerpt:
---
{excerpt}
---
"""


def build_event_type_list():
    lines = []
    for et, info in TAXONOMY.get("event_types", {}).items():
        lines.append(f'  - "{et}": {info.get("description", "")}')
    return "\n".join(lines)


EVENT_TYPE_LIST_TEXT = build_event_type_list()

CHUNK_EXTRACTION_PROMPT_TEMPLATE = """You are extracting a structured causal chain (DAG: nodes + edges) from a US construction-delay case opinion, for a forensic research database.

This case's text is long and has been split into {total_chunks} parts, in document order. You are processing PART {chunk_num} of {total_chunks}.

Case name: {case_name}
Court: {court}

{known_nodes_block}

You MUST classify every node's event_type using ONLY one of these exact values:
{event_type_list}

If nothing fits, use "unclassified" and explain why in extraction_notes -- do not invent new event_type values.

IMPORTANT for multi-part extraction:
- Number new nodes starting from n{next_node_num} (continuing the sequence, not restarting at n1).
- If an event in THIS part is caused by, contributes to, or is otherwise causally linked to a node from a PREVIOUS part (listed above), create an edge referencing that earlier node_id directly -- do not re-extract it as a new node.
- If this part describes an event that appears to be the SAME event already listed above (not just related, but the same event), do not create a duplicate node -- skip it.
- Only extract nodes/edges for events described in THIS part's text below.

Respond with ONLY a JSON object (no markdown fences, no preamble, no text before or after) matching this exact schema:

{{
  "nodes": [
    {{
      "node_id": "n{next_node_num}",
      "event_type": "<one of the allowed values above>",
      "description": "concise 1-2 sentence description of this specific event as it occurred in this case",
      "date": "YYYY-MM-DD or null if undated",
      "date_confidence": "exact" | "approximate" | "inferred" | "undated",
      "source_citation": "paragraph id, page number, or other locator from the text supporting this node",
      "source_excerpt": "short paraphrase (under 50 words) of the supporting text -- NOT a verbatim quote",
      "extraction_confidence": "explicit" | "implied" | "contested"
    }}
  ],
  "edges": [
    {{
      "edge_id": "e_c{chunk_num}_1",
      "source_node_id": "n1 (may reference a node from a previous part)",
      "target_node_id": "n2",
      "relationship": "caused" | "contributed_to" | "delayed_start_of" | "concurrent_with" | "mitigated",
      "causal_strength": "court_finding" | "expert_opinion_undisputed" | "expert_opinion_disputed" | "alleged_only",
      "source_citation": "paragraph id or locator supporting this specific causal claim"
    }}
  ],
  "case_metadata": {{
    "project_type": "short description if determinable from this part's text, else null",
    "government_contract_number": "if present in this part, else null"
  }},
  "extraction_notes": ["any ambiguities or unclassifiable elements worth human review"]
}}

Guidance:
- extraction_confidence "contested" is valid and useful: if a party alleged a causal link the court rejected or didn't resolve, extract it anyway and mark it contested.
- Paraphrase source_excerpt in your own words -- do not copy verbatim text from the opinion.

Part {chunk_num} of {total_chunks} -- opinion excerpt:
---
{chunk_text}
---
"""


def build_known_nodes_block(known_nodes):
    if not known_nodes:
        return "No nodes have been extracted from earlier parts yet -- this is the first part."
    lines = ["Nodes already extracted from EARLIER parts of this same case (reference these node_ids for cross-part causal edges; do not re-extract them):"]
    for n in known_nodes:
        lines.append(f'  - {n["node_id"]} ({n["event_type"]}): {n["description"]}')
    return "\n".join(lines)


def extract_dag_chunked(case_name, court, chunks):
    """
    Processes a case's content across multiple ordered chunks, maintaining
    a rolling summary of previously-extracted nodes so later chunks can
    create edges back to earlier ones. Returns a combined raw extraction
    dict in the same shape a single-call extraction would produce.
    """
    all_nodes, all_edges, all_notes = [], [], []
    case_metadata = {"project_type": None, "government_contract_number": None}

    for idx, chunk_text in enumerate(chunks, start=1):
        known_nodes_summary = [
            {"node_id": n["node_id"], "event_type": n.get("event_type", ""), "description": n.get("description", "")}
            for n in all_nodes
        ]

        prompt = CHUNK_EXTRACTION_PROMPT_TEMPLATE.format(
            case_name=case_name,
            court=court,
            chunk_num=idx,
            total_chunks=len(chunks),
            known_nodes_block=build_known_nodes_block(known_nodes_summary),
            event_type_list=EVENT_TYPE_LIST_TEXT,
            next_node_num=len(all_nodes) + 1,
            chunk_text=chunk_text,
        )

        print(f"    ┌─ part {idx}/{len(chunks)} ({len(chunk_text)} chars) — sending to LLM...")

        chunk_result = None
        for model_name in [MODEL] + FALLBACK_MODELS:
            print(f"    │  trying model: {model_name}")
            chunk_result = _try_extract_with_model(model_name, prompt)
            if chunk_result is not None:
                print(f"    │  ✓ got response from {model_name}")
                break
            print(f"    │  ✗ {model_name} failed for this part, trying next fallback...")

        if chunk_result is None:
            print(f"    └─ ✗ FAILED: all models exhausted for part {idx}")
            all_notes.append(f"Part {idx}/{len(chunks)}: extraction failed for this part (all models exhausted) -- content from this part may be missing.")
            continue

        new_nodes = chunk_result.get("nodes", [])
        new_edges = chunk_result.get("edges", [])
        new_notes = chunk_result.get("extraction_notes", [])

        for n in new_nodes:
            print(f"    │  + NODE {n.get('node_id', '?'):>4}  [{n.get('event_type', '?')}]  "
                  f"conf={n.get('extraction_confidence', '?')}  "
                  f"date={n.get('date') or 'undated'}")
            print(f"    │         {n.get('description', '(no description)')[:120]}")

        for e in new_edges:
            print(f"    │  + EDGE {e.get('edge_id', '?'):>8}  "
                  f"{e.get('source_node_id', '?')} ──[{e.get('relationship', '?')}]──▶ {e.get('target_node_id', '?')}  "
                  f"strength={e.get('causal_strength', '?')}")

        if new_notes:
            for note in new_notes:
                print(f"    │  ℹ NOTE: {note[:150]}")

        all_nodes.extend(new_nodes)
        all_edges.extend(new_edges)
        all_notes.extend(new_notes)

        part_meta = chunk_result.get("case_metadata", {}) or {}
        if not case_metadata["project_type"] and part_meta.get("project_type"):
            case_metadata["project_type"] = part_meta["project_type"]
            print(f"    │  ℹ project_type discovered: {part_meta['project_type']}")
        if not case_metadata["government_contract_number"] and part_meta.get("government_contract_number"):
            case_metadata["government_contract_number"] = part_meta["government_contract_number"]
            print(f"    │  ℹ contract# discovered: {part_meta['government_contract_number']}")

        print(f"    └─ part {idx} complete: {len(new_nodes)} nodes, {len(new_edges)} edges  "
              f"(running total: {len(all_nodes)} nodes, {len(all_edges)} edges)")

        if idx < len(chunks):
            time.sleep(REQUEST_DELAY_SECONDS)

    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "case_metadata": case_metadata,
        "extraction_notes": all_notes,
    }


# ---------------------------------------------------------------------------
# LLM CALL (same retry/fallback pattern as score_cases_nvidia_llm.py)
# ---------------------------------------------------------------------------

def extract_dag(case_name, court, excerpt):
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        case_name=case_name,
        court=court,
        event_type_list=EVENT_TYPE_LIST_TEXT,
        excerpt=excerpt,
    )

    models_to_try = [MODEL] + FALLBACK_MODELS if FALLBACK_MODELS else [MODEL]
    for model_name in models_to_try:
        result = _try_extract_with_model(model_name, prompt)
        if result is not None:
            return result
        print(f"    giving up on {model_name} after retries, trying next fallback...")

    raise RuntimeError("Exhausted all models (primary + fallbacks) against SambaNova API")


def _try_extract_with_model(model_name, prompt):
    messages = [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": prompt}
    ]

    for key_idx, api_key in enumerate(SAMBANOVA_KEYS):
        # Mask the key for logging (first 6 chars, then dots)
        masked_key = f"{api_key[:6]}..." if len(api_key) > 6 else "..."
        print(f"    │  [Key {key_idx + 1}/{len(SAMBANOVA_KEYS)}] trying extraction (key: {masked_key})...")

        for attempt in range(MAX_RETRIES):
            try:
                client = SambaNova(
                    api_key=api_key,
                    base_url="https://api.sambanova.ai/v1",
                )

                response = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=0.1,
                    top_p=0.1
                )

                raw_content = response.choices[0].message.content
                if not raw_content:
                    print(f"    │  [Key {key_idx + 1}] attempt {attempt + 1}: empty response")
                    continue

                raw_content = raw_content.strip()
                cleaned = re.sub(r"^```(?:json)?\s*", "", raw_content)
                cleaned = re.sub(r"\s*```$", "", cleaned)

                try:
                    parsed = json.loads(cleaned)
                    return parsed
                except json.JSONDecodeError:
                    print(f"    ! could not parse extraction output as JSON: {cleaned[:200]}")
                    return None

            except Exception as e:
                wait = 10 * (attempt + 1)
                print(f"    │  [Key {key_idx + 1}] attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    print(f"    │  backing off {wait}s...")
                    time.sleep(wait)

        print(f"    │  [Key {key_idx + 1}] exhausted all attempts. Trying next key if available...")

    return None


# ---------------------------------------------------------------------------
# VALIDATION -- enforce taxonomy compliance and structural integrity
# ---------------------------------------------------------------------------

def validate_and_normalize(extracted, case_id, case_name, court, date_filed):
    """
    Validates the raw LLM extraction against taxonomy + structural rules.
    Fixes what's safely fixable, flags the rest in extraction_notes,
    and returns a record matching dag_schema.json.
    """
    notes = list(extracted.get("extraction_notes", []))
    raw_nodes = extracted.get("nodes", [])
    raw_edges = extracted.get("edges", [])

    print(f"  ── validation: {len(raw_nodes)} raw nodes, {len(raw_edges)} raw edges")

    valid_node_ids = set()
    clean_nodes = []
    for n in raw_nodes:
        event_type = n.get("event_type", "unclassified")
        if event_type not in VALID_EVENT_TYPES:
            print(f"     ⚠ NODE {n.get('node_id')}: invalid event_type '{event_type}' → forced to 'unclassified'")
            notes.append(f"Node {n.get('node_id')}: invalid event_type '{event_type}' -> forced to 'unclassified'")
            event_type = "unclassified"

        node_id = n.get("node_id") or f"n{len(clean_nodes)+1}"
        valid_node_ids.add(node_id)

        category = EVENT_TYPE_TO_CATEGORY.get(event_type, "unclassified")
        print(f"     ✓ NODE {node_id:>4}  [{event_type}]  category={category}  "
              f"conf={n.get('extraction_confidence', 'implied')}")

        clean_nodes.append({
            "node_id": node_id,
            "event_type": event_type,
            "category": category,
            "description": n.get("description", ""),
            "date": n.get("date"),
            "date_confidence": n.get("date_confidence", "undated"),
            "source_citation": n.get("source_citation", ""),
            "source_excerpt": n.get("source_excerpt", ""),
            "extraction_confidence": n.get("extraction_confidence", "implied"),
        })

    clean_edges = []
    for e in raw_edges:
        src, tgt = e.get("source_node_id"), e.get("target_node_id")
        if src not in valid_node_ids or tgt not in valid_node_ids:
            print(f"     ✗ EDGE {e.get('edge_id')}: DROPPED — references unknown node ({src} → {tgt})")
            notes.append(f"Edge {e.get('edge_id')}: dropped -- references unknown node_id ({src} -> {tgt})")
            continue
        edge_id = e.get("edge_id") or f"e{len(clean_edges)+1}"
        rel = e.get("relationship", "contributed_to")
        strength = e.get("causal_strength", "alleged_only")
        print(f"     ✓ EDGE {edge_id:>8}  {src} ──[{rel}]──▶ {tgt}  strength={strength}")
        clean_edges.append({
            "edge_id": edge_id,
            "source_node_id": src,
            "target_node_id": tgt,
            "relationship": rel,
            "causal_strength": strength,
            "source_citation": e.get("source_citation", ""),
        })

    print(f"  ── validation complete: {len(clean_nodes)} nodes kept, {len(clean_edges)} edges kept, "
          f"{len(raw_nodes) - len(clean_nodes)} nodes dropped, {len(raw_edges) - len(clean_edges)} edges dropped")

    case_meta_extracted = extracted.get("case_metadata", {}) or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": str(case_id),
        "taxonomy_version": TAXONOMY_VERSION,
        "case_metadata": {
            "case_name": case_name,
            "court": court,
            "date_filed": date_filed,
            "project_type": case_meta_extracted.get("project_type"),
            "government_contract_number": case_meta_extracted.get("government_contract_number"),
        },
        "nodes": clean_nodes,
        "edges": clean_edges,
        "extraction_notes": notes,
    }


# ---------------------------------------------------------------------------
# STORAGE -- SQLite for real SQL queryability
# ---------------------------------------------------------------------------

def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY,
            case_name TEXT,
            court TEXT,
            date_filed TEXT,
            project_type TEXT,
            government_contract_number TEXT,
            schema_version TEXT,
            taxonomy_version TEXT,
            extracted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS nodes (
            node_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            event_type TEXT,
            category TEXT,
            description TEXT,
            date TEXT,
            date_confidence TEXT,
            source_citation TEXT,
            source_excerpt TEXT,
            extraction_confidence TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id),
            UNIQUE(case_id, node_id)
        );

        CREATE TABLE IF NOT EXISTS edges (
            edge_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            edge_id TEXT NOT NULL,
            source_node_id TEXT,
            target_node_id TEXT,
            relationship TEXT,
            causal_strength TEXT,
            source_citation TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id),
            UNIQUE(case_id, edge_id)
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_event_type ON nodes(event_type);
        CREATE INDEX IF NOT EXISTS idx_nodes_category ON nodes(category);
        CREATE INDEX IF NOT EXISTS idx_edges_relationship ON edges(relationship);
    """)
    conn.commit()
    return conn


def store_dag(conn, dag_record):
    meta = dag_record["case_metadata"]
    print(f"  ── storing to DB: case_id={dag_record['case_id']}")
    conn.execute("""
        INSERT OR REPLACE INTO cases
            (case_id, case_name, court, date_filed, project_type,
             government_contract_number, schema_version, taxonomy_version, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        dag_record["case_id"], meta["case_name"], meta["court"], meta["date_filed"],
        meta.get("project_type"), meta.get("government_contract_number"),
        dag_record["schema_version"], dag_record["taxonomy_version"],
        datetime.now(timezone.utc).isoformat(),
    ))
    print(f"     ✓ case row upserted")

    conn.execute("DELETE FROM nodes WHERE case_id = ?", (dag_record["case_id"],))
    conn.execute("DELETE FROM edges WHERE case_id = ?", (dag_record["case_id"],))
    print(f"     ✓ cleared old nodes/edges for this case")

    for n in dag_record["nodes"]:
        conn.execute("""
            INSERT INTO nodes
                (case_id, node_id, event_type, category, description, date,
                 date_confidence, source_citation, source_excerpt, extraction_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            dag_record["case_id"], n["node_id"], n["event_type"], n["category"],
            n["description"], n["date"], n["date_confidence"], n["source_citation"],
            n["source_excerpt"], n["extraction_confidence"],
        ))
        print(f"     → DB node {n['node_id']}  [{n['event_type']}]")

    for e in dag_record["edges"]:
        conn.execute("""
            INSERT INTO edges
                (case_id, edge_id, source_node_id, target_node_id,
                 relationship, causal_strength, source_citation)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            dag_record["case_id"], e["edge_id"], e["source_node_id"], e["target_node_id"],
            e["relationship"], e["causal_strength"], e["source_citation"],
        ))
        print(f"     → DB edge {e['edge_id']}  {e['source_node_id']} ──▶ {e['target_node_id']}")

    conn.commit()
    print(f"  ── DB commit complete: {len(dag_record['nodes'])} nodes, {len(dag_record['edges'])} edges written")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def should_extract(record):
    score = record.get("llm_score", {})
    if REQUIRE_IS_DELAY_CAUSATION_CASE and score.get("is_delay_causation_case") is not True:
        return False, "not flagged as delay causation case"
    if (score.get("relevance_score") or 0) < MIN_RELEVANCE_SCORE:
        return False, "relevance_score below threshold"
    if (score.get("richness_score") or 0) < MIN_RICHNESS_SCORE:
        return False, "richness_score below threshold"
    return True, ""


def main():
    conn = init_db()
    case_files = sorted(glob.glob(str(CASES_DIR / "*.json")))
    if not case_files:
        raise SystemExit(f"No case files found in {CASES_DIR}/")

    print(f"Found {len(case_files)} case files. Taxonomy version: {TAXONOMY_VERSION}\n")

    processed, skipped, failed = 0, 0, 0

    for path in case_files:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)

        case_id = str(record.get("cluster_id"))
        case_name = record.get("case_name", "unknown")
        court = record.get("court", "unknown")
        date_filed = record.get("date_filed", "")

        dag_out_path = DAGS_DIR / f"{case_id}.json"
        if dag_out_path.exists():
            print(f"skip (already extracted): {case_name}")
            skipped += 1
            continue

        ok, reason = should_extract(record)
        if not ok:
            print(f"skip ({reason}): {case_name}")
            skipped += 1
            continue

        full_text = record.get("full_text", "")
        score = record.get("llm_score", {})
        print(f"\n{'='*80}")
        print(f"EXTRACTING: {case_name}")
        print(f"  court:      {court}")
        print(f"  date_filed: {date_filed}")
        print(f"  case_id:    {case_id}")
        print(f"  full_text:  {len(full_text)} chars")
        print(f"  llm_score:  relevance={score.get('relevance_score', '?')} "
              f"richness={score.get('richness_score', '?')} "
              f"is_delay={score.get('is_delay_causation_case', '?')}")
        print(f"{'='*80}")

        content = get_priority_content(full_text)
        print(f"  priority content: {len(content)} chars (from {len(full_text)} raw)")
        chunks = chunk_content(content)
        print(f"  chunks: {len(chunks)} (limit {CHUNK_CHAR_LIMIT} chars/chunk)")
        for ci, ch in enumerate(chunks, 1):
            print(f"    chunk {ci}: {len(ch)} chars")

        raw_extraction = extract_dag_chunked(case_name, court, chunks)

        print(f"\n  ── raw extraction totals: {len(raw_extraction['nodes'])} nodes, "
              f"{len(raw_extraction['edges'])} edges, "
              f"{len(raw_extraction.get('extraction_notes', []))} notes")

        if not raw_extraction["nodes"] and any("extraction failed" in n for n in raw_extraction["extraction_notes"]):
            print(f"  ✗ ALL PARTS FAILED for {case_name}, skipping")
            failed += 1
            continue

        dag_record = validate_and_normalize(raw_extraction, case_id, case_name, court, date_filed)

        with open(dag_out_path, "w", encoding="utf-8") as f:
            json.dump(dag_record, f, indent=2, ensure_ascii=False)
        print(f"  ── wrote DAG JSON: {dag_out_path}")

        store_dag(conn, dag_record)

        print(f"\n  ══ DONE: {len(dag_record['nodes'])} nodes, {len(dag_record['edges'])} edges"
              f"{' [notes: ' + str(len(dag_record['extraction_notes'])) + ']' if dag_record['extraction_notes'] else ''}")
        if dag_record['extraction_notes']:
            for ni, note in enumerate(dag_record['extraction_notes'], 1):
                print(f"     note {ni}: {note[:200]}")
        processed += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    print(f"\nDone. Extracted: {processed}, Skipped: {skipped}, Failed: {failed}")
    print(f"DB: {DB_PATH}  |  Per-case files: {DAGS_DIR}/")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# EXAMPLE QUERIES once you have data (run with: sqlite3 forensic_dags.db)
# ---------------------------------------------------------------------------
#
# -- Most common event types across the whole case library:
#   SELECT event_type, COUNT(*) FROM nodes GROUP BY event_type ORDER BY 2 DESC;
#
# -- All nodes of a specific type, with source citations, for manual review:
#   SELECT case_id, description, source_citation FROM nodes WHERE event_type = 'differing_site_conditions';
#
# -- Cross-case edge frequency (which causal patterns recur most):
#   SELECT n1.event_type AS from_type, n2.event_type AS to_type, COUNT(*) AS freq
#   FROM edges e
#   JOIN nodes n1 ON e.case_id = n1.case_id AND e.source_node_id = n1.node_id
#   JOIN nodes n2 ON e.case_id = n2.case_id AND e.target_node_id = n2.node_id
#   GROUP BY from_type, to_type ORDER BY freq DESC;
#
# -- Contested causal claims (useful negative-example data):
#   SELECT case_id, description FROM nodes WHERE extraction_confidence = 'contested';
#
# -- Cases with unclassified nodes (candidates for taxonomy expansion):
#   SELECT DISTINCT case_id FROM nodes WHERE event_type = 'unclassified';