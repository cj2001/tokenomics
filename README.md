# tokenomics

Entity resolution experiments using Senzing, comparing Docker-based and local native deployments against PostgreSQL.

## Prerequisites

- PostgreSQL 15+
- Python 3.10+
- A Senzing license (base64-encoded, stored in `g2.lic_base64`)
- For Docker mode: Docker and Docker Compose

## Project Structure

```
tokenomics/
  load_data.py            # Load JSONL into Senzing via gRPC (Docker)
  load_data_local.py      # Load JSONL into Senzing via local SDK
  merge_stats.py          # ER overlap report via gRPC (Docker)
  merge_stats_local.py    # ER overlap report via local SDK
  llm_er.py               # LLM-based entity resolution
  clean_for_people.py     # Filter JSONL for person records
  estimate_tokens.py      # Token count estimator
  jsonl_to_csv.py         # JSONL to CSV converter
  randomize.py            # Randomize record order
  reset_db.py             # Reset database
data/                     # JSONL data files for loading
senzing/                  # Local Senzing project (created during setup)
docker-compose.yml        # Docker-based deployment
reset_senzing.sh          # Reset Senzing entity data in PostgreSQL
```

## Senzing License

The Senzing SDK uses a base64 string to specify license entitlement:
- Stored in `g2.lic_base64`
- Referenced via `SENZING_LICENSE_BASE64` in `.env`
- See: https://senzing.com/docs/tutorials/senzing_engine_config/

## Option A: Docker Deployment

All services (PostgreSQL, Senzing gRPC, Jupyter) run in containers.

### 1. Set up environment

Create a `.env` file with:
```
SENZING_LICENSE_BASE64=<contents of g2.lic_base64>
ANTHROPIC_API_KEY=<your key>
```

### 2. Start services

```bash
docker compose up -d
```

This starts:
- **PostgreSQL** on port 5436 (mapped from container port 5432)
- **Senzing gRPC** on port 8261
- **Jupyter Lab** on port 18888
- **Portainer** on port 9000

### 3. Load data

```bash
python tokenomics/load_data.py --files data/equifax-small.jsonl data/npi-small.jsonl
```

Options:
- `--host` / `--port` to override Senzing gRPC connection (default: localhost:8261)

### 4. Run merge stats

```bash
python tokenomics/merge_stats.py
```

Or from a pre-exported JSONL file:
```bash
python tokenomics/merge_stats.py --file data/some-results.jsonl
```

### 5. Reset data

```bash
bash reset_senzing.sh
```

Uses port 5436 by default (Docker-mapped). Type `YESPURGE` when prompted.

---

## Option B: Local Native Deployment

Senzing SDK runs directly on your machine against a local PostgreSQL instance. No Docker required.

### 1. Install PostgreSQL

```bash
sudo apt update && sudo apt install postgresql postgresql-client
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### 2. Create the database

```bash
sudo -u postgres psql -c "CREATE DATABASE tokenomics;"
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD 'workshop';"
```

### 3. Install the Senzing SDK

```bash
sudo apt install apt-transport-https
wget https://senzing-production-apt.s3.amazonaws.com/senzingrepo_2.0.1-1_all.deb
sudo apt install ./senzingrepo_2.0.1-1_all.deb
sudo apt update
sudo apt install senzingsdk-runtime senzingsdk-tools senzingsdk-poc
```

Accept the EULA when prompted (type `y`).

### 4. Create a Senzing project

```bash
/opt/senzing/er/bin/sz_create_project ~/tokenomics/senzing
```

### 5. Configure PostgreSQL connection

Edit `senzing/etc/sz_engine_config.ini`:

```ini
[PIPELINE]
 SUPPORTPATH=/home/<user>/tokenomics/senzing/data
 CONFIGPATH=/home/<user>/tokenomics/senzing/etc
 RESOURCEPATH=/home/<user>/tokenomics/senzing/resources
 LICENSESTRINGBASE64=<contents of g2.lic_base64>

[SQL]
 CONNECTION=postgresql://postgres:workshop@localhost:5432:tokenomics
```

Note: The `SUPPORTPATH`, `CONFIGPATH`, and `RESOURCEPATH` are set automatically by `sz_create_project`. You need to add the `LICENSESTRINGBASE64` line and change the `CONNECTION` from SQLite to PostgreSQL.

### 6. Apply the Senzing schema

```bash
sudo -u postgres psql -d tokenomics \
  -f ~/tokenomics/senzing/resources/schema/szcore-schema-postgresql-create.sql
```

### 7. Register the ER configuration

```bash
cd ~/tokenomics/senzing
source setupEnv
./bin/sz_setup_config
```

Enter `y` when prompted. This is a one-time step.

### 8. Install Python dependencies

```bash
pip install senzing-core
```

### 9. Load data

```bash
source ~/tokenomics/senzing/setupEnv
python tokenomics/load_data_local.py --files data/equifax-small.jsonl data/npi-small.jsonl
```

You must `source setupEnv` once per shell session before running the local scripts.

### 10. Run merge stats

```bash
python tokenomics/merge_stats_local.py
```

Or from a pre-exported JSONL file:
```bash
python tokenomics/merge_stats_local.py --file data/some-results.jsonl
```

### 11. Reset data

```bash
POSTGRES_PORT=5432 POSTGRES_PASSWORD=workshop bash reset_senzing.sh
```

Type `YESPURGE` when prompted. Note: the Docker restart at the end will fail harmlessly if Docker is not running.

Or reset directly:
```bash
PGPASSWORD=workshop psql -h localhost -p 5432 -U postgres -d tokenomics -c \
  "TRUNCATE TABLE dsrc_record, obs_ent, res_ent, res_ent_okey, lib_feat, res_feat_ekey, res_feat_stat, res_relate, res_rel_ekey, sys_codes_used, sys_eval_queue CASCADE"
```

---

## Converting JSONL to CSV

Convert JSONL files to CSV for lower token usage when sending records to LLMs. Nested arrays (e.g., NPI license numbers, Equifax features) are automatically flattened into columns.

```bash
python tokenomics/jsonl_to_csv.py data/equifax-small.jsonl data/npi-small.jsonl
```

Output CSVs are written alongside the input files (e.g., `data/equifax-small.csv`). To specify an output directory:

```bash
python tokenomics/jsonl_to_csv.py data/equifax-small.jsonl -o output/
```

## Data Files

JSONL files in `data/` follow the Senzing record format with `DATA_SOURCE`, `RECORD_ID`, and feature fields. Available datasets include various sizes:

- `*-small.jsonl` — small subsets for quick testing
- `*-med.jsonl` — medium subsets for benchmarking
- `*-lasvegas*.jsonl` — Las Vegas area records

## References

- Senzing EULA: https://senzing.com/end-user-license-agreement/
- Senzing Support: support@senzing.com
- Senzing v4 Linux Quickstart: https://www.senzing.com/docs/quickstart/quickstart_linux
- Senzing PostgreSQL Setup: https://www.senzing.com/docs/tutorials/database/postgres_setup
- Senzing Engine Configuration: https://www.senzing.com/docs/tutorials/senzing_engine_config
