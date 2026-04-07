#!/usr/bin/env python3
"""
Load NPI and Equifax Las Vegas JSONL files into Senzing via gRPC.

Usage:
    python load_lasvegas_data.py
    python load_lasvegas_data.py --data-dir ./data
    python load_lasvegas_data.py --files data/npi-lasvegas.jsonl data/equifax-lasvegas_A.jsonl

Environment variables:
    SENZING_GRPC_HOST  (default: localhost)
    SENZING_GRPC_PORT  (default: 8261)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import grpc
from senzing import SzError
from senzing_grpc import SzAbstractFactoryGrpc


DEFAULT_FILES = [
    "data/npi-lasvegas-people.jsonl",
    "data/equifax-lasvegas_A-people.jsonl",
]


def detect_data_source(file_path: Path) -> str:
    """Peek the first line of a JSONL file and return its DATA_SOURCE value."""
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            ds = record.get("DATA_SOURCE")
            if not ds:
                raise ValueError(f"{file_path}: first record has no DATA_SOURCE field")
            return ds
    raise ValueError(f"{file_path}: file is empty")


def ensure_data_sources(sz_abstract_factory, sz_engine, required_sources):
    """Register any missing data sources. Returns True if a restart is needed."""
    sz_configmanager = sz_abstract_factory.create_configmanager()
    default_config_id = sz_configmanager.get_default_config_id()
    active_config_id = sz_engine.get_active_config_id()

    print(f"Default config ID: {default_config_id}")
    print(f"Active config ID:  {active_config_id}")

    if default_config_id != active_config_id:
        print("\nWARNING: Engine is using an old configuration.")
        print("Restart the senzing container with:")
        print("  docker restart tokenomics_senzing")
        print("Then re run this script.")
        return True

    sz_config = sz_configmanager.create_config_from_config_id(default_config_id)
    data_sources = json.loads(sz_config.get_data_source_registry())
    existing = {ds["DSRC_CODE"] for ds in data_sources.get("DATA_SOURCES", [])}

    print(f"Existing data sources: {sorted(existing)}")

    sources_added = []
    for source in required_sources:
        if source in existing:
            print(f"  Already registered: {source}")
        else:
            print(f"  Registering: {source}")
            sz_config.register_data_source(source)
            sources_added.append(source)

    if not sources_added:
        print("\nAll required data sources already registered.")
        return False

    print("\nSaving configuration changes...")
    config_definition = sz_config.export()
    new_config_id = sz_configmanager.register_config(
        config_definition=config_definition,
        config_comment=f"Added data sources: {', '.join(sources_added)}",
    )
    sz_configmanager.set_default_config_id(new_config_id)
    print(f"Configuration saved with ID: {new_config_id}")
    print("\nIMPORTANT: Restart the senzing container to load the new config:")
    print("  docker restart tokenomics_senzing")
    print("Wait about 10 seconds, then re run this script.")
    return True


def load_jsonl_file(sz_engine, file_path: Path, data_source_name: str):
    """Load a JSONL file into Senzing. Returns (records_loaded, errors)."""
    records_loaded = 0
    errors = []

    print(f"\nLoading {file_path}")
    print(f"Expected data source: {data_source_name}")

    with open(file_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)

                if record.get("DATA_SOURCE") != data_source_name:
                    print(
                        f"  Warning: line {line_num} has unexpected DATA_SOURCE: "
                        f"{record.get('DATA_SOURCE')}"
                    )

                sz_engine.add_record(
                    data_source_code=record["DATA_SOURCE"],
                    record_id=record["RECORD_ID"],
                    record_definition=json.dumps(record),
                )
                records_loaded += 1

                if records_loaded % 100 == 0:
                    print(f"  Loaded {records_loaded} records...", end="\r")

            except SzError as e:
                errors.append(f"Line {line_num}: Senzing error: {e}")
            except json.JSONDecodeError as e:
                errors.append(f"Line {line_num}: JSON parse error: {e}")
            except KeyError as e:
                errors.append(f"Line {line_num}: missing required field: {e}")
            except Exception as e:
                errors.append(f"Line {line_num}: unexpected error: {e}")

    print(f"  Completed: {records_loaded} records loaded")
    if errors:
        print(f"  Errors encountered: {len(errors)}")
        print("  First 5 errors:")
        for err in errors[:5]:
            print(f"    {err}")

    return records_loaded, errors


def parse_args():
    parser = argparse.ArgumentParser(description="Load JSONL files into Senzing.")
    parser.add_argument(
        "--files",
        nargs="+",
        help="JSONL files to load (default: data/npi-lasvegas.jsonl data/equifax-lasvegas_A.jsonl)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory to resolve default file names against (default: cwd)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("SENZING_GRPC_HOST", "localhost"),
        help="Senzing gRPC host (default: localhost or $SENZING_GRPC_HOST)",
    )
    parser.add_argument(
        "--port",
        default=os.getenv("SENZING_GRPC_PORT", "8261"),
        help="Senzing gRPC port (default: 8261 or $SENZING_GRPC_PORT)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.files:
        files = [Path(f) for f in args.files]
    elif args.data_dir:
        files = [args.data_dir / Path(f).name for f in DEFAULT_FILES]
    else:
        files = [Path(f) for f in DEFAULT_FILES]

    missing = [f for f in files if not f.exists()]
    if missing:
        print("ERROR: the following files were not found:")
        for f in missing:
            print(f"  {f}")
        sys.exit(1)

    for f in files:
        print(f"Found: {f}")

    print(f"\nConnecting to Senzing at {args.host}:{args.port}")
    grpc_channel = grpc.insecure_channel(f"{args.host}:{args.port}")
    sz_abstract_factory = SzAbstractFactoryGrpc(grpc_channel)
    sz_engine = sz_abstract_factory.create_engine()

    print("\nDetecting data sources from input files...")
    file_sources = []
    for f in files:
        ds = detect_data_source(f)
        print(f"  {f.name} -> {ds}")
        file_sources.append((f, ds))

    required_sources = sorted({ds for _, ds in file_sources})
    print(f"\nEnsuring data sources are registered: {required_sources}")
    needs_restart = ensure_data_sources(sz_abstract_factory, sz_engine, required_sources)
    if needs_restart:
        sys.exit(2)

    totals = {}
    all_errors = {}
    for f, ds in file_sources:
        start = time.time()
        loaded, errors = load_jsonl_file(sz_engine, f, ds)
        duration = time.time() - start
        rate = loaded / duration if duration > 0 else 0
        print(f"  {f.name} loaded in {duration:.2f}s ({rate:.2f} records/s)")
        totals[ds] = totals.get(ds, 0) + loaded
        all_errors[ds] = all_errors.get(ds, []) + errors

    total_records = sum(totals.values())
    total_errors = sum(len(e) for e in all_errors.values())

    print("\n" + "=" * 60)
    print("LOADING SUMMARY")
    print("=" * 60)
    for ds, count in totals.items():
        print(f"  {ds}: {count:,} records, {len(all_errors[ds])} errors")
    print(f"  Total records loaded: {total_records:,}")
    print(f"  Total errors:         {total_errors}")
    print("=" * 60)

    try:
        stats = sz_engine.get_stats()
        stats_dict = json.loads(stats)
        print("\nSenzing engine stats:")
        if "workload" in stats_dict:
            print(f"  Loaded records: {stats_dict['workload'].get('loadedRecords', 'N/A')}")
    except Exception as e:
        print(f"Could not retrieve stats: {e}")

    sys.exit(0 if total_errors == 0 else 3)


if __name__ == "__main__":
    main()