#!/usr/bin/env python3
"""
LLM-based entity resolution using Claude.

Sends two data files (JSONL or CSV) to Claude to perform entity resolution
in a manner similar to Senzing, producing compatible output format.
Data is always sent to the LLM in CSV format to minimize token usage.

To keep output within token limits, the LLM only returns merged entities
and relationships. Singleton entities (unmatched records) are generated
programmatically from the input.

For large datasets that exceed API rate limits, records are automatically
batched into chunks, processed sequentially with rate-limit-aware retries,
and then consolidated with a cross-batch merge pass.

Usage:
    python llm_er.py data/equifax-small.csv data/npi-small.csv
    python llm_er.py data/equifax-small.jsonl data/npi-small.jsonl
    python llm_er.py data/equifax-small.csv data/npi-small.jsonl -o data/llm_results.jsonl
"""

import argparse
import csv
import io
import json
import os
import sys
import time

import anthropic
import tiktoken
from dotenv import load_dotenv

# Claude Opus 4.6 pricing (per million tokens)
INPUT_COST_PER_MTOK = 5.00
OUTPUT_COST_PER_MTOK = 25.00

# Rate limit budget: stay under the org's input token/min limit with margin
DEFAULT_TOKEN_BUDGET = 25000

SYSTEM_PROMPT = """\
You are an expert entity resolution (ER) engine. Your task is to analyze records \
from multiple data sources and determine which records refer to the same real-world \
entity (person or organization), exactly as a dedicated ER system like Senzing would.

## Entity Resolution Process

1. **Record Ingestion**: Accept all source records with their features (names, \
addresses, phone numbers, emails, identifiers, employers, gender, etc.).

2. **Feature Comparison & Matching**: Compare every record against every other \
record looking for evidence they refer to the same entity. Consider:
   - **Names**: Account for variations — nicknames, initials, prefixes/suffixes, \
hyphenation, maiden names. "TERRY LYNN MILLER-NEWCOMB" and "TERRY MILLER" may \
be the same person.
   - **Addresses**: Normalize and compare street, city, state, zip. Same address \
is strong corroboration.
   - **Phone numbers**: Exact match is strong evidence.
   - **Email addresses**: Exact match is strong evidence.
   - **Identifiers**: NPI numbers, license numbers, SSNs, EFX_IDs — exact match \
is very strong evidence.
   - **Employers**: Same employer + same name is corroborating evidence.
   - **Gender**: Use as a negative signal — conflicting gender weakens a match.
   - **Cross-source matching**: Matches ACROSS data sources (e.g., EQUIFAX matching \
NPI-PROVIDERS) are especially valuable.

3. **Match Keys**: For each record merged into an entity, identify WHAT features \
drove the match using these codes:
   - `+NAME` — names match
   - `+ADDRESS` — addresses match
   - `+PHONE` — phone numbers match
   - `+EMAIL` — email addresses match
   - `+DOB` — dates of birth match
   - `+EMPLOYER` — employer matches
   - `+NPI` / `+LICENSE` / `+SSN` / `+ID` — identifiers match
   - Combine multiple: `+NAME+ADDRESS+PHONE`

4. **ER Rule Codes**: Assign a rule code indicating match strength:
   - `SAME_A1` — Strong name + strong identifier match
   - `SAME_A2` — Strong name + address match
   - `SAME_A3` — Strong name + phone or email match
   - `SAME_B1` — Name + multiple corroborating features
   - `SAME_B2` — Name + single corroborating feature
   - `SAME_B3` — Weak name match with strong corroboration

5. **Related Entities**: Identify entities that are NOT the same but are related:
   - `PM` (Possibly Same) — Could be the same entity, insufficient evidence to merge
   - `AM` (Ambiguous Match) — Conflicting information prevents merge
   - `DR` (Disclosed Relation) — Explicit relationship (e.g., REL_POINTER links)
   - `PR` (Possible Relation) — Shared address, employer, or other indirect link

## CRITICAL: Output Format

You must return a JSON object with exactly two keys: "MERGED_ENTITIES" and \
"RELATIONSHIPS". Do NOT include singleton/unmatched records — those will be \
generated automatically. Only output entities where 2+ records resolved together.

```json
{
  "MERGED_ENTITIES": [
    {
      "ENTITY_NAME": "TERRY LYNN MILLER-NEWCOMB",
      "RECORDS": [
        {
          "DATA_SOURCE": "NPI-PROVIDERS",
          "RECORD_ID": "1558549139",
          "MATCH_KEY": "",
          "ERRULE_CODE": ""
        },
        {
          "DATA_SOURCE": "EQUIFAX",
          "RECORD_ID": "44123-CONTACT",
          "MATCH_KEY": "+NAME+ADDRESS",
          "ERRULE_CODE": "SAME_A2"
        }
      ]
    }
  ],
  "RELATIONSHIPS": [
    {
      "ENTITY_A_RECORD_IDS": ["1558549139"],
      "ENTITY_B_RECORD_IDS": ["1639903545"],
      "MATCH_LEVEL_CODE": "PR",
      "MATCH_KEY": "+ADDRESS"
    }
  ]
}
```

## Rules

- **MERGED_ENTITIES**: Only entities with 2+ records. Do NOT include singletons.
- The FIRST record in each entity's RECORDS array is the anchor — it gets empty \
MATCH_KEY and ERRULE_CODE strings.
- Subsequent records explain why they matched the anchor via MATCH_KEY and ERRULE_CODE.
- A record can only appear in ONE merged entity.
- **RELATIONSHIPS**: Pairs of entities (merged or singleton) that are related but \
not the same. Reference them by one or more RECORD_IDs from each entity.
- ENTITY_NAME is the best/most complete name for the entity.
- Return ONLY the JSON object. No explanations, no markdown fences, no extra text.
- If there are NO merges at all, return: {"MERGED_ENTITIES": [], "RELATIONSHIPS": []}

## ABSOLUTE REQUIREMENT
Your response must contain ONLY the JSON object — nothing else. No analysis, no \
explanation, no preamble, no summary, no commentary. Start your response with the \
opening brace `{` and end with the closing brace `}`. Any text outside the JSON \
will cause a system failure.
"""

CONSOLIDATION_SYSTEM_PROMPT = """\
You are an expert entity resolution (ER) engine. You are performing a CONSOLIDATION \
pass: checking whether any merged entities from separate batches actually refer to \
the same real-world entity and should be combined.

You will receive a list of previously merged entities, each with an ID and their \
constituent records. Determine if any of these entities should be merged together.

## Output Format

Return a JSON object with:
- "MERGES": array of arrays — each inner array lists the entity IDs that should be \
combined into a single entity. Only include groups where 2+ entities should merge.
- "RELATIONSHIPS": array of cross-entity relationships (same format as before).

Example:
```json
{
  "MERGES": [[1, 5], [3, 7, 12]],
  "RELATIONSHIPS": [
    {
      "ENTITY_A_IDS": [1],
      "ENTITY_B_IDS": [4],
      "MATCH_LEVEL_CODE": "PM",
      "MATCH_KEY": "+NAME"
    }
  ]
}
```

If no cross-batch merges are needed: {"MERGES": [], "RELATIONSHIPS": []}
Return ONLY the JSON object. No explanations, no markdown fences, no extra text.
"""

# tiktoken encoder (cached)
_encoder = None


def get_encoder():
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def estimate_tokens(text):
    """Fast local token estimate using tiktoken."""
    return len(get_encoder().encode(text))


def load_records(file_path):
    """Load records from JSONL or CSV and return list of dicts."""
    records = []
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".csv":
        with open(file_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Drop empty-string values (sparse CSV columns)
                records.append({k: v for k, v in row.items() if v})
    else:
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def get_record_name(rec):
    """Extract the best name from a record for use as ENTITY_NAME."""
    for first_key, last_key in [
        ("PRIMARY_NAME_FIRST", "PRIMARY_NAME_LAST"),
        ("NAME_FIRST", "NAME_LAST"),
    ]:
        if first_key in rec and last_key in rec:
            parts = []
            if rec.get("PRIMARY_NAME_PREFIX") or rec.get("NAME_PREFIX"):
                parts.append(rec.get("PRIMARY_NAME_PREFIX", rec.get("NAME_PREFIX", "")))
            parts.append(rec[first_key])
            if rec.get("PRIMARY_NAME_MIDDLE") or rec.get("NAME_MIDDLE"):
                parts.append(rec.get("PRIMARY_NAME_MIDDLE", rec.get("NAME_MIDDLE", "")))
            parts.append(rec[last_key])
            return " ".join(p for p in parts if p)

    if "NAME_FULL" in rec:
        return rec["NAME_FULL"]

    for feat in rec.get("FEATURES", []):
        if "NAME_FULL" in feat:
            return feat["NAME_FULL"]
        if "NAME_FIRST" in feat and "NAME_LAST" in feat:
            return f"{feat['NAME_FIRST']} {feat['NAME_LAST']}"

    return rec.get("RECORD_ID", "UNKNOWN")


def records_to_csv(records):
    """Serialize a list of record dicts as a CSV string (header + rows)."""
    if not records:
        return ""
    # Collect all keys in stable order across all records
    columns = list(dict.fromkeys(k for r in records for k in r))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()


def build_batch_message(batch_records):
    """Build user message for a single batch of records.

    Data is sent in CSV format (one table per source) to minimize token usage.
    """
    records_by_source = {}
    for rec in batch_records:
        ds = rec.get("DATA_SOURCE", "UNKNOWN")
        records_by_source.setdefault(ds, []).append(rec)

    parts = [
        "Perform entity resolution on the following records from multiple data sources. "
        "The data is provided in CSV format (one table per source). "
        "Return ONLY merged entities (2+ records that resolve to the same entity) and "
        "relationships. Do NOT include singleton/unmatched records in your output.\n"
    ]
    for source, records in records_by_source.items():
        parts.append(f"\n--- Data Source: {source} ({len(records)} records) ---\n")
        parts.append(records_to_csv(records))

    total = len(batch_records)
    parts.append(f"\n\nTotal records to resolve: {total}")
    parts.append(
        "Return the JSON object with MERGED_ENTITIES and RELATIONSHIPS. "
        "Only include entities where 2+ records matched. "
        "Singletons will be generated automatically — do NOT include them."
    )
    return "\n".join(parts)


def build_batches(all_records, token_budget):
    """
    Split records into batches that fit within the token budget.
    Each batch gets an interleaved mix of records from all data sources
    to maximize cross-source matching within a batch.
    """
    # Group by source
    by_source = {}
    for rec in all_records:
        ds = rec.get("DATA_SOURCE", "UNKNOWN")
        by_source.setdefault(ds, []).append(rec)

    # Interleave sources so each batch has a mix
    sources = list(by_source.keys())
    interleaved = []
    indices = {s: 0 for s in sources}
    while any(indices[s] < len(by_source[s]) for s in sources):
        for s in sources:
            if indices[s] < len(by_source[s]):
                interleaved.append(by_source[s][indices[s]])
                indices[s] += 1

    # Estimate system prompt tokens (constant overhead per batch)
    system_tokens = estimate_tokens(SYSTEM_PROMPT)
    # Overhead for the user message boilerplate + CSV headers
    boilerplate_tokens = 300

    # Estimate per-record tokens using CSV row size (values only, no keys)
    def csv_row_tokens(rec):
        return estimate_tokens(",".join(str(v) for v in rec.values()))

    batches = []
    current_batch = []
    current_tokens = system_tokens + boilerplate_tokens

    for rec in interleaved:
        rec_tokens = csv_row_tokens(rec)
        if current_batch and (current_tokens + rec_tokens) > token_budget:
            batches.append(current_batch)
            current_batch = [rec]
            current_tokens = system_tokens + boilerplate_tokens + rec_tokens
        else:
            current_batch.append(rec)
            current_tokens += rec_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def parse_response(text):
    """Parse the LLM response, extracting JSON even if surrounded by text."""
    text = text.strip()
    # Strip markdown fences
    if "```" in text:
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the first { and last } to extract JSON from surrounding text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("No JSON object found in response", text, 0)


def call_llm_with_retry(client, model, system, user_message, max_tokens,
                        max_retries=10):
    """Call the Claude API with streaming and exponential backoff for rate limits."""
    for attempt in range(max_retries):
        try:
            response_parts = []
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for text in stream.text_stream:
                    response_parts.append(text)
                result = stream.get_final_message()
            return "".join(response_parts), result
        except anthropic.RateLimitError as e:
            if attempt == max_retries - 1:
                raise
            # Extract retry-after from headers if available, else use backoff
            wait = min(2 ** attempt * 15, 120)
            print(f"  Rate limited. Waiting {wait}s before retry "
                  f"({attempt + 1}/{max_retries})...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < max_retries - 1:
                wait = min(2 ** attempt * 5, 60)
                print(f"  Server error ({e.status_code}). Waiting {wait}s "
                      f"before retry ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise


def process_single_batch(client, model, batch_records, batch_num, total_batches,
                         max_tokens):
    """Process a single batch of records and return the parsed LLM result plus usage."""
    print(f"\n  Batch {batch_num}/{total_batches}: "
          f"{len(batch_records)} records...", end="", flush=True)

    user_message = build_batch_message(batch_records)
    response_text, result = call_llm_with_retry(
        client, model, SYSTEM_PROMPT, user_message, max_tokens
    )

    if result.stop_reason == "max_tokens":
        print(f" TRUNCATED!", end="")

    input_tok = result.usage.input_tokens
    output_tok = result.usage.output_tokens

    try:
        llm_result = parse_response(response_text)
        n_merged = len(llm_result.get("MERGED_ENTITIES", []))
        n_rels = len(llm_result.get("RELATIONSHIPS", []))
        print(f" {n_merged} merges, {n_rels} rels "
              f"({input_tok:,}in/{output_tok:,}out)")
    except json.JSONDecodeError as e:
        preview = response_text[:200] if response_text else "(empty)"
        print(f" PARSE ERROR: {e}")
        print(f"    Response preview: {preview}")
        print(f"    Stop reason: {result.stop_reason}, "
              f"tokens: {input_tok:,}in/{output_tok:,}out")
        llm_result = {"MERGED_ENTITIES": [], "RELATIONSHIPS": []}

    return llm_result, input_tok, output_tok


def consolidate_cross_batch(client, model, batch_merges, max_tokens):
    """
    Run a consolidation pass: compare merged entities across batches
    to find additional merges.
    """
    # Collect all merged entities with a temporary ID
    all_merged = []
    temp_id = 1
    for batch_result in batch_merges:
        for m in batch_result.get("MERGED_ENTITIES", []):
            all_merged.append({"TEMP_ID": temp_id, **m})
            temp_id += 1

    if len(all_merged) < 2:
        return []  # Nothing to consolidate

    print(f"\n  Consolidation pass: comparing {len(all_merged)} merged entities "
          f"across batches...")

    # Build a compact summary for each merged entity
    parts = [
        "The following entities were identified in separate batches. "
        "Check if any should be merged together (same real-world entity "
        "appearing in different batches).\n"
    ]
    for m in all_merged:
        parts.append(f"\nEntity {m['TEMP_ID']}: {m.get('ENTITY_NAME', 'UNKNOWN')}")
        for rec in m.get("RECORDS", []):
            parts.append(f"  - {rec['DATA_SOURCE']}:{rec['RECORD_ID']}")

    parts.append(
        "\n\nReturn the JSON object with MERGES (groups of entity IDs to combine) "
        "and RELATIONSHIPS."
    )
    user_message = "\n".join(parts)

    msg_tokens = estimate_tokens(user_message)
    if msg_tokens > DEFAULT_TOKEN_BUDGET:
        print(f"  WARNING: Consolidation message ({msg_tokens:,} tokens) exceeds "
              f"budget. Skipping cross-batch consolidation.")
        return []

    response_text, result = call_llm_with_retry(
        client, model, CONSOLIDATION_SYSTEM_PROMPT, user_message, max_tokens
    )

    try:
        consol_result = parse_response(response_text)
    except json.JSONDecodeError:
        print("  WARNING: Could not parse consolidation response.")
        return []

    merge_groups = consol_result.get("MERGES", [])
    if merge_groups:
        print(f"  Found {len(merge_groups)} cross-batch merge group(s).")
    else:
        print("  No cross-batch merges found.")

    return merge_groups, all_merged, result.usage.input_tokens, result.usage.output_tokens


def apply_cross_batch_merges(batch_merges, merge_groups, all_merged_with_ids):
    """
    Apply consolidation merge groups: combine merged entities that span batches.
    Returns a single combined result dict.
    """
    # Build lookup: temp_id -> merged entity
    by_id = {m["TEMP_ID"]: m for m in all_merged_with_ids}

    # Track which temp IDs have been consumed by a cross-batch merge
    consumed = set()
    combined_merges = []

    for group in merge_groups:
        combined_records = []
        combined_name = ""
        for tid in group:
            if tid not in by_id:
                continue
            m = by_id[tid]
            consumed.add(tid)
            if not combined_name:
                combined_name = m.get("ENTITY_NAME", "")
            for rec in m.get("RECORDS", []):
                combined_records.append(rec)
        if len(combined_records) >= 2:
            # Set first record as anchor
            for i, rec in enumerate(combined_records):
                if i == 0:
                    rec["MATCH_KEY"] = ""
                    rec["ERRULE_CODE"] = ""
                elif not rec.get("MATCH_KEY"):
                    rec["MATCH_KEY"] = "+NAME"
                    rec["ERRULE_CODE"] = "SAME_B2"
            combined_merges.append({
                "ENTITY_NAME": combined_name,
                "RECORDS": combined_records,
            })

    # Keep unconsumed merged entities as-is
    for m in all_merged_with_ids:
        if m["TEMP_ID"] not in consumed:
            combined_merges.append({
                "ENTITY_NAME": m.get("ENTITY_NAME", ""),
                "RECORDS": m.get("RECORDS", []),
            })

    # Collect all relationships from all batches
    all_rels = []
    for batch_result in batch_merges:
        all_rels.extend(batch_result.get("RELATIONSHIPS", []))

    return {"MERGED_ENTITIES": combined_merges, "RELATIONSHIPS": all_rels}


def build_full_entity_list(llm_result, all_records):
    """
    Combine LLM-identified merges with programmatic singletons to produce
    the full Senzing-compatible entity list.
    """
    merged = llm_result.get("MERGED_ENTITIES", [])
    relationships = llm_result.get("RELATIONSHIPS", [])

    claimed = set()
    entities = []
    entity_id = 1
    record_to_entity = {}

    # Process merged entities, deduplicating records across merges
    duplicates = []
    for m in merged:
        raw_records = m.get("RECORDS", [])
        deduped_records = []
        for rec in raw_records:
            key = (rec["DATA_SOURCE"], rec["RECORD_ID"])
            if key in claimed:
                duplicates.append(
                    f"  {rec['DATA_SOURCE']}:{rec['RECORD_ID']} "
                    f"(already in entity {record_to_entity.get(rec['RECORD_ID'], '?')})"
                )
                continue
            deduped_records.append(rec)
            claimed.add(key)

        if not deduped_records:
            continue

        if len(deduped_records) == 1:
            key = (deduped_records[0]["DATA_SOURCE"], deduped_records[0]["RECORD_ID"])
            claimed.discard(key)
            continue

        entity = {
            "RESOLVED_ENTITY": {
                "ENTITY_ID": entity_id,
                "ENTITY_NAME": m.get("ENTITY_NAME", ""),
                "RECORD_COUNT": len(deduped_records),
                "RECORDS": deduped_records,
            },
            "RELATED_ENTITIES": [],
        }
        entities.append(entity)
        for rec in deduped_records:
            record_to_entity[rec["RECORD_ID"]] = entity_id
        entity_id += 1

    if duplicates:
        print(f"WARNING: LLM placed {len(duplicates)} record(s) in multiple entities. "
              f"Kept first occurrence only:")
        for d in duplicates[:10]:
            print(d)
        if len(duplicates) > 10:
            print(f"  ... and {len(duplicates) - 10} more")

    # Generate singleton entities for unclaimed records
    for rec in all_records:
        key = (rec.get("DATA_SOURCE", "UNKNOWN"), rec.get("RECORD_ID", ""))
        if key in claimed:
            continue
        entity = {
            "RESOLVED_ENTITY": {
                "ENTITY_ID": entity_id,
                "ENTITY_NAME": get_record_name(rec),
                "RECORD_COUNT": 1,
                "RECORDS": [
                    {
                        "DATA_SOURCE": rec.get("DATA_SOURCE", "UNKNOWN"),
                        "RECORD_ID": rec.get("RECORD_ID", ""),
                        "MATCH_KEY": "",
                        "ERRULE_CODE": "",
                    }
                ],
            },
            "RELATED_ENTITIES": [],
        }
        entities.append(entity)
        record_to_entity[rec.get("RECORD_ID", "")] = entity_id
        entity_id += 1

    # Map relationships onto entities
    for rel in relationships:
        a_ids = rel.get("ENTITY_A_RECORD_IDS", [])
        b_ids = rel.get("ENTITY_B_RECORD_IDS", [])
        match_level = rel.get("MATCH_LEVEL_CODE", "PM")
        match_key = rel.get("MATCH_KEY", "")

        ent_a = None
        ent_b = None
        for rid in a_ids:
            if rid in record_to_entity:
                ent_a = record_to_entity[rid]
                break
        for rid in b_ids:
            if rid in record_to_entity:
                ent_b = record_to_entity[rid]
                break

        if ent_a is not None and ent_b is not None and ent_a != ent_b:
            entities[ent_a - 1]["RELATED_ENTITIES"].append(
                {
                    "ENTITY_ID": ent_b,
                    "MATCH_LEVEL_CODE": match_level,
                    "MATCH_KEY": match_key,
                }
            )
            entities[ent_b - 1]["RELATED_ENTITIES"].append(
                {
                    "ENTITY_ID": ent_a,
                    "MATCH_LEVEL_CODE": match_level,
                    "MATCH_KEY": match_key,
                }
            )

    return entities


def format_duration(seconds):
    """Format seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.1f}s"


def main():
    parser = argparse.ArgumentParser(
        description="LLM-based entity resolution using Claude, producing Senzing-compatible output."
    )
    parser.add_argument("file1", help="Path to the first data file (JSONL or CSV)")
    parser.add_argument("file2", help="Path to the second data file (JSONL or CSV)")
    parser.add_argument(
        "--model",
        default="claude-opus-4-6",
        help="Claude model (default: claude-opus-4-6)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output file path (default: llm_er_output.jsonl in data/)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=16000,
        help="Maximum output tokens per batch (default: 16000)",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=DEFAULT_TOKEN_BUDGET,
        help=f"Max input tokens per batch (default: {DEFAULT_TOKEN_BUDGET}). "
             f"Set based on your API rate limit.",
    )
    args = parser.parse_args()

    # Load API key from .env (override=True ensures .env takes precedence over shell env)
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not found in .env file", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    for path in [args.file1, args.file2]:
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)

    # Load records
    print("Loading records...")
    records1 = load_records(args.file1)
    records2 = load_records(args.file2)
    all_records = records1 + records2

    records_by_source = {}
    for rec in all_records:
        ds = rec.get("DATA_SOURCE", "UNKNOWN")
        records_by_source.setdefault(ds, []).append(rec)

    total_records = len(all_records)
    print(f"  {os.path.basename(args.file1)}: {len(records1):,} records")
    print(f"  {os.path.basename(args.file2)}: {len(records2):,} records")
    print(
        f"  Total: {total_records:,} records across "
        f"{len(records_by_source)} data source(s)"
    )

    # Build batches
    batches = build_batches(all_records, args.token_budget)
    num_batches = len(batches)

    if num_batches == 1:
        print(f"\nAll records fit in a single batch.")
    else:
        print(f"\nSplit into {num_batches} batches "
              f"(token budget: {args.token_budget:,}/batch)")
        for i, batch in enumerate(batches, 1):
            sources = {}
            for rec in batch:
                ds = rec.get("DATA_SOURCE", "UNKNOWN")
                sources[ds] = sources.get(ds, 0) + 1
            src_str = ", ".join(f"{s}: {c}" for s, c in sorted(sources.items()))
            print(f"  Batch {i}: {len(batch)} records ({src_str})")

    # Estimate total cost
    total_input_est = 0
    for batch in batches:
        msg = build_batch_message(batch)
        total_input_est += estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(msg)

    est_merged = max(int(total_records * 0.10), 10)
    estimated_output_tokens = min(est_merged * 250, args.max_tokens * num_batches)

    input_cost = (total_input_est / 1_000_000) * INPUT_COST_PER_MTOK
    est_output_cost = (estimated_output_tokens / 1_000_000) * OUTPUT_COST_PER_MTOK
    est_total_cost = input_cost + est_output_cost

    print(f"\n{'=' * 60}")
    print("TOKEN & COST ESTIMATE")
    print(f"{'=' * 60}")
    print(f"  Model:                    {args.model}")
    print(f"  Batches:                  {num_batches:>12}")
    print(f"  Est. input tokens:        {total_input_est:>12,}")
    print(f"  Est. output tokens:       {estimated_output_tokens:>12,}")
    print(f"  Est. input cost:          ${input_cost:>11.4f}")
    print(f"  Est. output cost:         ${est_output_cost:>11.4f}")
    print(f"  Est. total cost:          ${est_total_cost:>11.4f}")
    if num_batches > 1:
        est_minutes = num_batches * 1.5
        print(f"  Est. time (with waits):   ~{est_minutes:.0f} minutes")
    print(f"{'=' * 60}")

    answer = input("\nProceed with sending data to the LLM? (yes/no): ").strip().lower()
    if answer not in ("yes", "y"):
        print("Aborted by user.")
        sys.exit(0)

    # ---- Timer starts HERE, after user confirmation ----
    start_time = time.time()

    total_actual_input = 0
    total_actual_output = 0

    if num_batches == 1:
        # Single batch — simple path
        print("\nSending data to Claude for entity resolution...")
        llm_result, inp_tok, out_tok = process_single_batch(
            client, args.model, batches[0], 1, 1, args.max_tokens
        )
        total_actual_input += inp_tok
        total_actual_output += out_tok
        combined_result = llm_result
    else:
        # Multi-batch processing
        print(f"\nProcessing {num_batches} batches...")
        batch_results = []

        for i, batch in enumerate(batches, 1):
            llm_result, inp_tok, out_tok = process_single_batch(
                client, args.model, batch, i, num_batches, args.max_tokens
            )
            batch_results.append(llm_result)
            total_actual_input += inp_tok
            total_actual_output += out_tok

            # Rate limit pause between batches
            if i < num_batches:
                wait = 65  # Just over 1 minute for token/min limits
                print(f"  Waiting {wait}s for rate limit cooldown...")
                time.sleep(wait)

        # Consolidation pass for cross-batch merges
        total_merges = sum(
            len(r.get("MERGED_ENTITIES", [])) for r in batch_results
        )
        if total_merges >= 2:
            consol = consolidate_cross_batch(
                client, args.model, batch_results, args.max_tokens
            )
            if consol and len(consol) == 4:
                merge_groups, all_merged, c_inp, c_out = consol
                total_actual_input += c_inp
                total_actual_output += c_out
                combined_result = apply_cross_batch_merges(
                    batch_results, merge_groups, all_merged
                )
            else:
                # No consolidation needed — just combine batch results
                all_merges = []
                all_rels = []
                for r in batch_results:
                    all_merges.extend(r.get("MERGED_ENTITIES", []))
                    all_rels.extend(r.get("RELATIONSHIPS", []))
                combined_result = {
                    "MERGED_ENTITIES": all_merges,
                    "RELATIONSHIPS": all_rels,
                }
        else:
            all_merges = []
            all_rels = []
            for r in batch_results:
                all_merges.extend(r.get("MERGED_ENTITIES", []))
                all_rels.extend(r.get("RELATIONSHIPS", []))
            combined_result = {
                "MERGED_ENTITIES": all_merges,
                "RELATIONSHIPS": all_rels,
            }

    elapsed = time.time() - start_time
    # ---- Timer ends ----

    # Build full entity list
    num_merges = len(combined_result.get("MERGED_ENTITIES", []))
    num_rels = len(combined_result.get("RELATIONSHIPS", []))
    print(f"\nTotal: {num_merges} merged entities, {num_rels} relationships.")
    print("Building full entity list with singletons...")

    entities = build_full_entity_list(combined_result, all_records)

    # Write output
    output_path = args.output or os.path.join(
        os.path.dirname(args.file1) or ".", "llm_er_output.jsonl"
    )
    with open(output_path, "w") as f:
        for entity in entities:
            f.write(json.dumps(entity) + "\n")

    # Compute summary stats
    total_entities = len(entities)
    multi_record = sum(
        1
        for e in entities
        if e.get("RESOLVED_ENTITY", {}).get("RECORD_COUNT", 1) > 1
    )
    records_accounted = sum(
        e.get("RESOLVED_ENTITY", {}).get("RECORD_COUNT", 0) for e in entities
    )
    related_pairs = sum(len(e.get("RELATED_ENTITIES", [])) for e in entities)

    actual_input_cost = (total_actual_input / 1_000_000) * INPUT_COST_PER_MTOK
    actual_output_cost = (total_actual_output / 1_000_000) * OUTPUT_COST_PER_MTOK
    actual_total_cost = actual_input_cost + actual_output_cost

    print(f"\n{'=' * 60}")
    print("ENTITY RESOLUTION RESULTS")
    print(f"{'=' * 60}")
    print(f"  Input records:            {total_records:>10,}")
    print(f"  Resolved entities:        {total_entities:>10,}")
    print(f"  Multi-record entities:    {multi_record:>10,}")
    print(f"  Records accounted for:    {records_accounted:>10,}")
    print(f"  Related entity pairs:     {related_pairs:>10,}")
    if total_records > 0:
        merge_rate = 100.0 * (total_records - total_entities) / total_records
        print(f"  Merge rate:               {merge_rate:>9.1f}%")

    print(f"\n{'-' * 60}")
    print("PERFORMANCE & COST")
    print(f"{'-' * 60}")
    print(f"  Batches processed:        {num_batches:>10}")
    print(f"  Wall clock time:          {format_duration(elapsed):>14}")
    print(f"  Input tokens (actual):    {total_actual_input:>12,}")
    print(f"  Output tokens (actual):   {total_actual_output:>12,}")
    print(f"  Input cost:               ${actual_input_cost:>11.4f}")
    print(f"  Output cost:              ${actual_output_cost:>11.4f}")
    print(f"  TOTAL COST:               ${actual_total_cost:>11.4f}")
    print(f"{'=' * 60}")

    print(f"\nResults written to: {output_path}")


if __name__ == "__main__":
    main()
