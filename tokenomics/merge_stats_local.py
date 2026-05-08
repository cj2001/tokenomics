#!/usr/bin/env python3
"""
Entity resolution overlap report from Senzing (local SDK).

Exports all resolved entities via local senzing-core and computes:
  - Per-data-source record counts, entity counts, and compression ratios
  - Cross-source overlap (shared entities between source pairs)
  - Entity size distribution (singletons, pairs, 3-5, 6-10, 11-50, 51+)
  - Relationship summary by match type (Possibly Same, Ambiguous, etc.)
  - Match key frequency (what features drove resolution)
  - ER rule usage frequency

Usage:
    source ~/tokenomics/senzing/setupEnv
    python merge_stats_local.py
    python merge_stats_local.py --file data/some-results.jsonl
"""

import argparse
import configparser
import json
import os
import sys
import time
from collections import Counter, defaultdict

from senzing import SzEngineFlags
from senzing_core import SzAbstractFactoryCore


def get_settings():
    """Get Senzing engine config as JSON string from env var or ini file."""
    settings = os.environ.get("SENZING_ENGINE_CONFIGURATION_JSON")
    if settings:
        return settings

    ini_path = os.environ.get("SENZING_CONFIG_FILE")
    if ini_path and os.path.isfile(ini_path):
        cfgp = configparser.ConfigParser()
        cfgp.optionxform = str
        cfgp.read(ini_path)
        settings = {section: dict(cfgp.items(section)) for section in cfgp.sections()}
        return json.dumps(settings)

    print("ERROR: Neither SENZING_ENGINE_CONFIGURATION_JSON nor SENZING_CONFIG_FILE is set.")
    print("Run: source ~/tokenomics/senzing/setupEnv")
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate entity resolution overlap report from Senzing (local SDK) or a JSONL file."
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to a JSONL file of resolved entities (e.g., LLM ER output). "
             "If provided, reads from the file instead of Senzing.",
    )
    return parser.parse_args()


def export_entities(sz_engine):
    """Export all entities with relationships and yield parsed JSON dicts."""
    flags = SzEngineFlags.SZ_EXPORT_DEFAULT_FLAGS
    handle = sz_engine.export_json_entity_report(flags)
    try:
        while True:
            row = sz_engine.fetch_next(handle)
            if not row:
                break
            row = row.strip()
            if not row:
                continue
            try:
                entity = json.loads(row)
            except json.JSONDecodeError:
                continue
            if "RESOLVED_ENTITY" in entity:
                yield entity
    finally:
        sz_engine.close_export_report(handle)


def entities_from_file(file_path):
    """Read resolved entities from a JSONL file (e.g., LLM ER output)."""
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entity = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "RESOLVED_ENTITY" in entity:
                yield entity


def build_stats(entity_iter):
    """Iterate over exported entities and accumulate statistics."""
    ds_record_count = Counter()
    ds_entity_count = Counter()
    cross_source_entities = defaultdict(set)
    entity_sizes = []
    relation_type_counts = Counter()
    total_relations = 0
    match_key_counter = Counter()
    errule_counter = Counter()
    entity_count = 0

    for entity in entity_iter:
        resolved = entity.get("RESOLVED_ENTITY", {})
        entity_id = resolved.get("ENTITY_ID")
        records = resolved.get("RECORDS", [])
        record_count = len(records)

        entity_count += 1
        entity_sizes.append(record_count)

        sources_in_entity = set()
        for rec in records:
            ds = rec.get("DATA_SOURCE", "UNKNOWN")
            ds_record_count[ds] += 1
            sources_in_entity.add(ds)

            mk = rec.get("MATCH_KEY", "")
            er = rec.get("ERRULE_CODE", "")
            if mk:
                match_key_counter[mk] += 1
            if er:
                errule_counter[er] += 1

        for ds in sources_in_entity:
            ds_entity_count[ds] += 1

        sources_list = sorted(sources_in_entity)
        for i in range(len(sources_list)):
            for j in range(i + 1, len(sources_list)):
                pair = (sources_list[i], sources_list[j])
                cross_source_entities[pair].add(entity_id)

        for rel_section in entity.get("RELATED_ENTITIES", []):
            mt = rel_section.get("MATCH_LEVEL_CODE", rel_section.get("MATCH_TYPE", ""))
            relation_type_counts[mt] += 1
            total_relations += 1

    return {
        "entity_count": entity_count,
        "ds_record_count": ds_record_count,
        "ds_entity_count": ds_entity_count,
        "cross_source_entities": cross_source_entities,
        "entity_sizes": entity_sizes,
        "relation_type_counts": relation_type_counts,
        "total_relations": total_relations,
        "match_key_counter": match_key_counter,
        "errule_counter": errule_counter,
    }


def size_bucket(n):
    if n == 1:
        return "1 (singletons)"
    if n == 2:
        return "2 (pairs)"
    if 3 <= n <= 5:
        return "3-5"
    if 6 <= n <= 10:
        return "6-10"
    if 11 <= n <= 50:
        return "11-50"
    return "51+ (review)"


BUCKET_ORDER = [
    "1 (singletons)",
    "2 (pairs)",
    "3-5",
    "6-10",
    "11-50",
    "51+ (review)",
]


def print_report(stats, duration=None):
    total_records = sum(stats["ds_record_count"].values())
    total_entities = stats["entity_count"]
    merged_records = total_records - total_entities

    w = 70
    print("=" * w)
    print("ENTITY RESOLUTION OVERLAP REPORT")
    print("=" * w)

    print(f"\n{'Total source records:':<35} {total_records:>10,}")
    print(f"{'Total resolved entities:':<35} {total_entities:>10,}")
    print(f"{'Records merged (resolved together):':<35} {merged_records:>10,}")
    if total_records > 0:
        merge_pct = 100.0 * merged_records / total_records
        print(f"{'Overall merge rate:':<35} {merge_pct:>9.1f}%")
    if duration is not None:
        print(f"{'Report generated in:':<35} {duration:>9.2f}s")

    print(f"\n{'-' * w}")
    print("DATA SOURCE SUMMARY")
    print(f"{'-' * w}")
    print(f"  {'Data Source':<25} {'Records':>10} {'Entities':>10} {'Compression':>12}")
    print(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*12}")
    for ds in sorted(stats["ds_record_count"]):
        recs = stats["ds_record_count"][ds]
        ents = stats["ds_entity_count"][ds]
        ratio = recs / ents if ents > 0 else 0
        print(f"  {ds:<25} {recs:>10,} {ents:>10,} {ratio:>11.2f}x")

    if stats["cross_source_entities"]:
        print(f"\n{'-' * w}")
        print("CROSS-SOURCE OVERLAP")
        print(f"{'-' * w}")
        print(f"  {'Source A':<20} {'Source B':<20} {'Shared Entities':>15} {'% of A':>8} {'% of B':>8}")
        print(f"  {'-'*20} {'-'*20} {'-'*15} {'-'*8} {'-'*8}")
        for pair in sorted(stats["cross_source_entities"]):
            src_a, src_b = pair
            shared = len(stats["cross_source_entities"][pair])
            ent_a = stats["ds_entity_count"].get(src_a, 0)
            ent_b = stats["ds_entity_count"].get(src_b, 0)
            pct_a = 100.0 * shared / ent_a if ent_a > 0 else 0
            pct_b = 100.0 * shared / ent_b if ent_b > 0 else 0
            print(f"  {src_a:<20} {src_b:<20} {shared:>15,} {pct_a:>7.1f}% {pct_b:>7.1f}%")
    else:
        print(f"\n  No cross-source overlap detected (single data source).")

    print(f"\n{'-' * w}")
    print("ENTITY SIZE DISTRIBUTION")
    print(f"{'-' * w}")
    bucket_counts = Counter()
    bucket_records = Counter()
    for sz in stats["entity_sizes"]:
        b = size_bucket(sz)
        bucket_counts[b] += 1
        bucket_records[b] += sz
    print(f"  {'Size Bucket':<20} {'Entities':>10} {'Records':>10} {'% Entities':>12}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*12}")
    for b in BUCKET_ORDER:
        if b in bucket_counts:
            ent = bucket_counts[b]
            rec = bucket_records[b]
            pct = 100.0 * ent / total_entities if total_entities > 0 else 0
            print(f"  {b:<20} {ent:>10,} {rec:>10,} {pct:>11.1f}%")

    if stats["total_relations"] > 0:
        print(f"\n{'-' * w}")
        print("RELATIONSHIP SUMMARY")
        print(f"{'-' * w}")
        match_type_labels = {
            "PM": "Possibly Same",
            "AM": "Ambiguous Match",
            "DR": "Disclosed Relation",
            "PR": "Possible Relation",
        }
        print(f"  {'Match Type':<30} {'Count':>10} {'%':>8}")
        print(f"  {'-'*30} {'-'*10} {'-'*8}")
        for mt, cnt in stats["relation_type_counts"].most_common():
            label = match_type_labels.get(mt, mt)
            pct = 100.0 * cnt / stats["total_relations"]
            print(f"  {label:<30} {cnt:>10,} {pct:>7.1f}%")
        print(f"  {'':>30} {'-'*10}")
        print(f"  {'Total':<30} {stats['total_relations']:>10,}")

    if stats["match_key_counter"]:
        print(f"\n{'-' * w}")
        print("TOP MATCH KEYS (what features drove resolution)")
        print(f"{'-' * w}")
        total_mk = sum(stats["match_key_counter"].values())
        print(f"  {'Match Key':<40} {'Count':>10} {'%':>8}")
        print(f"  {'-'*40} {'-'*10} {'-'*8}")
        for mk, cnt in stats["match_key_counter"].most_common(15):
            pct = 100.0 * cnt / total_mk
            print(f"  {mk:<40} {cnt:>10,} {pct:>7.1f}%")

    if stats["errule_counter"]:
        print(f"\n{'-' * w}")
        print("ER RULE USAGE")
        print(f"{'-' * w}")
        total_er = sum(stats["errule_counter"].values())
        print(f"  {'ER Rule':<30} {'Count':>10} {'%':>8}")
        print(f"  {'-'*30} {'-'*10} {'-'*8}")
        for er, cnt in stats["errule_counter"].most_common():
            pct = 100.0 * cnt / total_er
            print(f"  {er:<30} {cnt:>10,} {pct:>7.1f}%")

    print(f"\n{'=' * w}")


def main():
    args = parse_args()

    if args.file:
        if not os.path.isfile(args.file):
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        print(f"Reading entities from: {args.file}")
        start = time.time()
        entity_iter = entities_from_file(args.file)
        stats = build_stats(entity_iter)
        duration = time.time() - start
    else:
        settings = get_settings()

        print("Initializing Senzing (local SDK)...")
        sz_abstract_factory = SzAbstractFactoryCore("merge_stats_local", settings)
        sz_engine = sz_abstract_factory.create_engine()
        print("Senzing initialized.")

        print("Exporting entities and computing statistics...")
        start = time.time()
        entity_iter = export_entities(sz_engine)
        stats = build_stats(entity_iter)
        duration = time.time() - start

        sz_abstract_factory.destroy()

    print_report(stats, duration)


if __name__ == "__main__":
    main()
