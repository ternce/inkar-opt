# Phase 3 completion and Phase 5 SAP GetBalance readiness

## Phase 3 runtime controls

- `MATCH_CACHE_SOURCE_NOOP_ENABLED=false` keeps semantic source no-op detection disabled.
- `PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED=false` keeps percentile publication on the legacy atomic replacement path.
- `PERCENTILE_DIFFERENTIAL_REPLACEMENT_THRESHOLD=0.5` selects atomic replacement after staging when at least half of a publication scope would mutate.
- Matching-cache retention is dry-run only. `matching_cache_retention_manifest.py --apply` always exits without deleting data.

No cluster-wide PostgreSQL setting is changed by Phase 3. The local synthetic workload supports monitoring and sizing temporary space before enabling differential publication, but it does not support a production autovacuum or WAL configuration change by itself.

Cached `competitors_prices` remains on its Phase 2 atomic replacement path. The materialization preserves duplicate multiplicity and physical source lineage, but the published table has no database-enforced immutable row identity that distinguishes every business-identical duplicate across all legacy sources. A positional or value-derived MERGE could update the wrong duplicate or retain a stale physical source ID. Differential publication therefore fails the required identity proof; the materialization fingerprint continues to skip a completely unchanged publication, while changed publications replace atomically.

Production diagnostics required before rollout:

1. Record `pg_stat_database` WAL, temporary bytes and transaction rates across a normal import window.
2. Record `pg_stat_user_tables` dead tuples, modification counts, last vacuum/analyze and autovacuum counts for source, percentile and cache tables.
3. Record `pg_total_relation_size`, table size and index size before and after an unchanged and changed import.
4. Confirm no old transaction, replication slot or failed archive command is retaining WAL.
5. Size `temp_file_limit` and filesystem headroom from observed concurrent workers. Do not copy the synthetic benchmark value directly into production configuration.
6. Keep heavy publication concurrency at one per database until production-equivalent I/O and temporary-space measurements prove otherwise.

## Phase 5 existing integration points

### Reference APIs and source abstraction

- `GET /api/references/types`, `/branches`, `/status`, `/readiness-matrix`, `/imports`, and `/template` expose the existing reference-data control plane.
- `POST /api/references/import` and `/api/references/import/batch` are the Excel entry points.
- `backend.app.services.references.sources.ReferenceImportSource` is the source abstraction.
- `SapReferenceSourcePlaceholder` already reserves `source_type="sap"` and currently raises `NotImplementedError`.
- `import_reference_excel` contains parsing and activation together. Phase 5 should extract a source-neutral normalized stock activation function and make both Excel and SAP call it; Excel remains the fallback.

### Current stock model and atomic activation

- `branch_stock` is the active materialized snapshot, uniquely keyed by `(branch_id, product_id)`.
- Products are matched by normalized `Product.code`/SKU. The SAP material identifier must pass through `normalize_sku`; no name-based fallback should be introduced.
- `reference_import_jobs` records source type, requested branches, totals, status and timestamps.
- `reference_update_statuses` identifies the last successful job and active row count for each `(branch_id, data_type)`.
- A successful Excel stock import stages rows in memory, then deletes and replaces each selected branch inside one database transaction. Partial/error imports preserve the prior active snapshot.
- A successful empty import intentionally activates an empty snapshot. Therefore SAP “no rows returned” must not be interpreted as a successful empty balance without an explicit, authoritative complete-response indicator.
- A returned zero quantity is an explicit stock row with `stock=0`. A missing material is absent from the new complete snapshot. Missing and zero are distinct during validation even though both are excluded by positive-stock pricing queries.

### Worker and scheduling infrastructure

- The dedicated `app.worker` process polls the durable refresh queue with lease ownership and stale-job recovery.
- APScheduler, process-role validation, scheduler leases, heartbeats and retry infrastructure already exist for Provisor, Emit and the daily parser orchestrator.
- Phase 5 should add a durable SAP balance job type to the refresh queue and invoke it from the worker. API processes must not run a second scheduler.
- Default SAP automation must remain disabled until credentials, plant mapping, completeness validation and resource limits are configured.

### Region, plant and storage-location contract

Requested GetBalance coverage is fixed as follows:

- Include application regions `1` through `11`, and `13`.
- Exclude application region `12`.
- Request storage location (`lgort`) `1000` for every included region.
- Exclude storage location `4000`.

Plant and storage location are separate request dimensions. A plant such as `1001` must never be placed in `lgort`, and `lgort=1000` must never be treated as a plant.

The repository does **not** currently contain an authoritative GetBalance region-to-plant mapping. It contains two different mappings that cannot safely be conflated:

- Reference branch IDs in `references/types.py` are `1,2,3,4,5,6,8,9,10,11,13,15,16,17`; this list has no branch `7` or `12`.
- `DEFAULT_BRANCH_SAP_MAPPINGS` in `price_formats.py` maps city names to SAP price-export codes `1001` through `1014`. These are documented and used as price-format/export branch codes, not as GetBalance plant configuration.

Consequently, the proposed `1→1001 … 11→1011, 13→1013` relationship is only a hypothesis and must not be implemented from the repository evidence. An authoritative SAP interface document or owned configuration must confirm every included region’s exact `werks`/plant identifier.

### Date, time and snapshot requirements

- Application timestamps use `APP_TIMEZONE`, defaulting to `Asia/Qyzylorda`.
- GetBalance jobs need an explicit business date/as-of timestamp, SAP response timezone, request start/end, and activation timestamp.
- Persist the raw SAP request correlation ID and completeness marker in job metadata without logging credentials or tokens.
- Reject responses whose business date, plant or `lgort` differs from the requested tuple.
- Publish all included plants as one logical run only after every required plant succeeds, or explicitly define and approve per-region activation. The current batch-import rollback behavior is the safer default.

## Implementation-ready Phase 5 checklist

1. Obtain the authoritative GetBalance contract: endpoint, authentication, request/response schema, pagination, completeness marker, rate limits and retry rules.
2. Obtain an approved mapping for regions `1..11,13` to SAP plant IDs; verify region `12` is absent.
3. Add typed settings for the endpoint, credential reference, enabled flag, schedule, timeout, retry budget, plant mapping and fixed `lgort=1000`.
4. Validate configuration at startup: exact region set, no region 12, one distinct plant per region unless SAP documentation says otherwise, and no `lgort=4000`.
5. Implement `SapReferenceSource` as transport/parser only; do not give it direct publication access.
6. Extract normalized stock staging and atomic activation from `import_reference_excel` so SAP and Excel share product matching, status updates and rollback behavior.
7. Validate complete response, duplicate material handling, decimal quantities, zero quantities, missing materials, unknown SKUs and branch isolation before activation.
8. Add a durable refresh-queue job with lease heartbeat, retry exhaustion, cancellation and stale recovery.
9. Add API status/manual-trigger endpoints using existing reference authorization and branch-scope checks.
10. Test all included regions, exclusion of region 12 and `lgort=4000`, cross-dimension mistakes, partial SAP responses, timeouts, duplicate pages, rollback and Excel fallback.
11. Benchmark a synthetic full inventory snapshot and run a production-equivalent staging shadow comparison before enabling scheduling.
