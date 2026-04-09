#!/usr/bin/env python3
"""
Load JSONL files into Senzing via local SDK (senzing-core).

Usage:
    source ~/tokenomics/senzing/setupEnv
    python load_data_local.py --files data/npi-small.jsonl data/equifax-small.jsonl

Environment variables:
    SENZING_ENGINE_CONFIGURATION_JSON  (set by setupEnv)
"""

import argparse
import configparser
import json
import os
import sys
import time
from pathlib import Path

from senzing import SzError
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
    """Register any missing data sources. Returns True if engine was reinitialised."""
    sz_configmanager = sz_abstract_factory.create_configmanager()
    default_config_id = sz_configmanager.get_default_config_id()

    print(f"Default config ID: {default_config_id}")

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
    sz_configmanager.replace_default_config_id(default_config_id, new_config_id)
    print(f"Configuration saved with ID: {new_config_id}")
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
    parser = argparse.ArgumentParser(description="Load JSONL files into Senzing (local SDK).")
    parser.add_argument(
        "--files",
        nargs="+",
        required=True,
        help="JSONL files to load",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    settings = get_settings()

    files = [Path(f) for f in args.files]

    missing = [f for f in files if not f.exists()]
    if missing:
        print("ERROR: the following files were not found:")
        for f in missing:
            print(f"  {f}")
        sys.exit(1)

    for f in files:
        print(f"Found: {f}")

    print("\nInitializing Senzing (local SDK)...")
    overall_start = time.time()

    sz_abstract_factory = SzAbstractFactoryCore("load_data_local", settings)
    sz_engine = sz_abstract_factory.create_engine()

    print("Senzing initialized.\n")

    print("Detecting data sources from input files...")
    file_sources = []
    for f in files:
        ds = detect_data_source(f)
        print(f"  {f.name} -> {ds}")
        file_sources.append((f, ds))

    required_sources = sorted({ds for _, ds in file_sources})
    print(f"\nEnsuring data sources are registered: {required_sources}")
    ensure_data_sources(sz_abstract_factory, sz_engine, required_sources)

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

    overall_duration = time.time() - overall_start
    total_records = sum(totals.values())
    total_errors = sum(len(e) for e in all_errors.values())
    overall_rate = total_records / overall_duration if overall_duration > 0 else 0

    print("\n" + "=" * 60)
    print("LOADING SUMMARY")
    print("=" * 60)
    for ds, count in totals.items():
        print(f"  {ds}: {count:,} records, {len(all_errors[ds])} errors")
    print(f"  Total records loaded: {total_records:,}")
    print(f"  Total errors:         {total_errors}")
    print(f"  Total time:           {overall_duration:.2f}s")
    print(f"  Overall rate:         {overall_rate:.2f} records/s")
    print("=" * 60)

    try:
        stats = sz_engine.get_stats()
        stats_dict = json.loads(stats)
        print("\nSenzing engine stats:")
        if "workload" in stats_dict:
            print(f"  Loaded records: {stats_dict['workload'].get('loadedRecords', 'N/A')}")
    except Exception as e:
        print(f"Could not retrieve stats: {e}")

    sz_abstract_factory.destroy()
    sys.exit(0 if total_errors == 0 else 3)


if __name__ == "__main__":
    main()
