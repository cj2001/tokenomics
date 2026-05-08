#!/usr/bin/env bash
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-workshop}"
POSTGRES_DB="${POSTGRES_DB:-tokenomics}"

export PGPASSWORD="$POSTGRES_PASSWORD"

PSQL="psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB -t -A"

count_before=$($PSQL -c "SELECT COUNT(*) FROM dsrc_record")
echo "Records before reset: $count_before"

if [ "$count_before" -eq 0 ]; then
    echo "No records to purge."
    exit 0
fi

read -p "Type YESPURGE to wipe all entity data (preserves data sources): " answer
if [ "$answer" != "YESPURGE" ]; then
    echo "Aborted."
    exit 1
fi

echo "Truncating data tables..."
$PSQL -c "TRUNCATE TABLE dsrc_record, obs_ent, res_ent, res_ent_okey, lib_feat, res_feat_ekey, res_feat_stat, res_relate, res_rel_ekey, sys_codes_used, sys_eval_queue CASCADE"

count_after=$($PSQL -c "SELECT COUNT(*) FROM dsrc_record")
echo "Records after reset:  $count_after"
echo "Records removed:      $((count_before - count_after))"

echo "Restarting Senzing container..."
docker restart tokenomics_senzing > /dev/null
echo "Waiting for Senzing to be ready..."
sleep 10
echo "Done."
