# Matching cache production pilot (price format 88)

## Scope and safety

The cache reuses the existing immutable source-version, shadow-snapshot and
materialization tables. No pricing formula, SAP table, generated price list or
competitor source price is changed by backfill. Cache reads remain disabled
unless all master/read flags are enabled, and
`MATCH_CACHE_READ_PRICE_FORMAT_IDS=88` keeps every other format on the legacy
matcher. An empty allowlist permits no cache reads; global reads require the
separate `MATCH_CACHE_READ_ALLOW_ALL=true` gate and are not part of this pilot.

No new database migration is required. Production must already contain the
matching-cache foundation, stable-lineage tables, and all 15 dependency
triggers.

## Commands and write classification

Run Server 75 commands from `/opt/inkar-opt`; run Server 77 commands from
`/opt/inkar-optv2.0/inkar-opt`. Replace `RELEASE_COMMIT` only with the reviewed
commit SHA.

Before using any Compose command on either server, inspect the running
container's `com.docker.compose.project.config_files` label. If it lists an
override, pass the same ordered `-f` arguments to every `docker compose`
command below. Stop if the listed file is absent; using only the base file can
silently discard production-specific networking or environment configuration.

### 1. Read-only preflight on Server 75

```bash
set -euo pipefail
cd /opt/inkar-opt
RELEASE_COMMIT='<reviewed-full-sha>'
test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
docker compose -f docker-compose.prod.yml config --quiet
docker inspect -f '{{.State.Health.Status}}' inkar_opt_db | grep -Fx healthy
docker inspect -f '{{.State.Health.Status}}' inkar_opt_web | grep -Fx healthy
docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' inkar_opt_web

docker exec -i inkar_opt_db sh -ceu '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At
' <<'SQL'
SELECT count(*) FROM pg_tables
WHERE schemaname=current_schema() AND tablename IN (
  'competitor_source_versions',
  'competitor_source_version_items',
  'competitor_match_snapshots',
  'competitor_match_snapshot_items',
  'competitor_match_jobs',
  'price_format_competitor_materializations',
  'competitor_match_dependency_revisions',
  'competitor_source_publications',
  'competitor_source_publication_items'
);
SELECT count(*) FROM pg_trigger
WHERE NOT tgisinternal AND tgenabled <> 'D'
  AND tgname LIKE 'trg_match_cache_%';
SQL
```

Expected results are `9` tables and `15` enabled triggers. Stop otherwise.

Confirm the pilot assignments without writes:

```bash
docker exec -i inkar_opt_db sh -ceu '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off
' <<'SQL'
SELECT a.price_format_id, a.competitor_price_list_id, a.is_active,
       p.source_type, p.source_key, p.items_count
FROM price_format_competitor_assignments a
JOIN competitor_price_lists p ON p.id=a.competitor_price_list_id
WHERE a.price_format_id=88
ORDER BY a.competitor_price_list_id;
SQL
```

The active physical source IDs must be exactly `820`, `1588`, and `1607`.

### 2. PostgreSQL backup on Server 75 (writes a host backup file only)

```bash
set -euo pipefail
cd /opt/inkar-opt
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR=/opt/inkar-opt/.release-backups
install -d -m 700 "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/pre_matching_cache_pilot_${STAMP}.dump"

docker exec -i inkar_opt_db sh -ceu '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl
' > "$BACKUP_FILE"
test -s "$BACKUP_FILE"
docker exec -i inkar_opt_db pg_restore -l < "$BACKUP_FILE" >/dev/null
```

Stop if the archive is empty or cannot be listed.

### 3. Backfill dry-run on Server 75 (read-only)

```bash
docker exec --workdir /app/backend inkar_opt_web \
  python -m scripts.backfill_matching_cache \
  --price-format-id 88 \
  --source-ids 1607,1588,820 \
  --batch-size 2000 \
  --dry-run
```

Review the JSON lines, source row counts and SHA-256 values. Stop on a missing
source, inactive assignment, zero unexpected row count, or fingerprint error.

### 4. Backfill apply on Server 75 (database write)

This writes only matching-cache versions, physical lineage, pending snapshots
and idempotent jobs. Each source commits independently and a stopped command is
safe to repeat.

```bash
docker exec --workdir /app/backend inkar_opt_web \
  python -m scripts.backfill_matching_cache \
  --price-format-id 88 \
  --source-ids 1607,1588,820 \
  --batch-size 2000 \
  --apply
```

### 5. Worker rollout on Server 77 (container recreation; no global reads)

Set only these Server 77 environment values after explicit approval:

```dotenv
MATCH_CACHE_ENABLED=true
MATCH_CACHE_WORKER_ENABLED=true
MATCH_CACHE_WORKER_POLL_INTERVAL_SECONDS=5
MATCH_CACHE_WORKER_LEASE_SECONDS=900
MATCH_CACHE_READ=false
MATCH_CACHE_READ_PRICE_FORMAT_IDS=
MATCH_CACHE_READ_ALLOW_ALL=false
```

Then deploy the reviewed commit and recreate only the already-existing worker:

```bash
set -euo pipefail
cd /opt/inkar-optv2.0/inkar-opt
RELEASE_COMMIT='<reviewed-full-sha>'
test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
docker compose -f docker-compose.prod.yml config --quiet
docker inspect inkar_opt_worker >/dev/null
docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' inkar_opt_worker
docker compose -f docker-compose.prod.yml build worker
docker compose -f docker-compose.prod.yml up -d --no-deps worker
docker inspect -f '{{.State.Running}}' inkar_opt_worker | grep -Fx true
docker logs --since=10m inkar_opt_worker 2>&1 | grep -E 'MATCH_CACHE_(WORKER|JOB)'
```

Do not start a worker on Server 75 if it was not already part of that server's
topology.

Before worker activation, prove that the existing Server 77 worker reaches the
configured Server 75 PostgreSQL without printing its URL or credentials:

```bash
docker exec --workdir /app/backend inkar_opt_worker python - <<'PY'
from sqlalchemy import text
from app.db import engine
with engine.connect() as connection:
    connection.execute(text("SET TRANSACTION READ ONLY"))
    assert connection.scalar(text("SELECT 1")) == 1
    connection.rollback()
print("database_read_only_probe=pass")
PY
```

Stop if this read-only probe fails. Do not substitute a local Server 77
database or change `DATABASE_URL` during rollout.

### 6. Read-only readiness and exact shadow validation on Server 75

```bash
docker exec -i inkar_opt_db sh -ceu '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off
' <<'SQL'
SELECT j.id,j.source_version_id,j.match_snapshot_id,j.status,j.attempt_count,
       j.max_attempts,j.claimed_by,j.error
FROM competitor_match_jobs j
ORDER BY j.id DESC;
SELECT s.id,s.source_version_id,s.status,s.input_row_count,
       s.matched_row_count,s.unmatched_row_count,s.error
FROM competitor_match_snapshots s
ORDER BY s.id DESC;
SQL

docker exec --workdir /app/backend inkar_opt_web \
  python -m scripts.validate_matching_cache \
  --price-format-id 88 \
  --source-ids 1607,1588,820 \
  --output /tmp/matching-cache-pilot-validation.json
```

The validator uses a repeatable-read, read-only transaction and exits `0` only
when the active assignments are exactly those requested, current source
content and every legacy versus snapshot decision are exact, and the projected
cache materialization exactly matches the current `competitors_prices` rows.
The JSON explicitly reports product/source IDs, Goods IDs, raw competitor
price, effective coefficient, match type, manual/authoritative/rejected flags,
manufacturer evidence and duplicate multiplicity. Any mismatch blocks read
activation.

### 7. Pilot read activation on Server 75 (environment change and web recreation)

After an isolated same-input A/B pricing comparison has zero unexplained final
price differences, set:

```dotenv
MATCH_CACHE_ENABLED=true
MATCH_CACHE_DUAL_WRITE=true
MATCH_CACHE_SHADOW_ENABLED=true
MATCH_CACHE_READ=true
MATCH_CACHE_READ_MODE=required
MATCH_CACHE_READ_PRICE_FORMAT_IDS=88
MATCH_CACHE_READ_ALLOW_ALL=false
MATCH_CACHE_WORKER_ENABLED=false
```

Deploy only the web container on Server 75:

```bash
cd /opt/inkar-opt
docker compose -f docker-compose.prod.yml build web
docker compose -f docker-compose.prod.yml up -d --no-deps web
docker inspect -f '{{.State.Health.Status}}' inkar_opt_web | grep -Fx healthy
curl --fail --silent http://127.0.0.1:8010/health
```

Generate only the approved format-88 pilot. Its workflow metadata must show
`hit=true`, three source/snapshot IDs, `fullRematchInvocationCount=0`, and
`rawSourceRowsRead=0`. Stop if it falls back, if final-price comparison differs,
or if runtime/container/database health regresses.

## Same-input A/B validation

Use a disposable PostgreSQL clone and frozen copies of the same stock snapshot,
assignments, rules, source rows, product catalog and dependency revisions.
Run the legacy path with cache reads disabled, then the required cache path.
Compare competitor rows and final `CalculatedPrice`/analytics/export fields with
`compare_records`; matching row counts alone are insufficient. The existing
PostgreSQL end-to-end test exercises calculation, analytics and Excel parity.

## Rollback

1. Set `MATCH_CACHE_READ=false` on Server 75 and recreate only `web`.
2. Set `MATCH_CACHE_WORKER_ENABLED=false` on Server 77 and recreate only the
   existing `worker`.
3. Leave additive cache tables in place; they are inert and retain diagnostics.
4. Do not restore the database merely to disable cache reads. Restore the backup
   only under a separately approved database-recovery incident.

Legacy matching remains the fallback whenever reads are disabled. No SAP,
pricing-formula, source-price or generated-price-list rollback is required.
