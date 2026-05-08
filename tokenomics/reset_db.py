#!/usr/bin/env python3
"""
Reset the Senzing entity-resolved data stored in Postgres.

Truncates all record and entity data tables while preserving
data source definitions and configuration (sys_cfg, sys_vars, etc.).

Usage:
    python reset_db.py

Environment variables:
    POSTGRES_HOST      (default: localhost)
    POSTGRES_PORT      (default: 5436)
    POSTGRES_USER      (default: postgres)
    POSTGRES_PASSWORD  (default: workshop)
    POSTGRES_DB        (default: tokenomics)
"""

import os
import sys

import psycopg2

# Data tables to truncate (order doesn't matter with TRUNCATE CASCADE).
# Preserves: sys_cfg, sys_vars, sys_sequence, sys_status, sys_hw_check
DATA_TABLES = [
    "dsrc_record",
    "obs_ent",
    "res_ent",
    "res_ent_okey",
    "lib_feat",
    "res_feat_ekey",
    "res_feat_stat",
    "res_relate",
    "res_rel_ekey",
    "sys_codes_used",
    "sys_eval_queue",
]


def get_record_count(pg_conn):
    """Query Postgres for the number of records in the Senzing datastore."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM dsrc_record")
        return cur.fetchone()[0]


def main():
    pg_host = os.getenv("POSTGRES_HOST", "localhost")
    pg_port = os.getenv("POSTGRES_PORT", "5436")
    pg_user = os.getenv("POSTGRES_USER", "postgres")
    pg_password = os.getenv("POSTGRES_PASSWORD", "workshop")
    pg_db = os.getenv("POSTGRES_DB", "tokenomics")

    print(f"Connecting to Postgres at {pg_host}:{pg_port}/{pg_db}")
    pg_conn = psycopg2.connect(
        host=pg_host,
        port=pg_port,
        user=pg_user,
        password=pg_password,
        dbname=pg_db,
    )
    pg_conn.autocommit = False

    count_before = get_record_count(pg_conn)
    print(f"\nRecords before reset: {count_before:,}")

    if count_before == 0:
        print("No records to purge.")
        pg_conn.close()
        sys.exit(0)

    answer = input(
        "\nThis will purge ALL entity-resolved data while preserving data sources and config.\n"
        "Type YESPURGE to continue: "
    )
    if answer != "YESPURGE":
        print("Aborted.")
        pg_conn.close()
        sys.exit(1)

    print("\nTruncating data tables...")
    try:
        with pg_conn.cursor() as cur:
            table_list = ", ".join(DATA_TABLES)
            cur.execute(f"TRUNCATE TABLE {table_list} CASCADE")
        pg_conn.commit()
    except Exception as e:
        pg_conn.rollback()
        print(f"ERROR: {e}")
        pg_conn.close()
        sys.exit(2)

    print("Truncation complete.")

    count_after = get_record_count(pg_conn)
    print(f"\nRecords after reset:  {count_after:,}")
    print(f"Records removed:      {count_before - count_after:,}")

    pg_conn.close()

    print("\nIMPORTANT: Restart the Senzing container so it picks up the clean state:")
    print("  docker restart tokenomics_senzing")


if __name__ == "__main__":
    main()
