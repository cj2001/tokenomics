#!/usr/bin/env python3
"""Convert JSONL files to CSV for lower token usage when sending to LLMs.

Nested arrays are flattened:
  - NPI: PROVIDER_LICENSE_NUMS, PROVIDER_IDS, ENDPOINT_LIST get numbered suffixes
  - Equifax: FEATURES dicts are merged into flat columns, PAYLOAD likewise
"""

import argparse
import csv
import json
import sys
from collections import OrderedDict
from pathlib import Path


def flatten_npi_record(rec: dict) -> dict:
    """Flatten an NPI-PROVIDERS record."""
    flat = {}
    for k, v in rec.items():
        if k == "PROVIDER_LICENSE_NUMS":
            for i, item in enumerate(v, 1):
                for ik, iv in item.items():
                    flat[f"{ik}_{i}"] = iv
        elif k == "PROVIDER_IDS":
            for i, item in enumerate(v, 1):
                for ik, iv in item.items():
                    flat[f"{ik}_{i}"] = iv
        elif k == "ENDPOINT_LIST":
            for i, item in enumerate(v, 1):
                for ik, iv in item.items():
                    flat[f"{ik}_{i}"] = iv
        else:
            flat[k] = v
    return flat


def flatten_equifax_record(rec: dict) -> dict:
    """Flatten an EQUIFAX record by merging FEATURES and PAYLOAD dicts."""
    flat = {}
    for k, v in rec.items():
        if k == "FEATURES":
            for item in v:
                flat.update(item)
        elif k == "PAYLOAD":
            for item in v:
                flat.update(item)
        else:
            flat[k] = v
    return flat


def detect_source(records: list[dict]) -> str:
    if records and records[0].get("DATA_SOURCE") == "EQUIFAX":
        return "equifax"
    if records and "FEATURES" in records[0]:
        return "equifax"
    return "npi"


def convert(input_path: Path, output_path: Path | None = None):
    records = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if not records:
        print(f"No records in {input_path}", file=sys.stderr)
        return

    source = detect_source(records)
    flatten = flatten_equifax_record if source == "equifax" else flatten_npi_record

    flat_records = [flatten(r) for r in records]

    # Collect all columns in stable order (insertion order from first appearance)
    columns = list(OrderedDict.fromkeys(k for r in flat_records for k in r))

    if output_path is None:
        output_path = input_path.with_suffix(".csv")

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat_records)

    orig_size = input_path.stat().st_size
    new_size = output_path.stat().st_size
    pct = (1 - new_size / orig_size) * 100
    print(f"{input_path.name} -> {output_path.name}")
    print(f"  Records: {len(records)}")
    print(f"  JSONL: {orig_size:,} bytes | CSV: {new_size:,} bytes | {pct:.1f}% smaller")


def main():
    parser = argparse.ArgumentParser(description="Convert JSONL to CSV for token savings")
    parser.add_argument("files", nargs="+", type=Path, help="JSONL files to convert")
    parser.add_argument("-o", "--output-dir", type=Path, default=None,
                        help="Output directory (default: same as input)")
    args = parser.parse_args()

    for input_path in args.files:
        if args.output_dir:
            output_path = args.output_dir / input_path.with_suffix(".csv").name
        else:
            output_path = None
        convert(input_path, output_path)


if __name__ == "__main__":
    main()
