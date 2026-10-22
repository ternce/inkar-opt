# Phase 5 SAP release configuration

All SAP mutation paths are fail-closed. A normal Phase 1-5 deployment must start with:

```dotenv
SAP_BALANCE_ENABLED=false
SAP_BALANCE_SCHEDULE_ENABLED=false
SAP_OFFLINE_IMPORT_ENABLED=false
SAP_BALANCE_TARGET_ENVIRONMENT=piq
SAP_BALANCE_ALLOW_PIQ_REQUESTS=false
```

These flags are independent:

- `SAP_BALANCE_ENABLED` permits live GetBalance requests and the administrative refresh endpoint.
- `SAP_BALANCE_SCHEDULE_ENABLED` permits the worker scheduler only when the live integration is also enabled.
- `SAP_OFFLINE_IMPORT_ENABLED` permits administrators to preview and publish a previously saved SAP JSON payload. It does not enable live requests or scheduling.
- `SAP_BALANCE_TARGET_ENVIRONMENT` must explicitly be `piq` or `pip` and must agree with the endpoint hostname.
- `SAP_BALANCE_ALLOW_PIQ_REQUESTS=true` is an additional acknowledgement required before any request to PIQ. It has no effect while `SAP_BALANCE_ENABLED=false`.

For a controlled saved-payload production test, leave both live flags and PIQ request authorization false, temporarily set only `SAP_OFFLINE_IMPORT_ENABLED=true`, restart the web process, sign in as an administrator, preview the exact file, review the SHA-256 and registration count, and then publish with the explicit catalog-registration approval. Set the flag back to false and restart the web process immediately afterward.

For intentional PIQ request testing, operator approval must explicitly set all of `SAP_BALANCE_ENABLED=true`, `SAP_BALANCE_TARGET_ENVIRONMENT=piq`, and `SAP_BALANCE_ALLOW_PIQ_REQUESTS=true`, while keeping `SAP_BALANCE_SCHEDULE_ENABLED=false`. PIP operation must instead set `SAP_BALANCE_TARGET_ENVIRONMENT=pip` and use the separately approved PIP URL.

Credentials remain environment-only. They must not be committed, logged, copied into Compose files, or included in test output.
