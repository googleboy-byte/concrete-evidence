"""
score_cases_nvidia_llm.py

Runs an LLM relevance/quality pass over the case JSON files saved by
fetch_courtlistener_cases.py, using NVIDIA's free NIM API via plain HTTP
requests (non-streaming).

Scores each case for:
  - is_delay_causation_case: bool  -- is this substantively about construction
        delay causation (not a false positive like fee disputes, thin
        procedural affirmances, etc.)
  - relevance_score: 0-10
  - richness_score: 0-10  -- how much actual causal-chain material is present
        (dated events, findings of fact, CPM/schedule analysis) vs. thin/
        conclusory text
  - reasoning: short explanation
  - red_flags: list of specific concerns

Writes results back into each JSON file under a new "llm_score" key.

Setup:
    1. Create a free account at https://build.nvidia.com
    2. Generate an API key (starts with "nvapi-")
    3. pip install requests python-dotenv
    4. Add to your .env file:
         NIM_API_KEY=nvapi-your_key_here

Usage:
    python score_cases_nvidia_llm.py
"""

import os
import re
import json
import time
import glob
import requests
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

load_dotenv()

API_KEY = os.environ.get("NIM_API_KEY", "")
if not API_KEY:
    raise SystemExit(
        "No API key found. Add to .env:\n  NIM_API_KEY=nvapi-your_key_here"
    )

INVOKE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
STREAM = False

MODEL = "google/gemma-4-31b-it"

# If MODEL keeps hitting capacity/rate errors, the script falls back to these
# in order rather than aborting the whole run. Verify these are still listed
# as free at build.nvidia.com/models before relying on them.
FALLBACK_MODELS = [
    "meta/llama-3.1-70b-instruct",
]

CASES_DIR = Path("courtlistener_cases")

REQUEST_DELAY_SECONDS = 2.0
MAX_TEXT_CHARS = 12000
MAX_RETRIES = 4

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "text/event-stream" if STREAM else "application/json",
}

# ---------------------------------------------------------------------------
# TEXT CLEANUP
# ---------------------------------------------------------------------------

def strip_markup(text):
    """Remove XML/HTML tags left over from CourtListener's source formatting."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_excerpt(full_text, max_chars=MAX_TEXT_CHARS):
    """
    Take a cleaned excerpt of the opinion, skipping past likely caption/
    appearances boilerplate at the very start.
    """
    cleaned = strip_markup(full_text)
    if len(cleaned) <= max_chars:
        return cleaned
    start = min(800, len(cleaned) // 10)
    return cleaned[start:start + max_chars]


# ---------------------------------------------------------------------------
# LLM SCORING
# ---------------------------------------------------------------------------

SCORING_PROMPT_TEMPLATE = """You are screening US case law for a research project building forensic causal-chain models of construction project delays (why delays happened, not just that they happened).

Case name: {case_name}
Court: {court}

Below is an excerpt of the opinion text. Evaluate it and respond with ONLY a JSON object (no markdown fences, no preamble, no text before or after) matching this exact schema:

{{
  "is_delay_causation_case": true or false,
  "relevance_score": integer 0-10,
  "richness_score": integer 0-10,
  "reasoning": "one or two sentence explanation",
  "red_flags": ["short phrase", "short phrase"]
}}

Scoring guidance:
- is_delay_causation_case: true only if the case substantively concerns WHY a construction project was delayed (e.g. differing site conditions, critical path impacts, change orders causing schedule slip). False if it's really about something else (attorney's fees, arbitration procedure, contract formation, unrelated payment disputes) even if construction/delay terms appear incidentally.
- relevance_score: how directly useful this case is for building a delay-causation dataset.
- richness_score: how much actual causal-chain material is present -- dated events, findings of fact, expert schedule analysis, explicit cause-and-effect statements -- versus being thin, conclusory, or purely procedural.
- red_flags: call out anything like "thin appellate affirmance", "off-topic subject matter", "procedural only, no fact-finding", "OCR/text quality issues", etc. Empty list if none.

Opinion excerpt:
---
{excerpt}
---
"""


def score_case(case_name, court, excerpt):
    prompt = SCORING_PROMPT_TEMPLATE.format(
        case_name=case_name, court=court, excerpt=excerpt
    )

    for model_name in [MODEL] + FALLBACK_MODELS:
        result = _try_score_with_model(model_name, prompt)
        if result is not None:
            score, thinking = result
            score["_model_used"] = model_name
            return score, thinking
        print(f"    giving up on {model_name} after retries, trying next fallback...")

    raise RuntimeError("Exhausted all models (primary + fallbacks) against NVIDIA NIM API")


def _try_score_with_model(model_name, prompt):
    """
    Attempts scoring with a single model, retrying on capacity/rate errors.
    Returns (score_dict, thinking_str) on success, or None if this model
    should be abandoned in favor of a fallback.
    """
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16384,
        "temperature": 1.00,
        "top_p": 0.95,
        "stream": STREAM,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                INVOKE_URL, headers=HEADERS, json=payload, stream=STREAM, timeout=90
            )

            if response.status_code != 200:
                err_str = f"{response.status_code}: {response.text[:300]}"
                retryable_signals = [
                    "429", "rate", "resourceexhausted", "resource_exhausted",
                    "limit reached", "503", "overloaded", "capacity",
                ]
                if any(sig in err_str.lower() for sig in retryable_signals):
                    wait = 30 * (attempt + 1)
                    print(f"    [{model_name}] server capacity/rate issue -- backing off {wait}s ({err_str[:150]})")
                    time.sleep(wait)
                    continue
                # non-retryable error (e.g. bad request, auth failure, unknown model)
                print(f"    [{model_name}] request failed: {err_str}")
                return None

            data = response.json()
            message = data["choices"][0]["message"]
            raw_content = (message.get("content") or "").strip()
            raw_thinking = (message.get("reasoning_content") or "").strip()

            # strip markdown fences if the model added them despite instructions
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_content)
            cleaned = re.sub(r"\s*```$", "", cleaned)

            try:
                parsed = json.loads(cleaned)
                return parsed, raw_thinking
            except json.JSONDecodeError:
                print(f"    ! could not parse model output as JSON, got: {cleaned[:200]}")
                return {
                    "is_delay_causation_case": None,
                    "relevance_score": None,
                    "richness_score": None,
                    "reasoning": "PARSE_ERROR",
                    "red_flags": ["llm_output_not_valid_json"],
                    "raw_output": cleaned,
                }, raw_thinking

        except requests.RequestException as e:
            wait = 30 * (attempt + 1)
            print(f"    [{model_name}] network error -- backing off {wait}s ({e})")
            time.sleep(wait)
            continue

    return None  # exhausted retries on this model -- caller will try fallback


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    case_files = sorted(glob.glob(str(CASES_DIR / "*.json")))
    if not case_files:
        raise SystemExit(f"No case files found in {CASES_DIR}/")

    print(f"Scoring {len(case_files)} cases with model: {MODEL}\n")

    for path in case_files:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)

        if "llm_score" in record:
            print(f"skip (already scored): {record.get('case_name')}")
            continue

        case_name = record.get("case_name", "unknown")
        court = record.get("court", "unknown")
        excerpt = get_excerpt(record.get("full_text", ""))

        print(f"scoring: {case_name} ({court})")

        try:
            score, thinking = score_case(case_name, court, excerpt)
        except RuntimeError as e:
            print(f"  ! {e} -- stopping run")
            break

        record["llm_score"] = score
        record["llm_score_thinking"] = thinking  # likely empty for non-reasoning models like Gemma
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)

        flag_str = f" [{', '.join(score.get('red_flags', []))}]" if score.get("red_flags") else ""
        print(
            f"  -> relevant={score.get('is_delay_causation_case')} "
            f"relevance={score.get('relevance_score')} "
            f"richness={score.get('richness_score')}{flag_str}"
        )

        time.sleep(REQUEST_DELAY_SECONDS)

    print("\nDone.")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# NOTES
# ---------------------------------------------------------------------------
# - Gemma is not a dedicated reasoning/thinking model, so "enable_thinking"
#   is likely ignored and "reasoning_content" will probably come back empty.
#   That's expected -- you'll still get the JSON score, just no thinking
#   trace to audit. If you want visible reasoning, use a Nemotron/DeepSeek
#   reasoning model instead.
# - Verify "google/gemma-4-31b-it" is still listed as a free endpoint at
#   build.nvidia.com/models before a big run -- free model availability
#   changes over time.
# - This script skips any case that already has an "llm_score" key, so it's
#   safe to re-run after interruption without re-scoring everything.