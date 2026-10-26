# Matching Cache v3.1 multi-context rollout

## Design and compatibility

A physical competitor source can participate in more than one active pricing
format. V3.1 resolves every distinct assigned physical-source cohort and the
published versions of its Provisor reference-bearing sources. Each resulting
context has its own key, dependency fingerprint, snapshot and durable job.
Formats with identical effective context can still share decisions; formats
with different contexts never do.

The migration only adds non-null metadata columns with empty defaults and
indexes. It does not delete or rewrite snapshots, jobs, source rows,
materializations or prices. A v3 snapshot is reused only when its complete
stored dependency vector and source cohort are still exactly valid. Otherwise
the worker builds a parallel context-scoped snapshot.

## Gate 1 — schema and reads-off deployment

Keep `MATCH_CACHE_READ=false` and `MATCH_CACHE_READ_ALLOW_ALL=false`. After a
verified PostgreSQL backup, apply:

```bash
docker exec -i inkar_opt_db sh -ceu '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
' < backend/scripts/migrations/20261026_matching_cache_multi_context.up.sql
```

Deploy Server 75 web from `/opt/inkar-opt`, then Server 77 worker from
`/opt/inkar-optv2.0/inkar-opt`, using each server's existing ordered Compose
file set. Verify health once per server. The worker may prepare contexts, but
no format may read them in this gate.

## Gate 2 — formats 88 and 89

Wait until admin status reports every source ready for both exact contexts:

- format 88: `820,1588,1607`;
- format 89: `820,1180,1551,1565,1587`.

Run one differential report per format. Require exact assignment, decision,
duplicate multiplicity, competitor materialization, final calculated price and
Excel output equivalence. Then allowlist only `88,89`; keep
`MATCH_CACHE_READ_ALLOW_ALL=false`. Generate each format twice and require a
cache hit with zero full matcher invocations on the second generation.

## Gate 3 — broader opt-in

Add formats to the explicit allowlist in reviewed batches only after their
context is ready and their full differential is exact. Stop on a stale or
failed snapshot, context mismatch, retry exhaustion, unexpected legacy/cache
output difference, or database/worker instability.

## Rollback

Immediate behavioral rollback is one Server 75 web recreation with
`MATCH_CACHE_READ=false`; prepared artifacts can remain. If automatic
preparation must also stop, set `MATCH_CACHE_AUTO_PREPARE_ENABLED=false` and
recreate only the Server 77 worker.

Roll back the schema only after both servers run pre-v3.1 code and cache reads
are disabled:

```bash
docker exec -i inkar_opt_db sh -ceu '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
' < backend/scripts/migrations/20261026_matching_cache_multi_context.down.sql
```

The down migration removes only the two v3.1 context-key columns and indexes.
