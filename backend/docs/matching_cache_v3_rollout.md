# Matching Cache v3 controlled rollout

## Safety contract

V3 prepares immutable source versions and context-scoped snapshots in the
dedicated worker. Preparation does not authorize reads. Web pricing keeps the
legacy matcher unless `MATCH_CACHE_READ=true` and the price format is present
in `MATCH_CACHE_READ_PRICE_FORMAT_IDS`; `MATCH_CACHE_READ_ALLOW_ALL` remains
`false`. A missing/building artifact follows the configured fallback contract,
while corrupt, stale or incompatible cohorts fail closed.

The worker must find all matching-dependency and physical-source invalidation
triggers before it reconciles or publishes. New imports are captured with
`matchingDecisionMode=shadow_rematch`; only the explicit existing-source
backfill path may preserve finalized authoritative legacy decisions.

## Gate 1 — existing format 88

Deploy the reviewed commit with reads still disabled on both servers. On Server
75 (`/opt/inkar-opt`) recreate only `web`; on Server 77
(`/opt/inkar-optv2.0/inkar-opt`) recreate only `worker`, always using the
already-installed server-specific `docker-compose.prod.yml` and any existing
override shown by the running container labels.

Server 77 worker settings:

```dotenv
MATCH_CACHE_ENABLED=true
MATCH_CACHE_WORKER_ENABLED=true
MATCH_CACHE_AUTO_PREPARE_ENABLED=true
MATCH_CACHE_AUTO_PREPARE_INTERVAL_SECONDS=300
MATCH_CACHE_READ=false
MATCH_CACHE_READ_ALLOW_ALL=false
```

Run one admin readiness report and one exact differential/final-output report
for format 88 and sources 1607,1588,820. Only after both are exact may Server
75 retain the established pilot allowlist:

```dotenv
MATCH_CACHE_READ=true
MATCH_CACHE_READ_MODE=required
MATCH_CACHE_READ_PRICE_FORMAT_IDS=88
MATCH_CACHE_READ_ALLOW_ALL=false
```

## Gate 2 — one new test format

Upload its PLKs through the unchanged ingestion workflow. Do not backfill it.
Wait for the admin status to show the full physical-source cohort ready, then
run one differential/final price and export comparison under identical inputs.
Add only that format ID to `MATCH_CACHE_READ_PRICE_FORMAT_IDS`, recreate only
the Server 75 web service, and verify one generation plus one repeated
generation. Format 88 must remain exact throughout.

## Gate 3 — broader opt-in

Add reviewed format IDs to the explicit comma-separated allowlist in small
batches. Require ready context-scoped snapshots, zero differential/final-output
differences, no failed or repeatedly retried jobs, and stable worker/database
health for every batch. Do not use `MATCH_CACHE_READ_ALLOW_ALL=true` for this
rollout.

## Minimal deployment and rollback commands

After checking out the reviewed commit by the repository's normal deployment
mechanism, use the existing Compose file set (including any installed override):

```bash
# Server 75, from /opt/inkar-opt
docker compose -f docker-compose.prod.yml config --quiet && docker compose -f docker-compose.prod.yml build web && docker compose -f docker-compose.prod.yml up -d --no-deps web

# Server 77, from /opt/inkar-optv2.0/inkar-opt
docker compose -f docker-compose.prod.yml config --quiet && docker compose -f docker-compose.prod.yml build worker && docker compose -f docker-compose.prod.yml up -d --no-deps worker
```

If the existing deployment requires an override, append its current `-f` value
to both commands; never replace it. No migration or manual backfill is added by
V3.

One-step read rollback is `MATCH_CACHE_READ=false` followed by recreation of
only the Server 75 `web` service. Leave the worker enabled so artifacts remain
observable and recoverable; disable `MATCH_CACHE_AUTO_PREPARE_ENABLED` and
recreate only Server 77 `worker` if preparation itself must stop.

## No-go conditions

Stop a gate for any unavailable dependency trigger, incompatible shared-source
cohort, stale/corrupt snapshot, failed job after its retry budget, non-exact
decision/materialization/final-price/export comparison, unexpected full-matcher
invocation on a required cache hit, database instability, or regression in the
legacy path. Never delete current materializations or source rows as rollback.
